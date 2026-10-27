import Foundation

/// F4 通知: status.json を読み直すたびに前回との差分を見て、出すべき通知を返す純粋な型。
/// View / UNUserNotificationCenter / UserDefaults に依存しない
/// （StatusStore → NotificationCoordinator が結果を投稿し、キーを保存する）。
///
/// 通知するのは次の 4 種類。どれも key を UserDefaults に保存して、
/// 同じキーは二度と出さない（アプリ再起動後も）:
/// - カードの status が over / exhausted に入った（同じ期間に同じ状態で 1 回）
/// - カードの resets_at が変わった（リセットされた。60 秒の許容で比較）
/// - reset_credits の 24 時間以内に失効する権利（権利ごとに 1 回。previous に関係なく出す）
/// - burst_exhausts_at が新たに現れた（同じ期間で 1 回）
///
/// previous == nil（起動直後の初回読み込み）ではカードの遷移は差分の基準にするだけ
/// （リセット権は上記のとおり出る）。
enum NotificationPlanner {
    struct Planned: Equatable {
        /// 通知済み管理用の安定キー（UserDefaults に保存される）
        var key: String
        var title: String
        var body: String
    }

    /// reset_credits の「まもなく失効」判定窓（24 時間）
    static let creditWindow: TimeInterval = 24 * 3600
    /// リセット時刻の同一視許容（エンジン history の _same_reset と同じ 60 秒）
    static let resetTolerance: TimeInterval = 60
    /// 通知キーに入れる期間のバケット幅。わずかなずれでキーが変わらないよう 5 分単位
    static let periodBucketSeconds: TimeInterval = 300

    static func plan(previous: EngineStatus?, current: EngineStatus,
                     now: Date = Date(), notified: Set<String>) -> [Planned] {
        guard let previous else {
            // 初回読み込み: カードの遷移は基準にするだけ。
            // リセット権は previous に関係なく出すので、権限が確定する前の読み込みで
            // 投稿・保存できなかった権利も、次の refresh で自然に出る。
            return creditNotifications(current: current, now: now, notified: notified)
        }
        let prevCards = keyedCards(previous)
        var out: [Planned] = []

        for card in current.cards ?? [] {
            let id = cardKey(card)
            let period = periodBucket(card)
            let prev = prevCards[id]

            // status が over / exhausted に入った（同じ期間に同じ状態で 1 回。
            // 期間が変われば同じ状態でももう 1 回出す）
            if let status = card.status, status == "over" || status == "exhausted" {
                let key = "status|\(id)|\(period)|\(status)"
                let entered = prev?.status != status || !isSameReset(prev?.resetsAt, card.resetsAt)
                if entered && !notified.contains(key) {
                    out.append(Planned(key: key, title: title(of: card), body: statusBody(card, now: now)))
                }
            }

            // resets_at が変わった（リセットされた）。文字列ではなくパースした時刻を 60 秒の許容で比較
            if let newDate = card.resetsAt, let prevDate = prev?.resetsAt,
               !isSameReset(newDate, prevDate) {
                let key = "reset|\(id)|\(period)"
                if !notified.contains(key) {
                    out.append(Planned(key: key, title: title(of: card), body: resetBody(card, now: now)))
                }
            }

            // burst_exhausts_at が新たに現れた（同じ期間で 1 回）
            if card.burstExhaustsAtRaw != nil, prev?.burstExhaustsAtRaw == nil,
               let burst = card.burstExhaustsAt {
                let key = "burst|\(id)|\(period)"
                if !notified.contains(key) {
                    out.append(Planned(key: key, title: title(of: card), body: burstBody(card, at: burst, now: now)))
                }
            }
        }

        out.append(contentsOf: creditNotifications(current: current, now: now, notified: notified))
        return out
    }

    // MARK: - reset_credits

    /// 24 時間以内に失効する権利の通知。previous の有無・前回の窓内外に関係なく、
    /// 「今窓内で、通知済みキーに無い」ものだけを出す（権利ごとに 1 回）。
    /// 保存は投稿受理後なので、権限が確定する前の読み込みで出せなかった権利は
    /// 確定後の refresh で自然に出る。
    private static func creditNotifications(current: EngineStatus,
                                            now: Date, notified: Set<String>) -> [Planned] {
        var out: [Planned] = []
        for (provider, credits) in (current.resetCredits ?? [:]).sorted(by: { $0.key < $1.key }) {
            for credit in credits {
                guard let expires = credit.expiresAt else { continue }
                let interval = expires.timeIntervalSince(now)
                guard interval > 0, interval <= creditWindow else { continue } // 24 時間以内
                let key = "credit|\(provider)|\(credit.title ?? "")|\(credit.expiresAtRaw ?? "")"
                guard !notified.contains(key) else { continue }
                let name = displayName(provider, in: current)
                let rel = relativeText(expires, now: now)
                out.append(Planned(
                    key: key,
                    title: String(localized: "リセット権の期限"),
                    body: String(localized: "\(name) のリセット権が \(rel) に失効します")))
            }
        }
        return out
    }

    /// エンジン history の _same_reset と同じ: API が resets_at を数 ms〜数秒揺らすので
    /// パースした時刻を 60 秒の許容で同一視する（文字列の完全一致では繰り返し通知が出る）。
    private static func isSameReset(_ a: Date?, _ b: Date?) -> Bool {
        switch (a, b) {
        case let (x?, y?): return abs(x.timeIntervalSince(y)) <= resetTolerance
        case (nil, nil): return true
        default: return false
        }
    }

    /// 通知キー用の期間バケット（epoch / periodBucketSeconds）。ずれでキーが変わらないようにする。
    private static func periodBucket(_ card: StatusCard) -> String {
        guard let date = card.resetsAt else { return "-" }
        return String(Int(date.timeIntervalSince1970 / periodBucketSeconds))
    }

    // MARK: - text helpers

    private static func title(of card: StatusCard) -> String {
        card.name ?? card.provider ?? "QuotaCanvas"
    }

    private static func displayName(_ provider: String, in status: EngineStatus) -> String {
        status.cards?.first { $0.provider == provider }?.name ?? provider
    }

    private static func statusBody(_ card: StatusCard, now: Date) -> String {
        let label: String
        if let l = card.statusLabel, !l.isEmpty {
            label = l
        } else {
            label = card.status == "exhausted" ? String(localized: "枯渇") : String(localized: "ペース超過")
        }
        guard let reset = card.resetsAt, reset > now else { return label }
        return label + " · " + String(localized: "リセットまで あと\(relativeText(reset, now: now))")
    }

    private static func resetBody(_ card: StatusCard, now: Date) -> String {
        var body = String(localized: "リセットされました")
        if let reset = card.resetsAt, reset > now {
            body += " · " + String(localized: "リセットまで あと\(relativeText(reset, now: now))")
        }
        return body
    }

    private static func burstBody(_ card: StatusCard, at burst: Date, now: Date) -> String {
        String(localized: "直近のペースが速いので \(relativeText(burst, now: now)) に尽きる見込み")
    }

    private static func relativeText(_ date: Date, now: Date) -> String {
        RelativeDateTimeFormatter().localizedString(for: date, relativeTo: now)
    }

    private static func keyedCards(_ status: EngineStatus?) -> [String: StatusCard] {
        var out: [String: StatusCard] = [:]
        for card in status?.cards ?? [] {
            out[cardKey(card)] = card
        }
        return out
    }

    private static func cardKey(_ card: StatusCard) -> String {
        card.id ?? "\(card.provider ?? "?")|\(card.label ?? "?")"
    }
}
