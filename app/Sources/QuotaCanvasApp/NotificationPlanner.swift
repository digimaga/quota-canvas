import Foundation

struct NotificationCandidate: Decodable {
    let key: String
    let kind: String
    let card: StatusCard?
    let name: String?
    let expiresAt: String?
    enum CodingKeys: String, CodingKey { case key, kind, card, name; case expiresAt = "expires_at" }
}

/// Display-only conversion. Candidate selection lives in the Python engine.
enum NotificationPresentation {
    struct Planned { let key: String; let title: String; let body: String }

    static func format(_ candidate: NotificationCandidate, now: Date) throws -> Planned {
        switch candidate.kind {
        case "status":
            guard let card = candidate.card else { throw EngineError(message: "Notification candidate has no card") }
            return Planned(key: candidate.key, title: title(of: card), body: statusBody(card, now: now))
        case "reset":
            guard let card = candidate.card else { throw EngineError(message: "Notification candidate has no card") }
            return Planned(key: candidate.key, title: title(of: card), body: resetBody(card, now: now))
        case "burst":
            guard let card = candidate.card, let date = card.burstExhaustsAt else { throw EngineError(message: "Invalid burst candidate") }
            return Planned(key: candidate.key, title: title(of: card), body: burstBody(card, at: date, now: now))
        case "credit":
            guard let raw = candidate.expiresAt, let date = ISO8601Support.parse(raw), let name = candidate.name else { throw EngineError(message: "Invalid credit candidate") }
            let rel = relativeText(date, now: now)
            return Planned(key: candidate.key, title: String(localized: "リセット権の期限"),
                           body: String(localized: "\(name) のリセット権が \(rel) に失効します"))
        default:
            throw EngineError(message: "Unknown notification candidate: \(candidate.kind)")
        }
    }

    // MARK: - text helpers

    private static func title(of card: StatusCard) -> String {
        card.name ?? card.provider ?? "QuotaCanvas"
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

}
