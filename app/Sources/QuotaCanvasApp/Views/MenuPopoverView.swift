import SwiftUI
import AppKit

/// メニューバーのポップオーバー本体。
struct MenuPopoverView: View {
    @EnvironmentObject private var statusStore: StatusStore
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings

    @State private var pauseBusy = false
    @State private var alertMessage: String?

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
            // エンジン停止（5 分以上更新なし）でも直前のカードは残して見せ、警告を上に出す
            if statusStore.isStale {
                Label("エンジンが動いていません（5 分以上更新がありません）", systemImage: "zzz")
                    .font(.callout)
                    .foregroundStyle(.orange)
                Divider()
            }
            cardList
            balancesSection
            resetCreditsSection
            errorList
        }
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
        let ordered = Self.orderedCards(cards, layout: statusStore.status?.layout)
        ForEach(Array(ordered.enumerated()), id: \.offset) { index, entry in
            CardRow(card: entry.card, gauge: statusStore.status?.gauge)
                .padding(.leading, entry.isSub ? 12 : 0)
            if index != ordered.count - 1 {
                Divider()
            }
        }
    }

    /// 画面の配置（status.json の layout）の左のパネルから順に、主枠 → そのサブ枠の順で並べる。
    /// main が provider ID のときはそのサービスの未使用の最初のカードに当てる。
    /// 配置に無いカードは元の順で末尾に置く。
    static func orderedCards(_ cards: [StatusCard], layout: [LayoutPanel]?) -> [(card: StatusCard, isSub: Bool)] {
        var used = Set<Int>()
        var result: [(card: StatusCard, isSub: Bool)] = []
        func take(_ ref: String?, isSub: Bool) {
            guard let ref, !ref.isEmpty else { return }
            let index = cards.indices.first { !used.contains($0) && cards[$0].id == ref }
                ?? (ref.contains(":") ? nil
                    : cards.indices.first { !used.contains($0) && cards[$0].provider == ref })
            guard let index else { return }
            used.insert(index)
            result.append((cards[index], isSub))
        }
        for panel in layout ?? [] {
            take(panel.main, isSub: false)
            for sub in panel.subs ?? [] { take(sub, isSub: true) }
        }
        for index in cards.indices where !used.contains(index) {
            result.append((cards[index], false))
        }
        return result
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
                        .foregroundStyle(StatusColor.color(for: card.status))
                } else {
                    Text("—%").font(.callout).foregroundStyle(.secondary)
                }
                Text(card.statusLabel ?? statusFallback)
                    .font(.caption)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(StatusColor.color(for: card.status).opacity(0.18))
                    .foregroundStyle(StatusColor.color(for: card.status))
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

    private var statusFallback: String {
        switch card.status {
        case "over": return String(localized: "ペース超過")
        case "exhausted": return String(localized: "枯渇")
        case "tight": return String(localized: "使い切り")
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
