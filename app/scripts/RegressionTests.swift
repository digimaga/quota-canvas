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

        print("Regression checks: \(checks) passed")
    }
}
