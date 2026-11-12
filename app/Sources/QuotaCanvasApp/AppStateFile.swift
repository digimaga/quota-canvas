import Foundation
import AppKit

/// 常駐エンジン（Python の watchdog）がアプリの予期しない終了を検知して
/// 起動し直すための生存状態ファイル。`AppPaths.supportDir/app-state.json` に書く。
/// キーと値の型はエンジン側との取り決めで、変えてはいけない:
/// {"bundle_path": String, "pid": Int, "launched_at": epoch秒の数値, "user_quit": Bool}
enum AppStateFile {
    static let fileName = "app-state.json"

    /// app-state.json の本文を作る純粋関数。
    static func encode(bundlePath: String, pid: Int32, launchedAt: Date, userQuit: Bool) -> Data {
        // 固定のキーとスカラー値だけなので直列化は失敗しない。
        let object: [String: Any] = [
            "bundle_path": bundlePath,
            "pid": Int(pid),
            "launched_at": launchedAt.timeIntervalSince1970,
            "user_quit": userQuit,
        ]
        return try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }

    /// supportDir が無ければ作り、app-state.json を原子的に書く。
    /// .app バンドルから起動したときだけ書く（swift run などでは何もしない。
    /// NotificationsGate.available と同じ判定）。失敗してもアプリを止めないため
    /// 全て try? で握りつぶす。
    static func write(userQuit: Bool, launchedAt: Date,
                      bundleURL: URL = Bundle.main.bundleURL,
                      directory: URL = AppPaths.supportDir,
                      pid: Int32 = ProcessInfo.processInfo.processIdentifier) {
        guard bundleURL.pathExtension == "app" else { return }
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let data = encode(bundlePath: bundleURL.path, pid: pid, launchedAt: launchedAt, userQuit: userQuit)
        try? data.write(to: directory.appendingPathComponent(fileName), options: .atomic)
    }
}

/// 生存状態ファイルを書くだけの小さな delegate。
/// 起動直後（applicationDidFinishLaunching）に user_quit=false を書く。
/// ユーザーがアプリ内で終了したとき（メニューの「終了」・⌘Q = NSApp.terminate）
/// だけ user_quit=true に書き直す。外から届いた終了要求（quit Apple Event:
/// ログアウト・システムや他プロセスからの終了）やクラッシュ・kill では false の
/// まま残り、エンジンが予期しない終了として起動し直せる。
final class AppStateDelegate: NSObject, NSApplicationDelegate {
    private var launchedAt: Date?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let now = Date()
        launchedAt = now
        AppStateFile.write(userQuit: false, launchedAt: now)
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if AppStateFile.isUserInitiatedQuit(NSAppleEventManager.shared().currentAppleEvent) {
            AppStateFile.write(userQuit: true, launchedAt: launchedAt ?? Date())
        }
        return .terminateNow
    }
}

extension AppStateFile {
    /// 終了要求がアプリ内の操作（Apple Event なし）なら true。
    /// quit Apple Event（kAEQuitApplication）で届いた終了は外からの要求とみなす。
    static func isUserInitiatedQuit(_ event: NSAppleEventDescriptor?) -> Bool {
        guard let event else { return true }
        return event.eventID != kAEQuitApplication
    }
}
