import SwiftUI
import AppKit
import UniformTypeIdentifiers

/// 「配置」タブ: 画面の形を模した図で layout.panels（手動配置）を編集する。
/// 初期表示は status.json の layout（無ければ空スロット）。変更は即 config --set-json で保存。
/// 4 列 ×（大 1 or 小 2）。各列の「2 段に分ける」で上下 2 枚の小スロット（全部 2 段なら最大 8 枚）。
struct LayoutSettingsView: View {
    static let columnCount = 4
    static let maxSubs = 3

    private let client = EngineClient()

    @State private var doc: ConfigDocument?
    @State private var windows: [WindowChoice] = []
    @State private var providerColors: [String: String] = [:]
    @State private var slots: [LayoutSlot] = LayoutSettingsView.emptySlots()
    /// 各列を 2 段に分けるか（true = 上段・下段の小スロット 2 枚）
    @State private var splits = [Bool](repeating: false, count: LayoutSettingsView.columnCount)
    /// 自動（subs == nil）のときの表示用: status.json の layout（解決済み subs）を主枠窓 ID で覚えておく
    @State private var resolvedSubsByMain: [String: [String]] = [:]
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
    }

    struct LayoutSlot: Identifiable {
        let id: String     // "c0:large", "c0:top", "c0:bottom", "c1:large"…
        let column: Int    // 0..<columnCount
        let size: String   // "large" | "small"
        let title: String
        var main: String?   // 窓 ID（nil = 空）
        var subs: [String]? // 大スロットのみ（最大 maxSubs）。nil = 自動、[] = 明示的にサブなし
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

            if slot.size == "large" {
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
                        ForEach(windows) { window in
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
        let provider = main.split(separator: ":", maxSplits: 1).first.map(String.init) ?? main
        return HexColor.color(providerColors[provider])
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
                    slots[bi].main = nil
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
            let detected = try await client.detect()
            windows = detected.flatMap { service in
                service.windowList.map { window in
                    WindowChoice(id: "\(service.provider):\(window.key)",
                                 label: "\(service.displayName) \(window.displayLabel)")
                }
            }
        } catch {
            windows = []
            alertMessage = String(localized: "窓の一覧を取得できません: \(error.localizedDescription)")
        }

        if let snapshot {
            providerColors = snapshot.colors ?? [:]
            applyLayout(snapshot.layout ?? [],
                        config: doc?.layoutPanels ?? [])
        }
    }

    /// status.json の layout（解決済み subs）+ config の layout.panels（nil = 自動）→ 列への初期配置。
    /// エンジンの規則: 前から順に、large は 1 枚で 1 列。small は直前の列が small 列で
    /// まだ 1 枚なら同じ列の下段、そうでなければ新しい small 列の上段。列は最大 4（超過分は捨てる）。
    /// small の main "" は空き段（位置だけ保持）。
    /// subs は config の panels と status の layout を「同じ並び位置」で対応づける
    /// （同じ窓が複数のパネルに現れてもずれない）。枚数が違うとき（自動配置の表示・
    /// 未知の窓をエンジンが無視した場合など）は対応づけ不能なので全パネル「自動」扱いにする。
    private func applyLayout(_ panels: [LayoutPanel], config: [ConfigDocument.PanelConfig]) {
        var next = Self.emptySlots()
        var nextSplits = [Bool](repeating: false, count: Self.columnCount)
        resolvedSubsByMain = [:]
        let indexedSubs: [[String]?] = config.count == panels.count
            ? config.map(\.subs)
            : Array(repeating: nil, count: panels.count)

        var col = -1          // 直前のパネルが入った列（-1 = まだない）
        var colIsSmall = false
        var colFilled = 0     // その列に並んだパネル数
        for (i, panel) in panels.enumerated() {
            let main = panel.main ?? ""
            let isSmall = panel.size == "small"
            let continues = isSmall && colIsSmall && colFilled == 1
            if !continues {
                col += 1
                guard col < Self.columnCount else { break }
                colIsSmall = isSmall
                colFilled = 0
                nextSplits[col] = isSmall
            }
            if isSmall {
                if !main.isEmpty {
                    next[Self.slotIndex(col, continues ? 2 : 1)].main = main
                }
            } else {
                next[Self.slotIndex(col, 0)].main = main.isEmpty ? nil : main
                next[Self.slotIndex(col, 0)].subs = indexedSubs[i]
                if !main.isEmpty {
                    resolvedSubsByMain[main] = panel.subs ?? []
                }
            }
            colFilled += 1
        }
        slots = next
        splits = nextSplits
    }

    /// 表示中のスロットから layout.panels を組み立てて即保存。
    /// split でない列は大に main があるときだけ large を 1 枚（空なら出さない＝列が詰まる）。
    /// split の列は上段か下段のどちらかに main があれば上下両方を small で出す（空の段は main ""、
    /// small に subs はない）。両方空なら出さない。
    private func commit() {
        guard let original = doc else { return }
        var updated = original
        var panels: [ConfigDocument.PanelConfig] = []
        for col in 0..<Self.columnCount {
            if splits[col] {
                let top = slots[Self.slotIndex(col, 1)].main
                let bottom = slots[Self.slotIndex(col, 2)].main
                guard top != nil || bottom != nil else { continue }
                panels.append(ConfigDocument.PanelConfig(main: top ?? "", subs: nil, size: "small"))
                panels.append(ConfigDocument.PanelConfig(main: bottom ?? "", subs: nil, size: "small"))
            } else {
                let large = slots[Self.slotIndex(col, 0)]
                if let main = large.main, !main.isEmpty {
                    panels.append(ConfigDocument.PanelConfig(main: main, subs: large.subs, size: "large"))
                }
            }
        }
        updated.layoutPanels = panels
        doc = updated
        Task { await save(original, updated) }
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
            if slot.size == "large" {
                Text(subSummary)
                    .font(.caption).foregroundStyle(.secondary).lineLimit(2)
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
