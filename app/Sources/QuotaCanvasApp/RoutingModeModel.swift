import Combine
import Foundation

/// 「振り分け」タブがエンジンに求めるモード関連の操作。
/// EngineClient がそのまま準拠する。テストでは偽のクライアントを差す。
protocol RoutingModeClient: Sendable {
    /// `quota-canvas routing mode --json` の一覧。
    func routingModes() async throws -> [RoutingModeEntry]
    /// `quota-canvas routing mode <name> <mode> --json`（返り値は変更後の一覧）。
    func setRoutingMode(name: String, mode: String) async throws -> [RoutingModeEntry]
    /// `quota-canvas refresh --json`（常駐ループに即時再取得を依頼）。
    func refresh() async throws
}

/// 「振り分け」タブのモード切り替えとモード一覧（CLI）の状態。
/// View から切り離し、偽の RoutingModeClient で回帰テストできるようにする。
@MainActor
final class RoutingModeModel: ObservableObject {
    @Published var modeEntries: [RoutingModeEntry] = []
    @Published var modeLoaded = false  // モード一覧を一度でも取れた
    @Published var modeGeneration = 0  // 古いモード一覧の応答を捨てる用
    @Published var modeBusy = false

    private let client: any RoutingModeClient
    private let scheduleReload: () -> Void
    /// エラー文言の受け渡し先。View は alertMessage に入れるクロージャを渡す。
    var reportError: (String) -> Void

    init(client: any RoutingModeClient = EngineClient(),
         scheduleReload: @escaping () -> Void = EngineClient.scheduleArtifactReload,
         reportError: @escaping (String) -> Void = { _ in }) {
        self.client = client
        self.scheduleReload = scheduleReload
        self.reportError = reportError
    }

    /// Picker の表示値。一覧に無い名前・nil は "standard"。
    func mode(for name: String?) -> String {
        modeEntries.first { $0.name == name }?.mode ?? "standard"
    }

    /// モードを CLI で切り替える。空の名前は無視して nil を返す。
    /// 返した Task は切り替え処理（失敗時の一覧取り直しまで）が終わると完了する。
    @discardableResult
    func setMode(named name: String, mode: String) -> Task<Void, Never>? {
        guard !name.isEmpty else { return nil }
        modeBusy = true
        return Task {
            defer { modeBusy = false }
            do {
                let list = try await client.setRoutingMode(name: name, mode: mode)
                modeGeneration += 1
                modeEntries = list
                modeLoaded = true
                try? await client.refresh()
                scheduleReload()
            } catch {
                reportError(error.localizedDescription)
                await reloadModes()
            }
        }
    }

    /// モード一覧を CLI から取り直す。世代番号で古い応答を捨てる。
    /// 取得失敗時は前回の一覧を保つ。
    func reloadModes() async {
        modeGeneration += 1
        let generation = modeGeneration
        do {
            let list = try await client.routingModes()
            guard generation == modeGeneration else { return }
            modeEntries = list
            modeLoaded = true
        } catch {
            // 取得失敗時は前回の一覧を保つ。
        }
    }

    /// 判定ポーリング: キャンセルされるまで sleep → reload を繰り返す。
    /// sleep が投げたら（Task.sleep のキャンセル含む）抜ける。
    static func pollVerdicts(isCancelled: () -> Bool,
                             sleep: () async throws -> Void,
                             reload: () async -> Void) async {
        while !isCancelled() {
            do { try await sleep() } catch { return }
            await reload()
        }
    }
}
