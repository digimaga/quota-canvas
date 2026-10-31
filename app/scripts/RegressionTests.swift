import Foundation

@MainActor
final class DeliveryGate {
    private var started = false
    private var startWaiter: CheckedContinuation<Void, Never>?
    private var deliveryWaiter: CheckedContinuation<Void, Never>?
    func waitForStart() async {
        if started { return }
        await withCheckedContinuation { startWaiter = $0 }
    }
    func deliver() async {
        started = true
        startWaiter?.resume()
        startWaiter = nil
        await withCheckedContinuation { deliveryWaiter = $0 }
    }
    func accept() { deliveryWaiter?.resume(); deliveryWaiter = nil }
}

@main
struct RegressionTests {
    static var checks = 0
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        checks += 1
        guard condition() else { fatalError("Failed: \(message)") }
    }

    @MainActor static func main() async throws {
        check(RoutingSettingsView.isValidWorkerName("worker-1.a_b"), "valid worker name")
        check(RoutingSettingsView.isValidWorkerName(String(repeating: "a", count: 40)), "40 characters")
        check(!RoutingSettingsView.isValidWorkerName(""), "empty worker name")
        check(!RoutingSettingsView.isValidWorkerName("bad name"), "invalid worker name")
        check(!RoutingSettingsView.isValidWorkerName(String(repeating: "a", count: 41)), "41 characters")
        let freshnessNow = Date(timeIntervalSince1970: 1000)
        check(!RoutingSettingsView.verdictIsStale(freshnessNow.addingTimeInterval(-300), now: freshnessNow), "fresh threshold")
        check(RoutingSettingsView.verdictIsStale(freshnessNow.addingTimeInterval(-301), now: freshnessNow), "stale threshold")
        check(RoutingSettingsView.verdictIsStale(nil, now: freshnessNow), "missing timestamp")

        let configPanels = [
            ConfigDocument.PanelConfig(main: "alpha:week", subs: ["alpha:day"], size: "large"),
            ConfigDocument.PanelConfig(main: "", subs: nil, size: "small"),
            ConfigDocument.PanelConfig(main: "beta:day", subs: nil, size: "small"),
            ConfigDocument.PanelConfig(main: "gamma:week", subs: nil, size: "large"),
            ConfigDocument.PanelConfig(main: "delta:week", subs: [], size: "large")
        ]
        let staleLayout = try JSONDecoder().decode([LayoutPanel].self, from: Data(#"[{"main":"old:week","size":"large","subs":["old:day"]},{"main":"gamma:week","size":"large","subs":["gamma:day"]}]"#.utf8))
        for status in [[], staleLayout] {
            let restored = LayoutSettingsView.restoreLayout(config: configPanels, status: status)
            var slots = restored.slots
            check(slots[0].main == "alpha:week" && slots[0].subs == ["alpha:day"], "manual large from config")
            check(restored.splits[1] && slots[4].main == nil && slots[5].main == "beta:day", "manual split and empty top preserved")
            check(slots[6].main == "gamma:week" && slots[6].subs == nil, "automatic subs preserved")
            check(slots[9].subs == [], "explicit empty subs preserved")
            check(restored.resolvedSubs.isEmpty, "stale layout does not supply mismatched subs")
            slots[0].main = "alpha:new"
            let savedPanels = LayoutSettingsView.panelsForSave(slots: slots, splits: restored.splits)
            check(savedPanels.map(\.main) == ["alpha:new", "", "beta:day", "gamma:week", "delta:week"], "editing one slot preserves others")
            check(savedPanels[0].subs == ["alpha:day"] && savedPanels[3].subs == nil && savedPanels[4].subs == [], "saving retains sub modes")
        }
        let auto = LayoutSettingsView.restoreLayout(config: [], status: staleLayout)
        check(auto.slots[0].main == "old:week" && auto.slots[3].main == "gamma:week", "auto placement comes from status")
        check(auto.resolvedSubs["old:week"] == ["old:day"], "auto resolved subs come from status")
        let matchingStatus = try JSONDecoder().decode([LayoutPanel].self, from: Data(#"[{"main":"alpha:week","size":"large","subs":["alpha:resolved"]}]"#.utf8))
        let matching = LayoutSettingsView.restoreLayout(config: configPanels, status: matchingStatus)
        check(matching.resolvedSubs["alpha:week"] == ["alpha:resolved"], "matching status supplies resolved subs")

        // ローカル LLM サーバー ([providers.local.<名前>]) は主枠の候補に出る。
        // 表示名は label、無ければ名前。enabled = false と壊れた形の台は外す。
        // サブ窓の候補には出さない。
        let localConfig = try ConfigDocument(data: Data(#"{"providers":{"local":{"myserver":{"label":"My Server"},"tiny":{},"off":{"enabled":false},"broken":"oops"}}}"#.utf8))
        let localChoices = LayoutSettingsView.localChoices(from: localConfig, existing: [])
        check(localChoices.map(\.id) == ["local:myserver", "local:tiny"], "local servers offered as main slots in name order")
        check(localChoices.map(\.label) == ["My Server", "tiny"], "local label falls back to the server name")
        let deduped = LayoutSettingsView.localChoices(from: localConfig,
            existing: [LayoutSettingsView.WindowChoice(id: "local:myserver", label: "My Server")])
        check(deduped.map(\.id) == ["local:tiny"], "already offered local server is not duplicated")
        check(LayoutSettingsView.localChoices(from: nil, existing: []).isEmpty, "no config adds no local choices")
        check(LayoutSettingsView.isSubCandidate("claude:weekly") && !LayoutSettingsView.isSubCandidate("kimi")
              && !LayoutSettingsView.isSubCandidate("local:myserver"), "local servers are not sub candidates")

        let statusJSON = #"{"updated_at":"2026-10-28T12:00:00Z","cards":[{"id":"one","fetched_at":"2026-10-28T10:00:00Z"},{"id":"two","fetched_at":42},{"id":"three"}]}"#
        let decoded = try JSONDecoder().decode(EngineStatus.self, from: Data(statusJSON.utf8))
        let reference = ISO8601Support.parse("2026-10-28T12:00:30Z")!
        let firstCard = decoded.cards![0]
        check(firstCard.fetchedAt == ISO8601Support.parse("2026-10-28T10:00:00Z"), "card fetch time decoded")
        check(decoded.cards![1].fetchedAt == nil && decoded.cards![2].fetchedAt == nil, "malformed and missing fetch time are unknown")
        check(!StatusStore.engineIsStale(updatedAt: decoded.updatedAt, now: reference), "engine freshness uses update time")
        check(StatusStore.engineIsStale(updatedAt: decoded.updatedAt, now: reference.addingTimeInterval(300)), "engine stale after update threshold")
        check(StatusStore.engineIsStale(updatedAt: nil, now: reference), "missing engine timestamp is unknown")
        let fetchText = CardRow(card: firstCard, gauge: nil).fetchText(now: reference)
        let expectedAge = RelativeDateTimeFormatter().localizedString(for: firstCard.fetchedAt!, relativeTo: reference)
        check(fetchText.contains(expectedAge), "card shows fetch age independent of redraw")
        check(CardRow(card: decoded.cards![1], gauge: nil).fetchText(now: reference) == String(localized: "取得時刻: 不明"), "unknown fetch time shown")
        let balanceJSON = #"{"balances":[{"provider":"kimi","name":"Kimi","currency":"USD","amount":49.58,"label":"今月の使用","stale_error":"HTTP 429","fetched_at":"2026-01-02T03:04:05Z"},{"provider":"deepseek","name":"DeepSeek","currency":"CNY","amount":110}]}"#
        let balanceStatus = try JSONDecoder().decode(EngineStatus.self, from: Data(balanceJSON.utf8))
        check(balanceStatus.balances?.first?.amount == 49.58, "balance decoding")
        check(balanceStatus.balances?.first?.staleError == "HTTP 429", "balance stale state")
        check(balanceStatus.balances?.first?.fetchedAt != nil, "balance fetched_at decoding")
        check(balanceStatus.balances?.first?.displayText == "今月の使用 $49.58", "balance label precedes amount")
        check(balanceStatus.balances?.last?.fetchedAt == nil, "optional balance fetched_at")
        check(balanceStatus.balances?.last?.displayText.contains("¥110.00") == true, "default balance label")

        let input = Data(repeating: 65, count: 1_000_000)
        let code = "import sys; sys.stderr.buffer.write(b'e'*1000000); sys.stderr.flush(); sys.stdout.buffer.write(b'o'*1000000); sys.stdout.flush(); data=sys.stdin.buffer.read(); sys.stdout.buffer.write(str(len(data)).encode())"
        let output = try await EngineClient(enginePathOverride: "/usr/bin/python3").run(["-c", code], stdin: input)
        check(output.count == 1_000_007, "large stdout")
        check(String(data: output.suffix(7), encoding: .utf8) == "1000000", "large stdin")

        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        // Keep CLI language detection inside the isolated test directory.
        try Data("[display]\nlanguage = \"en\"\n".utf8).write(to: dir.appendingPathComponent("config.toml"))
        let script = dir.appendingPathComponent("engine")
        let python = #"""
        #!/usr/bin/python3
        import os, sys
        repo = os.environ["QUOTA_CANVAS_TEST_REPO"]
        executable = os.path.join(repo, ".venv", "bin", "python")
        config = os.path.join(os.path.dirname(__file__), "config.toml")
        os.execv(executable, [executable, "-m", "quotacanvas.cli", "--config", config] + sys.argv[1:])
        """#
        try Data(python.utf8).write(to: script)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
        let client = EngineClient(enginePathOverride: script.path)
        async let pause = client.updateConfig { $0.paused = true }
        async let brightness = client.updateConfig { $0.brightness = 25 }
        _ = try await (pause, brightness)
        let result = try await client.getConfig()
        check(result.paused, "concurrent pause retained")
        check(result.brightness == 25, "concurrent brightness retained")
        let original = try await client.getConfig()
        var edited = original
        edited.brightness = 40
        _ = try await client.updateConfig { $0.paused = false }
        let saved = try await client.saveChanges(from: original, to: edited)
        check(!saved.paused && saved.brightness == 40, "draft merges with latest through Python CLI")

        let now = ISO8601Support.parse("2026-01-01T00:00:00Z")!
        let current = Data(#"{"cards":[{"id":"glm:5h","provider":"glm","name":"GLM","status":"over","resets_at":"2026-01-02T00:00:00Z"}],"reset_credits":{"glm":[{"title":"reset","expires_at":"2026-01-02T00:00:00Z"}]}}"#.utf8)
        let previous = Data(#"{"cards":[{"id":"glm:5h","status":"go","resets_at":"2026-01-02T00:00:00Z"}]}"#.utf8)
        let first = try await client.notificationCandidates(previous: nil, current: current, now: now, notified: [])
        check(first.count == 1 && first[0].kind == "credit", "first snapshot only credit")
        let boundary = Data(#"{"reset_credits":{"glm":[{"expires_at":"2026-01-01T00:00:00Z"}]}}"#.utf8)
        let fractional = try await client.notificationCandidates(previous: nil, current: boundary,
            now: now.addingTimeInterval(-0.0001), notified: [])
        check(fractional.count == 1, "submillisecond time is preserved through CLI")
        let changes = try await client.notificationCandidates(previous: previous, current: current, now: now, notified: [])
        check(changes.map(\.kind) == ["status", "credit"], "transition candidates through Python CLI")

        var keys = Set<String>()
        var isEnabled = false
        var isAuthorized = true
        var shouldFail = false
        var posts: [String] = []
        let coordinator = NotificationCoordinator(client: client, enabled: { isEnabled }, available: { true },
            readKeys: { keys }, recordKeys: { keys.formUnion($0) }, authorized: { isAuthorized },
            post: { item in
                posts.append(item.key)
                if shouldFail { throw EngineError(message: "delivery failed") }
            })
        coordinator.enqueue(previous: nil, current: current, now: now)
        await coordinator.waitUntilIdle()
        check(keys.count == 1 && posts.isEmpty, "disabled records without posting")
        keys.removeAll()
        isEnabled = true
        isAuthorized = false
        coordinator.enqueue(previous: nil, current: current, now: now)
        await coordinator.waitUntilIdle()
        check(keys.isEmpty && posts.isEmpty, "unauthorized does not record")
        isAuthorized = true
        shouldFail = true
        coordinator.enqueue(previous: nil, current: current, now: now)
        await coordinator.waitUntilIdle()
        check(keys.isEmpty && coordinator.error != nil, "failed delivery does not record")
        shouldFail = false
        posts.removeAll()
        coordinator.enqueue(previous: nil, current: current, now: now)
        coordinator.enqueue(previous: nil, current: current, now: now)
        await coordinator.waitUntilIdle()
        check(keys.count == 1 && posts.count == 1, "serialized delivery suppresses duplicate")
        keys.removeAll()
        posts.removeAll()
        let gate = DeliveryGate()
        let pendingCoordinator = NotificationCoordinator(client: client, enabled: { true }, available: { true },
            readKeys: { keys }, recordKeys: { keys.formUnion($0) }, authorized: { true },
            post: { item in posts.append(item.key); await gate.deliver() })
        pendingCoordinator.enqueue(previous: nil, current: current, now: now)
        await gate.waitForStart()
        pendingCoordinator.enqueue(previous: nil, current: current, now: now)
        check(keys.isEmpty && posts.count == 1, "pending delivery has not recorded a key")
        gate.accept()
        await pendingCoordinator.waitUntilIdle()
        check(keys.count == 1 && posts.count == 1, "overlap waits for delivery before replanning")
        keys.removeAll()
        let broken = NotificationCoordinator(client: EngineClient(enginePathOverride: dir.appendingPathComponent("missing").path),
            enabled: { true }, available: { true }, readKeys: { keys }, recordKeys: { keys.formUnion($0) },
            authorized: { true }, post: { _ in })
        broken.enqueue(previous: nil, current: current, now: now)
        await broken.waitUntilIdle()
        check(keys.isEmpty && broken.error != nil, "planning failure is visible and records no key")
        // A real CLI wrapper fails once, then forwards to the actual Python engine.
        let flakyPython = #"""
        #!/usr/bin/python3
        import os, sys, time
        from pathlib import Path
        count_path = Path(__file__ + ".count")
        count = int(count_path.read_text()) + 1 if count_path.exists() else 1
        count_path.write_text(str(count))
        if count == 1:
            print('{"error":"temporary planner failure"}')
            sys.exit(1)
        repo = os.environ["QUOTA_CANVAS_TEST_REPO"]
        executable = os.path.join(repo, ".venv", "bin", "python")
        config = os.path.join(os.path.dirname(__file__), "config.toml")
        os.execv(executable, [executable, "-m", "quotacanvas.cli", "--config", config] + sys.argv[1:])
        """#
        func flakyClient(_ name: String, failOn: Int = 1, holdFailure: Bool = false) throws -> (EngineClient, URL) {
            let executable = dir.appendingPathComponent(name)
            let waitForRelease = holdFailure
                ? "\n    while not Path(__file__ + '.release').exists(): time.sleep(0.01)"
                : ""
            let source = flakyPython.replacingOccurrences(of: "if count == 1:",
                with: "if count == \(failOn):\(waitForRelease)")
            try Data(source.utf8).write(to: executable)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
            return (EngineClient(enginePathOverride: executable.path), URL(fileURLWithPath: executable.path + ".count"))
        }
        let (flaky, flakyCount) = try flakyClient("flaky-transition", holdFailure: true)
        let spare = Data(#"{"cards":[{"id":"demo:weekly","provider":"demo","status":"spare","resets_at":"2026-01-02T00:00:00Z"}]}"#.utf8)
        let over = Data(#"{"cards":[{"id":"demo:weekly","provider":"demo","status":"over","resets_at":"2026-01-03T00:00:00Z","burst_exhausts_at":"2026-01-01T12:00:00Z"}]}"#.utf8)
        let exhausted = Data(#"{"cards":[{"id":"demo:weekly","provider":"demo","status":"exhausted","resets_at":"2026-01-03T00:00:00Z","burst_exhausts_at":"2026-01-01T12:00:00Z"}]}"#.utf8)
        var retryKeys = Set<String>()
        var retryPosts: [String] = []
        let retryGate = DeliveryGate()
        let retry = NotificationCoordinator(client: flaky, enabled: { true }, available: { true },
            readKeys: { retryKeys }, recordKeys: { retryKeys.formUnion($0) }, authorized: { true },
            post: { item in
                retryPosts.append(item.key)
                if retryPosts.count == 1 { await retryGate.deliver() }
            })
        retry.enqueue(previous: spare, current: over, now: now)
        // Append while the planner subprocess is suspended, before it fails.
        while !FileManager.default.fileExists(atPath: flakyCount.path) {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        retry.enqueue(previous: over, current: exhausted, now: now)
        let releasePath = dir.appendingPathComponent("flaky-transition.release")
        try Data().write(to: releasePath)
        await retry.waitUntilIdle()
        check(retry.error != nil && retryPosts.isEmpty && retryKeys.isEmpty, "failed snapshot retained without posting")
        let attempts = try String(contentsOf: flakyCount, encoding: .utf8)
        check(attempts == "1", "no immediate retry despite queued snapshot")
        retry.enqueue(previous: exhausted, current: exhausted, now: now)
        await retryGate.waitForStart()
        check(retry.error == nil && retryKeys.isEmpty && retryPosts.count == 1, "recovered first post awaits acceptance")
        retry.enqueue(previous: exhausted, current: exhausted, now: now)
        retryGate.accept()
        await retry.waitUntilIdle()
        check(retryPosts.count == 4 && retryKeys.count == 4, "recovered transitions delivered once")
        check(retryPosts[0].hasPrefix("status|") && retryPosts[0].hasSuffix("|over") &&
              retryPosts[1].hasPrefix("reset|") && retryPosts[2].hasPrefix("burst|") &&
              retryPosts[3].hasPrefix("status|") && retryPosts[3].hasSuffix("|exhausted"),
              "status reset burst and later snapshot keep order")
        check(Set(retryPosts).count == retryPosts.count, "retry and in-flight enqueue do not duplicate")

        let (emptyFlaky, _) = try flakyClient("flaky-empty")
        let empty = Data(#"{"cards":[]}"#.utf8)
        let emptyRetry = NotificationCoordinator(client: emptyFlaky, enabled: { true }, available: { true },
            readKeys: { [] }, recordKeys: { _ in }, authorized: { true }, post: { _ in })
        emptyRetry.enqueue(previous: nil, current: empty, now: now)
        await emptyRetry.waitUntilIdle()
        check(emptyRetry.error != nil, "empty planning failure visible")
        emptyRetry.enqueue(previous: empty, current: empty, now: now)
        await emptyRetry.waitUntilIdle()
        check(emptyRetry.error == nil, "empty planning success clears planning error")

        var failureKeys = Set<String>()
        var failDelivery = true
        let (deliveryAndPlanningClient, _) = try flakyClient("delivery-and-planning", failOn: 2)
        let failureCoordinator = NotificationCoordinator(client: deliveryAndPlanningClient, enabled: { true }, available: { true },
            readKeys: { failureKeys }, recordKeys: { failureKeys.formUnion($0) }, authorized: { true },
            post: { _ in if failDelivery { throw EngineError(message: "delivery remains unresolved") } })
        failureCoordinator.enqueue(previous: nil, current: current, now: now)
        await failureCoordinator.waitUntilIdle()
        check(failureCoordinator.error != nil && failureKeys.isEmpty, "delivery failure remains unacknowledged")
        failureCoordinator.enqueue(previous: nil, current: empty, now: now)
        await failureCoordinator.waitUntilIdle()
        check(failureCoordinator.error == "temporary planner failure", "planning failure shown over delivery error")
        failureCoordinator.enqueue(previous: empty, current: empty, now: now)
        await failureCoordinator.waitUntilIdle()
        check(failureCoordinator.error == "delivery remains unresolved" && failureKeys.isEmpty,
              "empty planning recovery restores unresolved delivery error")
        failDelivery = false
        let otherCredit = Data(#"{"reset_credits":{"glm":[{"title":"other","expires_at":"2026-01-02T00:00:00Z"}]}}"#.utf8)
        failureCoordinator.enqueue(previous: nil, current: otherCredit, now: now)
        await failureCoordinator.waitUntilIdle()
        check(failureCoordinator.error != nil && failureKeys.count == 1, "different key success preserves delivery error")
        failureCoordinator.enqueue(previous: nil, current: current, now: now)
        await failureCoordinator.waitUntilIdle()
        check(failureCoordinator.error == nil && failureKeys.count == 2, "same key acceptance resolves delivery error")
        let menuCards = try JSONDecoder().decode([StatusCard].self, from: Data("""
            [{"id":"claude:weekly","provider":"claude"},{"id":"claude:5h","provider":"claude"},
             {"id":"codex:weekly","provider":"codex"},{"id":"gemini:weekly","provider":"gemini"},
             {"id":"glm:5h","provider":"glm"},{"id":"other:x","provider":"other"}]
            """.utf8))
        let menuLayout = try JSONDecoder().decode([LayoutPanel].self, from: Data("""
            [{"main":"codex:weekly","subs":[]},{"main":"claude:weekly","subs":["claude:5h"]},
             {"main":"gemini","subs":[]},{"main":"","subs":[]},{"main":"glm:5h"}]
            """.utf8))
        let menuOrder = MenuPopoverView.orderedCards(menuCards, layout: menuLayout)
        check(menuOrder.map { $0.card.id ?? "" } ==
              ["codex:weekly", "claude:weekly", "claude:5h", "gemini:weekly", "glm:5h", "other:x"],
              "menu cards follow panel order with subs after main")
        check(menuOrder.map(\.isSub) == [false, false, true, false, false, false], "menu sub cards marked")
        check(MenuPopoverView.orderedCards(menuCards, layout: nil).map { $0.card.id ?? "" } ==
              menuCards.map { $0.id ?? "" }, "menu keeps card order without layout")

        print("Regression checks: \(checks) passed")
    }
}
