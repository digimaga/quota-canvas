import SwiftUI

/// 「今すぐ画面に反映」ボタン（ポップオーバー・設定ウィンドウ共用）。
/// `quota-canvas refresh --json` を実行してエンジンの常駐ループに再描画を依頼する。
/// 押下中は ProgressView を出し、二重押しを防ぐ。
/// 成功したら直後 + 3 秒後 + 6 秒後の 3 回 reload() を呼び、status.json の表示を追従させる。
struct EngineRefreshButton: View {
    /// status.json などの再読み込み（例: StatusStore.refresh）
    var reload: () async -> Void = {}

    @State private var busy = false
    @State private var alertMessage: String?

    var body: some View {
        Button {
            Task { await refreshNow() }
        } label: {
            HStack(spacing: 6) {
                Label("今すぐ画面に反映", systemImage: "arrow.clockwise")
                if busy {
                    ProgressView()
                        .controlSize(.small)
                }
            }
        }
        .disabled(busy)
        .alert("エラー", isPresented: Binding(
            get: { alertMessage != nil },
            set: { if !$0 { alertMessage = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(alertMessage ?? "")
        }
    }

    private func refreshNow() async {
        guard !busy else { return }   // 二重押し防止
        busy = true
        defer { busy = false }
        do {
            try await EngineClient().refresh()
            await reload()
            for delay in [3_000_000_000.0, 6_000_000_000.0] {
                Task {
                    try? await Task.sleep(nanoseconds: UInt64(delay))
                    await reload()
                }
            }
        } catch {
            alertMessage = error.localizedDescription
        }
    }
}

/// 設定ウィンドウ全タブ共通の下部バー:「今すぐ画面に反映」ボタン + 最終更新時刻。
struct SettingsRefreshBar: View {
    /// status.json の updated_at（読めなければ nil）
    @State private var lastUpdated: Date?

    var body: some View {
        HStack {
            EngineRefreshButton(reload: { await reloadNow() })
            Spacer()
            updatedText
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .task { await reloadNow() }
        .onReceive(NotificationCenter.default.publisher(for: .engineArtifactsChanged)) { _ in
            Task { await reloadNow() }
        }
    }

    @ViewBuilder
    private var updatedText: some View {
        if let lastUpdated {
            Text("最終更新 \(Self.format(lastUpdated))")
                .font(.caption).monospacedDigit().foregroundStyle(.secondary)
        } else {
            Text("最終更新: 不明")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    /// status.json を読み直して最終更新時刻を更新する
    private func reloadNow() async {
        lastUpdated = StatusStore.loadStatusSnapshot()?.updatedAt
    }

    /// "22:48:44" 形式
    private static func format(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateStyle = .none
        f.timeStyle = .medium
        return f.string(from: date)
    }
}
