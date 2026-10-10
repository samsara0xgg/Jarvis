import Foundation

// What /phone/ws carries for this device's turns besides her words (ADR 0215, jarvis/surface/phone_link.py): the card she puts up or takes down, and the times her answer names.
// Rows that are her words are the voice session's. The session offers every row it receives to `PhoneRows.offer`;
// the engine takes what came, once a frame, and shows it.

/// One time an answer refers to: what the turn did (a reminder it set, an Outlook event it made or moved, the first leave time of a bus card), as an instant.
struct AnswerTime: Equatable {
    let kind: String                    // reminder | event | departure
    let atMs: Int64
    let label: String
    let ref: String                     // the reminder's id, the event's, the card's: a reminder is a star of the day under the same id
}

enum PhoneEvent: Equatable {
    /// `clarification.requested`: a card is waiting. It is a card, not words: it is never part of what she says.
    case card(WaitingCard)
    /// `clarification.withdrawn`: she took the card down (the brain's id of it)
    case cardGone(String)
    /// the final `surface.response_emitted`'s `times`, in the order the turn did them
    case times([AnswerTime])
}

enum PhoneRows {
    /// what a row says to the screen, or nil when it is only her words (or a row this phone does not read)
    static func event(_ r: RowFrame) -> PhoneEvent? {
        switch r.eventType {
        case "clarification.requested":
            guard let id = r.payload["clarification_id"] as? String, !id.isEmpty else { return nil }
            return .card(WaitingCards.clarification(r.payload, id))
        case "clarification.withdrawn":
            guard let id = r.payload["clarification_id"] as? String, !id.isEmpty else { return nil }
            return .cardGone(id)
        case "surface.response_emitted":
            guard r.phase != "commentary", let list = r.payload["times"] as? [[String: Any]] else { return nil }
            let times = list.compactMap { t -> AnswerTime? in
                guard let kind = t["kind"] as? String, let at = (t["at_ms"] as? NSNumber)?.int64Value else { return nil }
                return AnswerTime(kind: kind, atMs: at, label: (t["label"] as? String) ?? "", ref: (t["ref"] as? String) ?? "")
            }
            return times.isEmpty ? nil : .times(times)
        default: return nil
        }
    }

    @MainActor private static var queue: [PhoneEvent] = []
    @MainActor static func offer(_ r: RowFrame) { if let e = event(r) { queue.append(e) } }
    @MainActor static func take() -> [PhoneEvent] {
        if queue.isEmpty { return [] }
        defer { queue = [] }
        return queue
    }
}
