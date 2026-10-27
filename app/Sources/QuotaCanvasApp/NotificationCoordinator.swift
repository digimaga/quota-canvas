import Foundation
import UserNotifications

/// UNUserNotificationCenter は .app バンドルから起動したときだけ使える
/// （`swift run` 等の生実行では current() が落ちるため、呼ぶ前に見る）。
enum NotificationsGate {
    static var available: Bool {
        Bundle.main.bundleURL.pathExtension == "app"
    }
}

/// F4 通知の投稿係: NotificationPlanner（純粋）の結果を
/// UserDefaults（通知済みキー）と UNUserNotificationCenter（投稿）に仲介する。
@MainActor
final class NotificationCoordinator {
    static let shared = NotificationCoordinator()

    /// 設定「表示」タブの「通知を出す」トグル（既定オン）
    nonisolated static let enabledDefaultsKey = "NotificationsEnabled"
    /// 通知済みキーの保存先。アプリ再起動でも再通知しない。
    nonisolated static let keysDefaultsKey = "NotifiedNotificationKeys"
    nonisolated private static let maxStoredKeys = 400

    static var userEnabled: Bool {
        UserDefaults.standard.object(forKey: enabledDefaultsKey) as? Bool ?? true
    }

    private var authorizationRequested = false

    /// アプリ起動時に 1 回だけ権限を要求する。
    func requestAuthorizationIfNeeded() async {
        guard NotificationsGate.available, !authorizationRequested else { return }
        authorizationRequested = true
        _ = try? await UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert])
    }

    /// status.json を読み直すたびに呼ぶ。差分から通知を組み立てる。
    /// キーの保存タイミング:
    /// - トグルがオフ → 投稿せずキーだけ記録（後でオンにしても古い状態で通知しない）
    /// - トグルがオンで権限が確定 → UNUserNotificationCenter.add の completion が
    ///   エラー無しで返った後、そのキーだけ記録する
    /// - トグルがオンでも権限が未確定・拒否 → 保存しない（承認後に次の読み込みで出る）
    func process(previous: EngineStatus?, current: EngineStatus) {
        let planned = NotificationPlanner.plan(previous: previous, current: current,
                                               now: Date(), notified: Self.storedKeys())
        guard !planned.isEmpty else { return }
        guard Self.userEnabled, NotificationsGate.available else {
            Self.record(keys: planned.map(\.key))
            return
        }
        let center = UNUserNotificationCenter.current()
        center.getNotificationSettings { settings in
            switch settings.authorizationStatus {
            case .authorized, .provisional, .ephemeral:
                for item in planned {
                    let content = UNMutableNotificationContent()
                    content.title = item.title
                    content.body = item.body
                    let request = UNNotificationRequest(identifier: item.key, content: content, trigger: nil)
                    // current() をクロージャ内で取り直す（非 Sendable な center をキャプチャしない）。
                    // エラーのときはキーを保存しない（次の読み込みで再挑戦）
                    UNUserNotificationCenter.current().add(request) { error in
                        if error == nil {
                            Self.record(keys: [item.key]) // 投稿が受理されてから保存
                        }
                    }
                }
            default:
                break // 未確定・拒否: 投稿も保存もしない
            }
        }
    }

    nonisolated private static func storedKeys() -> Set<String> {
        Set(UserDefaults.standard.stringArray(forKey: keysDefaultsKey) ?? [])
    }

    nonisolated private static func record(keys: [String]) {
        var all = UserDefaults.standard.stringArray(forKey: keysDefaultsKey) ?? []
        all.append(contentsOf: keys)
        if all.count > maxStoredKeys {
            all = Array(all.suffix(maxStoredKeys))
        }
        UserDefaults.standard.set(all, forKey: keysDefaultsKey)
    }
}
