import SwiftUI
import AppKit

@main
struct QuotaCanvasApp: App {
    @StateObject private var statusStore = StatusStore()

    init() {
        // 起動時に 1 回だけ:
        // - F4 通知の権限要求（.app バンドルのときだけ）
        // - status.json の定期読み込み開始。以前は MenuPopoverView の .task が唯一の
        //   呼び出し元で、メニューを閉じている間は通知もメニューバーの点の灰色化も
        //   動かなかった。start() 内の started フラグで二重起動はガード済み。
        let store = statusStore
        Task { @MainActor in
            await NotificationCoordinator.shared.requestAuthorizationIfNeeded()
            store.start()
        }
    }

    var body: some Scene {
        MenuBarExtra {
            MenuPopoverView()
                .environmentObject(statusStore)
        } label: {
            MenuBarIconView(status: statusStore.dotStatus)
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsRootView()
        }

        Window("画面プレビュー", id: "frame-preview") {
            FramePreviewView()
        }
        .defaultSize(width: 1000, height: 300)
    }
}

/// メニューバーアイコン: gauge 系 SF シンボル + 最も悪い状態の色の点。
/// テンプレート描画では色が落ちるため、点は非テンプレートの NSImage で渡す。
struct MenuBarIconView: View {
    var status: String?

    private static let gaugeImage: NSImage = {
        let base = NSImage(systemSymbolName: "gauge.with.needle",
                           accessibilityDescription: "QuotaCanvas")!
        let config = NSImage.SymbolConfiguration(pointSize: 13, weight: .medium)
        return base.withSymbolConfiguration(config) ?? base
    }()

    private static func dotImage(color: NSColor) -> NSImage {
        let size = NSSize(width: 8, height: 8)
        let image = NSImage(size: size, flipped: false) { rect in
            color.setFill()
            NSBezierPath(ovalIn: rect.insetBy(dx: 0.5, dy: 0.5)).fill()
            return true
        }
        image.isTemplate = false
        return image
    }

    var body: some View {
        HStack(spacing: 3) {
            Image(nsImage: Self.gaugeImage)
            Image(nsImage: Self.dotImage(color: StatusColor.nsColor(for: status)))
        }
        .accessibilityLabel("QuotaCanvas")
    }
}
