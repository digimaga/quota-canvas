import Foundation

@main
struct RegressionTests {
    static var checks = 0
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        checks += 1
        guard condition() else { fatalError("Failed: \(message)") }
    }

    @MainActor static func main() async throws {
        let original = ConfigDocument(object: ["display": ["paused": false, "brightness": 100],
            "providers": ["example": ["color": "#123456", "enabled": true]],
            "routing": ["enabled": false, "floor_percent": 5, "stale_minutes": 20]])
        var edited = original
        edited.brightness = 40
        edited.removeProviderColor("example")
        edited.routingFloorPercent = 10
        var latest = original
        latest.paused = true
        latest.routingEnabled = true
        latest.routingStaleMinutes = 30
        let saved = ConfigDocument.applyingChanges(from: original, to: edited, onto: latest)
        check(saved.paused, "pause preserved")
        check(saved.brightness == 40, "brightness changed")
        check(saved.providerColor("example") == nil, "color removed")
        check(saved.providerEnabled("example"), "provider enabled preserved")
        check(saved.routingEnabled, "routing enabled preserved")
        check(saved.routingFloorPercent == 10, "routing floor changed")
        check(saved.routingStaleMinutes == 30, "routing stale preserved")
        let absent = ConfigDocument(object: [:])
        var added = absent
        added.brightness = 25
        let concurrentlyAdded = ConfigDocument(object: ["display": ["paused": true]])
        let merged = ConfigDocument.applyingChanges(from: absent, to: added, onto: concurrentlyAdded)
        check(merged.paused && merged.brightness == 25, "new section merges with concurrent section")
        let removed = ConfigDocument.applyingChanges(from: original, to: original, onto: absent)
        check(removed.object.isEmpty, "unchanged sections are not recreated")
        check(RoutingSettingsView.isValidWorkerName("worker-1.a_b"), "valid worker name")
        check(RoutingSettingsView.isValidWorkerName(String(repeating: "a", count: 40)), "40 characters")
        check(!RoutingSettingsView.isValidWorkerName(""), "empty worker name")
        check(!RoutingSettingsView.isValidWorkerName("bad name"), "invalid worker name")
        check(!RoutingSettingsView.isValidWorkerName(String(repeating: "a", count: 41)), "41 characters")
        let now = Date(timeIntervalSince1970: 1000)
        check(!RoutingSettingsView.verdictIsStale(now.addingTimeInterval(-300), now: now), "fresh threshold")
        check(RoutingSettingsView.verdictIsStale(now.addingTimeInterval(-301), now: now), "stale threshold")
        check(RoutingSettingsView.verdictIsStale(nil, now: now), "missing timestamp")

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

        let statusJSON = #"{"updated_at":"2026-10-28T12:00:00Z","cards":[{"id":"one","fetched_at":"2026-10-28T10:00:00Z"},{"id":"two","fetched_at":42},{"id":"three"}]}"#
        let decoded = try JSONDecoder().decode(EngineStatus.self, from: Data(statusJSON.utf8))
        let reference = ISO8601Support.parse("2026-10-28T12:00:30Z")!
        let first = decoded.cards![0]
        check(first.fetchedAt == ISO8601Support.parse("2026-10-28T10:00:00Z"), "card fetch time decoded")
        check(decoded.cards![1].fetchedAt == nil && decoded.cards![2].fetchedAt == nil, "malformed and missing fetch time are unknown")
        check(!StatusStore.engineIsStale(updatedAt: decoded.updatedAt, now: reference), "engine freshness uses update time")
        check(StatusStore.engineIsStale(updatedAt: decoded.updatedAt, now: reference.addingTimeInterval(300)), "engine stale after update threshold")
        check(StatusStore.engineIsStale(updatedAt: nil, now: reference), "missing engine timestamp is unknown")
        let fetchText = CardRow(card: first, gauge: nil).fetchText(now: reference)
        let expectedAge = RelativeDateTimeFormatter().localizedString(for: first.fetchedAt!, relativeTo: reference)
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
        let script = dir.appendingPathComponent("engine")
        let state = dir.appendingPathComponent("config.json")
        try Data("{\"display\":{\"paused\":false,\"brightness\":100}}".utf8).write(to: state)
        let python = #"""
        #!/usr/bin/python3
        import json, os, sys, time
        path = os.path.join(os.path.dirname(__file__), "config.json")
        if sys.argv[1:] == ["config", "--json"]:
            with open(path) as f: data = json.load(f)
            time.sleep(0.1)
        elif sys.argv[1:] == ["config", "--set-json"]:
            data = json.load(sys.stdin)
            with open(path, "w") as f: json.dump(data, f)
        else:
            sys.exit(1)
        print(json.dumps(data))
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
