import Foundation
import WebKit

/// Muse の利用枠をアプリ側で取得するポーラー。
///
/// エンジン (urllib) からの Server Action POST はボット対策で 403 になるため、
/// 専用データストアの WebKit (muse.ai ログイン済み) でページと同じ fetch を
/// 実行し、応答を `quota-canvas muse-ingest` に渡す。アプリ起動時・ログイン
/// ウィンドウの「完了」後・以後 5 分ごとに 1 回動く。
@MainActor
final class MuseUsagePoller: ObservableObject {
    static let shared = MuseUsagePoller()
    private init() {}

    /// ポーリング間隔（5 分）。
    private static let pollInterval: TimeInterval = 300
    /// ページ読み込みの打ち切り。
    private static let pageTimeout: TimeInterval = 30
    /// 応答本文は先頭 1MB まで（エンジンの INGEST_BODY_LIMIT と同じ）。
    private static let bodyLimit = 1_000_000
    private static let pageURL = URL(string: "https://muse.ai/")!

    private let client = EngineClient()
    private var timer: Timer?

    /// アプリ起動時に 1 回呼ぶ。即時 1 回 + 以後 5 分ごとに取得する。
    func start() {
        guard timer == nil else { return }
        timer = Timer.scheduledTimer(withTimeInterval: Self.pollInterval,
                                     repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.poll() }
        }
        Task { await poll() }
    }

    /// 1 回取得。専用ストアに muse.ai の Cookie が無ければ（未ログイン）
    /// ページも開かず何もしない。beginPoll が同時実行と削除中の実行を防ぐ。
    func poll() async {
        let cookies = MuseLoginModel.museCookies(
            from: await MuseLoginModel.dataStore.httpCookieStore.allCookies())
        guard !cookies.isEmpty else { return }
        guard let generation = MuseCredentialCoordinator.shared.beginPoll()
        else { return }
        defer { MuseCredentialCoordinator.shared.endPoll() }
        do {
            try await run(generation: generation)
        } catch {
            // 失敗は次の周期に任せる
        }
    }

    /// 1 回分の取得全体にかける上限（開始からの絶対期限として各待機に渡す。
    /// TaskGroup のキャンセルではなく、各ステップが deadline で必ず返るようにする）。
    private static let runTimeout: TimeInterval = 60

    /// ページ読み込み → ページ内 fetch → muse-ingest への書き込み。
    /// 途中で削除が走ったり deadline を超えたら ingest で書き込まない。
    /// どの経路でも戻るので、呼び出し側（poll）の defer で必ず polling が解除される。
    private func run(generation: Int) async throws {
        let deadline = Date().addingTimeInterval(Self.runTimeout)
        let config = WKWebViewConfiguration()
        config.websiteDataStore = MuseLoginModel.dataStore
        let webView = WKWebView(frame: .zero, configuration: config)
        defer {
            webView.navigationDelegate = nil // 毎回作って使い捨てる
            webView.stopLoading()
        }
        guard await Self.loadPage(webView, deadline: deadline) else {
            throw EngineError(message: "muse.ai page load failed")
        }
        // 未ログインの HTML には該当 chunk が無いので、ページが実際に読み込んだ
        // chunk のパスを集めてエンジンに渡して解決させる。
        let chunks: [String] = (try? await Self.withDeadline(deadline) { finish in
            webView.evaluateJavaScript(Self.chunkListProbe) { value, error in
                if let error { finish(.failure(error)) }
                else { finish(.success(value as? [String] ?? [])) }
            }
        }) ?? []
        var actionId = try await client.museActionId(
            refresh: false, chunks: chunks,
            timeout: deadline.timeIntervalSinceNow)
        var reply = try await Self.postAction(webView, actionId: actionId,
                                              deadline: deadline)
        if reply.status == 404 {
            // キャッシュの ID が古い → 1 回だけ解決し直して再試行
            actionId = try await client.museActionId(
                refresh: true, chunks: chunks,
                timeout: deadline.timeIntervalSinceNow)
            reply = try await Self.postAction(webView, actionId: actionId,
                                              deadline: deadline)
        }
        // 期限切れ・途中で削除済みなら ingest しない（消したデータの復活を防ぐ）。
        guard Date() < deadline,
              MuseCredentialCoordinator.shared.isCurrent(generation)
        else { return }
        try await client.museIngest(status: reply.status, body: reply.text,
                                    timeout: deadline.timeIntervalSinceNow)
        // サービス一覧（detect は muse-usage.json の有無を見る）を再検出させる。
        NotificationCenter.default.post(name: .museUsageUpdated, object: nil)
    }

    /// ページが読み込んだ muse.ai/_next/static/chunks/*.js のパス一覧を返す JS。
    private static let chunkListProbe = """
        (function () {
            var found = new Set();
            for (var i = 0; i < document.scripts.length; i++) {
                if (document.scripts[i].src) found.add(document.scripts[i].src);
            }
            performance.getEntriesByType('resource').forEach(function (entry) {
                found.add(entry.name);
            });
            var paths = [];
            found.forEach(function (url) {
                try {
                    var parsed = new URL(url);
                    var path = parsed.pathname;
                    if (parsed.origin === 'https://muse.ai'
                        && path.indexOf('/_next/static/chunks/') === 0
                        && path.slice(-3) === '.js') paths.push(path);
                } catch (e) { /* ignore */ }
            });
            return paths;
        })()
        """

    /// didFinish / 失敗 / deadline（pageTimeout との早い方）のいずれかで返る。
    private static func loadPage(_ webView: WKWebView, deadline: Date) async -> Bool {
        let loader = PageLoader()
        webView.navigationDelegate = loader
        webView.load(URLRequest(url: pageURL))
        return await loader.wait(timeout: max(0.05,
            min(pageTimeout, deadline.timeIntervalSinceNow)))
    }

    private struct ActionReply {
        let status: Int
        let text: String
    }

    /// WebKit の完了コールバック型の呼び出しを、通常の完了と deadline の
    /// 早い方で 1 回だけ終わらせる（WebContent が応答しなくても必ず戻る）。
    /// 期限切れ後に届いた遅いコールバックは無視する。
    private static func withDeadline<T>(
        _ deadline: Date,
        _ start: (@escaping (Result<T, Error>) -> Void) -> Void
    ) async throws -> T {
        let remaining = deadline.timeIntervalSinceNow
        guard remaining > 0 else { throw EngineError(message: "muse poll timed out") }
        return try await withCheckedThrowingContinuation { cont in
            var done = false // WebKit のコールバックとタイマーはどちらもメインキュー
            let finish: (Result<T, Error>) -> Void = { result in
                guard !done else { return }
                done = true
                cont.resume(with: result)
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + remaining) {
                finish(.failure(EngineError(message: "muse poll timed out")))
            }
            start(finish)
        }
    }

    /// ページ内で Server Action と同じ fetch を実行し {status, text} を受け取る。
    /// JS 側の AbortController（20 秒）に加え、Swift 側も deadline で必ず戻る
    /// （戻った後は run の defer で WebView を止めて捨てる）。
    private static func postAction(_ webView: WKWebView, actionId: String,
                                   deadline: Date) async throws -> ActionReply {
        // fetch が終わらなくても止まるよう AbortController（20 秒）を入れる。
        // 中断したときは {status:0} を返して呼び出し側の失敗にする。
        let result: Any = try await withDeadline(deadline) { finish in
            webView.callAsyncJavaScript(
            "return new Promise(function (resolve) {"
            + " var controller = new AbortController();"
            + " var timer = setTimeout(function () { controller.abort(); }, 20000);"
            + " fetch('/', {method: 'POST', signal: controller.signal,"
            + " headers: {'Next-Action': actionId, 'Accept': 'text/x-component',"
            + " 'Content-Type': 'text/plain;charset=UTF-8'},"
            + " body: '[{\"includeAgreement\":false}]'}).then(function (r) {"
            + " return r.text().then(function (t) {"
            + " return {status: r.status, text: t.slice(0, limit)}; }); })"
            + " .then(function (r) { clearTimeout(timer); resolve(r); })"
            + " .catch(function () { clearTimeout(timer);"
            + " resolve({status: 0, text: ''}); }); });",
            arguments: ["actionId": actionId, "limit": bodyLimit],
            in: nil, in: .page) { result in finish(result) }
        }
        guard let dict = result as? [String: Any],
              let status = dict["status"] as? Int else {
            throw EngineError(message: "muse action response invalid")
        }
        return ActionReply(status: status,
                           text: dict["text"] as? String ?? "")
    }

    /// didFinish / 失敗 / タイムアウトのどれか早い方で Bool を返すローダー。
    private final class PageLoader: NSObject, WKNavigationDelegate {
        private var continuation: CheckedContinuation<Bool, Never>?

        func wait(timeout: TimeInterval) async -> Bool {
            await withCheckedContinuation { cont in
                continuation = cont
                Task { [weak self] in
                    try? await Task.sleep(
                        nanoseconds: UInt64(timeout * 1_000_000_000))
                    self?.resume(false)
                }
            }
        }

        private func resume(_ ok: Bool) {
            continuation?.resume(returning: ok)
            continuation = nil
        }

        func webView(_ webView: WKWebView,
                     didFinish navigation: WKNavigation!) {
            resume(true)
        }
        func webView(_ webView: WKWebView,
                     didFail navigation: WKNavigation!, withError error: Error) {
            resume(false)
        }
        func webView(_ webView: WKWebView,
                     didFailProvisionalNavigation navigation: WKNavigation!,
                     withError error: Error) {
            resume(false)
        }
    }
}
