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

/// RoutingModeClient の偽物。呼ばれた引数を記録し、返す値・投げるエラー・
/// routingModes の応答タイミング（modesGate）を外から設定できる。
@MainActor
final class FakeRoutingModeClient: RoutingModeClient {
    var setCalls: [(name: String, mode: String)] = []
    var modesCalls = 0
    var refreshCalls = 0
    var setResult: [RoutingModeEntry] = []
    var setError: Error?
    var modesResult: [RoutingModeEntry] = []
    var modesError: Error?
    var modesGate: (() async -> Void)?

    func routingModes() async throws -> [RoutingModeEntry] {
        modesCalls += 1
        // 応答は呼び出し時点のものを返す（ゲート後に差し替えても古い応答のまま）。
        let gate = modesGate
        let result = modesResult
        let error = modesError
        if let gate { await gate() }
        if let error { throw error }
        return result
    }
    func setRoutingMode(name: String, mode: String) async throws -> [RoutingModeEntry] {
        setCalls.append((name: name, mode: mode))
        if let setError { throw setError }
        return setResult
    }
    func refresh() async throws { refreshCalls += 1 }
}

@main
struct RegressionTests {
    static var checks = 0
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        checks += 1
        guard condition() else { fatalError("Failed: \(message)") }
    }

    @MainActor static func main() async throws {
        let museNow = Date(timeIntervalSince1970: 1_800_000_000)
        for status in [400, 404, 409, 410, 422, 499] {
            check(MuseUsagePoller.shouldRefreshActionId(status: status, lastFailedRefresh: nil, now: museNow), "muse refreshes action id on \(status)")
        }
        for status in [0, 200, 399, 401, 403, 429, 500, 503] {
            check(!MuseUsagePoller.shouldRefreshActionId(status: status, lastFailedRefresh: nil, now: museNow), "muse keeps action id on \(status)")
        }
        let museInterval = MuseUsagePoller.refreshRetryInterval
        check(!MuseUsagePoller.shouldRefreshActionId(status: 409, lastFailedRefresh: museNow.addingTimeInterval(-museInterval + 1), now: museNow), "muse waits after a failed refresh")
        check(MuseUsagePoller.shouldRefreshActionId(status: 409, lastFailedRefresh: museNow.addingTimeInterval(-museInterval), now: museNow), "muse refreshes again after the interval")
        check(RoutingSettingsView.isValidWorkerName("worker-1.a_b"), "valid worker name")
        check(RoutingSettingsView.isValidWorkerName(String(repeating: "a", count: 40)), "40 characters")
        check(!RoutingSettingsView.isValidWorkerName(""), "empty worker name")
        check(!RoutingSettingsView.isValidWorkerName("bad name"), "invalid worker name")
        check(!RoutingSettingsView.isValidWorkerName(String(repeating: "a", count: 41)), "41 characters")
        let freshnessNow = Date(timeIntervalSince1970: 1000)
        check(!RoutingSettingsView.verdictIsStale(freshnessNow.addingTimeInterval(-300), now: freshnessNow), "fresh threshold")
        check(RoutingSettingsView.verdictIsStale(freshnessNow.addingTimeInterval(-301), now: freshnessNow), "stale threshold")
        check(RoutingSettingsView.verdictIsStale(nil, now: freshnessNow), "missing timestamp")

        // routing.models（モデル台帳）: 一覧は model_order 順、enabled が無い古い
        // JSON は true。enabled の更新は台帳の他のキーを壊さない。
        var ledger = try ConfigDocument(data: Data(#"""
        {"routing":{"models":{"b":{"workers":["codex"],"roles":["impl"],"invoke":"codex-run","edit":true,"note":"差分を先に確認"},
        "a":{"workers":["glm"],"roles":["review","consult"],"edit":false,"note":""}},"model_order":["a","b"]}}
        """#.utf8))
        check(ledger.routingModels.map(\.name) == ["a", "b"], "model ledger follows model_order")
        check(ledger.routingModels[0].roles == ["review", "consult"]
              && ledger.routingModels[0].enabled && !ledger.routingModels[0].edit
              && ledger.routingModels[1].note == "差分を先に確認",
              "model ledger reads roles, note, edit and defaults enabled to true")
        ledger.setRoutingModelEnabled("b", false)
        ledger.setRoutingModelEnabled("missing", false)
        let ledgerModels = ledger.object["routing"] as? [String: Any]
        let ledgerB = (ledgerModels?["models"] as? [String: Any])?["b"] as? [String: Any]
        let ledgerA = (ledgerModels?["models"] as? [String: Any])?["a"] as? [String: Any]
        check(ledgerB?["enabled"] as? Bool == false
              && ledgerB?["workers"] as? [String] == ["codex"]
              && ledgerB?["roles"] as? [String] == ["impl"]
              && ledgerB?["invoke"] as? String == "codex-run"
              && ledgerB?["edit"] as? Bool == true
              && ledgerB?["note"] as? String == "差分を先に確認",
              "toggling a model keeps the other ledger keys")
        check(ledgerA?["enabled"] == nil && ledgerModels?["model_order"] as? [String] == ["a", "b"],
              "toggling a model leaves other entries and model_order untouched")

        // 役割（実装 / レビュー）の選択: 正規の並びで書き、最後の 1 つは外せない。
        // roles の更新は台帳の他のキーと enabled を壊さない。
        check(ConfigDocument.routingModelRoles(["review", "consult"], setting: "impl", to: true) == ["impl", "review", "consult"],
              "turning impl on keeps the canonical role order")
        check(ConfigDocument.routingModelRoles(["impl", "review"], setting: "review", to: false) == ["impl"],
              "turning a role off keeps the others")
        check(ConfigDocument.routingModelRoles(["impl"], setting: "impl", to: false) == nil,
              "the last role cannot be turned off")
        check(ConfigDocument.routingModelRoles(["impl", "x"], setting: "impl", to: false) == ["x"],
              "unknown roles are kept and count as a remaining role")
        check(ConfigDocument.routingModelRoles(["impl"], setting: "x", to: true) == ["impl", "x"]
              && ConfigDocument.routingModelRoles(["impl", "x"], setting: "x", to: false) == ["impl"],
              "unknown roles can be added and removed")
        check(ConfigDocument.routingModelRoles(["impl"], setting: "impl", to: true) == ["impl"],
              "turning on an already-on role is a no-op")
        ledger.setRoutingModelRoles("a", ["impl", "review", "consult"])
        ledger.setRoutingModelRoles("a", [])
        ledger.setRoutingModelRoles("missing", ["impl"])
        let rolesModels = ledger.object["routing"] as? [String: Any]
        let rolesA = (rolesModels?["models"] as? [String: Any])?["a"] as? [String: Any]
        let rolesB = (rolesModels?["models"] as? [String: Any])?["b"] as? [String: Any]
        check(rolesA?["roles"] as? [String] == ["impl", "review", "consult"]
              && rolesA?["workers"] as? [String] == ["glm"]
              && rolesA?["edit"] as? Bool == false
              && rolesA?["enabled"] == nil,
              "setting roles keeps the other ledger keys and ignores an empty list")
        check(rolesB?["enabled"] as? Bool == false && rolesB?["roles"] as? [String] == ["impl"]
              && rolesModels?["model_order"] as? [String] == ["a", "b"],
              "setting roles leaves other entries and model_order untouched")


        // 監視のみのカード: monitor_only が true なら状態色ではなく専用色。無い・型違いは通常色。
        let monitorCards = try JSONDecoder().decode([StatusCard].self, from: Data(#"""
        [{"id":"a:w","status":"spare","status_label":"監視のみ","monitor_only":true},
         {"id":"b:w","status":"spare"},
         {"id":"c:w","status":"over","monitor_only":"yes"}]
        """#.utf8))
        check(monitorCards[0].monitorOnly == true && StatusColor.color(for: monitorCards[0]) == StatusColor.monitor,
              "monitor-only card uses the monitor color")
        check(monitorCards[1].monitorOnly == nil && StatusColor.color(for: monitorCards[1]) == StatusColor.color(for: "spare"),
              "card without monitor_only keeps its status color")
        check(monitorCards[2].monitorOnly == nil && StatusColor.color(for: monitorCards[2]) == StatusColor.color(for: "over"),
              "malformed monitor_only is ignored")

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

        // second_bar（小パネルの 2 本目のバー）: nil = キー省略（自動）、true/false は
        // 往復で保持。large では書き出さない。NSNull・型違いは nil。
        let secondBarDoc = try ConfigDocument(data: Data(#"{"layout":{"panels":[{"main":"alpha:week","size":"large","second_bar":true},{"main":"beta:day","size":"small","second_bar":true},{"main":"gamma:day","size":"small","second_bar":false},{"main":"delta:day","size":"small"},{"main":"epsilon:day","size":"small","second_bar":null},{"main":"zeta:day","size":"small","second_bar":"yes"}]}}"#.utf8))
        let secondBarPanels = secondBarDoc.layoutPanels
        check(secondBarPanels.map(\.secondBar) == [true, true, false, nil, nil, nil],
              "second_bar decodes; missing, null and wrong type are nil")
        var secondBarRewrite = secondBarDoc
        secondBarRewrite.layoutPanels = secondBarPanels
        let rewrittenPanels = (secondBarRewrite.object["layout"] as? [String: Any])?["panels"] as? [[String: Any]]
        check(rewrittenPanels?.count == secondBarPanels.count, "rewrite keeps every panel")
        check(rewrittenPanels?[0]["second_bar"] == nil, "large never writes second_bar")
        check(rewrittenPanels?[1]["second_bar"] as? Bool == true
              && rewrittenPanels?[2]["second_bar"] as? Bool == false,
              "small keeps explicit second_bar")
        check(rewrittenPanels?[3]["second_bar"] == nil && rewrittenPanels?[4]["second_bar"] == nil
              && rewrittenPanels?[5]["second_bar"] == nil, "nil second_bar omits the key")

        // status.json の layout にも second_bar が付くことがある（無くてもよい・型違いは nil）
        let secondBarStatus = try JSONDecoder().decode([LayoutPanel].self,
            from: Data(#"[{"main":"beta:day","size":"small","second_bar":true},{"main":"gamma:day","size":"small","second_bar":"yes"}]"#.utf8))
        check(secondBarStatus[0].secondBar == true && secondBarStatus[1].secondBar == nil,
              "status layout second_bar decodes; wrong type is nil")

        // 小スロットへの復元と保存の往復。空の段・大スロットには付かない。
        var secondBarSlots = LayoutSettingsView.emptySlots()
        var secondBarSplits = [Bool](repeating: false, count: LayoutSettingsView.columnCount)
        secondBarSplits[0] = true
        secondBarSplits[1] = true
        secondBarSlots[LayoutSettingsView.slotIndex(0, 1)].main = "beta:day"
        secondBarSlots[LayoutSettingsView.slotIndex(0, 1)].secondBar = true
        secondBarSlots[LayoutSettingsView.slotIndex(0, 2)].main = "gamma:day"
        secondBarSlots[LayoutSettingsView.slotIndex(0, 2)].secondBar = false
        secondBarSlots[LayoutSettingsView.slotIndex(1, 1)].secondBar = true // 空の段: 保存されない
        secondBarSlots[LayoutSettingsView.slotIndex(1, 2)].main = "delta:day"
        secondBarSlots[LayoutSettingsView.slotIndex(2, 0)].main = "alpha:week"
        secondBarSlots[LayoutSettingsView.slotIndex(2, 0)].secondBar = true // large: 保存されない
        let secondBarSaved = LayoutSettingsView.panelsForSave(slots: secondBarSlots, splits: secondBarSplits)
        check(secondBarSaved.map(\.main) == ["beta:day", "gamma:day", "", "delta:day", "alpha:week"],
              "second bar slots keep the panel order")
        check(secondBarSaved.map(\.secondBar) == [true, false, nil, nil, nil],
              "second_bar saves on filled small rows only")
        let secondBarRestored = LayoutSettingsView.restoreLayout(config: secondBarSaved, status: [])
        check(secondBarRestored.splits[0] && secondBarRestored.splits[1],
              "small pairs restore as split columns")
        check(secondBarRestored.slots[LayoutSettingsView.slotIndex(0, 1)].secondBar == true
              && secondBarRestored.slots[LayoutSettingsView.slotIndex(0, 2)].secondBar == false
              && secondBarRestored.slots[LayoutSettingsView.slotIndex(1, 2)].secondBar == nil
              && secondBarRestored.slots[LayoutSettingsView.slotIndex(2, 0)].secondBar == nil,
              "second_bar round-trips through restore and save")
        check(LayoutSettingsView.acceptsSecondBar("beta:day") && LayoutSettingsView.acceptsSecondBar("beta")
              && !LayoutSettingsView.acceptsSecondBar("calendar") && !LayoutSettingsView.acceptsSecondBar("local:x")
              && !LayoutSettingsView.acceptsSecondBar("") && !LayoutSettingsView.acceptsSecondBar(nil),
              "second bar is offered for windows and services but not calendar or local")

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
        let subWindows = ["claude:weekly", "claude:5h", "claude:weekly_fable", "codex:weekly", "local:gx10"]
            .map { LayoutSettingsView.WindowChoice(id: $0, label: $0) }
        check(LayoutSettingsView.subCandidates(main: "claude:weekly", windows: subWindows, selected: []).map(\.id)
              == ["claude:5h", "claude:weekly_fable"], "sub candidates are the main service's other windows")
        check(LayoutSettingsView.subCandidates(main: "claude:weekly", windows: subWindows, selected: ["codex:weekly"])
              .map(\.id) == ["claude:5h", "claude:weekly_fable", "codex:weekly"],
              "an already selected window of another service stays removable")
        check(LayoutSettingsView.subCandidates(main: nil, windows: subWindows, selected: []).isEmpty
              && !LayoutSettingsView.acceptsSubs(nil) && !LayoutSettingsView.acceptsSubs("")
              && !LayoutSettingsView.acceptsSubs("calendar") && !LayoutSettingsView.acceptsSubs("local:gx10")
              && !LayoutSettingsView.acceptsSubs("kimi") && LayoutSettingsView.acceptsSubs("claude:weekly"),
              "subs need a main window")

        // second_window（2 本目のバーに出す窓の ID）: nil = キー省略（自動）、値は往復で
        // 保持。空文字・null・型違いは nil。large では書き出さない。
        let secondWindowDoc = try ConfigDocument(data: Data(#"{"layout":{"panels":[{"main":"alpha:week","size":"large","second_window":"alpha:5h"},{"main":"beta:day","size":"small","second_window":"beta:5h"},{"main":"gamma:day","size":"small","second_window":""},{"main":"delta:day","size":"small"},{"main":"epsilon:day","size":"small","second_window":null},{"main":"zeta:day","size":"small","second_window":42}]}}"#.utf8))
        let secondWindowPanels = secondWindowDoc.layoutPanels
        check(secondWindowPanels.map(\.secondWindow) == ["alpha:5h", "beta:5h", nil, nil, nil, nil],
              "second_window decodes; empty, missing, null and wrong type are nil")
        var secondWindowRewrite = secondWindowDoc
        secondWindowRewrite.layoutPanels = secondWindowPanels
        let rewrittenWindows = (secondWindowRewrite.object["layout"] as? [String: Any])?["panels"] as? [[String: Any]]
        check(rewrittenWindows?.count == secondWindowPanels.count, "rewrite keeps every panel for second_window")
        check(rewrittenWindows?[0]["second_window"] == nil, "large never writes second_window")
        check(rewrittenWindows?[1]["second_window"] as? String == "beta:5h",
              "small keeps explicit second_window")
        check(rewrittenWindows?[2]["second_window"] == nil && rewrittenWindows?[3]["second_window"] == nil
              && rewrittenWindows?[4]["second_window"] == nil && rewrittenWindows?[5]["second_window"] == nil,
              "nil second_window omits the key")

        // status.json の layout にも second_window が付くことがある（無くてもよい・型違いは nil）
        let secondWindowStatus = try JSONDecoder().decode([LayoutPanel].self,
            from: Data(#"[{"main":"beta:day","size":"small","second_window":"beta:5h"},{"main":"gamma:day","size":"small","second_window":42}]"#.utf8))
        check(secondWindowStatus[0].secondWindow == "beta:5h" && secondWindowStatus[1].secondWindow == nil,
              "status layout second_window decodes; wrong type is nil")

        // 小スロットへの復元と保存の往復。空の段・大スロットには付かない。
        var secondWindowSlots = LayoutSettingsView.emptySlots()
        var secondWindowSplits = [Bool](repeating: false, count: LayoutSettingsView.columnCount)
        secondWindowSplits[0] = true
        secondWindowSplits[1] = true
        secondWindowSlots[LayoutSettingsView.slotIndex(0, 1)].main = "beta:day"
        secondWindowSlots[LayoutSettingsView.slotIndex(0, 1)].secondWindow = "beta:5h"
        secondWindowSlots[LayoutSettingsView.slotIndex(0, 2)].main = "gamma:day"
        secondWindowSlots[LayoutSettingsView.slotIndex(1, 1)].secondWindow = "epsilon:5h" // 空の段: 保存されない
        secondWindowSlots[LayoutSettingsView.slotIndex(1, 2)].main = "delta:day"
        secondWindowSlots[LayoutSettingsView.slotIndex(2, 0)].main = "alpha:week"
        secondWindowSlots[LayoutSettingsView.slotIndex(2, 0)].secondWindow = "alpha:5h" // large: 保存されない
        let secondWindowSaved = LayoutSettingsView.panelsForSave(slots: secondWindowSlots, splits: secondWindowSplits)
        check(secondWindowSaved.map(\.main) == ["beta:day", "gamma:day", "", "delta:day", "alpha:week"],
              "second window slots keep the panel order")
        check(secondWindowSaved.map(\.secondWindow) == ["beta:5h", nil, nil, nil, nil],
              "second_window saves on filled small rows only")
        let secondWindowRestored = LayoutSettingsView.restoreLayout(config: secondWindowSaved, status: [])
        check(secondWindowRestored.slots[LayoutSettingsView.slotIndex(0, 1)].secondWindow == "beta:5h"
              && secondWindowRestored.slots[LayoutSettingsView.slotIndex(0, 2)].secondWindow == nil
              && secondWindowRestored.slots[LayoutSettingsView.slotIndex(1, 2)].secondWindow == nil
              && secondWindowRestored.slots[LayoutSettingsView.slotIndex(2, 0)].secondWindow == nil,
              "second_window round-trips through restore and save")
        check(LayoutSettingsView.secondWindowCandidates(main: "claude:weekly", windows: subWindows,
                                                        selected: nil).map(\.id)
              == ["claude:5h", "claude:weekly_fable"],
              "second window candidates are the main service's other windows")
        check(LayoutSettingsView.secondWindowCandidates(main: "claude:weekly", windows: subWindows,
                                                        selected: "claude:gone").map(\.id)
              == ["claude:5h", "claude:weekly_fable", "claude:gone"],
              "a stored second window stays removable")
        check(LayoutSettingsView.secondWindowCandidates(main: "calendar", windows: subWindows, selected: nil).isEmpty
              && LayoutSettingsView.secondWindowCandidates(main: "local:gx10", windows: subWindows, selected: nil).isEmpty
              && LayoutSettingsView.secondWindowCandidates(main: nil, windows: subWindows, selected: nil).isEmpty,
              "no second window for calendar, local or empty main")

        // main がサービス ID のとき、エンジンが自動で選ぶ実際のメイン窓（最長、
        // 同長は key=="weekly"）は 2 本目に選べないので候補から除く。
        // 内訳窓（partOf）も出さない。窓 ID 指定では main 自身だけを除く。
        let serviceWindows = [
            LayoutSettingsView.WindowChoice(id: "opencode", label: "OpenCode"),
            LayoutSettingsView.WindowChoice(id: "opencode:5h", label: "5時間", length: 5 * 3600),
            LayoutSettingsView.WindowChoice(id: "opencode:weekly", label: "週間", length: 7 * 86400),
            LayoutSettingsView.WindowChoice(id: "opencode:daily", label: "日次", length: 86400),
            LayoutSettingsView.WindowChoice(id: "opencode:weekly_a", label: "内訳A",
                                            length: 86400, partOf: "weekly"),
            LayoutSettingsView.WindowChoice(id: "other:x", label: "Other", length: 3600)
        ]
        check(LayoutSettingsView.secondWindowCandidates(main: "opencode", windows: serviceWindows,
                                                        selected: nil).map(\.id)
              == ["opencode:5h", "opencode:daily"],
              "service main drops the engine-resolved main window and breakdown windows")
        check(LayoutSettingsView.secondWindowCandidates(main: "opencode:5h", windows: serviceWindows,
                                                        selected: nil).map(\.id)
              == ["opencode:weekly", "opencode:daily"],
              "window-id main drops only itself and breakdown windows")
        check(LayoutSettingsView.secondWindowCandidates(main: "opencode", windows: serviceWindows,
                                                        selected: "opencode:weekly").map(\.id)
              == ["opencode:5h", "opencode:daily", "opencode:weekly"],
              "a stored resolved main stays removable")
        let equalLengthWindows = [
            LayoutSettingsView.WindowChoice(id: "svc:long_a", label: "A", length: 7 * 86400),
            LayoutSettingsView.WindowChoice(id: "svc:weekly", label: "週間", length: 7 * 86400)
        ]
        check(LayoutSettingsView.secondWindowCandidates(main: "svc", windows: equalLengthWindows,
                                                        selected: nil).map(\.id) == ["svc:long_a"],
              "equal length resolves the weekly window as main")
        let partialWindows = [
            LayoutSettingsView.WindowChoice(id: "svc:unknown", label: "不明"),
            LayoutSettingsView.WindowChoice(id: "svc:weekly", label: "週間", length: 7 * 86400),
            LayoutSettingsView.WindowChoice(id: "svc:5h", label: "5時間", length: 5 * 3600)
        ]
        check(LayoutSettingsView.secondWindowCandidates(main: "svc", windows: partialWindows,
                                                        selected: nil).map(\.id)
              == ["svc:unknown", "svc:5h"],
              "windows without a length stay candidates; the resolvable main is still dropped")
        let noLengthWindows = [
            LayoutSettingsView.WindowChoice(id: "svc:5h", label: "5時間"),
            LayoutSettingsView.WindowChoice(id: "svc:weekly", label: "週間")
        ]
        check(LayoutSettingsView.secondWindowCandidates(main: "svc", windows: noLengthWindows,
                                                        selected: nil).map(\.id)
              == ["svc:5h", "svc:weekly"],
              "no resolvable main means no extra exclusion")

        // status.json の layout の "main_window": サービス ID の主枠が実際に解決された
        // メイン窓（解決できるときだけ付く）。型違い・欠落は nil。
        let mainWindowStatus = try JSONDecoder().decode([LayoutPanel].self,
            from: Data(#"[{"main":"opencode","size":"small","main_window":"opencode:weekly"},{"main":"beta:day","size":"small","main_window":42},{"main":"gamma:day","size":"small"}]"#.utf8))
        check(mainWindowStatus[0].mainWindow == "opencode:weekly" && mainWindowStatus[1].mainWindow == nil
              && mainWindowStatus[2].mainWindow == nil,
              "status layout main_window decodes; wrong type and missing are nil")

        // restoreLayout は status の対応パネル（同じ main・size）の main_window を
        // 主枠 → 窓 ID の対応として返す（small も large も対象）。
        let serviceMainConfig = [ConfigDocument.PanelConfig(main: "opencode", subs: nil, size: "small"),
                                 ConfigDocument.PanelConfig(main: "claude", subs: nil, size: "large")]
        let mainWindowLayout = try JSONDecoder().decode([LayoutPanel].self,
            from: Data(#"[{"main":"opencode","size":"small","main_window":"opencode:weekly"},{"main":"claude","size":"large","main_window":"claude:weekly"}]"#.utf8))
        check(LayoutSettingsView.restoreLayout(config: serviceMainConfig, status: mainWindowLayout)
              .resolvedMains == ["opencode": "opencode:weekly", "claude": "claude:weekly"],
              "status main_window resolves service mains on small and large panels")
        let mismatchedLayout = try JSONDecoder().decode([LayoutPanel].self,
            from: Data(#"[{"main":"opencode","size":"large","main_window":"opencode:weekly"}]"#.utf8))
        check(LayoutSettingsView.restoreLayout(config: serviceMainConfig, status: mismatchedLayout)
              .resolvedMains.isEmpty, "a main or size mismatch keeps no resolved main")
        check(LayoutSettingsView.restoreLayout(config: serviceMainConfig, status: [])
              .resolvedMains.isEmpty, "no status keeps no resolved main")

        // status が教える実際のメイン窓（main_window）があるときは、推定（最長窓・
        // weekly 優先）と違う窓でも推定ではなく main_window の方だけを候補から除く。
        // 取れないとき（古いエンジン・status 無し）は従来の推定が fallback。
        check(LayoutSettingsView.secondWindowCandidates(main: "opencode", windows: serviceWindows,
                                                        selected: nil, resolvedMain: "opencode:5h").map(\.id)
              == ["opencode:weekly", "opencode:daily"],
              "the engine-reported main window is excluded instead of the estimate")
        check(LayoutSettingsView.secondWindowCandidates(main: "opencode", windows: serviceWindows,
                                                        selected: nil, resolvedMain: "opencode:daily").map(\.id)
              == ["opencode:5h", "opencode:weekly"],
              "a reported main different from the estimate is the only exclusion")
        check(LayoutSettingsView.secondWindowCandidates(main: "opencode:5h", windows: serviceWindows,
                                                        selected: nil, resolvedMain: "opencode:daily").map(\.id)
              == ["opencode:weekly", "opencode:daily"],
              "a window-id main drops only itself even with a reported main")

        let statusJSON = #"{"updated_at":"2026-10-28T12:00:00Z","cards":[{"id":"one","fetched_at":"2026-10-28T10:00:00Z"},{"id":"two","fetched_at":42},{"id":"three"}]}"#
        let decoded = try JSONDecoder().decode(EngineStatus.self, from: Data(statusJSON.utf8))
        let reference = ISO8601Support.parse("2026-10-28T12:00:30Z")!
        let firstCard = decoded.cards![0]
        check(firstCard.fetchedAt == ISO8601Support.parse("2026-10-28T10:00:00Z"), "card fetch time decoded")
        check(decoded.cards![1].fetchedAt == nil && decoded.cards![2].fetchedAt == nil, "malformed and missing fetch time are unknown")
        check(!StatusStore.engineIsStale(updatedAt: decoded.updatedAt, now: reference), "engine freshness uses update time")
        check(StatusStore.engineIsStale(updatedAt: decoded.updatedAt, now: reference.addingTimeInterval(300)), "engine stale after update threshold")
        check(!StatusStore.engineIsStale(updatedAt: decoded.updatedAt, now: decoded.updatedAt!.addingTimeInterval(300)),
              "exactly 300 seconds is still fresh")
        check(StatusStore.engineIsStale(updatedAt: decoded.updatedAt, now: decoded.updatedAt!.addingTimeInterval(300.5)),
              "just past 300 seconds is stale")
        check(StatusStore.engineIsStale(updatedAt: nil, now: reference), "missing engine timestamp is unknown")
        let fetchText = CardRow(card: firstCard, gauge: nil).fetchText(now: reference)
        let expectedAge = RelativeDateTimeFormatter().localizedString(for: firstCard.fetchedAt!, relativeTo: reference)
        check(fetchText == "取得: \(expectedAge)", "card shows fetch age independent of redraw")
        check(CardRow(card: decoded.cards![1], gauge: nil).fetchText(now: reference) == String(localized: "取得時刻: 不明"), "unknown fetch time shown")
        let balanceJSON = #"{"balances":[{"provider":"kimi","name":"Kimi","currency":"USD","amount":49.58,"label":"今月の使用","stale_error":"HTTP 429","fetched_at":"2026-01-02T03:04:05Z"},{"provider":"deepseek","name":"DeepSeek","currency":"CNY","amount":110}]}"#
        let balanceStatus = try JSONDecoder().decode(EngineStatus.self, from: Data(balanceJSON.utf8))
        check(balanceStatus.balances?.first?.amount == 49.58, "balance decoding")
        check(balanceStatus.balances?.first?.staleError == "HTTP 429", "balance stale state")
        check(balanceStatus.balances?.first?.fetchedAt != nil, "balance fetched_at decoding")
        check(balanceStatus.balances?.first?.displayText == "今月の使用 $49.58", "balance label precedes amount")
        check(balanceStatus.balances?.last?.fetchedAt == nil, "optional balance fetched_at")
        check(balanceStatus.balances?.last?.displayText == "残高 ¥110.00", "default balance label precedes the amount")

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
        check(keys.count == 1 && posts.isEmpty, "unauthorized records without posting")
        keys.removeAll()
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
        check(menuOrder.map(\.inLayout) == [true, true, true, true, true, false],
              "menu marks cards outside the layout for the compact list")
        check(MenuPopoverView.orderedCards(menuCards, layout: nil).map(\.inLayout).allSatisfy { $0 }
              && MenuPopoverView.orderedCards(menuCards, layout: []).map(\.inLayout).allSatisfy { $0 },
              "empty or missing layout puts every card on screen")
        let staleMenuLayout = try JSONDecoder().decode([LayoutPanel].self, from: Data(#"[{"main":"gone:weekly","subs":[]}]"#.utf8))
        let staleMenuOrder = MenuPopoverView.orderedCards(menuCards, layout: staleMenuLayout)
        check(staleMenuOrder.map(\.inLayout).allSatisfy { !$0 }, "a layout matching no card leaves them all off screen")
        let compactReference = ISO8601Support.parse("2026-01-01T00:00:00Z")!
        let compactReset = compactReference.addingTimeInterval(3600)
        check(CompactCardRow.resetRemainingText(nil, now: compactReference) == nil, "compact row hides unknown reset time")
        check(CompactCardRow.resetRemainingText(compactReference.addingTimeInterval(-1), now: compactReference)
              == String(localized: "リセット済み"), "compact row labels an elapsed reset")
        check(CompactCardRow.resetRemainingText(compactReset, now: compactReference)
              == RelativeDateTimeFormatter().localizedString(for: compactReset, relativeTo: compactReference),
              "compact reset keeps the relative time")
        // provider ID の主枠は描画側（render.py の _main_rank）と同じ「一番長い窓、
        // 同長は key=="weekly"、取得失敗中は後回し」に当てる。窓の長さは status.json
        // の "windows" の length（秒）。カード順が 5h→weekly でも画面と同じ weekly を出す。
        let multiCards = try JSONDecoder().decode([StatusCard].self, from: Data("""
            [{"id":"claude:5h","provider":"claude"},{"id":"claude:weekly","provider":"claude"}]
            """.utf8))
        let multiWindows = try JSONDecoder().decode([StatusWindow].self, from: Data("""
            [{"id":"claude:5h","provider":"claude","key":"5h","length":18000},
             {"id":"claude:weekly","provider":"claude","key":"weekly","length":604800}]
            """.utf8))
        let claudeLayout = try JSONDecoder().decode([LayoutPanel].self, from: Data(#"[{"main":"claude","subs":[]}]"#.utf8))
        let resolved = MenuPopoverView.orderedCards(multiCards, layout: claudeLayout, windows: multiWindows)
        check(resolved.first?.card.id == "claude:weekly", "provider main resolves to the longest window like render")
        check(resolved.map(\.inLayout) == [true, false], "the shorter window stays an extra card")
        check(MenuPopoverView.mainWindowRank(multiCards[0], windows: multiWindows)
              < MenuPopoverView.mainWindowRank(multiCards[1], windows: multiWindows),
              "a longer window outranks a shorter one")
        // 長さ優先と weekly 優先を分ける競合入力: nova:monthly は nova:weekly より
        // 長いが key が "weekly" ではない。weekly 優先だけなら weekly が主枠になる。
        let novaCards = try JSONDecoder().decode([StatusCard].self, from: Data("""
            [{"id":"nova:weekly","provider":"nova"},{"id":"nova:monthly","provider":"nova"}]
            """.utf8))
        let novaWindows = try JSONDecoder().decode([StatusWindow].self, from: Data("""
            [{"id":"nova:weekly","provider":"nova","key":"weekly","length":604800},
             {"id":"nova:monthly","provider":"nova","key":"monthly","length":2592000}]
            """.utf8))
        let novaLayout = try JSONDecoder().decode([LayoutPanel].self, from: Data(#"[{"main":"nova","subs":[]}]"#.utf8))
        let novaResolved = MenuPopoverView.orderedCards(novaCards, layout: novaLayout, windows: novaWindows)
        check(novaResolved.first?.card.id == "nova:monthly", "a longer non-weekly window wins the main slot")
        check(novaResolved.map(\.inLayout) == [true, false], "the outranked weekly window stays an extra card")
        check(MenuPopoverView.mainWindowRank(novaCards[0], windows: novaWindows)
              < MenuPopoverView.mainWindowRank(novaCards[1], windows: novaWindows),
              "window length outranks the weekly key")
        // windows 省略（既定 nil）の呼び出しは orderedCards(menuCards, layout: menuLayout) で
        // コンパイル済み。明示的な windows: nil で長さ不明でも weekly key が勝つことを見る。
        check(MenuPopoverView.orderedCards(multiCards, layout: claudeLayout, windows: nil)
              .first?.card.id == "claude:weekly", "unknown window lengths still prefer the weekly key")
        let tieCards = try JSONDecoder().decode([StatusCard].self, from: Data("""
            [{"id":"sun:daily","provider":"sun"},{"id":"sun:weekly","provider":"sun"}]
            """.utf8))
        let tieWindows = try JSONDecoder().decode([StatusWindow].self, from: Data("""
            [{"id":"sun:daily","provider":"sun","key":"daily","length":86400},
             {"id":"sun:weekly","provider":"sun","key":"weekly","length":86400}]
            """.utf8))
        let sunLayout = try JSONDecoder().decode([LayoutPanel].self, from: Data(#"[{"main":"sun","subs":[]}]"#.utf8))
        check(MenuPopoverView.orderedCards(tieCards, layout: sunLayout, windows: tieWindows)
              .first?.card.id == "sun:weekly", "equal lengths prefer the weekly key")
        let staleCards = try JSONDecoder().decode([StatusCard].self, from: Data("""
            [{"id":"codex:weekly","provider":"codex","stale_error":"HTTP 429"},{"id":"codex:5h","provider":"codex"}]
            """.utf8))
        let staleWindows = try JSONDecoder().decode([StatusWindow].self, from: Data("""
            [{"id":"codex:weekly","provider":"codex","key":"weekly","length":604800},
             {"id":"codex:5h","provider":"codex","key":"5h","length":18000}]
            """.utf8))
        let codexLayout = try JSONDecoder().decode([LayoutPanel].self, from: Data(#"[{"main":"codex","subs":[]}]"#.utf8))
        // stale_error（取得失敗中の最後の成功値）も描画側と同じく主枠候補。
        // 描画側が候補から外すのは pu.error、つまり status.json の errors に載る provider だけ。
        check(MenuPopoverView.orderedCards(staleCards, layout: codexLayout, windows: staleWindows)
              .first?.card.id == "codex:weekly", "a stale last-good window stays a main candidate like render")
        check(MenuPopoverView.orderedCards(staleCards, layout: codexLayout, windows: staleWindows,
                                           errorProviders: ["codex"]).map(\.inLayout) == [false, false],
              "a provider with a fetch error stays off screen like the render error panel")
        // windows 既定引数（nil）の呼び出し契約は orderedCards(menuCards, layout: menuLayout) と
        // 上の windows: nil チェックでカバー済みのため、実行時同値のチェックは置かない。
        // main="claude:weekly" の次に main="claude" でも、描画側と同じく両方 weekly に
        // 解決し、同じカードは最初の位置に 1 回だけ出る（5h は配置外のまま）。
        let duplicateLayout = try JSONDecoder().decode([LayoutPanel].self, from: Data("""
            [{"main":"claude:weekly","subs":[]},{"main":"claude","subs":[]}]
            """.utf8))
        let dedupedMenu = MenuPopoverView.orderedCards(multiCards, layout: duplicateLayout, windows: multiWindows)
        let dedupedIds = dedupedMenu.map { $0.card.id ?? "" }
        let dedupedFlags = dedupedMenu.map { $0.inLayout }
        check(dedupedIds == ["claude:weekly", "claude:5h"] && dedupedFlags == [true, false],
              "both panels resolve to the same window, shown once at the first slot")

        // RoutingModeModel（振り分けタブのモード切り替え）を偽のクライアントで動かす。
        let routingClient = FakeRoutingModeClient()
        var scheduledReloads = 0
        var routingErrors: [String] = []
        let routingModel = RoutingModeModel(client: routingClient,
            scheduleReload: { scheduledReloads += 1 },
            reportError: { routingErrors.append($0) })
        // 自動で監視のみ（Muse）: auto が true のエントリだけ isAutoMonitor
        let autoEntries = try JSONDecoder().decode([RoutingModeEntry].self, from: Data(#"""
        [{"name":"muse","mode":"off","until":null,"auto":true},
         {"name":"grok","mode":"off","until":null},
         {"name":"codex","mode":"standard","until":null,"auto":false}]
        """#.utf8))
        routingModel.modeEntries = autoEntries
        check(routingModel.isAutoMonitor("muse") && !routingModel.isAutoMonitor("grok")
              && !routingModel.isAutoMonitor("codex") && !routingModel.isAutoMonitor("none"),
              "only auto:true entries are auto monitor-only")
        routingModel.modeEntries = []
        routingClient.setResult = [RoutingModeEntry(name: "w1", mode: "burn", until: nil)]
        let setTask = routingModel.setMode(named: "w1", mode: "burn")
        check(setTask != nil && routingModel.modeBusy, "setMode marks busy while running")
        await setTask?.value
        check(routingClient.setCalls.count == 1
              && routingClient.setCalls[0].name == "w1" && routingClient.setCalls[0].mode == "burn",
              "setMode calls the engine once with the worker name and mode")
        check(routingModel.modeEntries.count == 1 && routingModel.modeEntries[0].name == "w1"
              && routingModel.modeEntries[0].mode == "burn",
              "modeEntries are replaced by the reply")
        check(routingModel.modeLoaded && !routingModel.modeBusy, "setMode clears busy when finished")
        check(routingClient.refreshCalls == 1 && scheduledReloads == 1,
              "setMode refreshes the engine and schedules an artifact reload")
        check(routingErrors.isEmpty, "a successful setMode reports no error")
        check(routingModel.setMode(named: "", mode: "off") == nil && routingClient.setCalls.count == 1,
              "an empty worker name does not call the engine")
        check(routingModel.mode(for: "w1") == "burn" && routingModel.mode(for: "nobody") == "standard"
              && routingModel.mode(for: nil) == "standard",
              "mode(for:) falls back to standard for missing and nil names")

        let failingClient = FakeRoutingModeClient()
        failingClient.setError = EngineError(message: "set failed")
        failingClient.modesResult = [RoutingModeEntry(name: "w2", mode: "off", until: nil)]
        var failureErrors: [String] = []
        let failingModel = RoutingModeModel(client: failingClient,
            scheduleReload: {}, reportError: { failureErrors.append($0) })
        await failingModel.setMode(named: "w2", mode: "burn")?.value
        check(failureErrors == ["set failed"], "a failed setMode surfaces the error text")
        check(failingClient.modesCalls == 1 && failingClient.refreshCalls == 0,
              "a failed setMode reloads the mode list instead of refreshing")
        check(failingModel.modeEntries.count == 1 && failingModel.modeEntries[0].name == "w2"
              && !failingModel.modeBusy,
              "a failed setMode keeps the reloaded list and clears busy")

        // 世代番号: 進行中の古い reloadModes の応答は新しい一覧を上書きしない。
        let gatedClient = FakeRoutingModeClient()
        let gatedModel = RoutingModeModel(client: gatedClient)
        let modeGate = DeliveryGate()
        gatedClient.modesResult = [RoutingModeEntry(name: "stale", mode: "off", until: nil)]
        gatedClient.modesGate = { await modeGate.deliver() }
        async let staleReload: Void = gatedModel.reloadModes()
        await modeGate.waitForStart()
        gatedClient.modesGate = nil
        gatedClient.modesResult = [RoutingModeEntry(name: "fresh", mode: "burn", until: nil)]
        await gatedModel.reloadModes()
        check(gatedModel.modeEntries.first?.name == "fresh", "the newer reload applies its reply")
        modeGate.accept()
        await staleReload
        check(gatedModel.modeEntries.count == 1 && gatedModel.modeEntries[0].name == "fresh",
              "a stale routingModes reply does not overwrite a newer list")

        // 世代番号: 進行中の古い reloadModes は setMode 成功の一覧を上書きしない。
        let staleGateClient = FakeRoutingModeClient()
        let staleGateModel = RoutingModeModel(client: staleGateClient)
        let staleGate = DeliveryGate()
        staleGateClient.modesResult = [RoutingModeEntry(name: "stale", mode: "off", until: nil)]
        staleGateClient.modesGate = { await staleGate.deliver() }
        async let heldReload: Void = staleGateModel.reloadModes()
        await staleGate.waitForStart()
        staleGateClient.setResult = [RoutingModeEntry(name: "w3", mode: "burn", until: nil)]
        await staleGateModel.setMode(named: "w3", mode: "burn")?.value
        check(staleGateModel.modeEntries.count == 1 && staleGateModel.modeEntries[0].name == "w3",
              "a setMode reply replaces the mode list")
        staleGate.accept()
        await heldReload
        check(staleGateModel.modeEntries.count == 1 && staleGateModel.modeEntries[0].name == "w3",
              "a stale routingModes reply does not overwrite the setMode reply")

        // pollVerdicts: sleep が投げるまで sleep → reload を繰り返す。
        var pollSleeps = 0
        var pollReloads = 0
        await RoutingModeModel.pollVerdicts(
            isCancelled: { false },
            sleep: {
                pollSleeps += 1
                if pollSleeps == 3 { throw EngineError(message: "stop") }
            },
            reload: { pollReloads += 1 })
        check(pollSleeps == 3 && pollReloads == 2,
              "pollVerdicts reloads once per completed sleep and exits when sleep throws")
        var cancelledSleeps = 0
        var cancelledReloads = 0
        await RoutingModeModel.pollVerdicts(
            isCancelled: { true },
            sleep: { cancelledSleeps += 1 },
            reload: { cancelledReloads += 1 })
        check(cancelledSleeps == 0 && cancelledReloads == 0,
              "a poll cancelled from the start never sleeps or reloads")

        // app-state.json（エンジンの watchdog が読む生存状態）: キーと値の型、
        // user_quit の真偽。pid は Python 側で isinstance(int) を要求するので
        // 整数リテラルでなければならない。
        let stateLaunched = Date(timeIntervalSince1970: 1_800_000_000)
        let stateRunning = AppStateFile.encode(bundlePath: "/Applications/QuotaCanvas.app",
                                               pid: 1234, launchedAt: stateLaunched, userQuit: false)
        let stateRunningJSON = try JSONSerialization.jsonObject(with: stateRunning) as? [String: Any]
        check(stateRunningJSON?.count == 4, "app state writes exactly the four contract keys")
        check(stateRunningJSON?["bundle_path"] as? String == "/Applications/QuotaCanvas.app",
              "bundle_path is a string")
        check(stateRunningJSON?["pid"] as? Int == 1234, "pid is an integer")
        check((stateRunningJSON?["launched_at"] as? NSNumber)?.doubleValue == 1_800_000_000,
              "launched_at is epoch seconds")
        check(stateRunningJSON?["user_quit"] as? Bool == false, "user_quit is false while running")
        check(String(decoding: stateRunning, as: UTF8.self).contains("\"pid\":1234")
              && String(decoding: stateRunning, as: UTF8.self).contains("\"user_quit\":false"),
              "pid and user_quit serialize as integer and boolean literals")

        let stateQuit = AppStateFile.encode(bundlePath: "/tmp/QuotaCanvas.app", pid: 99,
                                            launchedAt: stateLaunched, userQuit: true)
        let stateQuitJSON = try JSONSerialization.jsonObject(with: stateQuit) as? [String: Any]
        check(stateQuitJSON?["user_quit"] as? Bool == true, "user_quit is true on normal quit")
        check(AppStateFile.isUserInitiatedQuit(nil), "an in-app quit (no Apple Event) counts as the user's quit")
        let quitEvent = NSAppleEventDescriptor(eventClass: AEEventClass(kCoreEventClass),
                                               eventID: AEEventID(kAEQuitApplication),
                                               targetDescriptor: nil, returnID: AEReturnID(kAutoGenerateReturnID),
                                               transactionID: AETransactionID(kAnyTransactionID))
        check(!AppStateFile.isUserInitiatedQuit(quitEvent),
              "a quit Apple Event (logout, system or another process) is not the user's quit")
        check(String(decoding: stateQuit, as: UTF8.self).contains("\"user_quit\":true"),
              "quit state serializes user_quit as a true literal")

        // write() は .app 以外の bundleURL では何も書かず、.app では存在しない
        // ディレクトリごと app-state.json を作る（原子的書き込み）。
        let stateDir = dir.appendingPathComponent("app-state-test")
        defer { try? FileManager.default.removeItem(at: stateDir) }
        let stateFile = stateDir.appendingPathComponent(AppStateFile.fileName)
        AppStateFile.write(userQuit: false, launchedAt: stateLaunched,
                           bundleURL: URL(fileURLWithPath: "/usr/local/bin/quota-canvas"),
                           directory: stateDir, pid: 4321)
        check(!FileManager.default.fileExists(atPath: stateFile.path),
              "non-.app launch writes no state file")
        AppStateFile.write(userQuit: false, launchedAt: stateLaunched,
                           bundleURL: URL(fileURLWithPath: "/Applications/QuotaCanvas.app"),
                           directory: stateDir, pid: 4321)
        let writtenState = try JSONSerialization.jsonObject(with: Data(contentsOf: stateFile)) as? [String: Any]
        check(writtenState?["bundle_path"] as? String == "/Applications/QuotaCanvas.app"
              && writtenState?["pid"] as? Int == 4321
              && writtenState?["user_quit"] as? Bool == false,
              "state file holds the running app identity")

        // MuseCredentialCoordinator: 削除と取得（muse-ingest）の直列化。削除は実行中の
        // 取得の完了を待ってからエンジンを呼び、削除中は新しい取得が始まらない。
        // これで muse-ingest --clear の後に古い・新しい取得が着地して
        // muse-usage.json（利用枠と「検出」表示）が復活しない。
        let coordDir = dir.appendingPathComponent("muse-coordinator")
        try FileManager.default.createDirectory(at: coordDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: coordDir) }
        let coordScript = coordDir.appendingPathComponent("engine")
        // 呼ばれた引数を QC_COORD_LOG に追記し、--delete（set-key muse --delete）は
        // release ファイルができるまで返らない（削除の進行を止めて状態を見る）。
        let coordPython = #"""
        #!/usr/bin/python3
        import os, sys, time
        with open(os.environ["QC_COORD_LOG"], "a") as f:
            f.write(" ".join(sys.argv[1:]) + "\n")
        release = __file__ + ".release"
        if "--delete" in sys.argv:
            while not os.path.exists(release):
                time.sleep(0.01)
        """#
        try Data(coordPython.utf8).write(to: coordScript)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: coordScript.path)
        let coordClient = EngineClient(enginePathOverride: coordScript.path)
        let coordRelease = URL(fileURLWithPath: coordScript.path + ".release")
        let coordLog = coordDir.appendingPathComponent("calls.log").path
        _ = setenv("QC_COORD_LOG", coordLog, 1)

        let coord = MuseCredentialCoordinator()
        check(coord.beginPoll() == 0 && coord.polling, "a muse poll starts and reports its generation")
        let deleteTask = Task { try await coord.deleteCredential(client: coordClient, clearWebsiteData: {}) }
        try await Task.sleep(nanoseconds: 200_000_000)
        check(coord.deleting == false && !FileManager.default.fileExists(atPath: coordLog),
              "a muse delete waits for the in-flight poll before calling the engine")
        coord.endPoll()
        try Data().write(to: coordRelease) // set-key muse --delete の待ちを解く
        try await deleteTask.value
        let coordCalls = try String(contentsOfFile: coordLog, encoding: .utf8)
        check(coordCalls.contains("set-key muse --delete") && coordCalls.contains("muse-ingest --clear"),
              "a muse delete removes the key and muse-usage.json after the poll ends")
        check(coord.generation == 1 && !coord.isCurrent(0),
              "a muse delete advances the generation so an older poll no longer counts as current")
        check(!coord.deleting && coord.beginPoll() != nil, "a new muse poll can start after the delete finished")
        coord.endPoll()

        // 削除の実行中は新しい取得が始まらない（clear の後に取得が着地しないため）。
        try FileManager.default.removeItem(at: coordRelease)
        let coordLog2 = coordDir.appendingPathComponent("calls2.log").path
        _ = setenv("QC_COORD_LOG", coordLog2, 1)
        let coord2 = MuseCredentialCoordinator()
        let deleteTask2 = Task { try await coord2.deleteCredential(client: coordClient, clearWebsiteData: {}) }
        while !FileManager.default.fileExists(atPath: coordLog2) {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        check(coord2.deleting, "a muse delete marks itself as running while it works")
        check(coord2.beginPoll() == nil, "no muse poll can start while a delete is running")
        try Data().write(to: coordRelease)
        try await deleteTask2.value
        check(!coord2.deleting, "a muse delete clears the running mark when finished")

        // ServiceSettingsView.windowKeysForSave: Set の並びのまま config.toml に書かずに
        // 必ずソートする（Set の順で書くと保存ごとに順番が変わり、同じ内容の差分が付く）。
        check(ServiceSettingsView.windowKeysForSave(selected: ["win-b", "win-a"], all: ["win-a", "win-b", "win-c"])
              == ["win-a", "win-b"], "a partial window selection is written sorted")
        check(ServiceSettingsView.windowKeysForSave(selected: ["win-c", "win-a", "win-b"],
                                                    all: ["win-a", "win-b", "win-c"]) == [],
              "a full window selection is stored as the empty array contract")
        check(ServiceSettingsView.windowKeysForSave(selected: [], all: ["win-a"]) == [],
              "an empty window selection keeps the empty array contract")

        // ServiceSettingsView.saveAndResync: 保存に失敗したら、失敗した編集の下書きでは
        // なく実際の設定の読み直しを画面に使う（下書きを残すと、それが次の保存の
        // original になり、差分なしとみなされて二度と送られない）。
        let savedDoc = try ConfigDocument(data: Data(#"{"display":{"paused":true}}"#.utf8))
        let reloadedDoc = try ConfigDocument(data: Data(#"{"display":{"paused":false}}"#.utf8))
        var reloadCalls = 0
        let success = await ServiceSettingsView.saveAndResync(save: { savedDoc },
                                                              reload: { reloadCalls += 1; return reloadedDoc })
        check(success.doc?.paused == true && reloadCalls == 0 && success.failure == nil,
              "a successful config save keeps the saved doc without reloading")
        let failed = await ServiceSettingsView.saveAndResync(
            save: { throw EngineError(message: "save failed") },
            reload: { reloadCalls += 1; return reloadedDoc })
        check(failed.doc?.paused == false && reloadCalls == 1 && failed.failure != nil,
              "a failed config save resyncs the screen from the real config instead of the draft")
        let brokenResync = await ServiceSettingsView.saveAndResync(
            save: { throw EngineError(message: "save failed") },
            reload: { nil })
        check(brokenResync.doc == nil && brokenResync.failure != nil,
              "a failed resync reports no doc so the view keeps its current screen")

        // 指摘: 保存と getConfig()（読み直し）の両方が失敗したときは、基準値
        // （最後にエンジンで確認できた設定）と編集中の値をそのまま残す。
        // 次の保存は「基準値 → 編集中の値」の差分なので、失敗した変更も含まれて
        // 送られる（読み直しが成功したときは従来どおり実設定に一致させる）。
        let draftDoc = try ConfigDocument(data: Data(#"{"display":{"paused":true,"brightness":75}}"#.utf8))
        let afterSaved: (doc: ConfigDocument?, failure: Error?) = (doc: savedDoc, failure: nil)
        let savedMerge = ServiceSettingsView.nextDocumentsAfterSave(
            edited: draftDoc, baseline: draftDoc, outcome: afterSaved)
        check(savedMerge.doc.paused && savedMerge.synced.paused,
              "a successful save moves the screen and the baseline to the saved config")
        let afterResynced: (doc: ConfigDocument?, failure: Error?) =
            (doc: reloadedDoc, failure: EngineError(message: "save failed"))
        let resyncedMerge = ServiceSettingsView.nextDocumentsAfterSave(
            edited: draftDoc, baseline: draftDoc, outcome: afterResynced)
        check(resyncedMerge.doc.paused == false && resyncedMerge.synced.paused == false,
              "a failed save with a working reload shows the real config instead of the draft")
        let afterBothFailed: (doc: ConfigDocument?, failure: Error?) =
            (doc: nil, failure: EngineError(message: "save failed"))
        let bothFailedMerge = ServiceSettingsView.nextDocumentsAfterSave(
            edited: draftDoc, baseline: reloadedDoc, outcome: afterBothFailed)
        check(bothFailedMerge.doc.paused && bothFailedMerge.doc.brightness == 75,
              "a save and reload both failing keeps the unsaved edit on screen")
        check(!bothFailedMerge.synced.paused,
              "a save and reload both failing keeps the baseline for the next diff")

        // 実際の CLI でも確かめる: 保存と読み直しの両方が失敗したあと、復旧して
        // 別の項目を含む保存をすると、基準値からの差分に失敗した変更が入って届く。
        let resyncDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: resyncDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: resyncDir) }
        try Data("[display]\npaused = false\nbrightness = 50\n".utf8)
            .write(to: resyncDir.appendingPathComponent("config.toml"))
        let resyncEngine = resyncDir.appendingPathComponent("engine")
        // 最初の 1 回だけ config --apply-json を失敗させる（エンジン一時停止の想定）。
        let applyOnceFails = #"""
        #!/usr/bin/python3
        import os, sys
        from pathlib import Path
        repo = os.environ["QUOTA_CANVAS_TEST_REPO"]
        executable = os.path.join(repo, ".venv", "bin", "python")
        config = os.path.join(os.path.dirname(__file__), "config.toml")
        if "--apply-json" in sys.argv and not Path(__file__ + ".applied").exists():
            Path(__file__ + ".applied").write_text("1")
            print('{"error":"engine is down"}')
            sys.exit(1)
        os.execv(executable, [executable, "-m", "quotacanvas.cli", "--config", config] + sys.argv[1:])
        """#
        try Data(applyOnceFails.utf8).write(to: resyncEngine)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: resyncEngine.path)
        let resyncClient = EngineClient(enginePathOverride: resyncEngine.path)
        let baselineDoc = try await resyncClient.getConfig()
        check(!baselineDoc.paused && baselineDoc.brightness == 50,
              "the resync fixture starts from the written config")
        var editedBaseline = baselineDoc
        editedBaseline.paused = true
        editedBaseline.brightness = 75
        let brokenOutcome = await ServiceSettingsView.saveAndResync(
            save: { try await resyncClient.saveChanges(from: baselineDoc, to: editedBaseline) },
            reload: { nil }) // 読み直しも失敗した状態を再現する
        let recovered = ServiceSettingsView.nextDocumentsAfterSave(
            edited: editedBaseline, baseline: baselineDoc, outcome: brokenOutcome)
        check(brokenOutcome.failure != nil && recovered.doc.paused && !recovered.synced.paused,
              "the both-failure state keeps the draft on screen and the baseline separate")
        // 復旧（2 回目の --apply-json は通る）。次の保存は基準値からの差分なので、
        // いったん失敗した変更がエンジンに届く。
        let recoveredSaved = try await resyncClient.saveChanges(from: recovered.synced, to: recovered.doc)
        check(recoveredSaved.paused && recoveredSaved.brightness == 75,
              "the next save after recovery sends the failed edit in the baseline diff")
        let recoveredConfig = try await resyncClient.getConfig()
        check(recoveredConfig.paused && recoveredConfig.brightness == 75,
              "the recovered engine config really contains the failed edit")

        // 指摘: 振り分けタブの読み直しは、それより新しい操作の値を上書きしない。
        // 項目ごとの編集の世代（editGeneration）と、保存がまだ着地していない
        // 項目（pendingFields）で反映の可否を決める。
        check(RoutingSettingsView.shouldApplyReload(editedAt: nil, reloadGeneration: 3, pending: false),
              "an item never edited follows the reloaded config")
        check(RoutingSettingsView.shouldApplyReload(editedAt: 2, reloadGeneration: 3, pending: false),
              "a failed edit older than the reload is reverted to the real config")
        check(!RoutingSettingsView.shouldApplyReload(editedAt: 4, reloadGeneration: 3, pending: false),
              "an edit made after the reload started survives it")
        check(!RoutingSettingsView.shouldApplyReload(editedAt: 2, reloadGeneration: 3, pending: true),
              "an item whose save has not landed is not overwritten by the reload")
        // レビューの筋書き: 保存 A の失敗後に読み直しが走り、その完了前に保存 B が
        // 成功しても、B の表示は読み直しで古い値に戻らない（A だけ実設定に戻す）。
        check(RoutingSettingsView.shouldApplyReload(editedAt: 1, reloadGeneration: 1, pending: false)
              && !RoutingSettingsView.shouldApplyReload(editedAt: 2, reloadGeneration: 1, pending: false),
              "a reload reverts the failed item but keeps the newer operation")

        // 指摘（手直しラウンド）: 上の判定を実呼び出し順でも検証する。
        // 保存 A 失敗 → 読み直し開始 → 保存 B → 読み直し完了 → 保存 B 完了。
        // 読み直しの世代は取得を始める前に控えるので、取得中に始まった保存 B は
        // 「読み直しより後」になり、古いスナップショットで上書きされない。
        // 判定は読み直し完了直後（runRoutingSave の返りを待った直後）の状態で行う。
        let orderTracker = RoutingSettingsView.RoutingSyncTracker()
        var orderReloadGenerations: [Int] = []
        let orderSaveAReturn = DeliveryGate() // 保存 A の応答を止める
        let orderFetch = DeliveryGate()       // 読み直しの取得（getConfig）を止める
        async let orderSaveA: Error? = RoutingSettingsView.runRoutingSave(
            tracker: orderTracker, field: .floor,
            save: { await orderSaveAReturn.deliver(); throw EngineError(message: "save A failed") },
            reload: { reloadGeneration in
                orderReloadGenerations.append(reloadGeneration)
                await orderFetch.deliver() // getConfig の返りを止める
            })
        await orderSaveAReturn.waitForStart()
        orderSaveAReturn.accept() // 保存 A の失敗が返り、読み直しが始まる
        await orderFetch.waitForStart()
        check(orderReloadGenerations == [2], // 保存 A の発行 1・着地 2 の後に読み直し
              "the reload records the generation before its fetch starts")
        let orderSaveBReturn = DeliveryGate() // 読み直しの取得中に始まる保存 B
        async let orderSaveB: Error? = RoutingSettingsView.runRoutingSave(
            tracker: orderTracker, field: .floor,
            save: { await orderSaveBReturn.deliver() },
            reload: { _ in }) // B は成功するので読み直しは走らない
        await orderSaveBReturn.waitForStart()
        orderFetch.accept() // 読み直しが完了する（保存 B はまだ着地していない）
        let orderAFailure = await orderSaveA
        check(!orderTracker.shouldApplyReload(.floor, reloadGeneration: orderReloadGenerations[0]),
              "a snapshot fetched before save B does not overwrite B's display")
        orderSaveBReturn.accept() // 保存 B が成功する
        let orderBFailure = await orderSaveB
        check(orderAFailure != nil && orderBFailure == nil
              && orderTracker.shouldApplyReload(.floor, reloadGeneration: 100),
              "save A reports its failure, save B lands and clears the pending flag")

        // 指摘（手直しラウンド）: 同じ項目に保存 2 回で古い方が先に返る。
        // 古い保存の完了で未着地（pending）は解除されず、その読み直しは
        // 新しい保存の表示を上書きしない。新しい保存が返って初めて解除される。
        let doubleTracker = RoutingSettingsView.RoutingSyncTracker()
        var doubleReloadGenerations: [Int] = []
        let doubleOldReturn = DeliveryGate()
        let doubleNewReturn = DeliveryGate()
        async let doubleOldSave: Error? = RoutingSettingsView.runRoutingSave(
            tracker: doubleTracker, field: .enabled,
            save: { await doubleOldReturn.deliver(); throw EngineError(message: "old save failed") },
            reload: { reloadGeneration in doubleReloadGenerations.append(reloadGeneration) })
        await doubleOldReturn.waitForStart() // 古い保存の発行（世代 1）
        async let doubleNewSave: Error? = RoutingSettingsView.runRoutingSave(
            tracker: doubleTracker, field: .enabled,
            save: { await doubleNewReturn.deliver() },
            reload: { _ in })
        await doubleNewReturn.waitForStart() // 新しい保存の発行（世代 2・どちらも未着地）
        doubleOldReturn.accept() // 古い保存が先に失敗して読み直しが走る
        _ = await doubleOldSave
        check(doubleReloadGenerations == [3] // 発行 1・2、古い保存の着地 3
              && !doubleTracker.shouldApplyReload(.enabled, reloadGeneration: doubleReloadGenerations[0]),
              "the older save's completion keeps the field pending until the newer save lands")
        doubleNewReturn.accept() // 新しい保存が成功する
        _ = await doubleNewSave
        check(doubleTracker.shouldApplyReload(.enabled, reloadGeneration: 100),
              "the pending flag clears only when the newest save lands")

        // 先に取った世代（画面を書き換えた同期の流れで beginSave）を渡した保存は、
        // Task の開始前から未着地で、世代を取り直さず、返ったら解除される。
        let earlyBeginTracker = RoutingSettingsView.RoutingSyncTracker()
        let earlyBeginGeneration = earlyBeginTracker.beginSave(field: .model("a"))
        check(!earlyBeginTracker.shouldApplyReload(.model("a"), reloadGeneration: 100),
              "a save begun before its task starts already blocks a stale reload")
        let earlyBeginFailure = await RoutingSettingsView.runRoutingSave(
            tracker: earlyBeginTracker, field: .model("a"), saveGeneration: earlyBeginGeneration,
            save: {}, reload: { _ in })
        check(earlyBeginFailure == nil && earlyBeginTracker.generation == earlyBeginGeneration + 1
              && earlyBeginTracker.pendingSave[.model("a")] == nil,
              "a pre-begun save does not take a second generation and clears pending when it lands")

        // 指摘（再確認ラウンド）: 読み直しの開始前から保存中だった項目も守る。
        // 保存 B（floor）開始 → 保存 A（stale）失敗 → 読み直しが B の保存前の値を読む
        // → B 成功 → 読み直し完了。B の着地も世代を進めるので、着地前に始まった
        // 読み直しは floor の表示を古い値に戻さない（A の stale だけ実設定に戻す）。
        let earlyTracker = RoutingSettingsView.RoutingSyncTracker()
        var earlyReloadGenerations: [Int] = []
        let earlySaveBReturn = DeliveryGate()
        let earlyFetch = DeliveryGate()
        async let earlySaveB: Error? = RoutingSettingsView.runRoutingSave(
            tracker: earlyTracker, field: .floor,
            save: { await earlySaveBReturn.deliver() },
            reload: { _ in })
        await earlySaveBReturn.waitForStart() // B は保存中（未着地）
        async let earlySaveA: Error? = RoutingSettingsView.runRoutingSave(
            tracker: earlyTracker, field: .stale,
            save: { throw EngineError(message: "save A failed") },
            reload: { reloadGeneration in
                earlyReloadGenerations.append(reloadGeneration)
                await earlyFetch.deliver() // B の保存前の値を読んでいる最中
            })
        await earlyFetch.waitForStart()
        earlySaveBReturn.accept() // 読み直しの取得中に B が成功して着地する
        _ = await earlySaveB
        earlyFetch.accept() // 読み直しが完了する
        _ = await earlySaveA
        check(earlyReloadGenerations.count == 1
              && !earlyTracker.shouldApplyReload(.floor, reloadGeneration: earlyReloadGenerations[0])
              && earlyTracker.shouldApplyReload(.stale, reloadGeneration: earlyReloadGenerations[0]),
              "a reload started before save B landed keeps B's display and reverts only A")

        // 指摘（手直しラウンド）: 表示タブは保存失敗後の読み直しで doc / synced だけ
        // でなく操作部品の @State（brightness など）も確認済みの設定に揃える。
        var confirmedDisplay = ConfigDocument(object: [:])
        confirmedDisplay.gauge = "used"
        confirmedDisplay.brightness = 40
        confirmedDisplay.percentColors = false
        confirmedDisplay.separator = " / "
        confirmedDisplay.weekStart = "monday"
        confirmedDisplay.language = "en"
        confirmedDisplay.sleepWithMac = false
        confirmedDisplay.night = ConfigDocument.Night(start: "21:30", end: "06:15", brightness: 15)
        let confirmedControls = DisplaySettingsView.controlState(confirmed: confirmedDisplay)
        check(confirmedControls.gaugeShowsUsed && confirmedControls.brightness == 40
              && !confirmedControls.percentColors && confirmedControls.separator == " / "
              && confirmedControls.weekStart == "monday" && confirmedControls.language == "en"
              && !confirmedControls.sleepWithMac && confirmedControls.nightOn
              && confirmedControls.nightBrightness == 15,
              "the control state mirrors the confirmed config after a reload")
        check(confirmedControls.nightStart == DisplaySettingsView.parseHHMM("21:30")
              && confirmedControls.nightEnd == DisplaySettingsView.parseHHMM("06:15"),
              "night hours parse into the control state dates")
        var noNightDisplay = ConfigDocument(object: [:])
        noNightDisplay.brightness = 80
        noNightDisplay.night = nil
        let noNightControls = DisplaySettingsView.controlState(confirmed: noNightDisplay)
        check(noNightControls.brightness == 80 && !noNightControls.nightOn
              && noNightControls.nightBrightness == 20
              && noNightControls.nightStart == DisplaySettingsView.DisplayControlState.defaultNightStart
              && noNightControls.nightEnd == DisplaySettingsView.DisplayControlState.defaultNightEnd,
              "a config without night resets the night controls to their defaults")

        // 指摘: 配置タブ（LayoutSettingsView）も同じ synced 方針。保存は
        // 「基準値 → 編集中の値」の差分。controlState(confirmed:status:) が
        // config → 表示値の変換（restoreLayout）を包んで初回読み込みと、
        // 失敗後の読み直しで共有する。保存に成功したときの表示は
        // nextControlState が nil を返し、編集中の値のまま保つ。
        let layoutBaseline = try ConfigDocument(data: Data(#"{"layout":{"panels":[{"main":"alpha:week","size":"large","subs":["alpha:day"]}]}}"#.utf8))
        let layoutEdited = try ConfigDocument(data: Data(#"{"layout":{"panels":[{"main":"beta:day","size":"small"},{"main":"gamma:day","size":"small"}]}}"#.utf8))
        let layoutReal = try ConfigDocument(data: Data(#"{"layout":{"panels":[{"main":"delta:week","size":"large"}]}}"#.utf8))
        var layoutReloadCalls = 0

        // 成功: 画面（doc）も基準値（synced）も保存後の設定に揃い、読み直しは走らない。
        let layoutSavedOutcome = await ServiceSettingsView.saveAndResync(
            save: { layoutEdited }, reload: { layoutReloadCalls += 1; return layoutReal })
        let layoutSaved = ServiceSettingsView.nextDocumentsAfterSave(
            edited: layoutEdited, baseline: layoutBaseline, outcome: layoutSavedOutcome)
        check(layoutSavedOutcome.failure == nil && layoutReloadCalls == 0
              && layoutSaved.doc.layoutPanels.map(\.main) == ["beta:day", "gamma:day"]
              && layoutSaved.synced.layoutPanels.map(\.main) == ["beta:day", "gamma:day"],
              "a successful layout save moves the screen and the baseline to the saved panels")

        // 成功でも表示を組み直すと退行になる。まず空き列の 2 段 split:
        // panelsForSave は両段とも空の列を保存しないので保存後の config には
        // small が無く、組み直すと split が false に戻って小スロットを選ぶ前に
        // 分割が解除される。成功のときは nextControlState が nil で画面が保たれる。
        var emptySplitSlots = LayoutSettingsView.emptySlots()
        var emptySplitSplits = [Bool](repeating: false, count: LayoutSettingsView.columnCount)
        emptySplitSlots[LayoutSettingsView.slotIndex(0, 0)].main = "alpha:week"
        emptySplitSplits[2] = true // 空き列を 2 段に分けた直後
        let emptySplitSavedPanels = LayoutSettingsView.panelsForSave(slots: emptySplitSlots, splits: emptySplitSplits)
        check(emptySplitSavedPanels.map(\.main) == ["alpha:week"],
              "panelsForSave keeps the filled column and drops the empty split column")
        var emptySplitSavedDoc = layoutSaved.doc
        emptySplitSavedDoc.layoutPanels = emptySplitSavedPanels
        let emptySplitControls = LayoutSettingsView.nextControlState(
            previous: LayoutSettingsView.LayoutControlState(slots: emptySplitSlots, splits: emptySplitSplits),
            outcome: (doc: emptySplitSavedDoc, failure: nil), status: [])
        check(emptySplitControls == nil,
              "a successful save keeps the editing screen so the empty split column survives")
        check(emptySplitSplits[2] == true,
              "the empty column stays split while the user picks its small slots")

        // 同じく成功の退行: パネルを入れ替えた直後は status の位置照合（同じ配列
        // 位置の main・size）が崩れるので、組み直すと解決済み sub が落ちて自動
        // sub がすべて未選択になる。nil で保たれる。status はまだ入れ替え前の
        // 並び（前回の描画）のまま。
        let swappedStatus = try JSONDecoder().decode([LayoutPanel].self, from: Data((
            #"[{"main":"alpha:week","size":"large","subs":["alpha:resolved"],"main_window":"alpha:week"},"# +
            #"{"main":"epsilon:week","size":"large","subs":["epsilon:resolved"],"main_window":"epsilon:week"}]"#).utf8))
        let swappedPrevious = LayoutSettingsView.controlState(
            confirmed: try ConfigDocument(data: Data(
                #"{"layout":{"panels":[{"main":"alpha:week","size":"large"},{"main":"epsilon:week","size":"large"}]}}"#.utf8)),
            status: swappedStatus)
        check(swappedPrevious.resolvedSubsByMain["alpha:week"] == ["alpha:resolved"]
              && swappedPrevious.resolvedSubsByMain["epsilon:week"] == ["epsilon:resolved"]
              && swappedPrevious.resolvedMainWindows["epsilon:week"] == "epsilon:week",
              "the previous control state holds the resolved subs before the swap")
        var swappedSavedDoc = layoutSaved.doc
        swappedSavedDoc.layoutPanels = [
            ConfigDocument.PanelConfig(main: "epsilon:week", subs: ["epsilon:day"], size: "large"),
            ConfigDocument.PanelConfig(main: "alpha:week", subs: ["alpha:day"], size: "large")]
        let swappedControls = LayoutSettingsView.nextControlState(
            previous: swappedPrevious,
            outcome: (doc: swappedSavedDoc, failure: nil), status: swappedStatus)
        check(swappedControls == nil,
              "a successful save keeps the resolved subs after swapping panels")
        let swappedRebuilt = LayoutSettingsView.controlState(confirmed: swappedSavedDoc, status: swappedStatus)
        check(swappedRebuilt.resolvedSubsByMain.isEmpty && swappedRebuilt.resolvedMainWindows.isEmpty,
              "rebuilding from the stale status after a swap would drop the resolved subs")

        // 失敗して読み直し成功: 失敗した下書き（split への変更）は画面・基準値から
        // 消えて実際の設定（delta:week）に揃う。表示用 @State も実際の配置に戻る。
        let layoutResyncedOutcome = await ServiceSettingsView.saveAndResync(
            save: { throw EngineError(message: "layout save failed") },
            reload: { layoutReloadCalls += 1; return layoutReal })
        let layoutResynced = ServiceSettingsView.nextDocumentsAfterSave(
            edited: layoutEdited, baseline: layoutBaseline, outcome: layoutResyncedOutcome)
        check(layoutResyncedOutcome.failure != nil
              && layoutResynced.doc.layoutPanels.map(\.main) == ["delta:week"]
              && layoutResynced.synced.layoutPanels.map(\.main) == ["delta:week"],
              "a failed layout save with a working reload shows the real panels")
        let resyncedStatus = try JSONDecoder().decode([LayoutPanel].self,
            from: Data(#"[{"main":"delta:week","size":"large","subs":["delta:resolved"]}]"#.utf8))
        let resyncedLayoutControls = LayoutSettingsView.nextControlState(
            previous: LayoutSettingsView.LayoutControlState(slots: emptySplitSlots, splits: emptySplitSplits),
            outcome: layoutResyncedOutcome, status: resyncedStatus)
        check(resyncedLayoutControls != nil && !resyncedLayoutControls!.splits[0] && !resyncedLayoutControls!.splits[2]
              && resyncedLayoutControls!.slots[LayoutSettingsView.slotIndex(0, 0)].main == "delta:week"
              && resyncedLayoutControls!.resolvedSubsByMain["delta:week"] == ["delta:resolved"],
              "a working reload after a failed save rebuilds the controls from the real panels")

        // 読み直し後も status は保存前の並びのままなので、並びが変わったパネルの
        // 解決済み sub は位置照合で落ちる。実際の設定にまだいる main は
        // 画面に残っていた値を引き継ぎ、設定から消えた main は捨てる。
        let swappedResyncControls = LayoutSettingsView.nextControlState(
            previous: swappedPrevious,
            outcome: (doc: swappedSavedDoc, failure: EngineError(message: "layout save failed")),
            status: swappedStatus)
        check(swappedResyncControls?.splits[0] == false
              && swappedResyncControls?.slots[LayoutSettingsView.slotIndex(0, 0)].main == "epsilon:week"
              && swappedResyncControls?.slots[LayoutSettingsView.slotIndex(1, 0)].main == "alpha:week",
              "a reload after a failed save still rebuilds the slots from the real panels")
        check(swappedResyncControls?.resolvedSubsByMain["alpha:week"] == ["alpha:resolved"]
              && swappedResyncControls?.resolvedSubsByMain["epsilon:week"] == ["epsilon:resolved"]
              && swappedResyncControls?.resolvedMainWindows["alpha:week"] == "alpha:week",
              "a reload after a failed save carries over the resolved subs of reordered panels")
        let droppedResyncControls = LayoutSettingsView.nextControlState(
            previous: swappedPrevious,
            outcome: (doc: layoutReal, failure: EngineError(message: "layout save failed")),
            status: [])
        check(droppedResyncControls?.resolvedSubsByMain.isEmpty == true,
              "resolved subs whose main left the confirmed layout are not carried over")

        // 保存も読み直しも失敗: 下書き（編集中の値）と基準値をそのまま残し、
        // 画面は下書きのまま。次の保存の差分（基準値 → 下書き）に失敗した変更が入る。
        let layoutBothFailedOutcome = await ServiceSettingsView.saveAndResync(
            save: { throw EngineError(message: "layout save failed") },
            reload: { nil })
        check(layoutBothFailedOutcome.doc == nil && layoutBothFailedOutcome.failure != nil,
              "a failed layout resync reports no doc so the view keeps its screen")
        let layoutBothFailed = ServiceSettingsView.nextDocumentsAfterSave(
            edited: layoutEdited, baseline: layoutBaseline, outcome: layoutBothFailedOutcome)
        check(layoutBothFailed.doc.layoutPanels.map(\.main) == ["beta:day", "gamma:day"]
              && layoutBothFailed.synced.layoutPanels.map(\.main) == ["alpha:week"],
              "a layout save and reload both failing keep the draft and the baseline apart")
        let bothFailedControls = LayoutSettingsView.nextControlState(
            previous: LayoutSettingsView.LayoutControlState(slots: emptySplitSlots, splits: emptySplitSplits),
            outcome: layoutBothFailedOutcome, status: [])
        check(bothFailedControls == nil && emptySplitSplits[2] == true,
              "the failed layout draft stays on screen while the engine is unreachable")

        // 実際の CLI でも確かめる: 配置の保存と読み直しが両方失敗したあと復旧して
        // 次の保存をすると、基準値からの差分に失敗した配置が入って届く。
        let layoutResyncDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: layoutResyncDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: layoutResyncDir) }
        try Data("[display]\npaused = false\n".utf8)
            .write(to: layoutResyncDir.appendingPathComponent("config.toml"))
        let layoutEngine = layoutResyncDir.appendingPathComponent("engine")
        try Data(applyOnceFails.utf8).write(to: layoutEngine) // 最初の 1 回の --apply-json を失敗させる
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: layoutEngine.path)
        let layoutClient = EngineClient(enginePathOverride: layoutEngine.path)
        let layoutStart = try await layoutClient.getConfig()
        check(layoutStart.layoutPanels.isEmpty, "the layout fixture starts with auto placement")
        var layoutDraft = layoutStart
        layoutDraft.layoutPanels = [ConfigDocument.PanelConfig(main: "alpha:week", subs: ["alpha:day"], size: "large")]
        let layoutBrokenOutcome = await ServiceSettingsView.saveAndResync(
            save: { try await layoutClient.saveChanges(from: layoutStart, to: layoutDraft) },
            reload: { nil })
        let layoutRecovered = ServiceSettingsView.nextDocumentsAfterSave(
            edited: layoutDraft, baseline: layoutStart, outcome: layoutBrokenOutcome)
        check(layoutBrokenOutcome.failure != nil
              && layoutRecovered.doc.layoutPanels.map(\.main) == ["alpha:week"]
              && layoutRecovered.synced.layoutPanels.isEmpty,
              "the both-failure state keeps the layout draft and the baseline separate")
        let layoutRecoveredSaved = try await layoutClient.saveChanges(from: layoutRecovered.synced, to: layoutRecovered.doc)
        check(layoutRecoveredSaved.layoutPanels.map(\.main) == ["alpha:week"]
              && layoutRecoveredSaved.layoutPanels[0].subs == ["alpha:day"],
              "the next layout save after recovery sends the failed placement in the baseline diff")
        let layoutRecoveredConfig = try await layoutClient.getConfig()
        check(layoutRecoveredConfig.layoutPanels.map(\.main) == ["alpha:week"],
              "the recovered engine config really contains the failed placement")

        // MARK: swift-engine 担当分の追加テスト（指摘 1〜5）

        // 指摘 4: 通知計画は内容が変わったとき、保留 work の再試行、もしくは
        // 低頻度の定期実行（planningRetryInterval）のときだけ起こす。
        let planCurrent = Data(#"{"cards":[]}"#.utf8)
        check(StatusStore.shouldPlanNotifications(previous: nil, current: planCurrent, lastPlanAt: nil,
                                                  hasPendingWork: false, now: now),
              "the first status read plans notifications")
        check(!StatusStore.shouldPlanNotifications(previous: planCurrent, current: planCurrent,
                                                   lastPlanAt: now, hasPendingWork: false, now: now.addingTimeInterval(10)),
              "an unchanged read inside the interval does not replan")
        check(StatusStore.shouldPlanNotifications(previous: Data(#"{"cards":[{"id":"x"}]}"#.utf8), current: planCurrent,
                                                  lastPlanAt: now, hasPendingWork: false, now: now.addingTimeInterval(10)),
              "a changed read plans notifications")
        check(StatusStore.shouldPlanNotifications(previous: planCurrent, current: planCurrent,
                                                  lastPlanAt: now, hasPendingWork: true, now: now.addingTimeInterval(10)),
              "pending planning or delivery work retries on the next read")
        check(!StatusStore.shouldPlanNotifications(previous: planCurrent, current: planCurrent,
                                                   lastPlanAt: now.addingTimeInterval(-StatusStore.planningRetryInterval + 1),
                                                   hasPendingWork: false, now: now),
              "just before the interval is not due yet")
        check(StatusStore.shouldPlanNotifications(previous: planCurrent, current: planCurrent,
                                                  lastPlanAt: now.addingTimeInterval(-StatusStore.planningRetryInterval),
                                                  hasPendingWork: false, now: now),
              "the periodic replan runs after the interval")

        // 指摘 2: .app バンドルの外では LaunchAgent を一切調整しない。
        // bundledEnginePath が無い（swift run など）ことを無効化と誤認して
        // 常駐エンジンを bootout しないようにする。
        let agentBundle = "/tmp/QuotaCanvasTest.app/Contents/Resources/engine/bin/quota-canvas"
        check(AppEngineAgent.agentAction(enabled: true, bundledEngine: agentBundle, selectedEngine: agentBundle,
                                         hasConflict: false, screen: true, inAppBundle: false) == .none,
              "a non-.app launch never touches the LaunchAgents")
        check(AppEngineAgent.agentAction(enabled: true, bundledEngine: agentBundle, selectedEngine: agentBundle,
                                         hasConflict: false, screen: true, inAppBundle: true) == .run(screen: true),
              "a bundled engine with screen enabled installs the screen agent")
        check(AppEngineAgent.agentAction(enabled: true, bundledEngine: agentBundle, selectedEngine: agentBundle,
                                         hasConflict: false, screen: false, inAppBundle: true) == .run(screen: false),
              "a bundled engine without screen installs the headless agent")
        check(AppEngineAgent.agentAction(enabled: true, bundledEngine: nil, selectedEngine: agentBundle,
                                         hasConflict: false, screen: true, inAppBundle: true) == .removeAll,
              "no bundled engine removes the agents")
        check(AppEngineAgent.agentAction(enabled: true, bundledEngine: agentBundle,
                                         selectedEngine: "~/dev/quota-canvas/.venv/bin/quota-canvas",
                                         hasConflict: false, screen: true, inAppBundle: true) == .removeAll,
              "a selected engine other than the bundle removes the agents")
        check(AppEngineAgent.agentAction(enabled: true, bundledEngine: agentBundle, selectedEngine: agentBundle,
                                         hasConflict: true, screen: true, inAppBundle: true) == .removeAll,
              "a legacy-label conflict removes the agents")
        check(AppEngineAgent.agentAction(enabled: false, bundledEngine: agentBundle, selectedEngine: agentBundle,
                                         hasConflict: false, screen: true, inAppBundle: true) == .removeAll,
              "a disabled bundled engine removes the agents")

        // 指摘 1: コマンドごとの既定タイムアウト。読み取りは短く、保存は長め、
        // detect（ネットワーク）はさらに長め。
        check(EngineClient.defaultTimeout(for: ["config", "--json"]) == EngineClient.readTimeout,
              "config read uses the short default timeout")
        check(EngineClient.defaultTimeout(for: ["config", "--apply-json"]) == EngineClient.saveTimeout,
              "config save uses the longer default timeout")
        check(EngineClient.defaultTimeout(for: ["config", "--set-json"]) == EngineClient.saveTimeout,
              "config set-json counts as a save")
        check(EngineClient.defaultTimeout(for: ["notifications-plan"]) == EngineClient.planningTimeout,
              "notifications-plan uses the planning timeout")
        check(EngineClient.defaultTimeout(for: ["detect", "--json"]) == EngineClient.detectTimeout,
              "detect uses the network timeout")
        check(EngineClient.defaultTimeout(for: ["detect", "--json", "--cached"]) == EngineClient.detectTimeout,
              "detect --cached keeps the network timeout (cache miss refetches)")
        check(EngineClient.defaultTimeout(for: ["muse-action-id"]) == EngineClient.museTimeout
              && EngineClient.defaultTimeout(for: ["muse-ingest", "--status", "200"]) == EngineClient.museTimeout,
              "muse commands use the muse timeout")
        check(EngineClient.defaultTimeout(for: ["refresh", "--json"]) == EngineClient.readTimeout
              && EngineClient.defaultTimeout(for: ["set-key", "demo"]) == EngineClient.readTimeout,
              "other commands use the read timeout")
        check(EngineClient.readTimeout < EngineClient.saveTimeout && EngineClient.saveTimeout < EngineClient.detectTimeout,
              "read, save and detect timeouts are ordered short to long")

        // 指摘 1: SIGTERM を無視する子プロセスは猶予後に SIGKILL され、continuation が
        // 必ず一度だけ完了する（明示 timeout と既定 timeout の両方）。
        let savedGrace = EngineClient.killGrace
        let savedReadTimeout = EngineClient.readTimeout
        defer {
            EngineClient.killGrace = savedGrace
            EngineClient.readTimeout = savedReadTimeout
        }
        let ignoreTermCode = """
        import signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(30)
        """
        EngineClient.killGrace = 0.3
        let timeoutStartedAt = Date()
        var explicitTimeoutError: String?
        do {
            _ = try await EngineClient(enginePathOverride: "/usr/bin/python3")
                .run(["-c", ignoreTermCode], timeout: 0.5)
        } catch {
            explicitTimeoutError = error.localizedDescription
        }
        let explicitTimeoutElapsed = Date().timeIntervalSince(timeoutStartedAt)
        check(explicitTimeoutError?.contains("打ち切りました") == true,
              "an explicit timeout throws the cutoff error")
        check(explicitTimeoutElapsed >= 0.5, "the explicit timeout waits for the deadline")
        check(explicitTimeoutElapsed < 20, "the SIGKILL fallback unblocks waitUntilExit")
        EngineClient.readTimeout = 0.4
        var defaultTimeoutError: String?
        do {
            _ = try await EngineClient(enginePathOverride: "/usr/bin/python3")
                .run(["-c", ignoreTermCode])
        } catch {
            defaultTimeoutError = error.localizedDescription
        }
        check(defaultTimeoutError?.contains("打ち切りました") == true,
              "run applies the default timeout when none is passed")

        // 指摘 1 (2 回目): 孫プロセスが stdout / stderr を握ったまま残っても、
        // 子の終了後 readGrace で読み取りを打ち切り、continuation が完了する。
        // （直し前は readDataToEndOfFile が EOF を待って止まり、run が戻らない）
        let savedReadGrace = EngineClient.readGrace
        let savedKillGrace = EngineClient.killGrace
        defer {
            EngineClient.readGrace = savedReadGrace
            EngineClient.killGrace = savedKillGrace
        }
        EngineClient.readGrace = 0.6
        EngineClient.killGrace = 0.3
        let gcDir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: gcDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: gcDir) }
        let grandchildScript = gcDir.appendingPathComponent("grandchild-engine")
        try Data(#"""
        #!/bin/sh
        sh -c 'sleep 30' &
        echo ok
        exit 0
        """#.utf8).write(to: grandchildScript)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: grandchildScript.path)
        let grandchildStartedAt = Date()
        let grandchildOutput = try await EngineClient(enginePathOverride: grandchildScript.path)
            .run(["ignored"], timeout: 5)
        let grandchildElapsed = Date().timeIntervalSince(grandchildStartedAt)
        check(String(data: grandchildOutput, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) == "ok",
              "output is returned even when a grandchild keeps the pipe open")
        check(grandchildElapsed < 20, "reading is cut off after the child exits instead of waiting for the grandchild")

        // 期限では孫を含むプロセスグループごと止まる。SIGTERM を無視する親の下で
        // 動く孫（SIGTERM では死ぬが親は死なない）も、最後のグループ SIGKILL と
        // 親の死亡で書き込みが止まる。孫のログの増加が止まることで確認する。
        let gcLog = gcDir.appendingPathComponent("grandchild.log")
        let groupScript = gcDir.appendingPathComponent("group-engine")
        try Data(#"""
        #!/usr/bin/python3
        import signal, subprocess, sys, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        subprocess.Popen(["/bin/sh", "-c",
            "i=0; while :; do echo $i >> \"$1\"; i=$((i+1)); sleep 0.05; done", "sh", sys.argv[1]])
        time.sleep(30)
        """#.utf8).write(to: groupScript)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: groupScript.path)
        var groupTimeoutError: String?
        let groupStartedAt = Date()
        do {
            _ = try await EngineClient(enginePathOverride: groupScript.path)
                .run([gcLog.path], timeout: 0.5)
        } catch {
            groupTimeoutError = error.localizedDescription
        }
        let groupElapsed = Date().timeIntervalSince(groupStartedAt)
        check(groupTimeoutError?.contains("打ち切りました") == true,
              "the timeout still reports the cutoff error when a grandchild is involved")
        check(groupElapsed >= 0.5 && groupElapsed < 20,
              "the group kill unblocks the run within the grace period")
        try await Task.sleep(nanoseconds: 300_000_000)
        let stoppedSize = (try? Data(contentsOf: gcLog))?.count ?? 0
        try await Task.sleep(nanoseconds: 500_000_000)
        let stoppedSizeAgain = (try? Data(contentsOf: gcLog))?.count ?? 0
        check(stoppedSize > 0 && stoppedSize == stoppedSizeAgain,
              "the deadline stops the whole process group including the grandchild")

        // 指摘 1〜3 (3 回目): fd の所有管理・stdin の期限付き nonblocking 書き込み・
        // SIGPIPE 抑制。
        let savedStdinGrace = EngineClient.stdinWriteGrace
        defer { EngineClient.stdinWriteGrace = savedStdinGrace }
        EngineClient.stdinWriteGrace = 0.6

        // 指摘 2: 子は stdin を読まず、読み口を握る孫（python の Popen は fd 0 を
        // 子に継承する）を残してすぐ終わる。stdin は 8MB で pipe バッファ（64KB）を
        // 超えるので、blocking な書き込みは永遠に戻らない。子の終了後の猶予で
        // 書き込みを諦めて run が戻ること（直し前は group.wait() が戻らず、
        // このテスト自体が永久に終わらなかった）。
        let stdinHolderScript = gcDir.appendingPathComponent("stdin-holder-engine")
        try Data(#"""
        #!/usr/bin/python3
        import subprocess, sys
        subprocess.Popen(["/bin/sleep", "30"])
        sys.exit(0)
        """#.utf8).write(to: stdinHolderScript)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: stdinHolderScript.path)
        let bigStdin = Data(repeating: 0x61, count: 8 * 1024 * 1024)
        let stdinStartedAt = Date()
        let stdinHolderOutput = try await EngineClient(enginePathOverride: stdinHolderScript.path)
            .run(["ignored"], stdin: bigStdin, timeout: 5)
        let stdinElapsed = Date().timeIntervalSince(stdinStartedAt)
        check(stdinElapsed < 20,
              "a large stdin write gives up when only a non-reading grandchild holds the pipe")
        check(stdinHolderOutput.isEmpty, "giving up the stdin write still completes the run")

        // 指摘 3: 子は stdin を一切読まずに終わる（孫なし）。読み口が全部閉じた
        // あとの write は SIGPIPE を出すが、F_SETNOSIGPIPE で EPIPE として受け、
        // このプロセス（テスト本体）が道連れに終了しないこと。stdin が pipe バッファ
        // を超えているので、書き込みが子の死亡後も必ず続く（EPIPE に必ず届く）。
        let exitScript = gcDir.appendingPathComponent("exit-engine")
        try Data(#"""
        #!/bin/sh
        exit 0
        """#.utf8).write(to: exitScript)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: exitScript.path)
        _ = try await EngineClient(enginePathOverride: exitScript.path)
            .run(["ignored"], stdin: bigStdin, timeout: 5)
        check(true, "stdin writes survive a child that exits without reading (no SIGPIPE death)")

        // 指摘 1: EOF を迎えた読み口は open から外れるだけで閉じられていなかった。
        // 通常終了・エラー終了・stdin あり・読み取り打ち切り・書き込み打ち切りの
        // どの経路でも、実行の前後で開いている fd の数が増えないこと。
        func openFdCount() -> Int {
            (try? FileManager.default.contentsOfDirectory(atPath: "/dev/fd"))?.count ?? -1
        }
        let pythonClient = EngineClient(enginePathOverride: "/usr/bin/python3")
        let grandchildClient = EngineClient(enginePathOverride: grandchildScript.path)
        let stdinHolderClient = EngineClient(enginePathOverride: stdinHolderScript.path)
        let exitClient = EngineClient(enginePathOverride: exitScript.path)
        // 最初の実行で遅延初期化が fd を増やしうるので、各経路を一度回してから測る
        _ = try? await pythonClient.run(["-c", "print(1)"])
        _ = try? await pythonClient.run(["-c", "import sys; sys.exit(3)"])
        _ = try? await pythonClient.run(["-c", "print(1)"], stdin: Data("x".utf8))
        _ = try? await grandchildClient.run(["ignored"], timeout: 5)
        _ = try? await stdinHolderClient.run(["ignored"], stdin: bigStdin, timeout: 5)
        _ = try? await exitClient.run(["ignored"], stdin: bigStdin, timeout: 5)
        try await Task.sleep(nanoseconds: 300_000_000)
        let baselineFdCount = openFdCount()
        for _ in 0..<2 {
            _ = try? await pythonClient.run(["-c", "print(2)"])                                // 通常終了
            _ = try? await pythonClient.run(["-c", "import sys; sys.exit(3)"])                  // エラー終了
            _ = try? await pythonClient.run(["-c", "print(3)"], stdin: Data("hello".utf8))      // 小さい stdin
            _ = try? await grandchildClient.run(["ignored"], timeout: 5)                        // 読み取り打ち切り
            _ = try? await stdinHolderClient.run(["ignored"], stdin: bigStdin, timeout: 5)      // 書き込み打ち切り
            _ = try? await exitClient.run(["ignored"], stdin: bigStdin, timeout: 5)             // EPIPE 打ち切り
        }
        try await Task.sleep(nanoseconds: 300_000_000)
        let afterFdCount = openFdCount()
        check(baselineFdCount > 0 && afterFdCount == baselineFdCount,
              "pipe fds are closed exactly once on every path (before \(baselineFdCount), after \(afterFdCount))")

        // 指摘 2 (2 回目): detect の既定タイムアウトは detect_all の最悪直列時間を
        // 覆えること。12 サービス直列: HTTP 15s×10 + gemini 30s + muse 20s +
        // opendesign 51s ≒ 251s。muse の既定は RESOLVE_BUDGET=60s の解決を覆えること。
        check(EngineClient.detectTimeout >= 251,
              "detect timeout covers the worst-case serial detect_all (~251s)")
        check(EngineClient.museTimeout >= 75,
              "muse timeout covers a full action-id resolve (RESOLVE_BUDGET 60s)")

        // 指摘 5: post に失敗した候補は undelivered に保持され、計画が同じキューを
        // 作り直せない読み取り（同じ status の再読み取り）で再送される。
        var lostKeys = Set<String>()
        var lostPosts: [String] = []
        var lostShouldFail = false
        var lostAuthorized = true
        let plainSpare = Data(#"{"cards":[{"id":"demo:weekly","provider":"demo","status":"spare"}]}"#.utf8)
        let plainOver = Data(#"{"cards":[{"id":"demo:weekly","provider":"demo","status":"over"}]}"#.utf8)
        let lostCoordinator = NotificationCoordinator(client: client, enabled: { true }, available: { true },
            readKeys: { lostKeys }, recordKeys: { lostKeys.formUnion($0) }, authorized: { lostAuthorized },
            post: { item in
                if lostShouldFail { throw EngineError(message: "post failed") }
                lostPosts.append(item.key)
            })
        lostShouldFail = true
        lostCoordinator.enqueue(previous: plainSpare, current: plainOver, now: now)
        await lostCoordinator.waitUntilIdle()
        check(lostCoordinator.error != nil && lostKeys.isEmpty && lostPosts.isEmpty,
              "a failed post keeps the candidate unrecorded")
        // 権限が無い間は配達せず、無効時と同じくキーを記録して未配達候補を捨てる。
        lostAuthorized = false
        lostCoordinator.enqueue(previous: plainOver, current: plainOver, now: now)
        await lostCoordinator.waitUntilIdle()
        check(lostKeys.count == 1 && lostPosts.isEmpty && lostCoordinator.error == nil,
              "unauthorized records the undelivered candidate without posting")
        lostAuthorized = true
        lostShouldFail = false
        // 古い遷移は記録済みなので、許可後も再送しない。
        lostCoordinator.enqueue(previous: plainOver, current: plainOver, now: now)
        await lostCoordinator.waitUntilIdle()
        check(lostKeys.count == 1 && lostPosts.isEmpty,
              "granting authorization does not replay the dropped candidate")
        // 別の失敗候補は、許可後の通常の読み取りで再送される。
        let plainExhausted = Data(#"{"cards":[{"id":"demo:weekly","provider":"demo","status":"exhausted"}]}"#.utf8)
        lostShouldFail = true
        lostCoordinator.enqueue(previous: plainOver, current: plainExhausted, now: now)
        await lostCoordinator.waitUntilIdle()
        check(lostCoordinator.error != nil && lostKeys.count == 1 && lostPosts.isEmpty,
              "a later failed post stays unrecorded")
        lostShouldFail = false
        lostCoordinator.enqueue(previous: plainExhausted, current: plainExhausted, now: now)
        await lostCoordinator.waitUntilIdle()
        check(lostKeys.count == 2 && lostPosts.count == 1 && lostCoordinator.error == nil,
              "the next normal read re-sends the undelivered candidate once")
        // 受理済みなので再送しない。
        lostCoordinator.enqueue(previous: plainExhausted, current: plainExhausted, now: now)
        await lostCoordinator.waitUntilIdle()
        check(lostKeys.count == 2 && lostPosts.count == 1, "an accepted candidate is not re-sent")

        // 権限なしで記録した遷移は、あとで許可しても通知されない。新しい遷移は通知される。
        var grantKeys = Set<String>()
        var grantPosts: [String] = []
        var grantAuthorized = false
        let grantSpare = Data(#"{"cards":[{"id":"demo:weekly","provider":"demo","status":"spare"}]}"#.utf8)
        let grantOver = Data(#"{"cards":[{"id":"demo:weekly","provider":"demo","status":"over"}]}"#.utf8)
        let grantExhausted = Data(#"{"cards":[{"id":"demo:weekly","provider":"demo","status":"exhausted"}]}"#.utf8)
        let grant = NotificationCoordinator(client: client, enabled: { true }, available: { true },
            readKeys: { grantKeys }, recordKeys: { grantKeys.formUnion($0) }, authorized: { grantAuthorized },
            post: { item in grantPosts.append(item.key) })
        grant.enqueue(previous: grantSpare, current: grantOver, now: now)
        await grant.waitUntilIdle()
        check(grantKeys.count == 1 && grantPosts.isEmpty,
              "unauthorized transition is recorded without posting")
        grantAuthorized = true
        grant.enqueue(previous: grantOver, current: grantOver, now: now)
        await grant.waitUntilIdle()
        check(grantKeys.count == 1 && grantPosts.isEmpty,
              "an old transition is not notified after authorization")
        grant.enqueue(previous: grantOver, current: grantExhausted, now: now)
        await grant.waitUntilIdle()
        check(grantKeys.count == 2 && grantPosts.count == 1,
              "a new transition after authorization is notified")
        check(grant.error == nil, "no delivery error after authorization")

        // レビュー指摘: 権限なしで新しい候補を記録するときも、残っている古い未配達の
        // 候補を捨てる（あとで許可しても古い遷移を出さない）。
        var mixKeys = Set<String>()
        var mixPosts: [String] = []
        var mixShouldFail = true
        var mixAuthorized = true
        let mix = NotificationCoordinator(client: client, enabled: { true }, available: { true },
            readKeys: { mixKeys }, recordKeys: { mixKeys.formUnion($0) }, authorized: { mixAuthorized },
            post: { item in
                if mixShouldFail { throw EngineError(message: "post failed") }
                mixPosts.append(item.key)
            })
        mix.enqueue(previous: plainSpare, current: plainOver, now: now) // 失敗して未配達に残る
        await mix.waitUntilIdle()
        mixAuthorized = false
        mix.enqueue(previous: plainOver, current: plainExhausted, now: now) // 権限なしで新しい候補
        await mix.waitUntilIdle()
        check(mixKeys.count == 2 && mixPosts.isEmpty && mix.error == nil,
              "unauthorized drops the old undelivered candidate along with the new one")
        mixAuthorized = true
        mixShouldFail = false
        mix.enqueue(previous: plainExhausted, current: plainExhausted, now: now)
        await mix.waitUntilIdle()
        check(mixPosts.isEmpty, "granting authorization replays neither candidate")

        // レビュー指摘: 通知が使えない（.app の外など）ときは authorized() を呼ばない
        // （UNUserNotificationCenter は .app の外で落ちる）。
        var probeCalls = 0
        var probeKeys = Set<String>()
        let probe = NotificationCoordinator(client: client, enabled: { true }, available: { false },
            readKeys: { probeKeys }, recordKeys: { probeKeys.formUnion($0) },
            authorized: { probeCalls += 1; return true },
            post: { _ in })
        probe.enqueue(previous: plainSpare, current: plainOver, now: now)
        await probe.waitUntilIdle()
        check(probeCalls == 0 && probeKeys.count == 1,
              "unavailable notifications record without asking for authorization")

        // last-window: 窓が 2 つ以上あるサービスでは、チェックが 1 つだけのとき
        // その最後の 1 つを外させない（空配列 = 全窓表示に戻り、「チェックした
        // 窓だけ表示」と食い違うため）。全部隠したいときはサービスをオフにする。
        let twoWindows: Set<String> = ["win-a", "win-b"]
        check(ServiceSettingsView.blocksUncheckingLastWindow(
            selected: ["win-a"], all: twoWindows),
              "the last checked window of a multi-window service cannot be unchecked")
        check(!ServiceSettingsView.blocksUncheckingLastWindow(
            selected: ["win-a", "win-b"], all: twoWindows),
              "unchecking is allowed while multiple windows are checked")
        check(!ServiceSettingsView.blocksUncheckingLastWindow(
            selected: ["win-a"], all: ["win-a"]),
              "a single-window service keeps its existing behavior (empty means all)")
        check(!ServiceSettingsView.blocksUncheckingLastWindow(
            selected: [], all: twoWindows),
              "an empty selection is not treated as the last window")

        check(!ServiceSettingsView.canUncheckWindow(
            "win-a", selected: ["win-a"], all: twoWindows),
              "the checkbox of the last checked window is disabled")
        check(ServiceSettingsView.canUncheckWindow(
            "win-a", selected: ["win-a", "win-b"], all: twoWindows),
              "a checked window can be unchecked while another stays checked")
        check(ServiceSettingsView.canUncheckWindow(
            "win-b", selected: ["win-a"], all: twoWindows),
              "an unchecked window can still be checked")
        check(ServiceSettingsView.canUncheckWindow(
            "win-a", selected: ["win-a"], all: ["win-a"]),
              "a single-window service can still clear its check as before")

        check(ServiceSettingsView.windowSelectionAfterEdit(
            current: ["win-a"], proposed: [], all: twoWindows) == ["win-a"],
              "removing the last checked window is ignored (defense behind disabled)")
        check(ServiceSettingsView.windowSelectionAfterEdit(
            current: ["win-a", "win-b"], proposed: ["win-b"], all: twoWindows) == ["win-b"],
              "a normal uncheck is kept")
        check(ServiceSettingsView.windowSelectionAfterEdit(
            current: ["win-a"], proposed: [], all: ["win-a"]).isEmpty,
              "a single-window service can still clear its check as before")

        check(ServiceSettingsView.effectiveWindowSelection(saved: [], all: twoWindows) == twoWindows,
              "an empty saved array still means all windows checked")
        check(ServiceSettingsView.effectiveWindowSelection(saved: ["win-b"], all: twoWindows) == ["win-b"],
              "a partial saved selection is shown as is")

        // MARK: subs-restore 担当分の追加テスト（split 切り替えでの明示サブの退避・復元）
        //
        // splitBinding・mainBinding・clearSlot・resetSubsToAuto・swapSlots・save の
        // 失敗経路が呼ぶ純粋関数（stashSubs / restoredSubs / discardStash /
        // revertedStash）を、実際の経路と同じ呼び出し順で確かめる。

        func srSlot(_ id: String, _ column: Int, _ size: String,
                    _ main: String?, _ subs: [String]?) -> LayoutSettingsView.LayoutSlot {
            LayoutSettingsView.LayoutSlot(id: id, column: column, size: size,
                                          title: id, main: main, subs: subs)
        }
        let srNoSplits = [Bool](repeating: false, count: LayoutSettingsView.columnCount)

        // 退避: 大スロットの明示 subs を「退避元の列 → 主枠の窓 ID」の組で覚える。
        // 自動（subs == nil）・主枠なし・小スロットは退避しない。同じ列・主枠は
        // 新しい指定で上書き。明示「サブなし」の [] も退避する。
        var srStash: [Int: [String: [String]]] = [:]
        srStash = LayoutSettingsView.stashSubs(
            for: srSlot("c0:large", 0, "large", "alpha:week", ["alpha:day"]), into: srStash)
        check(srStash == [0: ["alpha:week": ["alpha:day"]]],
              "explicit large subs are stashed by column and main window")
        srStash = LayoutSettingsView.stashSubs(
            for: srSlot("c1:large", 1, "large", "beta:day", nil), into: srStash)
        check(srStash.count == 1, "automatic subs stash nothing")
        srStash = LayoutSettingsView.stashSubs(
            for: srSlot("c2:large", 2, "large", nil, []), into: srStash)
        check(srStash.count == 1, "a large slot without a main stashes nothing")
        srStash = LayoutSettingsView.stashSubs(
            for: srSlot("c1:top", 1, "small", "beta:day", nil), into: srStash)
        check(srStash.count == 1, "small slots never stash")
        srStash = LayoutSettingsView.stashSubs(
            for: srSlot("c0:large", 0, "large", "alpha:week", ["alpha:5h"]), into: srStash)
        check(srStash == [0: ["alpha:week": ["alpha:5h"]]],
              "a new designation overwrites the stash for the same column and main")
        srStash = LayoutSettingsView.stashSubs(
            for: srSlot("c3:large", 3, "large", "delta:week", []), into: srStash)
        check(srStash[3]?["delta:week"] == [], "an explicit empty sub list is stashed too")

        // 復元: 自分の列に退避された明示 subs のうち同じ主枠のものだけ戻す。
        // 別の主枠・空の主枠は nil（自動のまま）。明示「サブなし」は [] のまま戻る。
        check(LayoutSettingsView.restoredSubs(
            forMain: "alpha:week", of: srSlot("c0:large", 0, "large", nil, nil), in: srStash) == ["alpha:5h"],
              "the same main in the same column restores the stashed subs")
        check(LayoutSettingsView.restoredSubs(
            forMain: "delta:week", of: srSlot("c3:large", 3, "large", nil, nil), in: srStash) == [],
              "an explicit empty stash restores as empty, not automatic")
        check(LayoutSettingsView.restoredSubs(
            forMain: "gamma:week", of: srSlot("c0:large", 0, "large", nil, nil), in: srStash) == nil,
              "a different main restores nothing")
        check(LayoutSettingsView.restoredSubs(
            forMain: nil, of: srSlot("c0:large", 0, "large", nil, nil), in: srStash) == nil
            && LayoutSettingsView.restoredSubs(
                forMain: "", of: srSlot("c0:large", 0, "large", nil, nil), in: srStash) == nil,
              "an empty main restores nothing")

        // 指摘 2: 退避は列で仕切る。同じ主枠 A を持つ 2 列を両方 2 段にしても、
        // 退避は列ごとに分かれて後から上書きされず、他方の列の指定で復元されない。
        // 片方の列が自動（退避なし）でも、他方の退避で明示指定にならない。
        var srTwoCols: [Int: [String: [String]]] = [:]
        srTwoCols = LayoutSettingsView.stashSubs(
            for: srSlot("c0:large", 0, "large", "alpha:week", ["alpha:day"]), into: srTwoCols)
        srTwoCols = LayoutSettingsView.stashSubs(
            for: srSlot("c1:large", 1, "large", "alpha:week", ["alpha:5h"]), into: srTwoCols)
        check(LayoutSettingsView.restoredSubs(
            forMain: "alpha:week", of: srSlot("c0:large", 0, "large", nil, nil), in: srTwoCols) == ["alpha:day"]
            && LayoutSettingsView.restoredSubs(
                forMain: "alpha:week", of: srSlot("c1:large", 1, "large", nil, nil), in: srTwoCols) == ["alpha:5h"],
              "two columns with the same main keep their own stashes")
        let srAutoCol = LayoutSettingsView.stashSubs(
            for: srSlot("c2:large", 2, "large", "alpha:week", nil), into: srTwoCols)
        check(LayoutSettingsView.restoredSubs(
            forMain: "alpha:week", of: srSlot("c2:large", 2, "large", nil, nil), in: srAutoCol) == nil,
              "an automatic column stays automatic when a sibling column stashed the same main")

        // 無効化: mainBinding・clearSlot・resetSubsToAuto・swapSlots が呼ぶ
        // discardStash はそのパネル（列。大・小どちらのスロットからでも同じ列）
        // の退避だけを消し、別の列は残す。
        var srDiscarded = LayoutSettingsView.discardStash(
            of: srSlot("c1:top", 1, "small", "alpha:week", nil), in: srStash)
        check(srDiscarded[1] == nil && srDiscarded[0]?["alpha:week"] == ["alpha:5h"]
              && srDiscarded[3]?["delta:week"] == [],
              "discarding via a small slot clears only its own column's stash")
        srDiscarded = LayoutSettingsView.discardStash(
            of: srSlot("c0:large", 0, "large", "alpha:week", nil), in: srDiscarded)
        check(srDiscarded[0] == nil && srDiscarded[3]?["delta:week"] == [],
              "discarding via the large slot clears the column's stash and keeps other columns")
        srDiscarded = LayoutSettingsView.discardStash(
            of: srSlot("c2:large", 2, "large", nil, nil), in: srDiscarded)
        check(srDiscarded == [3: ["delta:week": []]],
              "discarding a column without a stash leaves the others untouched")

        // split の往復（splitBinding の on → off と同じ呼び出し順）: 大 → 2 段で
        // 退避して消え、同じ主枠で 1 段に戻ると復元されて保存に載る。主枠が変わったら
        // 自動のまま（退避があっても主枠が違うと復元しない）。
        var srSlots = LayoutSettingsView.emptySlots()
        var srSplits = srNoSplits
        let srLargeIdx = LayoutSettingsView.slotIndex(0, 0)
        let srTopIdx = LayoutSettingsView.slotIndex(0, 1)
        srSlots[srLargeIdx].main = "alpha:week"
        srSlots[srLargeIdx].subs = ["alpha:day"]
        var srFlowStash: [Int: [String: [String]]] = [:]
        srFlowStash = LayoutSettingsView.stashSubs(for: srSlots[srLargeIdx], into: srFlowStash)
        srSlots[srTopIdx].main = srSlots[srLargeIdx].main
        srSlots[srLargeIdx].main = nil
        srSlots[srLargeIdx].subs = nil
        srSplits[0] = true
        check(LayoutSettingsView.panelsForSave(slots: srSlots, splits: srSplits).allSatisfy { $0.subs == nil },
              "the split column saves no subs while the main is small")
        srSlots[srTopIdx].main = "beta:day" // 2 段の間に Picker で主枠を変える
        let srChanged = srSlots[srTopIdx].main
        srSlots[srLargeIdx].main = srChanged
        srSlots[srLargeIdx].subs = LayoutSettingsView.restoredSubs(
            forMain: srChanged, of: srSlots[srLargeIdx], in: srFlowStash)
        srSlots[srTopIdx].main = nil
        srSplits[0] = false
        let srChangedSaved = LayoutSettingsView.panelsForSave(slots: srSlots, splits: srSplits)
        check(srChangedSaved.map(\.main) == ["beta:day"] && srChangedSaved[0].subs == nil,
              "a changed main comes back automatic without the stashed subs")

        // 指摘 1・経路「主枠の変更」: 2 段に分けた後の小スロットで Picker を
        // A → B → A と変えてから 1 段に戻しても、古い退避は復活しない。
        // mainBinding の set と同じ呼び出し順（discardStash → main/subs の更新）。
        srSlots = LayoutSettingsView.emptySlots()
        srSplits = srNoSplits
        srSlots[srLargeIdx].main = "alpha:week"
        srSlots[srLargeIdx].subs = ["alpha:day"]
        srFlowStash = LayoutSettingsView.stashSubs(for: srSlots[srLargeIdx], into: [:])
        srSlots[srTopIdx].main = srSlots[srLargeIdx].main
        srSlots[srLargeIdx].main = nil
        srSlots[srLargeIdx].subs = nil
        srSplits[0] = true
        for srPicked in ["beta:day", "alpha:week"] {
            srFlowStash = LayoutSettingsView.discardStash(of: srSlots[srTopIdx], in: srFlowStash)
            srSlots[srTopIdx].main = srPicked
            srSlots[srTopIdx].subs = nil
        }
        check(srFlowStash[0] == nil,
              "re-picking the split column's main through the picker discards its stash")
        let srRepicked = srSlots[srTopIdx].main
        srSlots[srLargeIdx].main = srRepicked
        srSlots[srLargeIdx].subs = LayoutSettingsView.restoredSubs(
            forMain: srRepicked, of: srSlots[srLargeIdx], in: srFlowStash)
        srSlots[srTopIdx].main = nil
        srSplits[0] = false
        let srRepickedSaved = LayoutSettingsView.panelsForSave(slots: srSlots, splits: srSplits)
        check(srRepickedSaved.map(\.main) == ["alpha:week"] && srRepickedSaved[0].subs == nil,
              "a re-picked main comes back automatic without the discarded stash")

        // 指摘 1・経路「空にする」: 2 段の小スロットを clearSlot で空にしてから
        // 同じ主枠を選び直しても（clearSlot → mainBinding と同じ呼び出し順）、
        // 1 段に戻すと自動のまま。
        srSlots = LayoutSettingsView.emptySlots()
        srSplits = srNoSplits
        srSlots[srLargeIdx].main = "alpha:week"
        srSlots[srLargeIdx].subs = ["alpha:day"]
        srFlowStash = LayoutSettingsView.stashSubs(for: srSlots[srLargeIdx], into: [:])
        srSlots[srTopIdx].main = srSlots[srLargeIdx].main
        srSlots[srLargeIdx].main = nil
        srSlots[srLargeIdx].subs = nil
        srSplits[0] = true
        // clearSlot（小スロット）: 退避を消してから main を空にする
        srFlowStash = LayoutSettingsView.discardStash(of: srSlots[srTopIdx], in: srFlowStash)
        srSlots[srTopIdx].main = nil
        srSlots[srTopIdx].subs = nil
        check(srFlowStash[0] == nil,
              "clearing the split column's top slot discards its stash")
        // mainBinding（空 → A）: 退避を消してから主枠を戻す
        srFlowStash = LayoutSettingsView.discardStash(of: srSlots[srTopIdx], in: srFlowStash)
        srSlots[srTopIdx].main = "alpha:week"
        srSlots[srTopIdx].subs = nil
        srSlots[srLargeIdx].main = srSlots[srTopIdx].main
        srSlots[srLargeIdx].subs = LayoutSettingsView.restoredSubs(
            forMain: srSlots[srTopIdx].main, of: srSlots[srLargeIdx], in: srFlowStash)
        srSlots[srTopIdx].main = nil
        srSplits[0] = false
        let srClearedSaved = LayoutSettingsView.panelsForSave(slots: srSlots, splits: srSplits)
        check(srClearedSaved.map(\.main) == ["alpha:week"] && srClearedSaved[0].subs == nil,
              "a cleared then re-picked main comes back automatic")

        // 指摘 1・経路「入れ替え」: 2 段の小スロット同士を swapSlots で入れ替えると
        // 両方のパネルの退避が消え、どちらの列も退避なしで 1 段に戻る。
        var srSwapSlots = LayoutSettingsView.emptySlots()
        var srSwapStash: [Int: [String: [String]]] = [:]
        let srCol1LargeIdx = LayoutSettingsView.slotIndex(1, 0)
        let srCol1TopIdx = LayoutSettingsView.slotIndex(1, 1)
        srSwapSlots[srLargeIdx].main = "alpha:week"
        srSwapSlots[srLargeIdx].subs = ["alpha:day"]
        srSwapSlots[srCol1LargeIdx].main = "beta:day"
        srSwapSlots[srCol1LargeIdx].subs = ["beta:5h"]
        srSwapStash = LayoutSettingsView.stashSubs(for: srSwapSlots[srLargeIdx], into: srSwapStash)
        srSwapStash = LayoutSettingsView.stashSubs(for: srSwapSlots[srCol1LargeIdx], into: srSwapStash)
        srSwapSlots[srTopIdx].main = srSwapSlots[srLargeIdx].main
        srSwapSlots[srLargeIdx].main = nil
        srSwapSlots[srLargeIdx].subs = nil
        srSwapSlots[srCol1TopIdx].main = srSwapSlots[srCol1LargeIdx].main
        srSwapSlots[srCol1LargeIdx].main = nil
        srSwapSlots[srCol1LargeIdx].subs = nil
        // swapSlots（small↔small）: 両方の列の退避を消してから main を入れ替える
        srSwapStash = LayoutSettingsView.discardStash(of: srSwapSlots[srTopIdx], in: srSwapStash)
        srSwapStash = LayoutSettingsView.discardStash(of: srSwapSlots[srCol1TopIdx], in: srSwapStash)
        let srMoved0 = srSwapSlots[srTopIdx].main
        srSwapSlots[srTopIdx].main = srSwapSlots[srCol1TopIdx].main
        srSwapSlots[srCol1TopIdx].main = srMoved0
        check(srSwapStash.isEmpty, "swapping two split columns discards both stashes")
        for srCol in 0..<2 {
            let srL = LayoutSettingsView.slotIndex(srCol, 0)
            let srT = LayoutSettingsView.slotIndex(srCol, 1)
            srSwapSlots[srL].main = srSwapSlots[srT].main
            srSwapSlots[srL].subs = LayoutSettingsView.restoredSubs(
                forMain: srSwapSlots[srT].main, of: srSwapSlots[srL], in: srSwapStash)
            srSwapSlots[srT].main = nil
        }
        let srSwappedSaved = LayoutSettingsView.panelsForSave(slots: srSwapSlots, splits: srNoSplits)
        check(srSwappedSaved.map(\.main) == ["beta:day", "alpha:week"]
              && srSwappedSaved.allSatisfy { $0.subs == nil },
              "swapped mains come back automatic in both columns")

        // 指摘 3・経路「save の失敗」: 小スロットを空にして退避を消した直後に保存が
        // 失敗し、読み直しで確認できた配置に小スロットの A が戻っても、退避は
        // 保存が最後に成功したときの状態に戻るので、1 段に戻すと subs が復元される。
        // save の失敗経路と同じ呼び出し順: revertedStash(beforeEdit: stashBeforeSave,
        // confirmedSlots: 読み直した配置のスロット)。
        srSlots = LayoutSettingsView.emptySlots()
        srSplits = srNoSplits
        srSlots[srLargeIdx].main = "alpha:week"
        srSlots[srLargeIdx].subs = ["alpha:day"]
        let srSplitStash = LayoutSettingsView.stashSubs(for: srSlots[srLargeIdx], into: [:])
        srSlots[srTopIdx].main = srSlots[srLargeIdx].main
        srSlots[srLargeIdx].main = nil
        srSlots[srLargeIdx].subs = nil
        srSplits[0] = true
        let srAfterSplit = srSlots
        // clearSlot で小スロットを空にする（退避も消える）→ この保存が失敗したとする
        var srFailedStash = LayoutSettingsView.discardStash(of: srSlots[srTopIdx], in: srSplitStash)
        srSlots[srTopIdx].main = nil
        srSlots[srTopIdx].subs = nil
        check(srFailedStash.isEmpty,
              "clearing the top slot discards the stash before the failed save")
        // 読み直し: 確認できた設定には split のまま上段 A が残る
        let srConfirmed = LayoutSettingsView.restoreLayout(config: [
            ConfigDocument.PanelConfig(main: "alpha:week", subs: nil, size: "small"),
            ConfigDocument.PanelConfig(main: "", subs: nil, size: "small"),
        ], status: [])
        let srReverted = LayoutSettingsView.revertedStash(
            beforeEdit: srSplitStash, confirmedSlots: srConfirmed.slots)
        check(srReverted == srSplitStash,
              "a failed save restores the stash from before the edit")
        // 1 段に戻す: 退避が戻っているので元の明示 subs も復元される
        srSlots = srAfterSplit
        srSlots[srLargeIdx].main = srSlots[srTopIdx].main
        srSlots[srLargeIdx].subs = LayoutSettingsView.restoredSubs(
            forMain: srSlots[srTopIdx].main, of: srSlots[srLargeIdx], in: srReverted)
        srSlots[srTopIdx].main = nil
        srSplits[0] = false
        let srRecoveredSaved = LayoutSettingsView.panelsForSave(slots: srSlots, splits: srSplits)
        check(srRecoveredSaved.map(\.main) == ["alpha:week"] && srRecoveredSaved[0].subs == ["alpha:day"],
              "the restored stash brings back the explicit subs on merge")

        // 指摘 3: 退避を触らない編集（サブのチェックなど）の保存が失敗したときは、
        // 現在の退避が確認できた配置と整合するのでそのまま残る（現状維持になる）。
        check(LayoutSettingsView.revertedStash(
            beforeEdit: srFailedStash, confirmedSlots: srAfterSplit) == srFailedStash
            && srFailedStash.isEmpty,
              "a failed unrelated edit keeps the current stash")

        // 指摘 3: 確認できた配置に主枠が現れない列の退避は戻さない（その列に主枠が
        // 戻らない限り復元先を得られない）。主枠が別の列に移っていても戻さない。
        check(LayoutSettingsView.revertedStash(
            beforeEdit: srSplitStash, confirmedSlots: LayoutSettingsView.emptySlots()).isEmpty,
              "a stash for a column without that main is not restored")
        let srOtherCol = LayoutSettingsView.restoreLayout(config: [
            ConfigDocument.PanelConfig(main: "", subs: nil, size: "large"),
            ConfigDocument.PanelConfig(main: "alpha:week", subs: nil, size: "large"),
        ], status: [])
        check(LayoutSettingsView.revertedStash(
            beforeEdit: srSplitStash, confirmedSlots: srOtherCol.slots).isEmpty,
              "a stash is not restored into a different column")

        print("Regression checks: \(checks) passed")
    }
}
