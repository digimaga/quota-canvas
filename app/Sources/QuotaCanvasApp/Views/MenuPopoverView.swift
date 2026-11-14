import SwiftUI
import AppKit

/// メニューバーのポップオーバー本体。
struct MenuPopoverView: View {
    @EnvironmentObject private var statusStore: StatusStore
    @ObservedObject private var notifications = NotificationCoordinator.shared
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings

    @State private var pauseBusy = false
    @State private var alertMessage: String?
    /// 詳細表示（今までの複数行カード）。既定は簡易表示（カード 1 行）。
    @AppStorage("menuDetailedCards") private var detailedCards = false
    /// 配置外のカードを「他 N 件を表示」で展開したか。
    @State private var showExtraCards = false
    /// ポップオーバーの窓が載っている画面の高さ（visibleFrame）。0 = まだ分からない。
    @State private var screenVisibleHeight: CGFloat = 0
    /// スクロール内容の実の高さ。0 = まだ測っていない。
    @State private var contentHeight: CGFloat = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            Divider()
            content
            Divider()
            controls
        }
        .padding(12)
        .frame(width: 360)
        .background(PopoverScreenReader(visibleHeight: $screenVisibleHeight))
        .task { await statusStore.refresh() }
        .alert("エラー", isPresented: Binding(
            get: { alertMessage != nil },
            set: { if !$0 { alertMessage = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(alertMessage ?? "")
        }
    }

    // MARK: header

    @ViewBuilder
    private var header: some View {
        HStack {
            Image(systemName: "gauge.with.needle")
            Text("QuotaCanvas").font(.headline)
            Spacer()
            if statusStore.isPaused {
                Text("一時停止中").font(.caption).foregroundStyle(.secondary)
            }
        }
        updatedText
        // F1: 今使う先（engine の推奨）を 1 行で
        recommendationLine
        // カードの表示密度の切り替え（常に見える位置に置く）
        HStack {
            Toggle(isOn: $detailedCards) {
                Label("詳細表示", systemImage: "list.bullet.rectangle")
                    .font(.caption)
            }
            .toggleStyle(.switch)
            .controlSize(.mini)
            Spacer()
        }
    }

    /// F1: status.json の recommendation.text（エンジンが表示言語に訳済み）。
    @ViewBuilder
    private var recommendationLine: some View {
        if let text = statusStore.status?.recommendation?.text, !text.isEmpty {
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Image(systemName: "hand.point.up.left")
                    .font(.caption)
                    .foregroundStyle(.mint)
                Text(text)
                    .font(.callout)
                    .fontWeight(.medium)
            }
        }
    }

    @ViewBuilder
    private var updatedText: some View {
        if let status = statusStore.status, let updated = status.updatedAt {
            Text("更新: \(Self.format(updated))")
                .font(.caption).foregroundStyle(.secondary)
        } else {
            Text("更新時刻: 不明").font(.caption).foregroundStyle(.secondary)
        }
    }

    // MARK: main content

    @ViewBuilder
    private var content: some View {
        if statusStore.status == nil {
            Label(statusStore.fileError ?? String(localized: "ステータスを取得できていません"), systemImage: "exclamationmark.triangle")
                .font(.callout)
                .foregroundStyle(.secondary)
            Text("エンジンのパスを「設定 → 表示」で確認してください。")
                .font(.caption).foregroundStyle(.secondary)
        } else {
            if let error = notifications.error {
                Label(error, systemImage: "exclamationmark.triangle").font(.callout).foregroundStyle(.orange)
            }
            // エンジン停止（5 分以上更新なし）でも直前のカードは残して見せ、警告を上に出す
            if statusStore.isStale {
                Label("エンジンが動いていません（5 分以上更新がありません）", systemImage: "zzz")
                    .font(.callout)
                    .foregroundStyle(.orange)
            }
            // カード・残高・リセット権・エラーはスクロール。高さ上限で小さい画面でもはみ出さない。
            // 内容の高さを測って frame(height:) に渡すので、短い内容のときは余白を出さず縮む。
            ScrollView {
                VStack(alignment: .leading, spacing: 6) {
                    cardList
                    balancesSection
                    resetCreditsSection
                    errorList
                }
                .background(GeometryReader { geo in
                    Color.clear.preference(key: ScrollContentHeightKey.self, value: geo.size.height)
                })
            }
            .onPreferenceChange(ScrollContentHeightKey.self) { contentHeight = $0 }
            .frame(height: contentHeight > 0 ? min(contentHeight, contentMaxHeight) : nil)
        }
    }

    /// スクロール領域の高さ上限: ポップオーバーの窓が載っている画面の高さから、
    /// ヘッダー・切り替え・操作ボタンと余白の概算を引く。窓の画面がまだ分からない
    /// ときは NSScreen.main にフォールバックする。
    private var contentMaxHeight: CGFloat {
        let screenHeight = screenVisibleHeight > 0
            ? screenVisibleHeight
            : (NSScreen.main?.visibleFrame.height ?? 700)
        return max(160, screenHeight - 280)
    }

    @ViewBuilder
    private var balancesSection: some View {
        let balances = statusStore.status?.balances ?? []
        if !balances.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                Text("残高").font(.caption).bold()
                ForEach(Array(balances.enumerated()), id: \.offset) { _, balance in
                    HStack {
                        Text("\(balance.name): \(balance.displayText)")
                        if balance.staleError != nil { Text("取得失敗中").foregroundStyle(.orange) }
                    }.font(.caption2)
                }
            }
        }
    }

    // MARK: reset credits（Codex のリセット権）

    /// リセット権の一覧: 残り回数と最短の期限。期限 24 時間未満は赤・2 日未満は黄。
    @ViewBuilder
    private var resetCreditsSection: some View {
        let credits = statusStore.status?.resetCredits ?? [:]
        let cards = statusStore.status?.cards ?? []
        if !credits.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                Text("リセット権").font(.caption).bold()
                ForEach(sortedProviders(credits), id: \.self) { provider in
                    if let list = credits[provider], !list.isEmpty {
                        let earliest = list.compactMap(\.expiresAt).min()
                        let name = cards.first { $0.provider == provider }?.name ?? provider
                        HStack(spacing: 4) {
                            Image(systemName: "clock.arrow.circlepath").font(.caption2)
                            Text(String(localized: "\(name) ×\(list.count) · 最短 \(Self.expiryText(earliest, now: Date()))"))
                                .font(.caption2)
                                .foregroundStyle(Self.creditColor(earliest, now: Date()))
                        }
                    }
                }
            }
        }
    }

    private func sortedProviders(_ credits: [String: [ResetCredit]]) -> [String] {
        credits.keys.sorted {
            (credits[$0]?.compactMap(\.expiresAt).min() ?? .distantFuture)
                < (credits[$1]?.compactMap(\.expiresAt).min() ?? .distantFuture)
        }
    }

    private static func expiryText(_ date: Date?, now: Date) -> String {
        guard let date else { return String(localized: "期限不明") }
        if date <= now { return String(localized: "失効済み") }
        return RelativeDateTimeFormatter().localizedString(for: date, relativeTo: now)
    }

    private static func creditColor(_ date: Date?, now: Date) -> Color {
        guard let date, date > now else { return .red }
        let interval = date.timeIntervalSince(now)
        if interval < 24 * 3600 { return .red }
        if interval < 2 * 24 * 3600 { return .yellow }
        return .secondary
    }

    @ViewBuilder
    private var cardList: some View {
        let cards = statusStore.status?.cards ?? []
        if cards.isEmpty && (statusStore.status?.balances ?? []).isEmpty {
            Text("表示するカードがありません").font(.callout).foregroundStyle(.secondary)
        }
        let ordered = Self.orderedCards(cards, layout: statusStore.status?.layout, windows: statusStore.status?.windows,
                                        errorProviders: Set((statusStore.status?.errors ?? []).compactMap(\.provider)))
        if detailedCards {
            detailedRows(ordered)
        } else {
            compactRows(ordered)
        }
    }

    /// 詳細表示: 今までの複数行カード（全カード）。
    @ViewBuilder
    private func detailedRows(_ ordered: [(card: StatusCard, isSub: Bool, inLayout: Bool)]) -> some View {
        ForEach(Array(ordered.enumerated()), id: \.offset) { index, entry in
            CardRow(card: entry.card, gauge: statusStore.status?.gauge)
                .padding(.leading, entry.isSub ? 12 : 0)
            if index != ordered.count - 1 {
                Divider()
            }
        }
    }

    /// 簡易表示: 画面の配置（layout）に載っているカードだけ 1 行で。
    /// 配置外は「他 N 件を表示」で展開。layout が空・無い・どのカードにも当たらないときは全部出す。
    @ViewBuilder
    private func compactRows(_ ordered: [(card: StatusCard, isSub: Bool, inLayout: Bool)]) -> some View {
        let placed = ordered.filter(\.inLayout)
        let hidden = ordered.filter { !$0.inLayout }
        let visible = placed.isEmpty ? ordered : placed
        let extra = placed.isEmpty ? [(card: StatusCard, isSub: Bool, inLayout: Bool)]() : hidden
        ForEach(Array(visible.enumerated()), id: \.offset) { index, entry in
            CompactCardRow(card: entry.card, gauge: statusStore.status?.gauge)
                .padding(.leading, entry.isSub ? 12 : 0)
            if index != visible.count - 1 {
                Divider()
            }
        }
        if !extra.isEmpty {
            if showExtraCards {
                ForEach(Array(extra.enumerated()), id: \.offset) { _, entry in
                    Divider()
                    CompactCardRow(card: entry.card, gauge: statusStore.status?.gauge)
                }
            }
            Button {
                showExtraCards.toggle()
            } label: {
                Label(showExtraCards ? String(localized: "折りたたむ") : String(localized: "他 \(extra.count) 件を表示"),
                      systemImage: showExtraCards ? "chevron.up" : "chevron.down")
                    .font(.caption)
            }
            .buttonStyle(.borderless)
            .controlSize(.small)
            .padding(.top, 2)
        }
    }

    /// 画面の配置（status.json の layout）の左のパネルから順に、主枠 → そのサブ枠の順で並べる。
    /// 解決は描画側（render.py の _resolve_panel）と同じく各パネルとも全候補から独立に:
    /// 窓 ID（"claude:weekly"）はそのカード、provider ID（"claude"）は「使える窓のうち
    /// 一番長い窓、同長は key=="weekly"」（_main_rank の移植 = MainWindowRank）。
    /// 窓の長さは status.json の "windows" の length（秒）。使える窓は pu.error が無い
    /// もので、status.json では provider が errors に載っていること
    /// （取得失敗中 stale_error の最後の成功値は候補に残る。cli.py の
    /// last-good 置き換えで error は nil になる）。
    /// windows に載っていない id は長さ不明で既知の窓より後回し。そのときも
    /// id から分かる key=="weekly" は優先する。
    /// 表示では重複を除き、同じカードは最初の位置に 1 回だけ出る（画面には
    /// 2 回出ても簡易一覧では 1 行）。解決できなかったカードは元の順で末尾に置く。
    /// inLayout は layout のどれかに当たったかどうか（layout が空・無いときは全部 true）。
    static func orderedCards(_ cards: [StatusCard], layout: [LayoutPanel]?, windows: [StatusWindow]? = nil,
                             errorProviders: Set<String> = []) -> [(card: StatusCard, isSub: Bool, inLayout: Bool)] {
        let panels = layout ?? []
        func resolve(_ ref: String?) -> Int? {
            guard let ref, !ref.isEmpty, !errorProviders.contains(ref) else { return nil }
            if let index = cards.indices.first(where: {
                cards[$0].id == ref && !errorProviders.contains(cards[$0].provider ?? "")
            }) { return index }
            guard !ref.contains(":") else { return nil }
            // 同点では先頭のカードを優先（render 側の max と同じ）。
            var best: Int?
            for candidate in cards.indices
            where cards[candidate].provider == ref
                && !errorProviders.contains(cards[candidate].provider ?? "") {
                if best == nil
                    || mainWindowRank(cards[candidate], windows: windows) > mainWindowRank(cards[best!], windows: windows) {
                    best = candidate
                }
            }
            return best
        }
        var placed: [(index: Int, isSub: Bool)] = []
        for panel in panels {
            if let index = resolve(panel.main) { placed.append((index, false)) }
            for sub in panel.subs ?? [] {
                if let index = resolve(sub) { placed.append((index, true)) }
            }
        }
        var seen = Set<Int>()
        var result: [(card: StatusCard, isSub: Bool, inLayout: Bool)] = []
        for entry in placed where !seen.contains(entry.index) {
            seen.insert(entry.index)
            result.append((cards[entry.index], entry.isSub, true))
        }
        for index in cards.indices where !seen.contains(index) {
            result.append((cards[index], false, panels.isEmpty))
        }
        return result
    }

    /// render.py _main_rank の移植: 長い窓ほど主枠向き、同長なら key が "weekly"。
    /// key は窓 id（"provider:key"、ローカルサーバーは "local:名前"）の最後の ":" 以降。
    /// 内訳窓（part_of 付き）は status.json の cards に載らないため考慮しない。
    struct MainWindowRank: Comparable {
        let length: Double   // windows に載っていない id は -1（長さ不明）
        let weekly: Int      // windows が無くても id から判定できる

        static func < (lhs: MainWindowRank, rhs: MainWindowRank) -> Bool {
            if lhs.length != rhs.length { return lhs.length < rhs.length }
            return lhs.weekly < rhs.weekly
        }
    }

    static func mainWindowRank(_ card: StatusCard, windows: [StatusWindow]?) -> MainWindowRank {
        let length = windows?.first { $0.id == card.id }?.length ?? -1
        let key = card.id?.split(separator: ":").last.map(String.init)
        return MainWindowRank(length: length, weekly: key == "weekly" ? 1 : 0)
    }

    @ViewBuilder
    private var errorList: some View {
        let errors = statusStore.status?.errors ?? []
        if !errors.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                Text("エラー").font(.caption).bold().foregroundStyle(.red)
                ForEach(Array(errors.enumerated()), id: \.offset) { _, e in
                    Text("\(e.name ?? e.provider ?? "?"): \(e.error ?? String(localized: "不明なエラー"))")
                        .font(.caption).foregroundStyle(.red)
                }
            }
        }
    }

    // MARK: controls

    private var controls: some View {
        VStack(spacing: 6) {
            EngineRefreshButton {
                await statusStore.refresh()
            }
            .frame(maxWidth: .infinity)

            Button {
                Task { await togglePause() }
            } label: {
                Label(statusStore.isPaused ? String(localized: "再開") : String(localized: "一時停止"),
                      systemImage: statusStore.isPaused ? "play.fill" : "pause.fill")
                    .frame(maxWidth: .infinity)
            }
            .disabled(pauseBusy)

            HStack {
                Button {
                    NSApp.activate(ignoringOtherApps: true)
                    openWindow(id: "frame-preview")
                } label: {
                    Label("画面プレビュー", systemImage: "display").frame(maxWidth: .infinity)
                }
                Button {
                    NSApp.activate(ignoringOtherApps: true)
                    openSettings()
                } label: {
                    Label("設定…", systemImage: "gearshape").frame(maxWidth: .infinity)
                }
            }

            Button(role: .destructive) {
                AppEngineAgent.shared.waitForPendingWork()
                NSApp.terminate(nil)
            } label: {
                Label("終了", systemImage: "power").frame(maxWidth: .infinity)
            }
        }
        .controlSize(.small)
    }

    private func togglePause() async {
        pauseBusy = true
        defer { pauseBusy = false }
        do {
            let client = EngineClient()
            let paused = !statusStore.isPaused
            _ = try await client.updateConfig { $0.paused = paused }
            EngineClient.scheduleArtifactReload() // 2 秒後に status.json / frame.png を読み直させる
            await statusStore.refresh()
        } catch {
            alertMessage = error.localizedDescription
        }
    }

    static func format(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateStyle = .none
        f.timeStyle = .short
        return f.string(from: date)
    }
}

struct CardRow: View {
    let card: StatusCard
    let gauge: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline) {
                Text(card.name ?? card.provider ?? "?").font(.callout).bold()
                if let label = card.label, !label.isEmpty {
                    Text(label).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if let pct = card.displayPercent(gauge: gauge) {
                    Text(String(format: "%.0f%%", pct))
                        .font(.callout)
                        .monospacedDigit()
                        .bold()
                        .foregroundStyle(StatusColor.color(for: card))
                } else {
                    Text("—%").font(.callout).foregroundStyle(.secondary)
                }
                Text(card.statusLabel ?? Self.statusFallback(for: card.status))
                    .font(.caption)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(StatusColor.color(for: card).opacity(0.18))
                    .foregroundStyle(StatusColor.color(for: card))
                    .clipShape(Capsule())
            }
            if let advice = card.advice, !advice.isEmpty {
                Text(advice).font(.caption).foregroundStyle(.secondary)
            }
            // F7: 取得失敗中に最後の成功値を出しているときは、その理由を小さく出す
            if let stale = card.staleError, !stale.isEmpty {
                Text(String(localized: "取得失敗中（最後の値）: \(stale)"))
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
            HStack(spacing: 8) {
                Text(fetchText(now: Date())).font(.caption2).foregroundStyle(.secondary)
                HStack(spacing: 4) {
                    Image(systemName: "arrow.counterclockwise").font(.caption2)
                    Text(resetText).font(.caption2).foregroundStyle(.secondary)
                }
                // F3: 前回の期間の結果
                if let previousText {
                    Text(previousText).font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 2)
    }

    func fetchText(now: Date) -> String {
        guard let fetched = card.fetchedAt else { return String(localized: "取得時刻: 不明") }
        let relative = RelativeDateTimeFormatter().localizedString(for: fetched, relativeTo: now)
        return String(localized: "取得: \(relative)")
    }

    /// F3: 「前回 36% 余り」。gauge=used のときは「前回 64% 使用」。
    private var previousText: String? {
        guard let prev = card.previousUsed else { return nil }
        if gauge == "used" {
            return String(localized: "前回 \(Self.percent(prev)) 使用")
        }
        return String(localized: "前回 \(Self.percent(max(0, 100 - prev))) 余り")
    }

    private static func percent(_ value: Double) -> String {
        String(format: "%.0f%%", value)
    }

    static func statusFallback(for status: String?) -> String {
        switch status {
        case "over": return String(localized: "ペース超過")
        case "exhausted": return String(localized: "枯渇")
        case "tight": return String(localized: "適正ペース")
        case "on_track": return String(localized: "順調")
        case "spare": return String(localized: "余裕あり")
        case "fresh": return String(localized: "リセット直後")
        default: return String(localized: "不明")
        }
    }

    private var resetText: String {
        guard let reset = card.resetsAt else { return String(localized: "リセット時刻: 不明") }
        if reset <= Date() { return String(localized: "リセット済み") }
        let formatter = RelativeDateTimeFormatter()
        return String(localized: "リセットまで あと\(formatter.localizedString(for: reset, relativeTo: Date()))")
    }
}

/// 簡易表示のカード 1 行分: 名前・窓ラベル・%・状態チップ・リセットまでの短い残り時間。
/// advice・取得時刻・前回の結果・取得失敗の詳細は出さず、失敗中は小さな警告アイコンだけ出す。
struct CompactCardRow: View {
    let card: StatusCard
    let gauge: String?

    var body: some View {
        HStack(spacing: 6) {
            Text(card.name ?? card.provider ?? "?")
                .font(.callout)
                .lineLimit(1)
            if let label = card.label, !label.isEmpty {
                Text(label)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            if let stale = card.staleError, !stale.isEmpty {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    .help(stale)
            }
            Spacer(minLength: 8)
            percent
            statusChip
            if let reset = Self.resetRemainingText(card.resetsAt, now: Date()) {
                Text(reset)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private var percent: some View {
        if let pct = card.displayPercent(gauge: gauge) {
            Text(String(format: "%.0f%%", pct))
                .font(.callout)
                .monospacedDigit()
                .bold()
                .foregroundStyle(StatusColor.color(for: card))
        } else {
            Text("—%").font(.callout).foregroundStyle(.secondary)
        }
    }

    private var statusChip: some View {
        Text(card.statusLabel ?? CardRow.statusFallback(for: card.status))
            .font(.caption2)
            .lineLimit(1)
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(StatusColor.color(for: card).opacity(0.18))
            .foregroundStyle(StatusColor.color(for: card))
            .clipShape(Capsule())
    }

    /// 簡易表示用の短いリセット表記。リセット時刻が不明なら nil（何も出さない）。
    static func resetRemainingText(_ reset: Date?, now: Date) -> String? {
        guard let reset else { return nil }
        if reset <= now { return String(localized: "リセット済み") }
        return RelativeDateTimeFormatter().localizedString(for: reset, relativeTo: now)
    }
}

/// ポップオーバー自身の窓が載っている画面の高さを追跡する。NSScreen.main は
/// キーボードフォーカスの画面なので、ポップオーバーの表示先と一致するとは限らない。
/// 窓への接続時と NSWindow.didChangeScreenNotification で更新する。
private struct PopoverScreenReader: NSViewRepresentable {
    @Binding var visibleHeight: CGFloat

    final class TrackingView: NSView {
        var onUpdate: (() -> Void)?
        override func viewDidMoveToWindow() { onUpdate?() }
    }

    final class Coordinator {
        var observer: NSObjectProtocol?
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> NSView {
        let view = TrackingView()
        view.onUpdate = { [weak view] in
            guard let window = view?.window else { return }
            visibleHeight = window.screen?.visibleFrame.height ?? 0
        }
        context.coordinator.observer = NotificationCenter.default.addObserver(
            forName: NSWindow.didChangeScreenNotification, object: nil, queue: .main
        ) { [weak view] note in
            guard let view, let window = view.window,
                  (note.object as? NSWindow) === window else { return }
            visibleHeight = window.screen?.visibleFrame.height ?? 0
        }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        // 更新は viewDidMoveToWindow と通知で行うため、ここでは何もしない。
    }

    static func dismantleNSView(_ nsView: NSView, coordinator: Coordinator) {
        if let observer = coordinator.observer {
            NotificationCenter.default.removeObserver(observer)
        }
    }
}

/// スクロール内容の実の高さを外へ出す（短い内容で余白を出さないための測定）。
private struct ScrollContentHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}
