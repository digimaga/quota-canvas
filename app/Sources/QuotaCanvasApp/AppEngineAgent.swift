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
            guard enabled, let bundle, (selected as NSString).expandingTildeInPath == bundle, !hasConflict else {
                for label in labels { try remove(label) }
                return
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
