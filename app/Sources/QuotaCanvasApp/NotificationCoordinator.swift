import Foundation
import Combine
import UserNotifications

enum NotificationsGate {
    static var available: Bool { Bundle.main.bundleURL.pathExtension == "app" }
}

/// Owns OS delivery and acknowledged keys; Python owns candidate selection.
@MainActor
final class NotificationCoordinator: ObservableObject {
    static let shared = NotificationCoordinator()
    nonisolated static let enabledDefaultsKey = "NotificationsEnabled"
    nonisolated static let keysDefaultsKey = "NotifiedNotificationKeys"
    nonisolated private static let maxStoredKeys = 400

    nonisolated static var userEnabled: Bool {
        UserDefaults.standard.object(forKey: enabledDefaultsKey) as? Bool ?? true
    }

    @Published private(set) var error: String?
    private var planningError: String?
    private var deliveryErrors: [String: String] = [:]
    private let client: EngineClient
    private let enabled: () -> Bool
    private let available: () -> Bool
    private let readKeys: () -> Set<String>
    private let recordKeys: ([String]) -> Void
    private let authorized: () async -> Bool
    private let post: (NotificationPresentation.Planned) async throws -> Void
    private struct Snapshot { let previous: Data?; let current: Data; let now: Date }
    private var queue: [Snapshot] = []
    private static let maxQueued = 30
    private var processing = false
    private var authorizationRequested = false
    private var idleWaiters: [CheckedContinuation<Void, Never>] = []

    init(client: EngineClient = EngineClient(),
         enabled: @escaping () -> Bool = { NotificationCoordinator.userEnabled },
         available: @escaping () -> Bool = { NotificationsGate.available },
         readKeys: @escaping () -> Set<String> = { NotificationCoordinator.storedKeys() },
         recordKeys: @escaping ([String]) -> Void = { NotificationCoordinator.record(keys: $0) },
         authorized: @escaping () async -> Bool = {
             let settings = await UNUserNotificationCenter.current().notificationSettings()
             return [.authorized, .provisional].contains(settings.authorizationStatus)
         },
         post: @escaping (NotificationPresentation.Planned) async throws -> Void = { item in
             let content = UNMutableNotificationContent()
             content.title = item.title
             content.body = item.body
             try await UNUserNotificationCenter.current().add(
                 UNNotificationRequest(identifier: item.key, content: content, trigger: nil))
         }) {
        self.client = client
        self.enabled = enabled
        self.available = available
        self.readKeys = readKeys
        self.recordKeys = recordKeys
        self.authorized = authorized
        self.post = post
    }

    func requestAuthorizationIfNeeded() async {
        guard available(), !authorizationRequested else { return }
        authorizationRequested = true
        _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert])
    }

    /// Queue snapshots in read order and wait for delivery acknowledgement before the next.
    func enqueue(previous: Data?, current: Data, now: Date) {
        queue.append(Snapshot(previous: previous, current: current, now: now))
        // While planning keeps failing, keep the waiting head and the newest snapshots only.
        if queue.count > Self.maxQueued {
            queue.removeSubrange(1...(queue.count - Self.maxQueued))
        }
        guard !processing else { return }
        processing = true
        Task { await drain() }
    }

    func waitUntilIdle() async {
        if !processing { return }
        await withCheckedContinuation { idleWaiters.append($0) }
    }

    private func updateError() {
        error = planningError ?? deliveryErrors.keys.sorted().first.flatMap { deliveryErrors[$0] }
    }

    private func drain() async {
        while !queue.isEmpty {
            // Keep the head in place across suspension: enqueue can only append on MainActor.
            let item = queue[0]
            let planned: [NotificationPresentation.Planned]
            do {
                let candidates = try await client.notificationCandidates(previous: item.previous, current: item.current,
                                                                          now: item.now, notified: readKeys())
                planned = try candidates.map { try NotificationPresentation.format($0, now: item.now) }
            } catch {
                planningError = error.localizedDescription
                updateError()
                // Leave this snapshot and any appended snapshots queued. A later normal
                // enqueue starts a new drain; do not spin or retry immediately.
                break
            }
            planningError = nil
            updateError()
            queue.removeFirst()
            if !planned.isEmpty && (!enabled() || !available()) {
                recordKeys(planned.map(\.key))
            } else if !planned.isEmpty {
                if await authorized() {
                    for notification in planned {
                        if readKeys().contains(notification.key) { continue }
                        do {
                            try await post(notification)
                            recordKeys([notification.key])
                            deliveryErrors.removeValue(forKey: notification.key)
                        } catch {
                            deliveryErrors[notification.key] = error.localizedDescription
                        }
                        updateError()
                    }
                }
            }
        }
        processing = false
        let waiters = idleWaiters
        idleWaiters.removeAll()
        for waiter in waiters { waiter.resume() }
    }

    nonisolated private static func storedKeys() -> Set<String> {
        Set(UserDefaults.standard.stringArray(forKey: keysDefaultsKey) ?? [])
    }
    nonisolated private static func record(keys: [String]) {
        var all = UserDefaults.standard.stringArray(forKey: keysDefaultsKey) ?? []
        all.append(contentsOf: keys)
        if all.count > maxStoredKeys { all = Array(all.suffix(maxStoredKeys)) }
        UserDefaults.standard.set(all, forKey: keysDefaultsKey)
    }
}
