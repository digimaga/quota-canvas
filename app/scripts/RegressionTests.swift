import Foundation

@main
struct RegressionTests {
    static var checks = 0
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        checks += 1
        guard condition() else { fatalError("Failed: \(message)") }
    }

    static func main() async throws {
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
        print("Regression checks: \(checks) passed")
    }
}
