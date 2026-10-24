// words: legacy
import Foundation

// The item he has open when he talks (ADR 0214): `about` on `POST /inherent/submit` and on the phone's `say` frame of /phone/ws.
// The host checks it twice and refuses the whole turn when it is malformed (422 / `bad_say`), so the phone only offers what the host can take.

struct About: Equatable {
    let kind: String
    let id: String
    let title: String
    let startMs: Int64
    var endMs: Int64?                   // a stretch of what he was doing is named by its window (ADR 0410): only `doing` carries an end
    var mark = Kd.next                  // the star's own mark, for the chip

    /// the item's time as the chip writes it: the clock today, with 明天 or the day in front of it on another day
    func when(_ cal: Calendar, _ now: Date) -> String {
        let d = Date(timeIntervalSince1970: Double(startMs) / 1000)
        let day = cal.isDate(d, inSameDayAs: now) ? "" : cal.isDate(d, inSameDayAs: cal.date(byAdding: .day, value: 1, to: now)!) ? "明天 " : TalkStream.title(d, cal, now) + " "
        return day + TalkStream.clock(d, cal)
    }

    /// the wire object: `{kind, id, title, start_ms}`, and `end_ms` for a stretch (any other kind with an end is refused). The id goes whole; a cut id names another item.
    var json: [String: Any] {
        var o: [String: Any] = ["kind": kind, "id": id, "title": title, "start_ms": startMs]
        if kind == "doing", let endMs { o["end_ms"] = endMs }
        return o
    }

    /// what the phone may have open: the kinds of the day line (jarvis/shared/about.py `ABOUT_KINDS`), and a stretch of what he was doing. A run of Claude Code sessions folded into one row
    /// is no one item, and has none.
    static let kinds: Set<String> = ["reminder", "event", "todo", "stay", "move", "sleep", "work", "call", "talk", "picture", "ride", "departure", "payment", "claude_chat", "claude_day", "workout", "code", "doing"]

    /// the id shape the host accepts: 1-256 of `[A-Za-z0-9_-=+/.|:@]` (the `@` is a commitment's: `commitment-<hex>@<day>`)
    static func fits(id: String) -> Bool {
        let n = id.unicodeScalars.count
        guard (1...256).contains(n) else { return false }
        return id.unicodeScalars.allSatisfy { $0.isASCII && (CharacterSet.alphanumerics.contains($0) || "_-=+/.|:@".unicodeScalars.contains($0)) }
    }

    /// The chip for a star of the brain's day, or nil when it is not an item the host can be told about (a prototype star, a card, an id it would refuse).
    static func of(_ e: Ev) -> About? {
        guard e.live, let kind = e.kind, kinds.contains(kind), let ms = e.startMs, fits(id: e.id) else { return nil }
        return About(kind: kind, id: e.id, title: e.title, startMs: ms)
    }
}

/// The chip that shows now, for whoever sends a turn without going through the engine (the voice session's `say`).
@MainActor enum AboutNow {
    static var current: About?
}
