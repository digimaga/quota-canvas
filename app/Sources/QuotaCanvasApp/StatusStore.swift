import Combine
import Foundation
import ServiceManagement
import SwiftUI

/// status.json を 10 秒ごとに読み直すストア。
/// エンジンとは直接通信せず、ファイルを見るだけ（app-contract.md）。
@MainActor
final class StatusStore: ObservableObject {
    @Published private(set) var status: EngineStatus?
    @Published private(set) var loadedAt: Date?
    @Published private(set) var fileError: String?   // ファイルが無い・壊れている
    @Published private(set) var isStale = true       // 5 分以上更新が無い

    static let staleInterval: TimeInterval = 5 * 60
    private var timer: Timer?
    private var started = false
    private var previousStatusData: Data?
    private var reloadObserver: AnyCancellable?

    /// メニューバーの点の色。最も悪いカードの状態。カードが無く errors があれば赤。
    /// F7: status.json が 5 分以上古い（isStale）ときは停止を表す灰色（nil）。
    var dotStatus: String? {
        if isStale { return nil }
        let cards = status?.cards ?? []
        if let worst = StatusColor.worstStatus(of: cards) {
            return StatusColor.rank(for: worst) < 5 ? worst : (hasErrors ? "over" : nil)
        }
        return hasErrors ? "over" : nil
    }

    var hasErrors: Bool { !(status?.errors ?? []).isEmpty }

    var isPaused: Bool { status?.paused ?? false }

    func start() {
        guard !started else { return }
        started = true
        Task { await refresh() }
        let t = Timer(timeInterval: 10, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in await self?.refresh() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        // 設定保存 2 秒後や refresh ボタンなど、即時の読み直しが求められたとき
        reloadObserver = NotificationCenter.default.publisher(for: .engineArtifactsChanged)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                Task { @MainActor [weak self] in await self?.refresh() }
            }
    }

    func refresh() async {
        let result = Self.readFile()
        if let data = result.data {
            do {
                let decoded = try JSONDecoder().decode(EngineStatus.self, from: data)
                let previous = previousStatusData
                previousStatusData = data
                status = decoded
                fileError = nil
                isStale = Self.engineIsStale(updatedAt: decoded.updatedAt, now: Date())
                NotificationCoordinator.shared.enqueue(previous: previous, current: data, now: Date())
            } catch {
                fileError = String(localized: "status.json を解釈できません: \(error.localizedDescription)")
                isStale = true
            }
        } else {
            status = nil
            previousStatusData = nil
            isStale = true
            if let message = result.message {
                fileError = message
            }
        }
        loadedAt = Date()
    }

    static func engineIsStale(updatedAt: Date?, now: Date) -> Bool {
        guard let updatedAt else { return true }
        return now.timeIntervalSince(updatedAt) > staleInterval
    }

    private struct ReadResult {
        var data: Data?
        var message: String?
    }

    private nonisolated static func readFile() -> ReadResult {
        let url = AppPaths.statusURL
        guard FileManager.default.fileExists(atPath: url.path) else {
            return ReadResult(message: String(localized: "status.json がまだ作成されていません"))
        }
        do {
            let data = try Data(contentsOf: url)
            return ReadResult(data: data, message: nil)
        } catch {
            return ReadResult(message: String(localized: "status.json を読めません: \(error.localizedDescription)"))
        }
    }

    /// status.json を 1 回だけ読む（設定タブの初期値: layout / colors 用）。無ければ nil。
    nonisolated static func loadStatusSnapshot() -> EngineStatus? {
        guard let data = try? Data(contentsOf: AppPaths.statusURL) else { return nil }
        return try? JSONDecoder().decode(EngineStatus.self, from: data)
    }
}

// MARK: - ログイン時起動 (SMAppService)

enum LoginItem {
    static var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    static func setEnabled(_ enabled: Bool) throws {
        if enabled {
            try SMAppService.mainApp.register()
        } else {
            try SMAppService.mainApp.unregister()
        }
    }

    static var statusText: String {
        switch SMAppService.mainApp.status {
        case .enabled: return String(localized: "ログイン時に起動します")
        case .requiresApproval: return String(localized: "システム設定で承認が必要です")
        case .notRegistered: return String(localized: "未登録")
        case .notFound: return String(localized: "アプリが見つかりません（app バンドルから起動してください）")
        @unknown default: return String(localized: "不明")
        }
    }
}
