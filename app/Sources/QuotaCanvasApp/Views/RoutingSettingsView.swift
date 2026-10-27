import SwiftUI
import AppKit

/// 「振り分け」タブ: AI ワーカー振り分け判定（config.toml の [routing]）の設定。
/// 判定そのものはエンジン担当。アプリは config --set-json で設定を保存し、
/// status.json の "routing" で現在の判定を表示し、`routing burn` CLI で
/// 使い切りモードを切り替えるだけ（app/README.md の方針どおり直接通信しない）。
/// 保存は 1 本に直列化し、そのたびに `config --json` を取り直して routing の
/// キーだけを上書きして送る（他のタブが保存した値を古い値で上書きしないため）。
struct RoutingSettingsView: View {
    @State private var doc: ConfigDocument?
    @State private var loadFailed = false
    @State private var busy = false // 保存（config --set-json）の実行中
    @State private var alertMessage: String?

    // UI 状態（doc 読み込み後に反映）
    @State private var routingOn = false
    @State private var floorPercent: Double = 5
    @State private var staleMinutes = 20

    // ワーカーの編集（保存ボタンで明示的に保存する）
    @State private var rows: [WorkerRow] = []
    @State private var savedWorkers: [ConfigDocument.RoutingWorker] = []
    @State private var pendingWorkers: [ConfigDocument.RoutingWorker]? // 保存予定（保存ボタンで設定）
    @State private var saveRunning = false
    @State private var saveQueued = false
    @State private var showResetConfirm = false

    // 現在の判定（status.json の "routing"）と burn 一覧（CLI）
    @State private var verdicts: [RoutingWorkerStatus] = []
    @State private var hasRoutingStatus = false // status.json に "routing" がある
    @State private var usableWindows: [String] = []
    @State private var burnEntries: [RoutingBurnEntry] = []
    @State private var burnLoaded = false  // burn 一覧を一度でも取れた
    @State private var burnGeneration = 0  // 古い burn 一覧の応答を捨てる用
    @State private var burnBusy = false

    private let client = EngineClient()

    /// ワーカー行の編集モデル。窓 id はカンマ区切りのテキストのまま編集する。
    struct WorkerRow: Identifiable {
        let id = UUID()
        var name: String
        var windowsText: String
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if doc != nil {
                Form {
                    toggleSection
                    verdictSection
                    workersSection
                    pluginSection
                }
                .formStyle(.grouped)
                .disabled(busy)
                .confirmationDialog("既定に戻す（サービスごと）", isPresented: $showResetConfirm,
                                    titleVisibility: .visible) {
                    Button("すべてのワーカーを削除", role: .destructive) {
                        resetWorkersToDefault()
                    }
                    Button("キャンセル", role: .cancel) {}
                } message: {
                    Text("routing.workers を空にします。判定は取得できているサービスごとの既定ワーカーに戻ります。")
                }
            } else if loadFailed {
                ContentUnavailableView("設定を取得できませんでした",
                                       systemImage: "exclamationmark.triangle",
                                       description: Text("エンジンのパスを確認し、ウィンドウを開き直してください。"))
            } else {
                ProgressView("設定を読み込み中…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .task { await load() }
        .onReceive(NotificationCenter.default.publisher(for: .engineArtifactsChanged)) { _ in
            Task { await reloadVerdicts() } // 設定保存 2 秒後などに判定を取り直す
        }
        .alert("エラー", isPresented: Binding(
            get: { alertMessage != nil },
            set: { if !$0 { alertMessage = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(alertMessage ?? "")
        }
    }

    // MARK: - 振り分け判定（[routing] の基本設定）

    private var toggleSection: some View {
        Section("振り分け判定") {
            Toggle("AI ワーカーの振り分け判定を使う", isOn: $routingOn)
                .onChange(of: routingOn) { _, _ in enqueueSave() }
            Text("残り % に応じてワーカーごとに「使ってよし / 控える / 止める」の判定を出します。エンジン自身は何も止めず、プラグイン経由で Claude Code・Codex のセッション開始時に結果を渡せます。")
                .font(.caption).foregroundStyle(.secondary)
            Stepper(value: $floorPercent, in: 0...50, step: 1) {
                HStack {
                    Text("止める下限（残り %）")
                    Spacer()
                    Text("\(Int(floorPercent))").monospacedDigit().foregroundStyle(.secondary)
                }
            }
            .onChange(of: floorPercent) { _, _ in enqueueSave() }
            Stepper(value: $staleMinutes, in: 5...120, step: 5) {
                HStack {
                    Text("古いとみなす（分）")
                    Spacer()
                    Text("\(staleMinutes)").monospacedDigit().foregroundStyle(.secondary)
                }
            }
            .onChange(of: staleMinutes) { _, _ in enqueueSave() }
        }
    }

    // MARK: - 現在の判定（status.json の "routing"）

    private var verdictSection: some View {
        Section("現在の判定") {
            if !routingOn {
                Text("判定は無効です。「AI ワーカーの振り分け判定を使う」をオンにすると、エンジンの次の更新から判定が現れます。")
                    .font(.callout).foregroundStyle(.secondary)
                burnOnlyRows
            } else if !hasRoutingStatus {
                Text("判定はまだ status.json にありません（エンジンの更新待ち）。")
                    .font(.callout).foregroundStyle(.secondary)
                burnOnlyRows
            } else if verdicts.isEmpty {
                Text("判定できるワーカーがありません").font(.callout).foregroundStyle(.secondary)
            } else {
                ForEach(verdicts) { worker in
                    verdictRow(worker)
                }
            }
        }
    }

    private func verdictRow(_ worker: RoutingWorkerStatus) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(worker.name ?? "?").font(.callout).bold()
                Text(verdictLabel(worker.verdict))
                    .font(.caption).bold()
                    .foregroundStyle(verdictColor(worker.verdict))
                if let remaining = worker.remaining {
                    Text(String(format: "%.0f%%", remaining))
                        .font(.caption).monospacedDigit().foregroundStyle(.secondary)
                }
                if worker.stale == true {
                    Text("データが古い").font(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
            }
            Text(reasonText(worker)).font(.caption).foregroundStyle(.secondary)
            // burn 一覧が取れるまでトグルは出さない（実態とずれないように）
            if burnLoaded {
                Toggle("使い切り（今日 23:59 まで）", isOn: burnBinding(worker))
                    .font(.caption)
                    .disabled(burnBusy)
            } else {
                Text("使い切り状態を取得中…").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 1)
    }

    /// 判定オフや判定なしのときでも、有効な burn があれば一覧と「解除」を出す。
    @ViewBuilder
    private var burnOnlyRows: some View {
        if burnLoaded && !activeBurns.isEmpty {
            ForEach(activeBurns, id: \.name) { burn in
                HStack {
                    Text(burn.name).font(.callout)
                    if let until = burn.until {
                        Text(String(localized: "使い切り \(Self.hhmm(until)) まで"))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("解除") { setBurn(named: burn.name, on: false) }
                        .controlSize(.small)
                        .disabled(burnBusy)
                }
            }
        }
    }

    private var activeBurns: [(name: String, until: Date?)] {
        burnEntries.compactMap { entry in
            guard let name = entry.name, !name.isEmpty else { return nil }
            return (name, entry.untilDate)
        }
    }

    /// 判定の色（既存の状態色に合わせる）: stop=赤 / reserve=黄 / go=紫 / boost=緑 / unknown=灰。
    private func verdictColor(_ verdict: String?) -> Color {
        switch verdict {
        case "stop": return .red
        case "reserve": return .yellow
        case "go": return .purple
        case "boost": return .green
        default: return .gray
        }
    }

    private func verdictLabel(_ verdict: String?) -> String {
        switch verdict {
        case "stop": return String(localized: "止める")
        case "reserve": return String(localized: "控える")
        case "go": return String(localized: "使ってよし")
        case "boost": return String(localized: "積極的に")
        default: return String(localized: "判定できず")
        }
    }

    /// 理由の短い日本語文 + 使い切りモードの期限（期限は burn 一覧（CLI）に合わせる。
    /// status.json の burn_until は再描画まで更新されないため）。
    private func reasonText(_ worker: RoutingWorkerStatus) -> String {
        let reason: String
        switch worker.reason {
        case "exhausted": reason = String(localized: "残り枠がない")
        case "below_floor": reason = String(localized: "残りが下限を切った")
        case "over": reason = String(localized: "ペース超過")
        case "over_near_reset": reason = String(localized: "リセット直前のペース超過（使い切ってよい）")
        case "tight": reason = String(localized: "ギリギリ")
        case "on_track": reason = String(localized: "順調")
        case "spare": reason = String(localized: "余裕あり")
        case "fresh": reason = String(localized: "リセット直後")
        case "burn": reason = String(localized: "使い切りモード中")
        case "stale": reason = String(localized: "取得データが古い")
        case "missing": reason = String(localized: "窓が見つからない")
        case "missing_status": reason = String(localized: "status.json がない・古い")
        case "error": reason = String(localized: "取得エラー")
        case "broken": reason = String(localized: "status.json を読めない")
        default: reason = String(localized: "判定できない")
        }
        if let name = worker.name,
           let entry = burnEntries.first(where: { $0.name == name }),
           let until = entry.untilDate {
            return reason + " · " + String(localized: "使い切り \(Self.hhmm(until)) まで")
        }
        return reason
    }

    /// トグルの状態は burn 一覧（CLI の応答）が真実値。切り替えたら CLI を呼び、
    /// 返ってきた一覧で状態を更新し、status.json を早めに取り直させる。
    private func burnBinding(_ worker: RoutingWorkerStatus) -> Binding<Bool> {
        Binding(
            get: { burnEntries.contains { $0.name == worker.name } },
            set: { on in
                if let name = worker.name { setBurn(named: name, on: on) }
            }
        )
    }

    private func setBurn(named name: String, on: Bool) {
        guard !name.isEmpty else { return }
        burnBusy = true
        Task {
            defer { burnBusy = false }
            do {
                let list = try await client.setRoutingBurn(name: name, on: on)
                // 代入前に世代を進め、飛行中の古い一覧取得がこの結果を
                // 古い一覧で上書きしないようにする。
                burnGeneration += 1
                burnEntries = list
                burnLoaded = true
                // エンジンに再描画を依頼し（失敗しても burn 自体は保存済み）、
                // 2 秒後に status.json を読み直して判定を取り直す。
                try? await client.refresh()
                EngineClient.scheduleArtifactReload()
            } catch {
                alertMessage = error.localizedDescription
                await reloadBurns() // 失敗したら実態に戻す
            }
        }
    }

    /// burn 一覧を取り直す。世代番号で、古い応答は捨てる。失敗時は前回の一覧を保つ。
    private func reloadBurns() async {
        burnGeneration += 1
        let generation = burnGeneration
        do {
            let list = try await client.routingBurnList()
            guard generation == burnGeneration else { return }
            burnEntries = list
            burnLoaded = true
        } catch {
            // 取得失敗時は前回の一覧のまま（表示を実態とずらさない優先度は低い）
        }
    }

    // MARK: - ワーカーの設定（routing.workers、保存ボタンで明示的に保存）

    private var workersSection: some View {
        Section("ワーカーの設定") {
            Text("ワーカーごとに判定に使う窓 id を決めます（上から順に判定に並びます）。ワーカーが無ければ、取得できているサービスごとに 1 ワーカーになります。")
                .font(.caption).foregroundStyle(.secondary)
            ForEach($rows) { rowBinding in
                workerEditor(rowBinding)
            }
            if let blocker = workersBlocker {
                Text(blocker).font(.caption).foregroundStyle(.red)
            }
            HStack {
                Button("保存") { saveWorkers() }
                    .disabled(!canSaveWorkers)
                Button("元に戻す") { revertWorkers() }
                    .disabled(!workersDirty)
                Spacer()
                Button("既定に戻す（サービスごと）", role: .destructive) {
                    showResetConfirm = true
                }
                .disabled(savedWorkers.isEmpty && rows.isEmpty)
            }
            Text("変更は「保存」で反映されます。")
                .font(.caption2).foregroundStyle(.secondary)
            if !usableWindows.isEmpty {
                Text(String(localized: "使える窓 id: \(usableWindows.joined(separator: ", "))"))
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private func workerEditor(_ row: Binding<WorkerRow>) -> some View {
        HStack(spacing: 6) {
            TextField("名前", text: row.name)
                .textFieldStyle(.squareBorder)
                .frame(width: 120)
            TextField("窓 id（カンマ区切り、末尾 * で前方一致）", text: row.windowsText)
                .textFieldStyle(.squareBorder)
            VStack(spacing: 0) {
                Button {
                    moveRow(row.wrappedValue.id, -1)
                } label: {
                    Image(systemName: "chevron.up")
                }
                .buttonStyle(.borderless)
                .controlSize(.small)
                .help(Text("上へ"))
                Button {
                    moveRow(row.wrappedValue.id, 1)
                } label: {
                    Image(systemName: "chevron.down")
                }
                .buttonStyle(.borderless)
                .controlSize(.small)
                .help(Text("下へ"))
            }
            Button(role: .destructive) {
                rows.removeAll { $0.id == row.wrappedValue.id }
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
            .controlSize(.small)
            .help(Text("削除"))
        }
    }

    private func moveRow(_ id: UUID, _ delta: Int) {
        guard let index = rows.firstIndex(where: { $0.id == id }) else { return }
        let target = index + delta
        guard rows.indices.contains(target) else { return }
        rows.swapAt(index, target)
    }

    /// 現在の行を正規化（前後の空白を除き、窓 id をカンマ区切りで分割）したもの。
    private var normalizedRows: [ConfigDocument.RoutingWorker] {
        rows.map { row in
            ConfigDocument.RoutingWorker(
                name: row.name.trimmingCharacters(in: .whitespaces),
                windows: row.windowsText.split(separator: ",")
                    .map { $0.trimmingCharacters(in: .whitespaces) }
                    .filter { !$0.isEmpty })
        }
    }

    /// 保存できないときの理由（保存ボタンを無効にして表示する）。nil = 保存できる内容。
    private var workersBlocker: String? {
        for row in rows {
            let name = row.name.trimmingCharacters(in: .whitespaces)
            let ids = row.windowsText.split(separator: ",")
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            if name.isEmpty && ids.isEmpty {
                return String(localized: "空の行があります。入力するか削除してください。")
            }
            if !Self.isValidWorkerName(name) {
                return String(localized: "名前は半角英数字と . _ - の 1〜40 文字にしてください")
            }
            if ids.isEmpty {
                return String(localized: "ワーカー「\(name)」に窓 id を入力してください（カンマ区切り）")
            }
        }
        let names = normalizedRows.map(\.name)
        if Set(names).count != names.count {
            return String(localized: "ワーカー名が重複しています")
        }
        return nil
    }

    private var workersDirty: Bool { normalizedRows != savedWorkers }
    private var canSaveWorkers: Bool { workersBlocker == nil && workersDirty }

    private func saveWorkers() {
        guard canSaveWorkers else { return }
        pendingWorkers = normalizedRows
        enqueueSave()
    }

    private func revertWorkers() {
        pendingWorkers = nil // 失敗して残った保存予定があれば捨てる
        rows = savedWorkers.map {
            WorkerRow(name: $0.name, windowsText: $0.windows.joined(separator: ","))
        }
    }

    private func resetWorkersToDefault() {
        rows = []
        pendingWorkers = [] // workers を空にして保存（= サービスごとの既定ワーカー）
        enqueueSave()
    }

    /// 名前は ^[A-Za-z0-9._-]{1,40}$（エンジンの gate.WORKER_NAME_RE と同じ）。
    private static let nameRegex = try! NSRegularExpression(pattern: "^[A-Za-z0-9._-]{1,40}$")

    static func isValidWorkerName(_ name: String) -> Bool {
        nameRegex.firstMatch(in: name, range: NSRange(name.startIndex..., in: name)) != nil
    }

    // MARK: - Claude Code / Codex への組み込み（プラグイン）

    private var pluginSection: some View {
        Section("Claude Code / Codex への組み込み") {
            pluginRow(title: "Claude Code", commands: [
                "/plugin marketplace add digimaga/quota-canvas",
                "/plugin install quota-canvas-routing@quota-canvas",
            ])
            pluginRow(title: "Codex", commands: [
                "codex plugin marketplace add digimaga/quota-canvas",
                "codex plugin add quota-canvas-routing@quota-canvas",
            ])
            Text("設定ファイル（settings.json・CLAUDE.md・AGENTS.md）は書き換えません。上のコマンドを各 CLI で実行してください。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func pluginRow(title: String, commands: [String]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(title).font(.callout)
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(commands.joined(separator: "\n"), forType: .string)
                } label: {
                    Label("コピー", systemImage: "doc.on.doc")
                }
                .controlSize(.small)
            }
            ForEach(commands, id: \.self) { command in
                Text(command)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    // MARK: - load

    private func load() async {
        do {
            let fetched = try await client.getConfig()
            doc = fetched
            loadFailed = false
            routingOn = fetched.routingEnabled
            floorPercent = fetched.routingFloorPercent
            staleMinutes = fetched.routingStaleMinutes
            savedWorkers = fetched.routingWorkers
            rows = savedWorkers.map {
                WorkerRow(name: $0.name, windowsText: $0.windows.joined(separator: ","))
            }
            pendingWorkers = nil
        } catch {
            loadFailed = true
            alertMessage = error.localizedDescription
        }
        await reloadVerdicts()
    }

    /// 現在の判定（status.json の "routing" / "windows"）と burn 一覧（CLI）を取り直す。
    /// burn 一覧は世代番号で古い応答を捨てる。失敗時は前回の一覧を保つ。
    private func reloadVerdicts() async {
        burnGeneration += 1
        let generation = burnGeneration
        let snapshot = StatusStore.loadStatusSnapshot()
        hasRoutingStatus = snapshot?.routing != nil
        verdicts = snapshot?.routing?.workers ?? []
        usableWindows = (snapshot?.windows ?? []).compactMap { window in
            window.partOf == nil ? window.id : nil
        }
        do {
            let list = try await client.routingBurnList()
            guard generation == burnGeneration else { return } // 古い応答は捨てる
            burnEntries = list
            burnLoaded = true
        } catch {
            // 取得失敗時は前回の一覧のまま
        }
    }

    // MARK: - save（1 本に直列化）

    /// 保存を直列化するキューに入れる。実行中のあいかに来た要求は最新の 1 件だけを残す。
    private func enqueueSave() {
        saveQueued = true
        runQueuedSave()
    }

    private func runQueuedSave() {
        guard !saveRunning, saveQueued else { return }
        saveQueued = false
        saveRunning = true
        Task {
            await performSave()
            saveRunning = false
            runQueuedSave() // 待っていた最新の 1 件を送る
        }
    }

    /// 保存のたびに `config --json` を取り直し、routing のキー（enabled /
    /// floor_percent / stale_minutes / 開始時に保留だった workers）だけを
    /// 上書きして送る。他のタブが保存した値を古い値で上書きしないため。
    /// workers を送るのは「保存」「既定に戻す」の保存だけ（開始時に
    /// pendingWorkers があった保存）。トグル・Stepper の保存は触らない。
    private func performSave() async {
        guard doc != nil else { return }
        busy = true
        defer { busy = false }
        let sendWorkers = pendingWorkers // この保存が運ぶのは開始時に保留だった内容だけ
        do {
            var updated = try await client.getConfig()
            updated.routingEnabled = routingOn
            updated.routingFloorPercent = floorPercent
            updated.routingStaleMinutes = staleMinutes
            var includesWorkers = false
            if let pending = sendWorkers {
                updated.routingWorkers = pending
                includesWorkers = true
            }
            let saved = try await client.setConfig(updated)
            if sendWorkers != nil {
                pendingWorkers = nil // 運んだ保存予定を消す（実行中に押された分は温存）
            }
            doc = saved
            if includesWorkers {
                savedWorkers = saved.routingWorkers
                rows = savedWorkers.map {
                    WorkerRow(name: $0.name, windowsText: $0.windows.joined(separator: ","))
                }
            }
            EngineClient.scheduleArtifactReload() // 2 秒後に status.json を読み直させる
        } catch {
            // 失敗した保存予定は捨てる。残しておくと、次のトグル・Stepper の
            // 保存が古い workers を書いてしまう。
            pendingWorkers = nil
            alertMessage = error.localizedDescription
        }
    }

    // MARK: - time helpers

    private static let hhmmFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX") // 保存/パース用の固定書式
        f.dateFormat = "HH:mm"
        return f
    }()

    private static func hhmm(_ date: Date) -> String { hhmmFormatter.string(from: date) }
}
