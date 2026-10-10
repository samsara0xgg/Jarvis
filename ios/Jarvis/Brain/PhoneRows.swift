import Foundation

// What /phone/ws carries for this device's turns besides her words (ADR 0215, jarvis/surface/phone_link.py): the card she puts up or takes down.
// Rows that are her words are the voice session's. The session offers every row it receives to `PhoneRows.offer`;
// the engine takes what came, once a frame, and shows it.

enum PhoneEvent: Equatable {
    /// `clarification.requested`: a card is waiting. It is a card, not words: it is never part of what she says.
    case card(WaitingCard)
    /// `clarification.withdrawn`: she took the card down (the brain's id of it)
    case cardGone(String)
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
