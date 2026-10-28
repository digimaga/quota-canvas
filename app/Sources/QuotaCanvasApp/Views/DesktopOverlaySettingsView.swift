import SwiftUI
import AppKit

struct DesktopOverlaySettingsView: View {
    @AppStorage(DesktopOverlayController.enabledKey) private var enabled = false
    @AppStorage(DesktopOverlayController.positionKey) private var position = DesktopOverlayController.Position.topRight.rawValue
    @AppStorage(DesktopOverlayController.widthKey) private var width = 640.0
    @AppStorage(DesktopOverlayController.opacityKey) private var opacity = 1.0
    @AppStorage(DesktopOverlayController.aboveIconsKey) private var aboveIcons = false
    @AppStorage(DesktopOverlayController.screenKey) private var screen = ""
    @State private var screens = NSScreen.screens

    var body: some View {
        Form {
            Toggle("デスクトップに表示", isOn: $enabled)
            Picker("位置", selection: $position) {
                Text("左上").tag(DesktopOverlayController.Position.topLeft.rawValue)
                Text("上中央").tag(DesktopOverlayController.Position.topCenter.rawValue)
                Text("右上").tag(DesktopOverlayController.Position.topRight.rawValue)
                Text("左中央").tag(DesktopOverlayController.Position.centerLeft.rawValue)
                Text("右中央").tag(DesktopOverlayController.Position.centerRight.rawValue)
                Text("左下").tag(DesktopOverlayController.Position.bottomLeft.rawValue)
                Text("下中央").tag(DesktopOverlayController.Position.bottomCenter.rawValue)
                Text("右下").tag(DesktopOverlayController.Position.bottomRight.rawValue)
            }
            Picker("表示する画面", selection: $screen) {
                Text("メインの画面").tag("")
                ForEach(screens, id: \.self) { display in
                    if let id = DesktopOverlayController.screenID(display) {
                        Text(display.localizedName).tag(id)
                    }
                }
                if !screen.isEmpty && !screens.contains(where: { DesktopOverlayController.screenID($0) == screen }) {
                    Text("接続されていない画面").tag(screen)
                }
            }
            LabeledContent("幅") {
                HStack {
                    Slider(value: $width, in: 320...1920, step: 1)
                    Text("\(Int(width)) pt").monospacedDigit().frame(width: 70, alignment: .trailing)
                }
            }
            LabeledContent("不透明度") {
                HStack {
                    Slider(value: $opacity, in: 0.3...1, step: 0.01)
                    Text("\(Int((opacity * 100).rounded()))% ").monospacedDigit().frame(width: 70, alignment: .trailing)
                }
            }
            Picker("重なり順", selection: $aboveIcons) {
                Text("デスクトップアイコンの奥").tag(false)
                Text("デスクトップアイコンの手前").tag(true)
            }
            .pickerStyle(.segmented)
            Text("どちらでも通常のアプリのウィンドウより奥に表示されます")
                .font(.caption).foregroundStyle(.secondary)
        }
        .formStyle(.grouped)
        .onChange(of: enabled) { _, _ in DesktopOverlayController.shared.settingsChanged() }
        .onChange(of: position) { _, _ in DesktopOverlayController.shared.settingsChanged() }
        .onChange(of: screen) { _, _ in DesktopOverlayController.shared.settingsChanged() }
        .onChange(of: width) { _, _ in DesktopOverlayController.shared.settingsChanged() }
        .onChange(of: opacity) { _, _ in DesktopOverlayController.shared.settingsChanged() }
        .onChange(of: aboveIcons) { _, _ in DesktopOverlayController.shared.settingsChanged() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didChangeScreenParametersNotification)) { _ in
            screens = NSScreen.screens
        }
    }
}
