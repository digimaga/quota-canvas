import SwiftUI
import AppKit

/// 設定ウィンドウ: 「サービス」「配置」「表示」「振り分け」の 4 タブ + 全タブ共通の下部バー。
struct SettingsRootView: View {
    var body: some View {
        VStack(spacing: 0) {
            TabView {
                ServiceSettingsView()
                    .tabItem { Label("サービス", systemImage: "list.bullet.rectangle") }
                LayoutSettingsView()
                    .tabItem { Label("配置", systemImage: "rectangle.split.3x1") }
                DisplaySettingsView()
                    .tabItem { Label("表示", systemImage: "slider.horizontal.3") }
                RoutingSettingsView()
                    .tabItem { Label("振り分け", systemImage: "arrow.triangle.branch") }
            }
            Divider()
            SettingsRefreshBar()
        }
        .frame(width: 640, height: 588)
    }
}

// MARK: - 表示タブ

struct DisplaySettingsView: View {
    @AppStorage(AppDefaults.enginePathKey) private var enginePath = AppDefaults.enginePath
    // F4: macOS 通知を出すか（UserDefaults、既定オン）
    @AppStorage(NotificationCoordinator.enabledDefaultsKey) private var notificationsOn = true

    @State private var doc: ConfigDocument?
    @State private var loadFailed = false
    @State private var busy = false
    @State private var alertMessage: String?

    // UI 状態（doc 読み込み後に反映）
    @State private var gaugeShowsUsed = false
    @State private var language = "auto"
    @State private var brightness: Double = 100
    @State private var nightOn = false
    @State private var nightStart = Date()
    @State private var nightEnd = Date()
    @State private var nightBrightness: Double = 20
    @State private var sleepWithMac = true
    @State private var percentColors = true
    @State private var separator = ""
    @State private var loginOn = LoginItem.isEnabled

    private let client = EngineClient()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let doc {
                Form {
                    gaugeSection(doc: doc)
                    languageSection(doc: doc)
                    brightnessSection(doc: doc)
                    nightSection(doc: doc)
                    notificationSection
                    engineSection
                    loginSection
                }
                .formStyle(.grouped)
                .disabled(busy)
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
        .alert("エラー", isPresented: Binding(
            get: { alertMessage != nil },
            set: { if !$0 { alertMessage = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(alertMessage ?? "")
        }
    }

    // MARK: sections

    private func gaugeSection(doc: ConfigDocument) -> some View {
        Section("ゲージの表示方式") {
            Picker("方式", selection: $gaugeShowsUsed) {
                Text("残量で表示（Codex 方式）").tag(false)
                Text("使用量で表示（Claude 方式）").tag(true)
            }
            .pickerStyle(.radioGroup)
            .onChange(of: gaugeShowsUsed) { _, newValue in
                commit { $0.gauge = newValue ? "used" : "remaining" }
            }
            Text("メニューバーのポップオーバーの残り % にも同じ方式が使われます。")
                .font(.caption).foregroundStyle(.secondary)
            Toggle("利用状況に合わせて % の文字色を変える", isOn: $percentColors)
                .onChange(of: percentColors) { _, on in
                    commit { $0.percentColors = on }
                }
            Text("超過・枯渇は赤、余裕ありは緑。オフにすると全部白になります。")
                .font(.caption).foregroundStyle(.secondary)
            Picker("区切り文字", selection: $separator) {
                ForEach(Self.separatorChoices(current: separator), id: \.self) { value in
                    Text(Self.separatorLabel(value)).tag(value)
                }
            }
            .onChange(of: separator) { _, newValue in
                commit { $0.separator = newValue }
            }
            Text("「言語に合わせる」では日本語表示のとき ／、英語表示のとき \" · \" になります。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func languageSection(doc: ConfigDocument) -> some View {
        Section("言語") {
            Picker("言語", selection: $language) {
                Text("自動（macOS に合わせる）").tag("auto")
                Text("日本語").tag("ja")
                Text("English").tag("en")
            }
            .onChange(of: language) { _, newValue in
                Self.applyAppleLanguages(newValue)
                commit { $0.language = newValue }
            }
            Text("アプリの表示は再起動後に切り替わります")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    /// 表示言語の即時反映用。アプリ自身の言語は再起動後に AppleLanguages で切り替わる。
    private static func applyAppleLanguages(_ value: String) {
        let defaults = UserDefaults.standard
        if value == "auto" {
            defaults.removeObject(forKey: "AppleLanguages")
        } else {
            defaults.set([value], forKey: "AppleLanguages")
        }
    }

    private func brightnessSection(doc: ConfigDocument) -> some View {
        Section("画面の明るさ") {
            HStack {
                Slider(value: $brightness, in: 0...100, step: 1) {
                    Text("明るさ")
                } onEditingChanged: { editing in
                    if !editing { commit { $0.brightness = brightness } }
                }
                Text("\(Int(brightness))").monospacedDigit().frame(width: 34, alignment: .trailing)
            }
        }
    }

    private func nightSection(doc: ConfigDocument) -> some View {
        Section("夜間の減光") {
            Toggle("夜間は暗くする", isOn: $nightOn)
                .onChange(of: nightOn) { _, on in
                    commit { d in
                        d.night = on ? ConfigDocument.Night(
                            start: Self.hhmm(nightStart),
                            end: Self.hhmm(nightEnd),
                            brightness: nightBrightness
                        ) : nil
                    }
                }
            if nightOn {
                HStack {
                    DatePicker("開始", selection: $nightStart, displayedComponents: .hourAndMinute)
                    DatePicker("終了", selection: $nightEnd, displayedComponents: .hourAndMinute)
                }
                .onChange(of: nightStart) { _, _ in commitNight() }
                .onChange(of: nightEnd) { _, _ in commitNight() }
                HStack {
                    Slider(value: $nightBrightness, in: 0...100, step: 1) {
                        Text("夜間の明るさ")
                    } onEditingChanged: { editing in
                        if !editing { commitNight() }
                    }
                    Text("\(Int(nightBrightness))").monospacedDigit().frame(width: 34, alignment: .trailing)
                }
                Text("開始 > 終了 のときは日付をまたぎます（例: 23:00 → 07:00）。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Toggle("Mac のスリープ時に画面も消す", isOn: $sleepWithMac)
                .onChange(of: sleepWithMac) { _, on in
                    commit { $0.sleepWithMac = on }
                }
            Text("スリープやディスプレイの自動消灯に合わせてパネルを消し、復帰で戻します。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var notificationSection: some View {
        Section("通知") {
            Toggle("通知を出す", isOn: $notificationsOn)
            Text("枠が尽きた・リセットされた・加速で尽きる見込み・リセット権の期限が近い、ときに macOS の通知を出します。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var engineSection: some View {
        Section("エンジン") {
            VStack(alignment: .leading, spacing: 4) {
                LabeledContent("quota-canvas のパス") {
                    TextField("~/dev/quota-canvas/.venv/bin/quota-canvas", text: $enginePath)
                        .textFieldStyle(.squareBorder)
                        .onSubmit { Task { await load() } }
                }
                HStack {
                    Button("規定に戻す") {
                        enginePath = AppDefaults.enginePath
                        Task { await load() }
                    }
                    Button("再読み込み") { Task { await load() } }
                    Spacer()
                }
                .controlSize(.small)
                Text("この CLI が検出・設定・キー保存を行います。アプリはここで指定する以外の通信をしません。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var loginSection: some View {
        Section("アプリ") {
            Toggle("ログイン時に起動", isOn: $loginOn)
                .onChange(of: loginOn) { _, on in
                    do {
                        try LoginItem.setEnabled(on)
                    } catch {
                        loginOn = LoginItem.isEnabled
                        alertMessage = String(localized: "ログイン時起動の設定に失敗しました: \(error.localizedDescription)")
                    }
                }
            Text(LoginItem.statusText).font(.caption).foregroundStyle(.secondary)
        }
    }

    // MARK: load / commit

    private func load() async {
        do {
            let fetched = try await client.getConfig()
            doc = fetched
            loadFailed = false
            gaugeShowsUsed = fetched.gauge == "used"
            language = fetched.language
            brightness = fetched.brightness
            nightOn = fetched.night != nil
            sleepWithMac = fetched.sleepWithMac ?? true
            percentColors = fetched.percentColors
            separator = fetched.separator
            if let night = fetched.night {
                nightStart = Self.parseHHMM(night.start) ?? Self.parseHHMM("23:00")!
                nightEnd = Self.parseHHMM(night.end) ?? Self.parseHHMM("07:00")!
                nightBrightness = night.brightness
            }
        } catch {
            loadFailed = true
            alertMessage = error.localizedDescription
        }
    }

    private func commit(_ mutate: (inout ConfigDocument) -> Void) {
        guard var updated = doc else { return }
        mutate(&updated)
        doc = updated
        Task { await save(updated) }
    }

    private func commitNight() {
        commit { d in
            d.night = ConfigDocument.Night(
                start: Self.hhmm(nightStart),
                end: Self.hhmm(nightEnd),
                brightness: nightBrightness
            )
        }
    }

    private func save(_ docToSave: ConfigDocument) async {
        busy = true
        defer { busy = false }
        do {
            doc = try await client.setConfig(docToSave)
            EngineClient.scheduleArtifactReload() // 2 秒後に status.json / frame.png を読み直させる
        } catch {
            alertMessage = error.localizedDescription
        }
    }

    // MARK: separator choices

    private static let presetSeparators = ["", "／", " · ", " / ", " | "]

    /// 保存中の値が既定の選択肢にない（手で書いた値）ときは、その値も選択肢に含める。
    private static func separatorChoices(current: String) -> [String] {
        presetSeparators.contains(current) ? presetSeparators : presetSeparators + [current]
    }

    private static func separatorLabel(_ value: String) -> String {
        switch value {
        case "": String(localized: "言語に合わせる")
        case "／": String(localized: "／（全角スラッシュ）")
        case " · ": String(localized: "·（中黒）")
        case " / ": String(localized: "/（半角スラッシュ）")
        case " | ": String(localized: "|")
        default: "\"\(value)\""
        }
    }

    // MARK: time helpers

    private static let hhmmFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX") // 保存/パース用の固定書式
        f.dateFormat = "HH:mm"
        return f
    }()

    private static func hhmm(_ date: Date) -> String { hhmmFormatter.string(from: date) }

    private static func parseHHMM(_ s: String) -> Date? { hhmmFormatter.date(from: s) }
}
