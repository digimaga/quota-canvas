import SwiftUI
import WebKit

/// 「muse.ai にログイン」ウィンドウ: 専用の永続データストアを持つ WKWebView で
/// muse.ai を開き、「保存」でログイン Cookie を `set-key muse` に渡す。
/// Cookie の値はログ・画面・引数に出さない（stdin 経由でエンジンに渡す）。
struct MuseLoginView: View {
    static let windowID = "muse-login"

    @Environment(\.dismissWindow) private var dismissWindow
    @StateObject private var model = MuseLoginModel()
    @ObservedObject private var coordinator = MuseCredentialCoordinator.shared

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("muse.ai にログインしたら「完了」を押してください")
                    Text("このログインは Safari などのブラウザとは別の、アプリ専用の領域に保存されます。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
                Button("キャンセル", role: .cancel) { close() }
                    .keyboardShortcut(.cancelAction)
                Button("完了") {
                    Task {
                        guard await model.canFinish() else { return }
                        close()
                        await MuseUsagePoller.shared.poll() // 保存後は即時に取得する
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(model.busy || coordinator.deleting) // 削除中は完了できない
            }
            .padding(10)
            Divider()
            MuseWebView(model: model)
            if let message = model.message {
                Text(message)
                    .font(.caption).foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 10).padding(.vertical, 6)
            }
        }
    }

    private func close() {
        dismissWindow(id: Self.windowID)
    }
}

/// Muse の Cookie 保存と削除の競合を防ぐ共有コーディネーター。
/// 保存中は設定側の「削除」を、削除中はログイン側の「保存」を無効化する。
/// 世代番号で、保存処理の途中に削除が走ったことを検出して保存を中止する。
@MainActor
final class MuseCredentialCoordinator: ObservableObject {
    static let shared = MuseCredentialCoordinator()
    private init() {}

    /// ポーリング実行中（設定側の「削除」を無効化する目印）。
    @Published private(set) var polling = false
    @Published private(set) var deleting = false
    private(set) var generation = 0

    /// ポーリング開始。削除中・実行中は nil（重複して走らない）。
    /// 返した世代を ingest 直前に isCurrent で確認する。
    func beginPoll() -> Int? {
        guard !deleting, !polling else { return nil }
        polling = true
        return generation
    }

    func endPoll() { polling = false }

    /// muse-ingest の直前に呼ぶ。世代が進んでいれば（途中で削除された）false。
    func isCurrent(_ generation: Int) -> Bool {
        generation == self.generation && !deleting
    }

    /// 設定側の「削除」。世代を進めてから、残ったキー・専用 WebKit 領域・
    /// muse-usage.json（muse-ingest --clear）を消す。
    func deleteCredential(client: EngineClient) async throws {
        deleting = true
        generation += 1
        defer { deleting = false }
        try await client.deleteKey(provider: "muse") // 旧 Cookie 版の残りを掃除
        await MuseLoginModel.clearMuseWebsiteData()
        try await client.museIngestClear()
        NotificationCenter.default.post(name: .museUsageUpdated, object: nil)
    }
}

/// ログインウィンドウの状態（ページ確認と Cookie の有無）。
@MainActor
final class MuseLoginModel: ObservableObject {
    /// 固定 UUID の専用永続ストア（macOS 14 の forIdentifier）。
    /// Safari や他の WebView と共有しない。セッションが残るので次回の
    /// 「ログインし直す」では再入力が不要なことがある。
    static let dataStore = WKWebsiteDataStore(
        forIdentifier: UUID(uuidString: "3F6E0A1B-9C2D-4E5F-A6B7-C8D9E0F1A2B3")!)

    @Published var busy = false
    @Published var message: String?

    /// ログイン中の WKWebView（MuseWebView がセットする）。未ログイン判定に使う。
    weak var webView: WKWebView?

    /// 表示されているログイン入力欄を探す JS。1 つでも見つかれば true を返す。
    private static let loginFieldProbe = """
        (function () {
            var inputs = document.querySelectorAll(
                'input[type=email],input[type=tel],input[type=password],' +
                'input[autocomplete=username],input[name=email]');
            for (var i = 0; i < inputs.length; i++) {
                if (inputs[i].getClientRects().length > 0) { return true; }
            }
            return false;
        })()
        """

    /// エンジンが https://muse.ai/ に送る Cookie だけに絞る:
    /// domain が muse.ai / .muse.ai、path は "/"（または空）、期限切れは除く。
    /// （secure 属性は https なので絞らない。）同名 Cookie は 1 つだけにし、
    /// domain がより具体的な "muse.ai"（host-only）を優先する。
    static func museCookies(from cookies: [HTTPCookie]) -> [HTTPCookie] {
        let now = Date()
        var byName: [String: HTTPCookie] = [:]
        for cookie in cookies {
            guard cookie.domain == "muse.ai" || cookie.domain == ".muse.ai",
                  cookie.path.isEmpty || cookie.path == "/",
                  cookie.expiresDate.map({ $0 > now }) ?? true else { continue }
            if byName[cookie.name]?.domain == "muse.ai" { continue }
            byName[cookie.name] = cookie
        }
        return byName.values.sorted { $0.name < $1.name }
    }

    /// ページの見た目からログイン済みかを判定する: muse.ai のページにいて、
    /// ログイン用の入力欄が表示されていないこと。判定できないときは安全側
    /// （未ログイン扱い）に倒す。
    private func looksLoggedIn() async -> Bool {
        guard let webView, webView.url?.host == "muse.ai" else { return false }
        let result = try? await webView.evaluateJavaScript(Self.loginFieldProbe)
        return (result as? Bool) == false
    }

    /// 「完了」で閉じてよい状態か。明らかに未ログインなら false（注意表示）。
    /// 判定本体は MuseUsagePoller 側（Cookie が無ければ何もしない）なので、
    /// ここではページ状態と Cookie の有無の緩い確認だけにする。
    func canFinish() async -> Bool {
        busy = true
        message = nil
        defer { busy = false }
        if await looksLoggedIn() { return true }
        let cookies = Self.museCookies(
            from: await Self.dataStore.httpCookieStore.allCookies())
        if !cookies.isEmpty { return true }
        message = String(localized: "まだログインしていないようです")
        return false
    }

    /// キーの「削除」に合わせて、専用データストアの muse.ai のデータも消す
    /// （ログアウト相当。キーを消すだけではログイン状態が残るため）。
    static func clearMuseWebsiteData() async {
        let types = WKWebsiteDataStore.allWebsiteDataTypes()
        let records = await dataStore.dataRecords(ofTypes: types)
        let museRecords = records.filter {
            $0.displayName == "muse.ai" || $0.displayName.hasSuffix(".muse.ai")
        }
        if !museRecords.isEmpty {
            await dataStore.removeData(ofTypes: types, for: museRecords)
        }
    }
}

/// muse.ai 表示用の WKWebView。Meta 系への遷移を含む同一フレームの遷移はそのまま
/// 許可し、新規ウィンドウ要求は同じビューで開き、ファイルダウンロードは行わない。
private struct MuseWebView: NSViewRepresentable {
    let model: MuseLoginModel

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = MuseLoginModel.dataStore
        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        model.webView = webView
        webView.load(URLRequest(url: URL(string: "https://muse.ai/")!))
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        /// window.open 系の子 WebView に対応する小ウィンドウ。
        private struct ChildPopup {
            weak var webView: WKWebView?
            let window: NSWindow
        }
        private var childWindows: [ObjectIdentifier: ChildPopup] = [:]
        /// 閉じたとき子も閉じるために監視中の親ウィンドウ。
        private let observedParents = NSHashTable<NSWindow>.weakObjects()

        func webView(_ webView: WKWebView,
                     decidePolicyFor navigationAction: WKNavigationAction
        ) async -> WKNavigationActionPolicy {
            if navigationAction.shouldPerformDownload { return .cancel }
            // 新規ウィンドウ要求は許可し、createWebViewWith が子ウィンドウを作る。
            return .allow
        }

        /// window.open / target=_blank: 渡された構成で子 WebView を作り、
        /// 小さな別ウィンドウに出して返す（返した WebView に元の遷移が続く。
        /// window.open の戻り値を使うページでも壊れない）。
        func webView(_ webView: WKWebView,
                     createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction,
                     windowFeatures: WKWindowFeatures) -> WKWebView? {
            let child = WKWebView(frame: .zero, configuration: configuration)
            child.navigationDelegate = self
            child.uiDelegate = self
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 520, height: 680),
                styleMask: [.titled, .closable, .resizable],
                backing: .buffered, defer: false)
            window.isReleasedWhenClosed = false
            window.contentView = child
            childWindows[ObjectIdentifier(child)] = ChildPopup(webView: child, window: window)
            // 赤い閉じるボタンで直接閉じられても後片付けできるよう監視する。
            NotificationCenter.default.addObserver(
                self, selector: #selector(childWindowWillClose(_:)),
                name: NSWindow.willCloseNotification, object: window)
            if let parent = webView.window {
                window.title = parent.title
                parent.addChildWindow(window, ordered: .above)
                // ログインウィンドウを閉じたら子も閉じる。
                if !observedParents.contains(parent) {
                    observedParents.add(parent)
                    NotificationCenter.default.addObserver(
                        self, selector: #selector(parentWindowWillClose(_:)),
                        name: NSWindow.willCloseNotification, object: parent)
                }
            }
            window.makeKeyAndOrderFront(nil)
            return child
        }

        /// 子ポップアップの後片付けを 1 か所に: 監視を外し、親から切り離し、
        /// 保持辞書から外して WebView の委譲も外す（JS・赤ボタン・親クローズの
        /// どの経路でも呼ぶ。閉じ済みのウィンドウに対しても無害）。
        private func detachChild(_ key: ObjectIdentifier) {
            guard let popup = childWindows.removeValue(forKey: key) else { return }
            NotificationCenter.default.removeObserver(
                self, name: NSWindow.willCloseNotification, object: popup.window)
            popup.window.parent?.removeChildWindow(popup.window)
            popup.webView?.navigationDelegate = nil
            popup.webView?.uiDelegate = nil
            popup.window.close()
        }

        /// 子ウィンドウが（赤い閉じるボタンなどで）直接閉じられたとき。
        @objc private func childWindowWillClose(_ note: Notification) {
            guard let window = note.object as? NSWindow,
                  let key = childWindows.first(where: { $0.value.window === window })?.key
            else { return }
            detachChild(key)
        }

        /// JS の window.close(): 対応する子ウィンドウを閉じる。
        func webViewDidClose(_ webView: WKWebView) {
            detachChild(ObjectIdentifier(webView))
        }

        /// 親（ログイン）ウィンドウが閉じたら子ウィンドウも全部閉じる。
        @objc private func parentWindowWillClose(_ note: Notification) {
            for key in Array(childWindows.keys) { detachChild(key) }
        }

        func webView(_ webView: WKWebView,
                     decidePolicyFor navigationResponse: WKNavigationResponse
        ) async -> WKNavigationResponsePolicy {
            navigationResponse.canShowMIMEType ? .allow : .cancel
        }
    }
}
