import Foundation
import Combine

/// Manages only the app-specific LaunchAgents, never the repository agents.
final class AppEngineAgent: ObservableObject {
    static let shared = AppEngineAgent()
    static let enabledKey = "BundledEngineEnabled"
    static let screenKey = "BundledEngineScreen"
    private let queue = DispatchQueue(label: "jp.digimaga.quotacanvas.engine-agent", qos: .utility)
    private let labels = ["com.digimaga.quota-canvas.app", "com.digimaga.quota-canvas.app-headless"]
    private let legacyLabels = ["com.digimaga.quota-canvas", "com.digimaga.quota-canvas-headless"]
    @Published private(set) var conflict = false
    @Published private(set) var error: String?

    private var agentsDir: URL { FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents") }
    private func plist(_ label: String) -> URL { agentsDir.appendingPathComponent("\(label).plist") }
    private var domain: String { "gui/\(getuid())" }

    @discardableResult private func launchctl(_ arguments: [String]) throws -> (Int32, String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    private func remove(_ label: String) throws {
        let path = plist(label)
        if try launchctl(["print", "\(domain)/\(label)"]).0 == 0 {
            let result = try launchctl(["bootout", "\(domain)/\(label)"])
            guard result.0 == 0 else { throw AgentError(result.1) }
        }
        if FileManager.default.fileExists(atPath: path.path) {
            try FileManager.default.removeItem(at: path)
        }
    }

    /// reconcile の判定。テスト用の純粋関数。
    /// - `.none`: 何もしない（.app バンドルの外での起動。swift run など）
    /// - `.removeAll`: 同梱エンジンの LaunchAgent をすべて外す
    /// - `.run(screen:)`: 指定の形で常駐エンジンをインストールする
    enum AgentAction: Equatable {
        case none
        case removeAll
        case run(screen: Bool)
    }

    static func agentAction(enabled: Bool, bundledEngine: String?, selectedEngine: String,
                            hasConflict: Bool, screen: Bool, inAppBundle: Bool) -> AgentAction {
        // 開発起動（swift run など .app の外）では bundledEnginePath が nil になるが、
        // それを「無効化」と誤認して常駐エンジンを bootout してはいけない。
        guard inAppBundle else { return .none }
        let selected = (selectedEngine as NSString).expandingTildeInPath
        guard enabled, let bundledEngine, selected == bundledEngine, !hasConflict else {
            return .removeAll
        }
        return .run(screen: screen)
    }

    func reconcile(completion: (() -> Void)? = nil) {
        queue.async {
            self.reconcileOnQueue(completion: completion)
        }
    }

    /// Blocks until queued reconcile work is done, so quitting right after a toggle still applies it.
    func waitForPendingWork() {
        queue.sync {}
    }

    private func reconcileOnQueue(completion: (() -> Void)?) {
        // .app バンドルの外（swift run など開発起動）では LaunchAgent を一切調整しない。
        guard Bundle.main.bundleURL.pathExtension == "app" else {
            DispatchQueue.main.async { completion?() }
            return
        }
        let hasConflict = legacyLabels.contains { FileManager.default.fileExists(atPath: plist($0).path) }
        var message: String?
        defer {
            DispatchQueue.main.async {
                self.conflict = hasConflict
                self.error = message
                completion?()
            }
        }
        do {
            let defaults = UserDefaults.standard
            let enabled = defaults.object(forKey: Self.enabledKey) == nil || defaults.bool(forKey: Self.enabledKey)
            let screen = defaults.object(forKey: Self.screenKey) == nil || defaults.bool(forKey: Self.screenKey)
            let bundle = AppDefaults.bundledEnginePath
            let selected = defaults.string(forKey: AppDefaults.enginePathKey).flatMap { $0.isEmpty ? nil : $0 } ?? AppDefaults.enginePath
            switch Self.agentAction(enabled: enabled, bundledEngine: bundle, selectedEngine: selected,
                                    hasConflict: hasConflict, screen: screen, inAppBundle: true) {
            case .none:
                return
            case .removeAll:
                for label in labels { try remove(label) }
                return
            case .run:
                break
            }
            let label = screen ? labels[0] : labels[1]
            let other = screen ? labels[1] : labels[0]
            try remove(other)
            let logDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/quota-canvas")
            try FileManager.default.createDirectory(at: agentsDir, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
            let args = screen ? [bundle, "run"] : [bundle, "--no-screen", "run"]
            let suffix = screen ? "app" : "app-headless"
            let content: [String: Any] = ["Label": label, "ProgramArguments": args, "RunAtLoad": true,
                "KeepAlive": true, "ThrottleInterval": 30,
                "StandardOutPath": logDir.appendingPathComponent("\(suffix).out.log").path,
                "StandardErrorPath": logDir.appendingPathComponent("\(suffix).err.log").path]
            let data = try PropertyListSerialization.data(fromPropertyList: content, format: .xml, options: 0)
            let path = plist(label)
            let existing = try? Data(contentsOf: path)
            let existingPlist = existing.flatMap { try? PropertyListSerialization.propertyList(from: $0, format: nil) as? NSDictionary }
            if existingPlist != (content as NSDictionary) {
                try remove(label)
                try data.write(to: path, options: .atomic)
            }
            if try launchctl(["print", "\(domain)/\(label)"]).0 != 0 {
                let result = try launchctl(["bootstrap", domain, path.path])
                guard result.0 == 0 else { throw AgentError(result.1) }
            }
        } catch {
            message = String(localized: "エンジンの常駐設定に失敗しました: \(error.localizedDescription)")
        }
    }
}

private struct AgentError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
