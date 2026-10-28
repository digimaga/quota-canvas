import Foundation
import SwiftUI
import AppKit

// MARK: - status.json models (docs/app-contract.md)

struct EngineStatus: Decodable {
    var updatedAtRaw: String?
    var paused: Bool?
    var gauge: String?
    var cards: [StatusCard]?
    var errors: [StatusError]?
    var balances: [StatusBalance]?
    /// status.json の "layout": 実際に使われた配置（{main, subs, size} の配列）。
    /// 旧形式 {panels: [...]} も受ける。型が合わなければ nil（status 全体のデコードは壊さない）。
    var layout: [LayoutPanel]?
    var colors: [String: String]?
    /// F1「今使う先」: engine が計算した推奨（text / ranking）。
    /// エンジンがまだ書いていない・型が合わないときは nil（status 全体のデコードは壊さない）。
    var recommendation: Recommendation?
    /// Codex のリセット権: {provider: [{title, granted_at, expires_at}]}（expires 頇）。
    /// 1 つも無ければ省略。型が合わなければ nil。
    var resetCredits: [String: [ResetCredit]]?
    /// 振り分け判定（[routing] enabled = true のときだけ書かれる）。無ければ nil。
    var routing: StatusRouting?
    /// 全窓（表示フィルタ前、part_of 付きの内訳窓も含む）。無ければ nil。
    var windows: [StatusWindow]?

    enum CodingKeys: String, CodingKey {
        case updatedAtRaw = "updated_at"
        case paused, gauge, cards, errors, balances, layout, colors
        case recommendation, resetCredits = "reset_credits"
        case routing, windows
    }

    /// 旧形式の "layout": {"panels": [...]}
    private struct LegacyLayout: Decodable {
        var panels: [LayoutPanel]?
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        updatedAtRaw = try c.decodeIfPresent(String.self, forKey: .updatedAtRaw)
        paused = try c.decodeIfPresent(Bool.self, forKey: .paused)
        gauge = try c.decodeIfPresent(String.self, forKey: .gauge)
        cards = try c.decodeIfPresent([StatusCard].self, forKey: .cards)
        errors = try c.decodeIfPresent([StatusError].self, forKey: .errors)
        balances = try? c.decodeIfPresent([StatusBalance].self, forKey: .balances)
        colors = try c.decodeIfPresent([String: String].self, forKey: .colors)
        // 現行は配列。旧形式 {panels: [...]}、それ以外の型は layout = nil。
        if let panels = try? c.decodeIfPresent([LayoutPanel].self, forKey: .layout) {
            layout = panels
        } else {
            layout = (try? c.decodeIfPresent(LegacyLayout.self, forKey: .layout))?.panels
        }
        // F1 / リセット権も layout と同じ方針: 型が違えばそのキーだけ nil。
        recommendation = try? c.decodeIfPresent(Recommendation.self, forKey: .recommendation)
        resetCredits = try? c.decodeIfPresent([String: [ResetCredit]].self, forKey: .resetCredits)
        routing = try? c.decodeIfPresent(StatusRouting.self, forKey: .routing)
        windows = try? c.decodeIfPresent([StatusWindow].self, forKey: .windows)
    }

    var updatedAt: Date? {
        guard let raw = updatedAtRaw else { return nil }
        return ISO8601Support.parse(raw)
    }
}

struct StatusBalance: Decodable {
    var provider: String
    var name: String
    var currency: String
    var amount: Double
    var label: String?
    var staleError: String?
    var fetchedAtRaw: String?

    enum CodingKeys: String, CodingKey {
        case provider, name, currency, amount, label
        case staleError = "stale_error"
        case fetchedAtRaw = "fetched_at"
    }

    var fetchedAt: Date? { fetchedAtRaw.flatMap(ISO8601Support.parse) }

    var displayText: String {
        let symbol = currency == "USD" ? "$" : (currency == "CNY" ? "¥" : currency + " ")
        let amountText = String(format: "%.2f", amount)
        return "\(label ?? String(localized: "残高")) \(symbol)\(amountText)"
    }
}

struct StatusCard: Decodable {
    var id: String?
    var provider: String?
    var name: String?
    var label: String?
    var used: Double?
    var remaining: Double?
    var status: String?
    var statusLabel: String?
    var advice: String?
    var resetsAtRaw: String?
    /// 実際に残量データを取得した時刻。再描画では変わらない。
    var fetchedAtRaw: String?
    /// F7: 取得失敗中に最後の成功値を出しているときの失敗理由。無ければ nil。
    var staleError: String?
    /// F3: 前回の期間の最終使用率（%）。無ければ nil。
    var previousUsed: Double?
    /// 通常の予測で尽きる時刻（OVER のとき）。無ければ nil。
    var exhaustsAtRaw: String?
    /// F2: 直近の加速を続けたとき 100% に届く時刻（リセット前のみ）。無ければ nil。
    var burstExhaustsAtRaw: String?

    enum CodingKeys: String, CodingKey {
        case id, provider, name, label, used, remaining, status, advice
        case statusLabel = "status_label"
        case resetsAtRaw = "resets_at"
        case fetchedAtRaw = "fetched_at"
        case staleError = "stale_error"
        case previousUsed = "previous_used"
        case exhaustsAtRaw = "exhausts_at"
        case burstExhaustsAtRaw = "burst_exhausts_at"
    }

    /// どのキーも型が違えば nil にする（カード 1 枚の malformed な新フィールドで
    /// status 全体・カード全体のデコードを壊さない）。
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try? c.decodeIfPresent(String.self, forKey: .id)
        provider = try? c.decodeIfPresent(String.self, forKey: .provider)
        name = try? c.decodeIfPresent(String.self, forKey: .name)
        label = try? c.decodeIfPresent(String.self, forKey: .label)
        used = try? c.decodeIfPresent(Double.self, forKey: .used)
        remaining = try? c.decodeIfPresent(Double.self, forKey: .remaining)
        status = try? c.decodeIfPresent(String.self, forKey: .status)
        statusLabel = try? c.decodeIfPresent(String.self, forKey: .statusLabel)
        advice = try? c.decodeIfPresent(String.self, forKey: .advice)
        resetsAtRaw = try? c.decodeIfPresent(String.self, forKey: .resetsAtRaw)
        fetchedAtRaw = try? c.decodeIfPresent(String.self, forKey: .fetchedAtRaw)
        staleError = try? c.decodeIfPresent(String.self, forKey: .staleError)
        previousUsed = try? c.decodeIfPresent(Double.self, forKey: .previousUsed)
        exhaustsAtRaw = try? c.decodeIfPresent(String.self, forKey: .exhaustsAtRaw)
        burstExhaustsAtRaw = try? c.decodeIfPresent(String.self, forKey: .burstExhaustsAtRaw)
    }

    var resetsAt: Date? {
        guard let raw = resetsAtRaw else { return nil }
        return ISO8601Support.parse(raw)
    }

    var fetchedAt: Date? { fetchedAtRaw.flatMap(ISO8601Support.parse) }

    var exhaustsAt: Date? { exhaustsAtRaw.flatMap(ISO8601Support.parse) }
    var burstExhaustsAt: Date? { burstExhaustsAtRaw.flatMap(ISO8601Support.parse) }

    /// 残量 %（gauge が used なら使用量 %）
    func displayPercent(gauge: String?) -> Double? {
        (gauge == "used") ? used : remaining
    }
}

struct StatusError: Decodable {
    var provider: String?
    var name: String?
    var error: String?
}

/// F1: status.json の "recommendation"（engine が計算した「今使う先」）。
/// text は表示言語済みの 1 行。無い・型が違うときは nil。
struct Recommendation: Decodable {
    var text: String?
    var provider: String?
    var name: String?
    var windowId: String?
    var ranking: [RecommendationEntry]?

    enum CodingKeys: String, CodingKey {
        case text, provider, name, ranking
        case windowId = "window_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try? c.decodeIfPresent(String.self, forKey: .text)
        provider = try? c.decodeIfPresent(String.self, forKey: .provider)
        name = try? c.decodeIfPresent(String.self, forKey: .name)
        windowId = try? c.decodeIfPresent(String.self, forKey: .windowId)
        ranking = try? c.decodeIfPresent([RecommendationEntry].self, forKey: .ranking)
    }
}

/// "recommendation.ranking" の 1 要素。
struct RecommendationEntry: Decodable {
    var provider: String?
    var name: String?
    var windowId: String?
    var usable: Bool?
    var score: Double?
    var remaining: Double?
    var resetsAtRaw: String?
    var reason: String?

    enum CodingKeys: String, CodingKey {
        case provider, name, usable, score, remaining, reason
        case windowId = "window_id"
        case resetsAtRaw = "resets_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        provider = try? c.decodeIfPresent(String.self, forKey: .provider)
        name = try? c.decodeIfPresent(String.self, forKey: .name)
        windowId = try? c.decodeIfPresent(String.self, forKey: .windowId)
        usable = try? c.decodeIfPresent(Bool.self, forKey: .usable)
        score = try? c.decodeIfPresent(Double.self, forKey: .score)
        remaining = try? c.decodeIfPresent(Double.self, forKey: .remaining)
        resetsAtRaw = try? c.decodeIfPresent(String.self, forKey: .resetsAtRaw)
        reason = try? c.decodeIfPresent(String.self, forKey: .reason)
    }

    var resetsAt: Date? { resetsAtRaw.flatMap(ISO8601Support.parse) }
}

/// status.json の "reset_credits" の 1 権利（Codex の週間リセット権など）。
struct ResetCredit: Decodable {
    var title: String?
    var grantedAtRaw: String?
    var expiresAtRaw: String?

    enum CodingKeys: String, CodingKey {
        case title
        case grantedAtRaw = "granted_at"
        case expiresAtRaw = "expires_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        title = try? c.decodeIfPresent(String.self, forKey: .title)
        grantedAtRaw = try? c.decodeIfPresent(String.self, forKey: .grantedAtRaw)
        expiresAtRaw = try? c.decodeIfPresent(String.self, forKey: .expiresAtRaw)
    }

    var grantedAt: Date? { grantedAtRaw.flatMap(ISO8601Support.parse) }
    var expiresAt: Date? { expiresAtRaw.flatMap(ISO8601Support.parse) }
}

// MARK: - status.json の "routing"（振り分け判定）と "windows"（全窓）

/// status.json の "routing"（[routing] enabled = true のときだけ書かれる）。
/// 無い・型が違うときは nil（status 全体のデコードは壊さない）。
struct StatusRouting: Decodable {
    var atRaw: String?
    var workers: [RoutingWorkerStatus]?
    var line: String?

    enum CodingKeys: String, CodingKey { case at, workers, line }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        atRaw = try? c.decodeIfPresent(String.self, forKey: .at)
        workers = try? c.decodeIfPresent([RoutingWorkerStatus].self, forKey: .workers)
        line = try? c.decodeIfPresent(String.self, forKey: .line)
    }

    var at: Date? { atRaw.flatMap(ISO8601Support.parse) }
}

/// "routing.workers" の 1 ワーカーぶんの判定。どのキーも型が違えば nil。
struct RoutingWorkerStatus: Decodable, Identifiable {
    var name: String?
    var verdict: String?      // stop | reserve | go | boost | unknown
    var reason: String?       // exhausted / below_floor / over / tight / on_track / spare /
                              // fresh / burn / stale / missing / missing_status / error / broken
    var remaining: Double?    // 代表（一番悪い）窓の残り %
    var resetsAtRaw: String?
    var burnUntilRaw: String? // 使い切りモードの期限（有効なときだけ）
    var stale: Bool?
    var windows: [String]?

    enum CodingKeys: String, CodingKey {
        case name, verdict, reason, remaining, stale, windows
        case resetsAtRaw = "resets_at"
        case burnUntilRaw = "burn_until"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try? c.decodeIfPresent(String.self, forKey: .name)
        verdict = try? c.decodeIfPresent(String.self, forKey: .verdict)
        reason = try? c.decodeIfPresent(String.self, forKey: .reason)
        remaining = try? c.decodeIfPresent(Double.self, forKey: .remaining)
        resetsAtRaw = try? c.decodeIfPresent(String.self, forKey: .resetsAtRaw)
        burnUntilRaw = try? c.decodeIfPresent(String.self, forKey: .burnUntilRaw)
        stale = try? c.decodeIfPresent(Bool.self, forKey: .stale)
        windows = try? c.decodeIfPresent([String].self, forKey: .windows)
    }

    var id: String { name ?? "?" }
    var burnUntil: Date? { burnUntilRaw.flatMap(ISO8601Support.parse) }
}

/// status.json の "windows" の 1 窓ぶん（表示フィルタ前の全窓。内訳窓は part_of に親の id）。
struct StatusWindow: Decodable, Identifiable {
    var id: String?
    var provider: String?
    var partOf: String?

    enum CodingKeys: String, CodingKey { case id, provider; case partOf = "part_of" }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try? c.decodeIfPresent(String.self, forKey: .id)
        provider = try? c.decodeIfPresent(String.self, forKey: .provider)
        partOf = try? c.decodeIfPresent(String.self, forKey: .partOf)
    }
}

/// status.json の layout 配列の 1 要素:
/// {"main": "claude:weekly", "subs": ["claude:weekly_fable"], "size": "large"|"small"}
/// main は窓 ID（"provider:key"）。型が違うキーは nil にして無視する。
struct LayoutPanel: Decodable {
    var main: String?
    var subs: [String]?
    var size: String?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        main = try? c.decodeIfPresent(String.self, forKey: .main)
        subs = try? c.decodeIfPresent([String].self, forKey: .subs)
        size = try? c.decodeIfPresent(String.self, forKey: .size)
    }

    enum CodingKeys: String, CodingKey { case main, subs, size }
}

// MARK: - detect --json models

struct DetectService: Decodable, Identifiable {
    var provider: String
    var name: String?
    var detected: Bool
    var source: String?
    var needsKey: Bool
    var plan: String?
    var windows: [DetectWindow]?
    var error: String?

    enum CodingKeys: String, CodingKey {
        case provider, name, detected, source, plan, windows, error
        case needsKey = "needs_key"
    }

    var id: String { provider }
    var displayName: String { name ?? provider }
    var windowList: [DetectWindow] { windows ?? [] }
}

struct DetectWindow: Decodable, Identifiable {
    var key: String
    var label: String?
    var id: String { key }
    var displayLabel: String { label ?? key }
}

// MARK: - routing burn (CLI reply)

/// `quota-canvas routing burn --json` の 1 エントリ（有効な使い切りモード）。
struct RoutingBurnEntry: Decodable {
    var name: String?
    var until: String?   // 期限（ISO 8601）

    var untilDate: Date? { until.flatMap(ISO8601Support.parse) }
}

// MARK: - error envelope ({"error": "..."})

struct ErrorEnvelope: Decodable {
    var error: String
}

struct EngineError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

// MARK: - helpers

/// "#RRGGBB" ⇄ SwiftUI Color（providers.<p>.color / status.json の colors 用）
enum HexColor {
    static func color(_ hex: String?) -> Color? {
        guard var s = hex?.trimmingCharacters(in: .whitespacesAndNewlines), s.hasPrefix("#") else { return nil }
        s.removeFirst()
        guard s.count == 6, let value = UInt64(s, radix: 16) else { return nil }
        return Color(red: Double((value >> 16) & 0xFF) / 255,
                     green: Double((value >> 8) & 0xFF) / 255,
                     blue: Double(value & 0xFF) / 255)
    }

    static func hex(_ color: Color) -> String {
        let ns = NSColor(color).usingColorSpace(.sRGB) ?? NSColor.white
        return String(format: "#%02X%02X%02X",
                      Int(round(ns.redComponent * 255)),
                      Int(round(ns.greenComponent * 255)),
                      Int(round(ns.blueComponent * 255)))
    }
}

enum ISO8601Support {
    private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    private static let fractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    static func parse(_ s: String) -> Date? {
        fractional.date(from: s) ?? plain.date(from: s)
    }
}

enum AppPaths {
    static var supportDir: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("quota-canvas", isDirectory: true)
    }
    static var statusURL: URL { supportDir.appendingPathComponent("status.json") }
    static var frameURL: URL { supportDir.appendingPathComponent("frame.png") }
}

enum AppDefaults {
    static let legacyEnginePath = "~/dev/quota-canvas/.venv/bin/quota-canvas"
    static var bundledEnginePath: String? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let path = resources.appendingPathComponent("engine/bin/quota-canvas").path
        return FileManager.default.isExecutableFile(atPath: path) ? path : nil
    }
    static var enginePath: String { bundledEnginePath ?? legacyEnginePath }
    static let enginePathKey = "EnginePath"
}

/// 設定保存の 2 秒後などに status.json / frame.png の再読み込みを促す通知。
extension Notification.Name {
    static let engineArtifactsChanged = Notification.Name("quota-canvas.engineArtifactsChanged")
}

/// status 値 → 色（over/exhausted=赤, tight/on_track=紫, spare/fresh=緑, unknown=灰）。
/// tight（リセット時予測 92-100%）は余った枠をほぼ使い切れる状態で警告ではないため、on_track と同じ紫。
/// F5: fresh（リセット直後）は spare と同じ緑系。
enum StatusColor {
    static func color(for status: String?) -> Color {
        switch status {
        case "over", "exhausted": return .red
        case "tight", "on_track": return .purple
        case "spare", "fresh": return .green
        default: return .gray
        }
    }

    static func nsColor(for status: String?) -> NSColor {
        switch status {
        case "over", "exhausted": return .systemRed
        case "tight", "on_track": return .systemPurple
        case "spare", "fresh": return .systemGreen
        default: return .systemGray
        }
    }

    /// 悪い方を 1..5 の小さい値で。dot 表示用に最悪値を求める。
    static func rank(for status: String?) -> Int {
        switch status {
        case "over", "exhausted": return 1
        case "tight": return 2
        case "on_track": return 3
        case "spare", "fresh": return 4
        default: return 5
        }
    }

    static func worstStatus(of cards: [StatusCard]) -> String? {
        cards.min { rank(for: $0.status) < rank(for: $1.status) }?.status
    }
}
