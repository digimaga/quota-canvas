import SwiftUI
import AppKit

/// 「サービス」タブ: detect --json の一覧 + providers 設定 + API キー管理。
struct ServiceSettingsView: View {
    @Environment(\.openWindow) private var openWindow
    @State private var doc: ConfigDocument?
    /// 基準値: 最後にエンジンで確認できた設定（読み込み・保存・読み直しの成功）。
    /// 編集中の値（doc）と分けて持ち、保存は「基準値 → 編集中の値」の差分で送る。
    /// 保存と読み直しの両方が失敗しても、未保存の編集が次の保存の差分に入る。
    @State private var synced: ConfigDocument?
    @State private var detect: [DetectService] = []
    @State private var loadingDetect = false
    @State private var loadFailed = false
    @State private var busy = false
    @State private var alertMessage: String?
    @State private var keyDrafts: [String: String] = [:]
    @State private var statusColors: [String: String] = [:]   // status.json の colors（既定色込み）

    private let client = EngineClient()

    /// 自動配置で画面に出るサービスの上限（エンジンの render.MAX_SERVICES と同じ値）
    static let maxAutoServices = 8

    var body: some View {
        VStack(spacing: 0) {
            if doc == nil && loadFailed {
                ContentUnavailableView("設定を取得できませんでした",
                                       systemImage: "exclamationmark.triangle",
                                       description: Text("エンジンのパスを「表示」タブで確認してください。"))
            } else if doc == nil {
                ProgressView("設定を読み込み中…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                list
            }
        }
        .task { await load(includeDetect: true) }
        .onReceive(NotificationCenter.default.publisher(for: .museUsageUpdated)) { _ in
            Task { await reloadDetect() } // muse-usage.json 更新・削除後は再検出する
        }
        .safeAreaInset(edge: .bottom) {
            HStack {
                Text("自動配置で画面に出るのは最大 \(Self.maxAutoServices) サービスです（「配置」タブの手動配置なら最大 \(LayoutSettingsView.columnCount * 2) 枚）")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button {
                    Task { await reloadDetect() }
                } label: {
                    HStack(spacing: 4) {
                        if loadingDetect { ProgressView().controlSize(.small) }
                        Text("再検出")
                    }
                }
                .disabled(loadingDetect || busy)
            }
            .padding(.horizontal, 16).padding(.vertical, 8)
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

    private var list: some View {
        Form {
            Section {
                if loadingDetect && detect.isEmpty {
                    HStack { ProgressView().controlSize(.small); Text("サービスを検出中…（利用状況を 1 回取得するため時間がかかります）") }
                        .foregroundStyle(.secondary)
                } else if detect.isEmpty {
                    Text("サービスを検出できませんでした。「再検出」を試してください。").foregroundStyle(.secondary)
                }
                ForEach(detect) { service in
                    ServiceRow(
                        service: service,
                        doc: doc,
                        keyDraft: Binding(
                            get: { keyDrafts[service.provider] ?? "" },
                            set: { keyDrafts[service.provider] = $0 }
                        ),
                        enabledBinding: enabledBinding(service),
                        windowsBinding: windowsBinding(service),
                        tokenRefreshBinding: tokenRefreshBinding(service),
                        colorBinding: colorBinding(service),
                        hasCustomColor: doc?.providerColor(service.provider) != nil,
                        onSaveKey: { Task { await saveKey(service) } },
                        onDeleteKey: { Task { await deleteKey(service) } },
                        onMuseLogin: { openWindow(id: MuseLoginView.windowID) },
                        onResetColor: { resetColor(service) },
                        busy: busy || loadingDetect
                    )
                }
            } header: {
                Text("検出されたサービス")
            } footer: {
                Text("チェックした窓だけが画面に表示されます。すべてチェック（既定）でそのサービスの全窓を表示します。")
            }
        }
        .formStyle(.grouped)
        .disabled(busy)
    }

    /// ColorPicker のバインディング。初期値は status.json の colors（既定色込み）→
    /// 設定済みの providers.<p>.color を優先。変更で "#RRGGBB" を保存。
    private func colorBinding(_ service: DetectService) -> Binding<Color> {
        Binding<Color>(
            get: {
                let hex = doc?.providerColor(service.provider) ?? statusColors[service.provider]
                return HexColor.color(hex) ?? Color(nsColor: .systemGray)
            },
            set: { newColor in
                guard var updated = doc else { return }
                updated.setProviderColor(service.provider, HexColor.hex(newColor))
                doc = updated
                Task { await save(updated) }
            }
        )
    }

    /// 「既定の色に戻す」: providers.<p>.color の color キーを削除して保存。
    private func resetColor(_ service: DetectService) {
        guard var updated = doc else { return }
        updated.removeProviderColor(service.provider)
        doc = updated
        Task { await save(updated) }
    }

    // MARK: bindings

    private func enabledBinding(_ service: DetectService) -> Binding<Bool> {
        Binding<Bool>(
            get: { doc?.providerEnabled(service.provider) ?? false },
            set: { newValue in
                guard var updated = doc else { return }
                updated.setProviderEnabled(service.provider, newValue)
                doc = updated
                Task { await save(updated) }
            }
        )
    }

    /// 窓チェックボックスのバインディング。空配列 = 全チェック扱い（コントラクト）。
    private func windowsBinding(_ service: DetectService) -> Binding<Set<String>> {
        Binding<Set<String>>(
            get: {
                let saved = Set(doc?.providerWindows(service.provider) ?? [])
                let all = Set(service.windowList.map(\.key))
                if saved.isEmpty { return all } // [] = すべて
                return saved
            },
            set: { selected in
                guard var updated = doc else { return }
                let keys = Self.windowKeysForSave(
                    selected: selected, all: Set(service.windowList.map(\.key)))
                updated.setProviderWindows(service.provider, keys)
                doc = updated
                Task { await save(updated) }
            }
        )
    }

    /// config.toml に書く窓キーの配列。Set の並びのまま書くと保存ごとに順番が
    /// 変わって同じ内容の差分が付くので、必ずソートする。
    /// 全選択は空配列（= すべて、コントラクト）。
    static func windowKeysForSave(selected: Set<String>, all: Set<String>) -> [String] {
        selected == all ? [] : selected.sorted()
    }

    /// トークン自動更新のバインディング（providers.claude.token_refresh）。
    /// オン = "cli"、オフ = "off"、未設定はオン扱い。Claude の行のみ表示するので nil を返す。
    private func tokenRefreshBinding(_ service: DetectService) -> Binding<Bool>? {
        guard service.provider == "claude" else { return nil }
        return Binding<Bool>(
            get: { doc?.providerTokenRefresh(service.provider) != "off" },
            set: { on in
                guard var updated = doc else { return }
                updated.setProviderTokenRefresh(service.provider, on ? "cli" : "off")
                doc = updated
                Task { await save(updated) }
            }
        )
    }

    // MARK: actions

    private func load(includeDetect: Bool) async {
        do {
            let fetched = try await client.getConfig()
            doc = fetched
            synced = fetched
            loadFailed = false
            statusColors = StatusStore.loadStatusSnapshot()?.colors ?? [:]
            if includeDetect { await loadCachedDetect() }
        } catch {
            loadFailed = true
            alertMessage = error.localizedDescription
        }
    }

    /// 開いたときの即表示: 常駐エンジンが書いた detect-cache.json を使う
    /// (キャッシュが無い・壊れている初回だけエンジン側で全取得する)。
    private func loadCachedDetect() async {
        loadingDetect = true
        defer { loadingDetect = false }
        do {
            detect = try await client.detectCached().services
        } catch {
            detect = []
            alertMessage = error.localizedDescription
        }
    }

    /// 「再検出」や API キー保存/削除・Muse ログイン後の最新化: 全サービスを
    /// その場で取得する従来の detect() を使う (エンジンがキャッシュも更新する)。
    private func reloadDetect() async {
        loadingDetect = true
        defer { loadingDetect = false }
        do {
            detect = try await client.detect()
        } catch {
            detect = []
            alertMessage = error.localizedDescription
        }
    }

    /// 保存と失敗時の復帰を 1 か所に: 成功なら保存後の設定を返す。失敗したら
    /// 実際の設定の読み直しを試み、その結果（読み直せなければ nil）を返す。
    /// 失敗した編集を画面（doc）に残して保存の original に使うと差分が消え、
    /// 二度と送られなくなるため（config --apply-json の差分適用）。
    /// 読み直しも失敗したときは nil を返し、呼び出し側は基準値（synced）と
    /// 編集中の値をそのまま残して、未保存の編集を次の保存の差分に含める。
    static func saveAndResync(
        save: () async throws -> ConfigDocument,
        reload: () async -> ConfigDocument?
    ) async -> (doc: ConfigDocument?, failure: Error?) {
        do {
            return (try await save(), nil)
        } catch {
            return (await reload(), error)
        }
    }

    /// 保存の結果から、次に使う (画面に表示する値, 保存の基準値) を決める。
    /// - 保存に成功 / 失敗しても読み直せたとき: 確認できた設定（保存後の実際の
    ///   設定 / 読み直した実際の設定）を両方に使う。失敗した下書きを画面に
    ///   残すと実設定と食い違うため消す。
    /// - 保存も読み直しも失敗: 画面の値（編集中の値）と基準値をそのまま残す。
    ///   次の保存は「基準値 → 編集中の値」の差分になるので、失敗した変更も
    ///   含まれて送られる。
    static func nextDocumentsAfterSave(
        edited: ConfigDocument, baseline: ConfigDocument,
        outcome: (doc: ConfigDocument?, failure: Error?)
    ) -> (doc: ConfigDocument, synced: ConfigDocument) {
        guard let confirmed = outcome.doc else { return (edited, baseline) }
        return (confirmed, confirmed)
    }

    private func save(_ docToSave: ConfigDocument) async {
        guard let baseline = synced else { return }
        busy = true
        defer { busy = false }
        let outcome = await Self.saveAndResync(
            save: { try await client.saveChanges(from: baseline, to: docToSave) },
            reload: { try? await self.client.getConfig() })
        let next = Self.nextDocumentsAfterSave(edited: docToSave, baseline: baseline, outcome: outcome)
        doc = next.doc
        synced = next.synced
        if let error = outcome.failure {
            alertMessage = error.localizedDescription
        } else {
            EngineClient.scheduleArtifactReload() // 2 秒後に status.json / frame.png を読み直させる
        }
    }

    private func saveKey(_ service: DetectService) async {
        let key = (keyDrafts[service.provider] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else {
            alertMessage = String(localized: "API キーを入力してください。")
            return
        }
        busy = true
        defer { busy = false }
        do {
            try await client.setKey(provider: service.provider, key: key)
            keyDrafts[service.provider] = ""
            await reloadDetect() // 保存後に検出し直して状態を更新
        } catch {
            alertMessage = error.localizedDescription
        }
    }

    private func deleteKey(_ service: DetectService) async {
        busy = true
        defer { busy = false }
        do {
            if service.provider == "muse" {
                // 世代を進めて進行中の保存を無効化しつつ、キーと専用 WebKit 領域を消す。
                // 再検出は deleteCredential が投げる .museUsageUpdated 通知の 1 回に統一する。
                try await MuseCredentialCoordinator.shared.deleteCredential(client: client)
            } else {
                try await client.deleteKey(provider: service.provider)
                await reloadDetect()
            }
            keyDrafts[service.provider] = ""
        } catch {
            alertMessage = error.localizedDescription
        }
    }
}

// MARK: - 1 サービス分の行

private struct ServiceRow: View {
    let service: DetectService
    let doc: ConfigDocument?
    @Binding var keyDraft: String
    @Binding var enabledBinding: Bool
    @Binding var windowsBinding: Set<String>
    let tokenRefreshBinding: Binding<Bool>? // Claude 行のみ非 nil
    @Binding var colorBinding: Color
    let hasCustomColor: Bool
    let onSaveKey: () -> Void
    let onDeleteKey: () -> Void
    let onMuseLogin: () -> Void
    let onResetColor: () -> Void
    let busy: Bool

    /// Muse ログイン Cookie の保存中は「削除」を押せないよう監視する（競合防止）。
    @ObservedObject private var museCoordinator = MuseCredentialCoordinator.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Toggle("", isOn: $enabledBinding)
                    .labelsHidden()
                    .disabled(busy)
                Text(service.displayName).font(.headline)
                detectBadge
                Spacer()
                if let plan = service.plan {
                    Text("プラン: \(plan)").font(.caption).foregroundStyle(.secondary)
                }
                colorControls
            }

            if service.detected, let source = service.source {
                Text("✓ \(source)").font(.caption).foregroundStyle(.green)
            } else if let error = service.error {
                Text("✗ \(error)").font(.caption).foregroundStyle(.red)
            }

            if !service.windowList.isEmpty {
                HStack(spacing: 12) {
                    Text("窓").font(.caption).foregroundStyle(.secondary)
                    ForEach(service.windowList) { window in
                        Toggle(isOn: windowBinding(window.key)) {
                            Text(window.displayLabel)
                        }
                        .toggleStyle(.checkbox)
                    }
                }
                .padding(.leading, 24)
            }

            if let tokenRefreshBinding {
                Toggle("トークンを自動で更新する", isOn: tokenRefreshBinding)
                    .disabled(busy)
                    .padding(.leading, 24)
                Text("期限切れ時に claude を軽く 1 回実行して更新させます。オフにすると、切れたらパネルに案内を出すだけになります。")
                    .font(.caption).foregroundStyle(.secondary)
                    .padding(.leading, 24)
            }

            if service.provider == "muse" {
                // Muse は API キーではなくアプリ内ログイン。アプリ内の WebView で
                // muse.ai にログインしてもらい、「完了」で利用枠の取得を始める。
                HStack(spacing: 6) {
                    Button(service.detected ? "ログインし直す" : "muse.ai にログイン", action: onMuseLogin)
                        .disabled(busy)
                    Button("削除", role: .destructive, action: onDeleteKey)
                        .disabled(busy || museCoordinator.polling) // 取得中は押せない。押せてしまっても coordinator が取得の完了を待ってから消す
                }
                .padding(.leading, 24)
                Text("ログイン状態はアプリ専用の WebKit 領域に、取得した利用枠はデータフォルダの muse-usage.json に保存されます。アプリが 5 分ごとに更新し、「削除」で両方を消します。")
                    .font(.caption2).foregroundStyle(.secondary)
                    .padding(.leading, 24)
            } else if service.needsKey {
                HStack(spacing: 6) {
                    SecureField("API キー", text: $keyDraft, prompt: Text("API キー"))
                        .textFieldStyle(.squareBorder)
                    Button("保存", action: onSaveKey).disabled(keyDraft.isEmpty || busy)
                    Button("削除", role: .destructive, action: onDeleteKey).disabled(busy)
                }
                .padding(.leading, 24)
                Text("キーはログインキーチェーン（quota-canvas）に保存され、アプリ自体には残りません。")
                    .font(.caption2).foregroundStyle(.secondary)
                    .padding(.leading, 24)
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private var detectBadge: some View {
        if service.detected {
            Label("検出", systemImage: "checkmark.circle.fill")
                .font(.caption2).foregroundStyle(.green).labelStyle(.iconOnly)
        } else {
            Image(systemName: "xmark.circle.fill")
                .font(.caption2).foregroundStyle(.red)
        }
    }

    private func windowBinding(_ key: String) -> Binding<Bool> {
        Binding<Bool>(
            get: { windowsBinding.contains(key) },
            set: { on in
                if on { windowsBinding.insert(key) } else { windowsBinding.remove(key) }
            }
        )
    }

    /// カードの色。status.json の colors を初期値に、変更で providers.<p>.color を保存する。
    private var colorControls: some View {
        HStack(spacing: 4) {
            Text("色").font(.caption).foregroundStyle(.secondary)
            ColorPicker("", selection: $colorBinding, supportsOpacity: false)
                .labelsHidden()
                .fixedSize()
                .disabled(busy)
                .help("画面のカードの色（\(service.provider)）")
            if hasCustomColor {
                Button("既定に戻す", action: onResetColor)
                    .controlSize(.small)
                    .help("providers.\(service.provider).color を削除し、既定の色に戻します")
            }
        }
    }
}
