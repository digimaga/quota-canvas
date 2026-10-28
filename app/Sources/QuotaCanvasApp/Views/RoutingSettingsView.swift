import SwiftUI
import AppKit

/// 「振り分け」タブ: AI ワーカー振り分け判定（config.toml の [routing]）の設定。
/// 判定そのものはエンジン担当。アプリは config --set-json で設定を保存し、
/// status.json の "routing" で現在の判定を表示し、`routing mode` CLI で
/// モードを切り替えるだけ（app/README.md の方針どおり直接通信しない）。
/// 設定保存はアプリ共通の直列化された経路を使う。
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
    @State private var showResetConfirm = false

    // 現在の判定（status.json の "routing"）とモード一覧（CLI）
    @State private var verdicts: [RoutingWorkerStatus] = []
    @State private var verdictTimestamp: Date?
    @State private var hasRoutingStatus = false // status.json に "routing" がある
    @State private var usableWindows: [String] = []
    @State private var modeEntries: [RoutingModeEntry] = []
    @State private var modeLoaded = false  // モード一覧を一度でも取れた
    @State private var modeGeneration = 0  // 古いモード一覧の応答を捨てる用
    @State private var modeBusy = false

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
        .task {
            await load()
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 60_000_000_000) } catch { break }
                await reloadVerdicts()
            }
        }
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
            Toggle("AI ワーカーの振り分け判定を使う", isOn: Binding(
                get: { routingOn }, set: { value in
                    routingOn = value
                    saveRouting { $0.routingEnabled = value }
                }))
            Text("残り % に応じてワーカーごとに「使ってよし / 控える / 止める」の判定を出します。エンジン自身は何も止めず、プラグイン経由で Claude Code・Codex のセッション開始時に結果を渡せます。")
                .font(.caption).foregroundStyle(.secondary)
            Stepper(value: Binding(get: { floorPercent }, set: { value in
                floorPercent = value
                saveRouting { $0.routingFloorPercent = value }
            }), in: 0...50, step: 1) {
                HStack {
                    Text("止める下限（残り %）")
                    Spacer()
                    Text("\(Int(floorPercent))").monospacedDigit().foregroundStyle(.secondary)
                }
            }
            Stepper(value: Binding(get: { staleMinutes }, set: { value in
                staleMinutes = value
                saveRouting { $0.routingStaleMinutes = value }
            }), in: 5...120, step: 5) {
                HStack {
                    Text("古いとみなす（分）")
                    Spacer()
                    Text("\(staleMinutes)").monospacedDigit().foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: - 現在の判定（status.json の "routing"）

    private var verdictSection: some View {
        Section("現在の判定") {
            if hasRoutingStatus && Self.verdictIsStale(verdictTimestamp, now: Date()) {
                Text("判定の更新が古いか、更新時刻が不明です").font(.caption).foregroundStyle(.orange)
            }
            if !routingOn {
                Text("判定は無効です。「AI ワーカーの振り分け判定を使う」をオンにすると、エンジンの次の更新から判定が現れます。")
                    .font(.callout).foregroundStyle(.secondary)
                modeOnlyRows
            } else if !hasRoutingStatus {
                Text("判定はまだ status.json にありません（エンジンの更新待ち）。")
                    .font(.callout).foregroundStyle(.secondary)
                modeOnlyRows
            } else if verdicts.isEmpty {
                Text("判定できるワーカーがありません").font(.callout).foregroundStyle(.secondary)
                modeOnlyRows
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
            if modeLoaded {
                Picker("モード", selection: modeBinding(worker)) {
                    Text("標準").tag("standard")
                    Text("使い切り").tag("burn")
                    Text("監視のみ").tag("off")
                }
                .pickerStyle(.segmented)
                .disabled(modeBusy || !modeEntries.contains { $0.name == worker.name })
            } else {
                Text("モードを取得中…").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 1)
    }

    /// 判定がなくても CLI のモード一覧を表示し、変更・解除できる。
    @ViewBuilder
    private var modeOnlyRows: some View {
        if modeLoaded {
            ForEach(modeEntries, id: \.name) { entry in
                if let name = entry.name {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(name).font(.callout)
                            if entry.mode == "burn", let until = entry.untilDate {
                                Text(String(localized: "使い切り \(Self.hhmm(until)) まで"))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            if entry.mode != "standard" {
                                Button("解除") { setMode(named: name, mode: "standard") }
                                    .controlSize(.small).disabled(modeBusy)
                            }
                        }
                        Picker("モード", selection: Binding(
                            get: { modeEntries.first { $0.name == name }?.mode ?? "standard" },
                            set: { setMode(named: name, mode: $0) })) {
                            Text("標準").tag("standard")
                            Text("使い切り").tag("burn")
                            Text("監視のみ").tag("off")
                        }
                        .pickerStyle(.segmented).disabled(modeBusy)
                    }
                }
            }
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

    /// 理由の短い日本語文 + 使い切りモードの期限（期限はモード一覧（CLI）に合わせる。
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
        case "disabled": reason = String(localized: "監視のみ（振り分けで使わない）")
        case "stale": reason = String(localized: "取得データが古い")
        case "missing": reason = String(localized: "窓が見つからない")
        case "missing_status": reason = String(localized: "status.json がない・古い")
        case "error": reason = String(localized: "取得エラー")
        case "broken": reason = String(localized: "status.json を読めない")
        default: reason = String(localized: "判定できない")
        }
        if let name = worker.name,
           let entry = modeEntries.first(where: { $0.name == name && $0.mode == "burn" }),
           let until = entry.untilDate {
            return reason + " · " + String(localized: "使い切り \(Self.hhmm(until)) まで")
        }
        return reason
    }

    /// CLI の一覧を真実値とし、保存後に判定を更新する。
    private func modeBinding(_ worker: RoutingWorkerStatus) -> Binding<String> {
        Binding(
            get: { modeEntries.first { $0.name == worker.name }?.mode ?? "standard" },
            set: { mode in
                if let name = worker.name { setMode(named: name, mode: mode) }
            }
        )
    }

    private func setMode(named name: String, mode: String) {
        guard !name.isEmpty else { return }
        modeBusy = true
        Task {
            defer { modeBusy = false }
            do {
                let list = try await client.setRoutingMode(name: name, mode: mode)
                modeGeneration += 1
                modeEntries = list
                modeLoaded = true
                try? await client.refresh()
                EngineClient.scheduleArtifactReload()
            } catch {
                alertMessage = error.localizedDescription
                await reloadModes()
            }
        }
    }

    private func reloadModes() async {
        modeGeneration += 1
        let generation = modeGeneration
        do {
            let list = try await client.routingModes()
            guard generation == modeGeneration else { return }
            modeEntries = list
            modeLoaded = true
        } catch {
            // 取得失敗時は前回の一覧を保つ。
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
                Button("ワーカーを追加") { rows.append(WorkerRow(name: "", windowsText: "")) }
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
        let workers = normalizedRows
        saveWorkersValue(workers)
    }

    private func revertWorkers() {
        rows = savedWorkers.map {
            WorkerRow(name: $0.name, windowsText: $0.windows.joined(separator: ","))
        }
    }

    private func resetWorkersToDefault() {
        rows = []
        saveWorkersValue([])
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
        } catch {
            loadFailed = true
            alertMessage = error.localizedDescription
        }
        await reloadVerdicts()
    }

    /// 現在の判定（status.json の "routing" / "windows"）とモード一覧（CLI）を取り直す。
    /// モード一覧は世代番号で古い応答を捨てる。失敗時は前回の一覧を保つ。
    private func reloadVerdicts() async {
        modeGeneration += 1
        let generation = modeGeneration
        let snapshot = StatusStore.loadStatusSnapshot()
        verdictTimestamp = snapshot?.routing?.at
        hasRoutingStatus = snapshot?.routing != nil
        verdicts = snapshot?.routing?.workers ?? []
        usableWindows = (snapshot?.windows ?? []).compactMap { window in
            window.partOf == nil ? window.id : nil
        }
        do {
            let list = try await client.routingModes()
            guard generation == modeGeneration else { return } // 古い応答は捨てる
            modeEntries = list
            modeLoaded = true
        } catch {
            // 取得失敗時は前回の一覧のまま
        }
    }

    // 変更した項目だけを、アプリ共通の直列化された保存経路に渡す。
    private func saveRouting(_ mutate: @escaping (inout ConfigDocument) -> Void) {
        guard doc != nil else { return }
        Task {
            do {
                _ = try await client.updateConfig(mutate)
                EngineClient.scheduleArtifactReload()
            } catch { alertMessage = error.localizedDescription }
        }
    }

    private func saveWorkersValue(_ workers: [ConfigDocument.RoutingWorker]) {
        guard doc != nil else { return }
        busy = true
        Task {
            defer { busy = false }
            do {
                _ = try await client.updateConfig { $0.routingWorkers = workers }
                savedWorkers = workers
                EngineClient.scheduleArtifactReload()
            } catch { alertMessage = error.localizedDescription }
        }
    }

    static func verdictIsStale(_ timestamp: Date?, now: Date) -> Bool {
        guard let timestamp else { return true }
        return now.timeIntervalSince(timestamp) > 5 * 60
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
