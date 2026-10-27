import SwiftUI
import AppKit

/// 「画面プレビュー」ウィンドウ: frame.png（1920x462）を幅に合わせて縮小表示し 10 秒ごとに再読込。
struct FramePreviewView: View {
    @State private var image: NSImage?
    @State private var frameDate: Date?
    @State private var missing = false

    private let timer = Timer.publish(every: 10, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(spacing: 6) {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .scaledToFit()
                    .frame(maxWidth: .infinity)
                    .accessibilityLabel("USB 画面に表示中のフレーム")
                if let frameDate {
                    Text("最終描画: \(frameDate.formatted(date: .abbreviated, time: .shortened))")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } else if missing {
                ContentUnavailableView("フレームがありません",
                                       systemImage: "display",
                                       description: Text("エンジンが 1 度も描画していないか、一時停止中です。"))
            } else {
                ProgressView("フレームを読み込み中…").frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            HStack {
                Spacer()
                Button {
                    load()
                } label: {
                    Label("再読み込み", systemImage: "arrow.clockwise")
                }
                .controlSize(.small)
            }
        }
        .padding(10)
        .navigationTitle("画面プレビュー")
        .task { load() }
        .onReceive(timer) { _ in load() }
        .onReceive(NotificationCenter.default.publisher(for: .engineArtifactsChanged)) { _ in
            load() // 設定保存 2 秒後などにすぐ追従させる
        }
    }

    private func load() {
        let url = AppPaths.frameURL
        let hadImage = image != nil
        DispatchQueue.global(qos: .utility).async {
            guard FileManager.default.fileExists(atPath: url.path),
                  let loaded = NSImage(contentsOf: url),
                  let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
                  let modified = attrs[.modificationDate] as? Date else {
                DispatchQueue.main.async {
                    if !hadImage { missing = true }
                }
                return
            }
            DispatchQueue.main.async {
                image = loaded
                frameDate = modified
                missing = false
            }
        }
    }
}
