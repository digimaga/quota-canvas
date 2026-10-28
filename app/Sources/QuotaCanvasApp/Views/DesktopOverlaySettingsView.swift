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
            LabeledContent("位置") {
                DesktopOverlayPositionPad(selection: $position)
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
                    // スライダーに加えて 1pt 単位で直接入力・増減できるようにする
                    TextField("幅", value: $width, format: .number.precision(.fractionLength(0)).grouping(.never))
                        .labelsHidden()
                        .multilineTextAlignment(.trailing)
                        .monospacedDigit()
                        .frame(width: 60)
                    Stepper("幅", value: $width, in: 320...1920, step: 1).labelsHidden()
                    Text(verbatim: "pt")
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
        .onChange(of: width) { _, newValue in
            // 手入力で範囲外や小数が入ったら 320〜1920 の整数に直す（直した値で再度呼ばれる）
            let clamped = min(1920, max(320, newValue.rounded()))
            if clamped != newValue { width = clamped; return }
            DesktopOverlayController.shared.settingsChanged()
        }
        .onChange(of: opacity) { _, _ in DesktopOverlayController.shared.settingsChanged() }
        .onChange(of: aboveIcons) { _, _ in DesktopOverlayController.shared.settingsChanged() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didChangeScreenParametersNotification)) { _ in
            screens = NSScreen.screens
        }
    }
}

/// 画面に見立てた四角形の 4 隅と各辺の中央に丸を置き、クリック 1 回で位置を選ぶ。
struct DesktopOverlayPositionPad: View {
    @Binding var selection: String

    private typealias Position = DesktopOverlayController.Position
    private static let size = CGSize(width: 176, height: 110)
    private static let dot: CGFloat = 18

    private static func label(_ position: Position) -> LocalizedStringKey {
        switch position {
        case .topLeft: return "左上"
        case .topCenter: return "上中央"
        case .topRight: return "右上"
        case .centerLeft: return "左中央"
        case .centerRight: return "右中央"
        case .bottomLeft: return "左下"
        case .bottomCenter: return "下中央"
        case .bottomRight: return "右下"
        }
    }

    /// 四角形の左上を原点にした丸の中心
    private static func point(_ position: Position) -> CGPoint {
        let w = size.width, h = size.height
        switch position {
        case .topLeft: return CGPoint(x: 0, y: 0)
        case .topCenter: return CGPoint(x: w / 2, y: 0)
        case .topRight: return CGPoint(x: w, y: 0)
        case .centerLeft: return CGPoint(x: 0, y: h / 2)
        case .centerRight: return CGPoint(x: w, y: h / 2)
        case .bottomLeft: return CGPoint(x: 0, y: h)
        case .bottomCenter: return CGPoint(x: w / 2, y: h)
        case .bottomRight: return CGPoint(x: w, y: h)
        }
    }

    var body: some View {
        let pad = Self.dot / 2
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 6)
                .fill(Color.secondary.opacity(0.12))
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.5), lineWidth: 1))
                .frame(width: Self.size.width, height: Self.size.height)
                .offset(x: pad, y: pad)
            ForEach(Position.allCases, id: \.self) { position in
                let selected = selection == position.rawValue
                let center = Self.point(position)
                Button {
                    selection = position.rawValue
                } label: {
                    Circle()
                        .fill(selected ? Color.accentColor : Color(nsColor: .windowBackgroundColor))
                        .overlay(Circle().stroke(selected ? Color.accentColor : Color.secondary, lineWidth: 1.5))
                        .frame(width: Self.dot, height: Self.dot)
                        .contentShape(Circle())
                }
                .buttonStyle(.plain)
                .help(Self.label(position))
                .accessibilityLabel(Self.label(position))
                .accessibilityAddTraits(selected ? .isSelected : [])
                .offset(x: center.x, y: center.y)
            }
        }
        .frame(width: Self.size.width + Self.dot, height: Self.size.height + Self.dot, alignment: .topLeading)
    }
}
