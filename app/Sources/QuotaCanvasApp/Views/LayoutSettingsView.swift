import SwiftUI
import AppKit
import UniformTypeIdentifiers

/// 「配置」タブ: 画面の形を模した図で layout.panels（手動配置）を編集する。
/// 手動配置は config、 自動配置は status.json の layout から復元。変更は即 config --set-json で保存。
/// 5 列 ×（大 1 or 小 2）。各列の「2 段に分ける」で上下 2 枚の小スロット（全部 2 段なら最大 10 枚）。
struct LayoutSettingsView: View {
    static let columnCount = 5
    static let maxSubs = 3
    /// カレンダーパネルの main 値 (engine 側の render.CALENDAR_MAIN と同じ)。
    /// 窓を持たない特殊パネルで、大・小どちらにも置けてサブは持たない。
    static let calendarMain = "calendar"

    private let client = EngineClient()

    @State private var doc: ConfigDocument?
    @State private var windows: [WindowChoice] = []
    @State private var providerColors: [String: String] = [:]
    @State private var slots: [LayoutSlot] = LayoutSettingsView.emptySlots()
    /// 各列を 2 段に分けるか（true = 上段・下段の小スロット 2 枚）
    @State private var splits = [Bool](repeating: false, count: LayoutSettingsView.columnCount)
    /// 自動（subs == nil）のときの表示用: status.json の layout（解決済み subs）を主枠窓 ID で覚えておく
    @State private var resolvedSubsByMain: [String: [String]] = [:]
    /// status.json の layout が教える、サービス ID の主枠 → エンジンが実際に選んだメイン窓
    /// （「2本目の窓」の候補から除く窓の特定に使う）
    @State private var resolvedMainWindows: [String: String] = [:]
    @State private var selectedSlotID: String?
    @State private var loading = true
    @State private var loadFailed = false
    @State private var busy = false
    @State private var alertMessage: String?
    @State private var subLimitHint = false

    // MARK: 編集モデル

    struct WindowChoice: Identifiable {
        let id: String     // 窓 ID "provider:key"
        let label: String  // "Claude 週間"
        /// 窓の長さ（秒）。status.json の windows から。取れないとき nil（長さ不明）。
        var length: Double? = nil
        /// 内訳窓なら親窓の key（status.json の part_of）。独立窓・不明は nil。
        var partOf: String? = nil
    }

    struct LayoutSlot: Identifiable {
        let id: String     // "c0:large", "c0:top", "c0:bottom", "c1:large"…
        let column: Int    // 0..<columnCount
        let size: String   // "large" | "small"
        let title: String
        var main: String?   // 窓 ID（nil = 空）
        var subs: [String]? // 大スロットのみ（最大 maxSubs）。nil = 自動、[] = 明示的にサブなし
        var secondBar: Bool? = nil // 小スロットのみ。nil = 自動
        var secondWindow: String? = nil // 小スロットのみ。2 本目のバーに出す窓。nil = 自動
    }

    /// 列内の行 → slots の添字。0 = 大 / 1 = 小 上段 / 2 = 小 下段
    static func slotIndex(_ column: Int, _ row: Int) -> Int { column * 3 + row }

    static func emptySlots() -> [LayoutSlot] {
        var result: [LayoutSlot] = []
        for c in 0..<columnCount {
            result.append(LayoutSlot(id: "c\(c):large", column: c, size: "large",
                                     title: String(localized: "列 \(c + 1)"), main: nil, subs: nil))
            result.append(LayoutSlot(id: "c\(c):top", column: c, size: "small",
                                     title: String(localized: "列 \(c + 1) 上段"), main: nil, subs: nil))
            result.append(LayoutSlot(id: "c\(c):bottom", column: c, size: "small",
                                     title: String(localized: "列 \(c + 1) 下段"), main: nil, subs: nil))
        }
        return result
    }

    var body: some View {
        VStack(spacing: 0) {
            if loadFailed && doc == nil {
                ContentUnavailableView("設定を取得できませんでした",
                                       systemImage: "exclamationmark.triangle",
                                       description: Text("エンジンのパスを「表示」タブで確認してください。"))
            } else if loading && doc == nil {
                ProgressView("設定を読み込み中…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        controlsRow
                        screenDiagram
                        if let slot = selectedSlot {
                            editor(for: slot)
                        }
                        Text("大スロットの空きは保存されません（2 段に分けた列は空き段も位置を保ちます）。変更はすぐ保存され、エンジンの次の描画から反映されます。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .disabled(busy)
            }
        }
        .task { await load() }
        .alert("エラー", isPresented: Binding(
            get: { alertMessage != nil },
            set: { if !$0 { alertMessage = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(alertMessage ?? "")
        }
    }

    // MARK: 上部のコントロール

    private var controlsRow: some View {
        HStack {
            Text("各列を 2 段に分けると上下 2 枚の小スロットになります（最大 \(Self.columnCount * 2) 枚）。")
                .font(.caption).foregroundStyle(.secondary)
            Spacer()
            Button("自動配置に戻す") { resetToAuto() }
                .disabled(busy)
        }
    }

    // MARK: 画面の形を模した図

    private var screenDiagram: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("画面").font(.caption2).foregroundStyle(.secondary)
            HStack(alignment: .top, spacing: 10) {
                ForEach(0..<Self.columnCount, id: \.self) { col in
                    VStack(spacing: 2) {
                        Toggle("2 段に分ける", isOn: splitBinding(col))
                            .toggleStyle(.checkbox)
                            .font(.caption)
                            .disabled(splits[col] && bothRowsFilled(col))
                        if splits[col] && bothRowsFilled(col) {
                            Text("片方を空にすると 1 段に戻せます")
                                .font(.caption2).foregroundStyle(.tertiary)
                                .lineLimit(1)
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
            }
            HStack(alignment: .top, spacing: 10) {
                ForEach(0..<Self.columnCount, id: \.self) { col in
                    VStack(spacing: 8) {
                        if splits[col] {
                            slotCard(slots[Self.slotIndex(col, 1)])
                            slotCard(slots[Self.slotIndex(col, 2)])
                        } else {
                            slotCard(slots[Self.slotIndex(col, 0)])
                        }
                    }
                    .frame(maxWidth: .infinity) // エンジンは等幅なので小列も大列と同じ幅
                }
            }
            .frame(maxWidth: .infinity)
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .windowBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 10)
                .strokeBorder(Color.secondary.opacity(0.35),
                              style: StrokeStyle(lineWidth: 1, dash: [5, 4])))
        }
    }

    private func slotCard(_ slot: LayoutSlot) -> some View {
        SlotCard(slot: slot,
                 isSelected: slot.id == selectedSlotID,
                 color: mainColor(slot),
                 mainLabel: mainLabel(slot),
                 subLabels: effectiveSubs(slot).map { windowLabel($0) },
                 subAuto: slot.size == "large" && slot.subs == nil,
                 secondWindowLabel: slot.secondWindow.map { windowLabel($0) },
                 onSelect: { selectedSlotID = slot.id },
                 onSwap: { swapSlots(from: $0, to: slot.id) })
    }

    // MARK: スロットの編集パネル

    @ViewBuilder
    private func editor(for slot: LayoutSlot) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("\(slot.title)（\(slot.size == "large" ? String(localized: "大") : String(localized: "小"))）").font(.headline)
                Spacer()
                Button("閉じる") { selectedSlotID = nil }.controlSize(.small)
            }

            Picker("窓", selection: mainBinding(slot)) {
                Text("（空）").tag("")
                ForEach(windows) { window in
                    Text(window.label).tag(window.id)
                }
            }
            .pickerStyle(.menu)
            .frame(width: 300, alignment: .leading)

            if slot.size == "small" && Self.acceptsSecondBar(slot.main) {
                VStack(alignment: .leading, spacing: 4) {
                    Picker("2本目のバー（短い窓）", selection: secondBarBinding(slot)) {
                        Text("自動").tag(Bool?.none)
                        Text("表示する").tag(Bool?.some(true))
                        Text("表示しない").tag(Bool?.some(false))
                    }
                    .pickerStyle(.menu)
                    .frame(width: 300, alignment: .leading)
                    Text("自動: OpenCode Go・OpenDesign Go・Gemini・Devin の小パネルで、5 時間枠などの短い窓を 2 本目のバーに出します。")
                        .font(.caption2).foregroundStyle(.tertiary)
                    Picker("2本目の窓", selection: secondWindowBinding(slot)) {
                        Text("自動").tag(String?.none)
                        ForEach(Self.secondWindowCandidates(main: slot.main, windows: windows,
                                                            selected: slot.secondWindow,
                                                            resolvedMain: slot.main.flatMap { resolvedMainWindows[$0] })) { window in
                            Text(window.label).tag(String?.some(window.id))
                        }
                    }
                    .pickerStyle(.menu)
                    .frame(width: 300, alignment: .leading)
                    .disabled(slot.secondBar == false) // 2 本目のバーを出さない設定では選べない
                    Text("自動: メインが 1 日未満の窓なら 1 日以上の最短の窓（週間など）、そうでなければメインより短い窓（5 時間枠など）を出します。")
                        .font(.caption2).foregroundStyle(.tertiary)
                }
            }

            if slot.size == "large" && Self.acceptsSubs(slot.main) {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 6) {
                        Text("サブ窓（最大 \(Self.maxSubs)）")
                            .font(.caption).foregroundStyle(.secondary)
                        if slot.subs == nil {
                            Text("自動")
                                .font(.caption2).bold()
                                .foregroundStyle(Color.accentColor)
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1)
                                .background(Capsule().fill(Color.accentColor.opacity(0.15)))
                        }
                        Spacer()
                        Button("サブを自動に戻す") { resetSubsToAuto(slot) }
                            .controlSize(.small)
                            .disabled(slot.subs == nil)
                    }
                    if slot.subs == nil {
                        Text("主枠と同じサービスの残りの窓を自動で並べています。チェックを変えると明示指定になります。")
                            .font(.caption2).foregroundStyle(.tertiary)
                    }
                    LazyVGrid(columns: [GridItem(.flexible(minimum: 10, maximum: .infinity), alignment: .leading),
                                        GridItem(.flexible(minimum: 10, maximum: .infinity), alignment: .leading)],
                              alignment: .leading, spacing: 4) {
                        ForEach(Self.subCandidates(main: slot.main, windows: windows,
                                                   selected: effectiveSubs(slot))) { window in
                            Toggle(window.label, isOn: subBinding(slot, window.id))
                                .toggleStyle(.checkbox)
                                .lineLimit(1)
                                .help(window.id)
                        }
                    }
                    if subLimitHint {
                        Text("サブ窓は最大 \(Self.maxSubs) つまでです")
                            .font(.caption).foregroundStyle(.red)
                    }
                }
            }

            HStack {
                Button("このスロットを空にする", role: .destructive) { clearSlot(slot) }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.secondary.opacity(0.3)))
    }

    // MARK: 表示用ヘルパー

    private var selectedSlot: LayoutSlot? {
        slots.first { $0.id == selectedSlotID }
    }

    private func windowLabel(_ id: String) -> String {
        windows.first { $0.id == id }?.label ?? id
    }

    private func mainLabel(_ slot: LayoutSlot) -> String? {
        guard let main = slot.main, !main.isEmpty else { return nil }
        return windowLabel(main)
    }

    private func mainColor(_ slot: LayoutSlot) -> Color? {
        guard let main = slot.main, !main.isEmpty else { return nil }
        // ローカル LLM サーバーは色のキーも id 全体 (status.json の colors が
        // "local:<名前>" で載る)。台ごとの指定がなければ既定色 "local" を使う。
        // クラウド側は従来どおりプロバイダー名。
        if main.hasPrefix("local:") {
            return HexColor.color(providerColors[main] ?? providerColors["local"])
        }
        let provider = main.split(separator: ":", maxSplits: 1).first.map(String.init) ?? main
        return HexColor.color(providerColors[provider])
    }

    /// [providers.local.<名前>] を主枠の候補にする。id は "local:<名前>"、
    /// 表示名は label、無ければ名前。名前順で、detect 側に同名があれば足さない。
    /// enabled = false の台は出さない（キー無し = 有効、engine と同じ既定）。
    static func localChoices(from doc: ConfigDocument?, existing: [WindowChoice]) -> [WindowChoice] {
        let locals = doc?.localProviders ?? [:]
        return locals.keys.sorted().compactMap { name in
            let id = "local:\(name)"
            guard !existing.contains(where: { $0.id == id }) else { return nil }
            guard locals[name]?["enabled"] as? Bool != false else { return nil }
            return WindowChoice(id: id, label: doc?.localProviderLabel(name) ?? name)
        }
    }

    /// サブ窓の候補か。窓 id ("provider:key") で、ローカル LLM サーバー
    /// (local:<名前>、窓を持たないカード 1 枚) は除外。
    static func isSubCandidate(_ id: String) -> Bool {
        id.contains(":") && !id.hasPrefix("local:")
    }

    /// サブ窓を選べる主枠か。主枠が空のスロットは保存されずサブも消えるので選ばせない。
    /// サービス全体 (エラーパネル)・ローカル LLM・カレンダーもサブを持たない。
    static func acceptsSubs(_ main: String?) -> Bool {
        guard let main, isSubCandidate(main) else { return false }
        return !isCalendar(main)
    }

    /// 2 本目のバーを選べる主枠か。small だけの設定で、カレンダー・ローカル LLM
    /// (local:<名前>) 以外の窓/サービスが対象。主枠が空の段は保存されないので選ばせない。
    static func acceptsSecondBar(_ main: String?) -> Bool {
        guard let main, !main.isEmpty, !isCalendar(main) else { return false }
        return !main.hasPrefix("local:")
    }

    /// 2 本目の窓の候補: 主枠と同じサービス（窓 ID の ":" の前が同じ）の独立窓から
    /// エンジンが実際にメインに使う窓を除いた一覧（エンジンはメイン自身を 2 本目に選べない）。
    /// main が窓 ID なら main 自身を除く。main がサービス ID（":" 無し）のときは、
    /// status.json の main_window が取れていれば（resolvedMain）その窓だけを除き、
    /// 取れなければ resolvedMainWindow の推定（render.py _main_rank と同じ規則）を fallback にする。
    /// 内訳窓（part_of 付き）も選べないので候補に出さない。
    /// 一覧に無い窓が設定に残っているときは外せるように候補に残す。
    static func secondWindowCandidates(main: String?, windows: [WindowChoice],
                                       selected: String?, resolvedMain: String? = nil) -> [WindowChoice] {
        guard let main, acceptsSecondBar(main) else { return [] }
        let provider = main.split(separator: ":", maxSplits: 1)[0]
        let excludedMain = main.contains(":")
            ? main
            : (resolvedMain ?? resolvedMainWindow(provider: String(provider), windows: windows))
        var candidates = windows.filter { window in
            window.id.contains(":") && window.id != main && window.id != excludedMain
                && window.partOf == nil
                && window.id.split(separator: ":", maxSplits: 1)[0] == provider
        }
        if let selected, !selected.isEmpty, !candidates.contains(where: { $0.id == selected }) {
            candidates.append(WindowChoice(id: selected, label: selected))
        }
        return candidates
    }

    /// main がサービス ID のときエンジンがメインに選ぶ窓（render.py _main_rank と
    /// 同じ規則）: 同じサービスの独立窓（内訳窓を除く）のうち長さが最大、同長なら
    /// key=="weekly" を優先。長さの取れる窓だけで判定し、全部取れなければ nil。
    static func resolvedMainWindow(provider: String, windows: [WindowChoice]) -> String? {
        var best: WindowChoice?
        for window in windows {
            guard window.partOf == nil, let length = window.length,
                  window.id.split(separator: ":", maxSplits: 1)[0] == provider else { continue }
            let weekly = window.id.split(separator: ":").last == "weekly" ? 1 : 0
            if let current = best,
               (length, weekly) <= (current.length ?? -1,
                                    current.id.split(separator: ":").last == "weekly" ? 1 : 0) {
                continue
            }
            best = window
        }
        return best?.id
    }

    /// サブ窓の候補: 主枠と同じサービスの窓（主枠自身を除く）。以前に別サービスの窓を
    /// サブにしていた設定は、外せるように選択中のものだけ残す。
    static func subCandidates(main: String?, windows: [WindowChoice], selected: [String]) -> [WindowChoice] {
        guard let main, acceptsSubs(main) else { return [] }
        let provider = main.split(separator: ":", maxSplits: 1)[0]
        return windows.filter { window in
            guard isSubCandidate(window.id), window.id != main else { return false }
            return window.id.split(separator: ":", maxSplits: 1)[0] == provider
                || selected.contains(window.id)
        }
    }

    /// カレンダーパネルか (サブを持たない特殊な主枠)。
    static func isCalendar(_ main: String?) -> Bool {
        main == calendarMain
    }

    /// チェックボックス等の表示に使う実効サブ: 明示リスト、nil（自動）なら status.json の解決済み subs
    private func effectiveSubs(_ slot: LayoutSlot) -> [String] {
        if let subs = slot.subs { return subs }
        guard let main = slot.main, !main.isEmpty else { return [] }
        return resolvedSubsByMain[main] ?? []
    }

    // MARK: バインディング・スロット操作

    private func mainBinding(_ slot: LayoutSlot) -> Binding<String> {
        Binding<String>(
            get: { slots.first { $0.id == slot.id }?.main ?? "" },
            set: { newValue in
                guard let index = slots.firstIndex(where: { $0.id == slot.id }) else { return }
                slots[index].main = newValue.isEmpty ? nil : newValue
                slots[index].subs = nil // 主枠を選び直したらサブは自動に戻す
                slots[index].secondBar = nil // 2 本目のバーも自動に戻す
                slots[index].secondWindow = nil
                commit()
            }
        )
    }

    private func subBinding(_ slot: LayoutSlot, _ windowID: String) -> Binding<Bool> {
        Binding<Bool>(
            get: { effectiveSubs(slot).contains(windowID) },
            set: { on in
                guard let index = slots.firstIndex(where: { $0.id == slot.id }) else { return }
                // 自動のときも含め、この時点の選択を明示リストに確定させてから操作する
                var selection = effectiveSubs(slots[index])
                if on {
                    guard selection.count < Self.maxSubs else {
                        showSubLimitHint()
                        return
                    }
                    selection.append(windowID)
                } else {
                    selection.removeAll { $0 == windowID }
                }
                slots[index].subs = selection
                commit()
            }
        )
    }

    /// 小スロットの 2 本目のバー。nil = 自動。
    private func secondBarBinding(_ slot: LayoutSlot) -> Binding<Bool?> {
        Binding<Bool?>(
            get: { slots.first { $0.id == slot.id }?.secondBar },
            set: { newValue in
                guard let index = slots.firstIndex(where: { $0.id == slot.id }) else { return }
                slots[index].secondBar = newValue
                commit()
            }
        )
    }

    /// 小スロットの 2 本目の窓。nil = 自動。
    private func secondWindowBinding(_ slot: LayoutSlot) -> Binding<String?> {
        Binding<String?>(
            get: { slots.first { $0.id == slot.id }?.secondWindow },
            set: { newValue in
                guard let index = slots.firstIndex(where: { $0.id == slot.id }) else { return }
                slots[index].secondWindow = newValue
                commit()
            }
        )
    }

    /// スロットに窓が入っているか（main "" は空扱い）
    private func rowFilled(_ index: Int) -> Bool {
        guard let main = slots[index].main else { return false }
        return !main.isEmpty
    }

    /// 上段・下段の両方が埋まっている split 列。1 段に戻すと下段が消えるので解除できない。
    private func bothRowsFilled(_ col: Int) -> Bool {
        rowFilled(Self.slotIndex(col, 1)) && rowFilled(Self.slotIndex(col, 2))
    }

    /// 列の「2 段に分ける」トグル。切替時は埋まっていた枠の main を移す（大→上段、上段→大）。
    /// 上段・下段の両方が埋まっている列は解除できない（UI 側でも disabled）。
    private func splitBinding(_ col: Int) -> Binding<Bool> {
        Binding<Bool>(
            get: { splits[col] },
            set: { on in
                guard splits[col] != on else { return }
                let li = Self.slotIndex(col, 0)
                let ti = Self.slotIndex(col, 1)
                let bi = Self.slotIndex(col, 2)
                if on {
                    if let main = slots[li].main, !main.isEmpty {
                        slots[ti].main = main
                        slots[ti].secondBar = nil // 別の主枠が入るので自動に戻す
                        slots[ti].secondWindow = nil
                        slots[li].main = nil
                    }
                    slots[li].subs = nil
                    if selectedSlotID == slots[li].id { selectedSlotID = slots[ti].id }
                } else {
                    guard !bothRowsFilled(col) else { return }
                    let moved = slots[ti].main ?? slots[bi].main
                    if let main = moved, !main.isEmpty {
                        slots[li].main = main
                        slots[li].subs = nil // 小スロットにサブはないので自動に戻す
                    }
                    slots[ti].main = nil
                    slots[ti].secondBar = nil
                    slots[ti].secondWindow = nil
                    slots[bi].main = nil
                    slots[bi].secondBar = nil
                    slots[bi].secondWindow = nil
                    if selectedSlotID == slots[ti].id || selectedSlotID == slots[bi].id {
                        selectedSlotID = slots[li].id
                    }
                }
                splits[col] = on
                commit()
            }
        )
    }

    private func clearSlot(_ slot: LayoutSlot) {
        guard let index = slots.firstIndex(where: { $0.id == slot.id }) else { return }
        slots[index].main = nil
        slots[index].subs = nil
        slots[index].secondBar = nil
        slots[index].secondWindow = nil
        commit()
    }

    /// そのスロットのサブだけ自動（subs = nil）に戻す
    private func resetSubsToAuto(_ slot: LayoutSlot) {
        guard let index = slots.firstIndex(where: { $0.id == slot.id }) else { return }
        slots[index].subs = nil
        commit()
    }

    private func resetToAuto() {
        for index in slots.indices {
            slots[index].main = nil
            slots[index].subs = nil
            slots[index].secondBar = nil
            slots[index].secondWindow = nil
        }
        for index in splits.indices { splits[index] = false }
        selectedSlotID = nil
        commit() // すべて空 → layout.panels = [] で保存（= 自動配置）
    }

    /// スロットへのドロップ: main の入れ替え（大↔大はサブも入れ替える。small にサブはない）。
    private func swapSlots(from sourceID: String, to targetID: String) {
        guard sourceID != targetID,
              let s = slots.firstIndex(where: { $0.id == sourceID }),
              let t = slots.firstIndex(where: { $0.id == targetID }) else { return }
        var source = slots[s]
        var target = slots[t]
        swap(&source.main, &target.main)
        if source.size == "large" && target.size == "large" {
            swap(&source.subs, &target.subs)
        } else {
            // 大↔小: 小側にはサブがないので、main をもらった大側のサブは自動に戻す
            if source.size == "large" { source.subs = nil }
            if target.size == "large" { target.subs = nil }
        }
        if source.size == "small" && target.size == "small" {
            swap(&source.secondBar, &target.secondBar)
            swap(&source.secondWindow, &target.secondWindow)
        } else {
            // 大↔小: secondBar / secondWindow は小スロットだけの設定なので、main をもらった小側は自動に戻す
            if source.size == "small" { source.secondBar = nil; source.secondWindow = nil }
            if target.size == "small" { target.secondBar = nil; target.secondWindow = nil }
        }
        slots[s] = source
        slots[t] = target
        commit()
    }

    private func showSubLimitHint() {
        subLimitHint = true
        Task {
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            if !Task.isCancelled { subLimitHint = false }
        }
    }

    // MARK: load / commit

    private func load() async {
        loading = true
        defer { loading = false }

        let snapshot = StatusStore.loadStatusSnapshot()
        do {
            doc = try await client.getConfig()
            loadFailed = false
        } catch {
            loadFailed = true
            alertMessage = error.localizedDescription
            return
        }

        do {
            // 常駐エンジンの detect-cache.json で即表示する (無ければエンジン側で全取得)。
            let detected = try await client.detectCached().services
            windows = detected.flatMap { service in
                service.windowList.map { window in
                    WindowChoice(id: "\(service.provider):\(window.key)",
                                 label: "\(service.displayName) \(window.displayLabel)")
                }
            }
            // Balance-only providers have no window to offer as a main slot.
            for balance in snapshot?.balances ?? [] {
                if !windows.contains(where: { $0.id == balance.provider || $0.id.hasPrefix(balance.provider + ":") }) {
                    windows.append(WindowChoice(id: balance.provider, label: balance.name))
                }
            }
        } catch {
            windows = []
            alertMessage = String(localized: "窓の一覧を取得できません: \(error.localizedDescription)")
        }
        // ローカル LLM サーバー ([providers.local.<名前>]) も主枠に選べる
        // (config --json の providers.local から。表示名は label、無ければ名前)。
        windows.append(contentsOf: Self.localChoices(from: doc, existing: windows))
        // カレンダーパネル (main = "calendar") も主枠に選べる。
        // 大・小どちらにも置ける窓を持たないパネルなので、サブの選択は出さない。
        if !windows.contains(where: { $0.id == Self.calendarMain }) {
            windows.append(WindowChoice(id: Self.calendarMain,
                                        label: String(localized: "カレンダー")))
        }
        // status.json の全窓（内訳窓の part_of と長さ）を窓一覧に載せる。
        // 「2本目の窓」の候補からエンジンが実際にメインにする窓・内訳窓を
        // 除く判定に使う（detect-cache には part_of が無い）。
        let statusWindows = Dictionary(
            (snapshot?.windows ?? []).compactMap { w -> (String, StatusWindow)? in
                guard let id = w.id else { return nil }
                return (id, w)
            }, uniquingKeysWith: { first, _ in first })
        for index in windows.indices {
            if let sw = statusWindows[windows[index].id] {
                windows[index].length = sw.length
                windows[index].partOf = sw.partOf
            }
        }

        providerColors = snapshot?.colors ?? [:]
        let restored = Self.restoreLayout(config: doc?.layoutPanels ?? [], status: snapshot?.layout ?? [])
        slots = restored.slots
        splits = restored.splits
        resolvedSubsByMain = restored.resolvedSubs
        resolvedMainWindows = restored.resolvedMains
    }

    /// 手動配置は config が正。status は自動配置と、対応するパネル（同じ main・size）の
    /// 解決済みサブ表示と、サービス ID の主枠が実際に解決されたメイン窓（main_window）に使う。
    static func restoreLayout(config: [ConfigDocument.PanelConfig], status: [LayoutPanel])
        -> (slots: [LayoutSlot], splits: [Bool], resolvedSubs: [String: [String]],
            resolvedMains: [String: String]) {
        let panels: [ConfigDocument.PanelConfig] = config.isEmpty
            ? status.map { ConfigDocument.PanelConfig(main: $0.main ?? "", subs: nil, size: $0.size ?? "large") }
            : config
        var next = emptySlots()
        var nextSplits = [Bool](repeating: false, count: columnCount)
        var resolvedSubs: [String: [String]] = [:]
        var resolvedMains: [String: String] = [:]
        var col = -1
        var colIsSmall = false
        var colFilled = 0
        for (i, panel) in panels.enumerated() {
            let main = panel.main
            let isSmall = panel.size == "small"
            let continues = isSmall && colIsSmall && colFilled == 1
            if !continues {
                col += 1
                guard col < columnCount else { break }
                colIsSmall = isSmall
                colFilled = 0
                nextSplits[col] = isSmall
            }
            let statusPanel: LayoutPanel? = (!main.isEmpty && i < status.count
                && status[i].main == main && (status[i].size ?? "large") == panel.size)
                ? status[i] : nil
            if isSmall {
                if !main.isEmpty {
                    next[slotIndex(col, continues ? 2 : 1)].main = main
                    next[slotIndex(col, continues ? 2 : 1)].secondBar = panel.secondBar
                    next[slotIndex(col, continues ? 2 : 1)].secondWindow = panel.secondWindow
                }
            } else {
                next[slotIndex(col, 0)].main = main.isEmpty ? nil : main
                // カレンダーは窓を持たない 1 枚: subs が config に残っていても捨てる
                // (engine 側でも無視される。local: と同じ扱い)。
                next[slotIndex(col, 0)].subs = main == calendarMain ? nil : panel.subs
                if let statusPanel {
                    resolvedSubs[main] = statusPanel.subs ?? []
                }
            }
            // サービス ID の主枠が実際に解決された窓（small も対象。「2本目の窓」の候補から除く）
            if let statusPanel, let mainWindow = statusPanel.mainWindow {
                resolvedMains[main] = mainWindow
            }
            colFilled += 1
        }
        return (next, nextSplits, resolvedSubs, resolvedMains)
    }

    /// 表示中のスロットから layout.panels を組み立てて即保存。
    /// split でない列は大に main があるときだけ large を 1 枚（空なら出さない＝列が詰まる）。
    /// split の列は上段か下段のどちらかに main があれば上下両方を small で出す（空の段は main ""、
    /// small に subs はない）。両方空なら出さない。
    private func commit() {
        guard let original = doc else { return }
        var updated = original
        updated.layoutPanels = Self.panelsForSave(slots: slots, splits: splits)
        doc = updated
        Task { await save(original, updated) }
    }

    static func panelsForSave(slots: [LayoutSlot], splits: [Bool]) -> [ConfigDocument.PanelConfig] {
        var panels: [ConfigDocument.PanelConfig] = []
        for col in 0..<Self.columnCount {
            if splits[col] {
                let top = slots[Self.slotIndex(col, 1)]
                let bottom = slots[Self.slotIndex(col, 2)]
                guard top.main != nil || bottom.main != nil else { continue }
                // secondBar / secondWindow は small だけの設定。空の段（main ""）には付けない
                panels.append(ConfigDocument.PanelConfig(main: top.main ?? "", subs: nil, size: "small",
                                                         secondBar: top.main?.isEmpty == false ? top.secondBar : nil,
                                                         secondWindow: top.main?.isEmpty == false ? top.secondWindow : nil))
                panels.append(ConfigDocument.PanelConfig(main: bottom.main ?? "", subs: nil, size: "small",
                                                         secondBar: bottom.main?.isEmpty == false ? bottom.secondBar : nil,
                                                         secondWindow: bottom.main?.isEmpty == false ? bottom.secondWindow : nil))
            } else {
                let large = slots[Self.slotIndex(col, 0)]
                if let main = large.main, !main.isEmpty {
                    // カレンダーはサブを持たない (engine 側でも無視される): subs は保存しない
                    let subs = main == Self.calendarMain ? nil : large.subs
                    panels.append(ConfigDocument.PanelConfig(main: main, subs: subs, size: "large"))
                }
            }
        }
        return panels
    }

    private func save(_ original: ConfigDocument, _ docToSave: ConfigDocument) async {
        busy = true
        defer { busy = false }
        do {
            doc = try await client.saveChanges(from: original, to: docToSave)
            EngineClient.scheduleArtifactReload() // 2 秒後に status.json / frame.png を読み直させる
        } catch {
            alertMessage = error.localizedDescription
        }
    }
}

// MARK: - スロットのカード

private struct SlotCard: View {
    let slot: LayoutSettingsView.LayoutSlot
    let isSelected: Bool
    let color: Color?
    let mainLabel: String?
    let subLabels: [String]
    var subAuto: Bool = false
    var secondWindowLabel: String? = nil
    let onSelect: () -> Void
    let onSwap: (String) -> Void

    private var subSummary: String {
        let joined = subLabels.joined(separator: String(localized: "・"))
        if subAuto {
            return subLabels.isEmpty ? String(localized: "サブ: 自動（現在なし）")
                                     : String(localized: "サブ（自動）: \(joined)")
        }
        return subLabels.isEmpty ? String(localized: "サブなし")
                                 : String(localized: "サブ: \(joined)")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 4) {
                Text(slot.title).font(.caption2).foregroundStyle(.secondary)
                Spacer()
                if let color {
                    Circle().fill(color).frame(width: 8, height: 8)
                }
            }
            if let mainLabel {
                Text(mainLabel).font(.callout).bold().lineLimit(1)
            } else {
                Text("クリックして設定").font(.callout).foregroundStyle(.tertiary)
            }
            if slot.size == "large" && LayoutSettingsView.acceptsSubs(slot.main) {
                Text(subSummary)
                    .font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            if slot.size == "small", slot.main?.isEmpty == false, let secondBar = slot.secondBar {
                // 明示したときだけ小さなバッジで分かるようにする（自動は表示しない）
                Text(secondBar ? String(localized: "2本目のバー: 出す") : String(localized: "2本目のバー: 出さない"))
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            if slot.size == "small", slot.main?.isEmpty == false, let secondWindowLabel {
                // 2 本目の窓を選んでいるときだけバッジに出す（自動は表示しない）
                Text(String(localized: "2本目: \(secondWindowLabel)"))
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, minHeight: slot.size == "large" ? 64 : 48, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 6)
            .fill(isSelected ? Color.accentColor.opacity(0.12) : Color(nsColor: .controlBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 6)
            .strokeBorder(isSelected ? Color.accentColor : Color.secondary.opacity(0.4),
                          lineWidth: isSelected ? 2 : 1))
        .contentShape(Rectangle())
        .onTapGesture { onSelect() }
        .onDrag { NSItemProvider(object: slot.id as NSString) }
        .onDrop(of: [UTType.text], isTargeted: nil) { providers in
            guard let provider = providers.first else { return false }
            provider.loadObject(ofClass: NSString.self) { object, _ in
                guard let dragged = object as? NSString else { return }
                let sourceID = dragged as String
                DispatchQueue.main.async { onSwap(sourceID) }
            }
            return true
        }
    }
}
