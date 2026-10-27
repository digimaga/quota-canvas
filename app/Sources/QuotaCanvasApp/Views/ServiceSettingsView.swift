import SwiftUI
import AppKit

/// 「サービス」タブ: detect --json の一覧 + providers 設定 + API キー管理。
struct ServiceSettingsView: View {
    @State private var doc: ConfigDocument?
    @State private var detect: [DetectService] = []
    @State private var loadingDetect = false
    @State private var loadFailed = false
    @State private var busy = false
    @State private var alertMessage: String?
    @State private var keyDrafts: [String: String] = [:]
    @State private var statusColors: [String: String] = [:]   // status.json の colors（既定色込み）

    private let client = EngineClient()

    /// 有効にできるサービスの上限
    static let maxEnabled = 5

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
        .safeAreaInset(edge: .bottom) {
            HStack {
                Text("有効にできるサービスは最大 \(Self.maxEnabled) つです")
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
        .alert("サービスの上限", isPresented: Binding(
            get: { limitAlertVisible },
            set: { if !$0 { limitAlertVisible = false } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text("有効にできるサービスは最大 \(Self.maxEnabled) つです。不要なサービスをオフにしてから試してください。")
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

    @State private var limitAlertVisible = false

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
                guard let original = doc else { return }
                var updated = original
                updated.setProviderColor(service.provider, HexColor.hex(newColor))
                doc = updated
                Task { await save(original, updated) }
            }
        )
    }

    /// 「既定の色に戻す」: providers.<p>.color の color キーを削除して保存。
    private func resetColor(_ service: DetectService) {
        guard let original = doc else { return }
        var updated = original
        updated.removeProviderColor(service.provider)
        doc = updated
        Task { await save(original, updated) }
    }

    // MARK: bindings

    private func enabledCount() -> Int {
        detect.filter { doc?.providerEnabled($0.provider) ?? false }.count
    }

    private func enabledBinding(_ service: DetectService) -> Binding<Bool> {
        Binding<Bool>(
            get: { doc?.providerEnabled(service.provider) ?? false },
            set: { newValue in
                guard let original = doc else { return }
                var updated = original
                if newValue && !updated.providerEnabled(service.provider) {
                    if enabledCount() >= Self.maxEnabled {
                        limitAlertVisible = true
                        return // doc を触らないのでトグルは元に戻る
                    }
                }
                updated.setProviderEnabled(service.provider, newValue)
                doc = updated
                Task { await save(original, updated) }
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
                guard let original = doc else { return }
                var updated = original
                let all = Set(service.windowList.map(\.key))
                let keys: [String] = (selected == all) ? [] : Array(selected)
                updated.setProviderWindows(service.provider, keys)
                doc = updated
                Task { await save(original, updated) }
            }
        )
    }

    /// トークン自動更新のバインディング（providers.claude.token_refresh）。
    /// オン = "cli"、オフ = "off"、未設定はオン扱い。Claude の行のみ表示するので nil を返す。
    private func tokenRefreshBinding(_ service: DetectService) -> Binding<Bool>? {
        guard service.provider == "claude" else { return nil }
        return Binding<Bool>(
            get: { doc?.providerTokenRefresh(service.provider) != "off" },
            set: { on in
                guard let original = doc else { return }
                var updated = original
                updated.setProviderTokenRefresh(service.provider, on ? "cli" : "off")
                doc = updated
                Task { await save(original, updated) }
            }
        )
    }

    // MARK: actions

    private func load(includeDetect: Bool) async {
        do {
            doc = try await client.getConfig()
            loadFailed = false
            statusColors = StatusStore.loadStatusSnapshot()?.colors ?? [:]
            if includeDetect { await reloadDetect() }
        } catch {
            loadFailed = true
            alertMessage = error.localizedDescription
        }
    }

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
            try await client.deleteKey(provider: service.provider)
            keyDrafts[service.provider] = ""
            await reloadDetect()
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
    let onResetColor: () -> Void
    let busy: Bool

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

            if service.needsKey {
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
