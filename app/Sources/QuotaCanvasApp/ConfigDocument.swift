import Foundation

/// `quota-canvas config --json` の結果を JSONSerialization で保持し、
/// 知らないキー（poll / pacing / display.windows など）を壊さずに
/// `config --set-json` へ丸ごと送り返すためのドキュメント。
struct ConfigDocument {
    private(set) var object: [String: Any]

    init(object: [String: Any]) {
        self.object = object
    }

    init(data: Data) throws {
        guard let obj = try JSONSerialization.jsonObject(with: data, options: []) as? [String: Any] else {
            throw EngineError(message: String(localized: "設定 JSON の形式が不正です（オブジェクトではありません）"))
        }
        self.object = obj
    }

    func jsonData() throws -> Data {
        try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted])
    }

    // MARK: - display

    private var display: [String: Any] {
        get { object["display"] as? [String: Any] ?? [:] }
        set { object["display"] = newValue }
    }

    var paused: Bool {
        get { display["paused"] as? Bool ?? false }
        set { var d = display; d["paused"] = newValue; display = d }
    }

    var gauge: String {
        get { display["gauge"] as? String ?? "remaining" }
        set { var d = display; d["gauge"] = newValue; display = d }
    }

    /// % の数字を利用状況の色にするか。既定 true（false なら全部白）。
    var percentColors: Bool {
        get { display["percent_colors"] as? Bool ?? true }
        set { var d = display; d["percent_colors"] = newValue; display = d }
    }

    /// TURZX 画面の文の区切り（「1時間経過／リセットまで…」の「／」）。"" = 表示言語の既定（日本語は「／」、英語は " · "）。
    var separator: String {
        get { display["separator"] as? String ?? "" }
        set { var d = display; d["separator"] = newValue; display = d }
    }

    /// UI 表示言語。"auto" / "ja" / "en"。既定 "auto"（macOS に合わせる）。
    var language: String {
        get { display["language"] as? String ?? "auto" }
        set { var d = display; d["language"] = newValue; display = d }
    }

    var brightness: Double {
        get { (display["brightness"] as? NSNumber)?.doubleValue ?? 100 }
        set { var d = display; d["brightness"] = Int(newValue.rounded()); display = d }
    }

    struct Night {
        var start: String
        var end: String
        var brightness: Double
    }

    var night: Night? {
        get {
            guard let n = display["night"] as? [String: Any] else { return nil }
            return Night(
                start: n["start"] as? String ?? "23:00",
                end: n["end"] as? String ?? "07:00",
                brightness: (n["brightness"] as? NSNumber)?.doubleValue ?? 20
            )
        }
        set {
            var d = display
            if let n = newValue {
                d["night"] = ["start": n.start, "end": n.end, "brightness": Int(n.brightness.rounded())]
            } else {
                d.removeValue(forKey: "night")
            }
            display = d
        }
    }

    /// Mac のスリープ時に画面も消すか。nil = 未設定（true 扱い）。
    var sleepWithMac: Bool? {
        get { display["sleep_with_mac"] as? Bool }
        set {
            var d = display
            if let newValue {
                d["sleep_with_mac"] = newValue
            } else {
                d.removeValue(forKey: "sleep_with_mac")
            }
            display = d
        }
    }

    // MARK: - providers

    private func providerDict(_ provider: String) -> [String: Any] {
        (object["providers"] as? [String: Any])?[provider] as? [String: Any] ?? [:]
    }

    private mutating func setProviderDict(_ provider: String, _ value: [String: Any]) {
        var providers = object["providers"] as? [String: Any] ?? [:]
        var existing = providers[provider] as? [String: Any] ?? [:]
        for (k, v) in value { existing[k] = v }
        providers[provider] = existing
        object["providers"] = providers
    }

    func providerEnabled(_ provider: String) -> Bool {
        providerDict(provider)["enabled"] as? Bool ?? false
    }

    mutating func setProviderEnabled(_ provider: String, _ enabled: Bool) {
        setProviderDict(provider, ["enabled": enabled])
    }

    /// 窓キーのリスト。空配列 = すべて表示（コントラクト）。
    func providerWindows(_ provider: String) -> [String] {
        providerDict(provider)["windows"] as? [String] ?? []
    }

    mutating func setProviderWindows(_ provider: String, _ keys: [String]) {
        setProviderDict(provider, ["windows": keys])
    }

    /// providers.<p>.token_refresh。"cli"（既定）/ "off"。nil = 未設定（"cli" 扱い）。
    func providerTokenRefresh(_ provider: String) -> String? {
        providerDict(provider)["token_refresh"] as? String
    }

    /// token_refresh だけを設定・解除する（providers 配下の他のキーは壊さない）。
    mutating func setProviderTokenRefresh(_ provider: String, _ value: String?) {
        var providers = object["providers"] as? [String: Any] ?? [:]
        var existing = providers[provider] as? [String: Any] ?? [:]
        if let value {
            existing["token_refresh"] = value
        } else {
            existing.removeValue(forKey: "token_refresh")
        }
        providers[provider] = existing
        object["providers"] = providers
    }

    // MARK: - providers.<p>.color

    /// "#RRGGBB"。未設定（既定色）なら nil。
    func providerColor(_ provider: String) -> String? {
        providerDict(provider)["color"] as? String
    }

    mutating func setProviderColor(_ provider: String, _ hex: String) {
        setProviderDict(provider, ["color": hex])
    }

    /// 「既定の色に戻す」: color キーだけを取り除く（他のキーは壊さない）。
    mutating func removeProviderColor(_ provider: String) {
        var providers = object["providers"] as? [String: Any] ?? [:]
        var existing = providers[provider] as? [String: Any] ?? [:]
        existing.removeValue(forKey: "color")
        providers[provider] = existing
        object["providers"] = providers
    }

    // MARK: - routing（振り分け判定）

    /// routing.workers の 1 件ぶん。配列で持つ（順番が `routing line` の表示順になる）。
    struct RoutingWorker: Equatable {
        var name: String
        var windows: [String]
    }

    private var routing: [String: Any] {
        get { object["routing"] as? [String: Any] ?? [:] }
        set { object["routing"] = newValue }
    }

    /// 振り分け判定を使うか（既定オフ）。
    var routingEnabled: Bool {
        get { routing["enabled"] as? Bool ?? false }
        set { var r = routing; r["enabled"] = newValue; routing = r }
    }

    /// 残り % がこの下限未満のワーカーは stop（既定 5）。
    var routingFloorPercent: Double {
        get { (routing["floor_percent"] as? NSNumber)?.doubleValue ?? 5 }
        set {
            var r = routing
            let rounded = newValue.rounded()
            // 整数値なら整数のまま送る（TOML に 5.0 と書かれないように）
            r["floor_percent"] = newValue == rounded ? Int(rounded) : newValue
            routing = r
        }
    }

    /// fetched_at がこの分数より古い窓を含むワーカーは stale（既定 20）。
    var routingStaleMinutes: Int {
        get { (routing["stale_minutes"] as? NSNumber)?.intValue ?? 20 }
        set { var r = routing; r["stale_minutes"] = newValue; routing = r }
    }

    /// ワーカーの一覧（routing.workers）。
    /// 読み込み: エンジンは名前 → 窓 id 配列のオブジェクト + 表示順の `worker_order`
    /// で返す（JSONSerialization はオブジェクトのキー順を保てないため）。順番は
    /// worker_order のとおり、無ければ名前順。
    /// 書き込み: 順番を運べる `[{"name": ..., "windows": [...]}]` の配列形式で送る
    /// （worker_order はもう不要なので取り除く）。
    var routingWorkers: [RoutingWorker] {
        get {
            let r = routing
            if let arr = r["workers"] as? [[String: Any]] {
                return arr.compactMap { item in
                    guard let name = item["name"] as? String else { return nil }
                    let ids = (item["windows"] as? [Any])?.compactMap { $0 as? String } ?? []
                    return RoutingWorker(name: name, windows: ids)
                }
            }
            guard let dict = r["workers"] as? [String: Any] else { return [] }
            let order = r["worker_order"] as? [String] ?? []
            var workers: [RoutingWorker] = []
            for (name, value) in dict {
                let ids = (value as? [Any])?.compactMap { $0 as? String } ?? []
                workers.append(RoutingWorker(name: name, windows: ids))
            }
            return workers.sorted { a, b in
                let ia = order.firstIndex(of: a.name) ?? Int.max
                let ib = order.firstIndex(of: b.name) ?? Int.max
                return ia == ib ? a.name < b.name : ia < ib
            }
        }
        set {
            var r = routing
            r["workers"] = newValue.map { ["name": $0.name, "windows": $0.windows] }
            r.removeValue(forKey: "worker_order")
            routing = r
        }
    }

    // MARK: - layout.panels

    struct PanelConfig {
        var main: String
        var subs: [String]? // nil = 自動（キー省略で送る）、[] = 明示的にサブなし
        var size: String    // "large" | "small"
    }

    /// 手動配置。subs のキーなし / null = 自動。layout 配下の他のキーは保持する。
    var layoutPanels: [PanelConfig] {
        get {
            guard let layout = object["layout"] as? [String: Any],
                  let panels = layout["panels"] as? [[String: Any]] else { return [] }
            return panels.map { p in
                // NSNull / キーなしも `as? [String]` で nil（自動）になる。[] は [] のまま。
                PanelConfig(main: p["main"] as? String ?? "",
                            subs: p["subs"] as? [String],
                            size: p["size"] as? String ?? "large")
            }
        }
        set {
            var layout = object["layout"] as? [String: Any] ?? [:]
            layout["panels"] = newValue.map { p in
                var dict: [String: Any] = ["main": p.main, "size": p.size]
                if let subs = p.subs { dict["subs"] = subs } // nil はキーごと省略（= 自動）
                return dict
            }
            object["layout"] = layout
        }
    }
}
