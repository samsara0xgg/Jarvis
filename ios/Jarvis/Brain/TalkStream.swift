// words: legacy
import Foundation

// What the open talk panel lists: the conversation of record by day, then the talk that is on show and not in the conversation yet.

/// One row of that list, his or hers, with the time to write above it when the gap before it is long.
struct TalkRow: Identifiable, Equatable {
    let id: String                      // `c<seq>` of the conversation, `l<n>` of the live talk
    var his: Bool
    var text: String
    var reveal: Reveal?
    var pics: [SentItem] = []
    var leadIn = false
    var growing = false
    var at = 0.0                        // seconds since 1970
    var stamp: String?
    var current = false                 // a row of the talk on show
    var card: WaitingCard?              // not words: a card she put up while she talks (ADR 0215)
    var about: About?                   // not words: the chip of the item he has open, the first row of the talk
    var status: StatusLine?             // not words yet: the typing dots or the step she is on, in her place
    var steps: StepsInfo?               // what she did to answer: the small line under her words
    var failed = false                  // her turn failed: `text` says so, and a button sends the same words again
    var send = SendState.sent           // his message: on its way, or not sent, with a button that sends it again under the same id
    var recordId: String?               // the conversation record of these words, which a reaction names; a live row has it once the brain has told (ADR 0256)
    var reactions = RowReactions()      // his and hers on this message, with the frames since the conversation was read
    var places: [AnswerPlace] = []      // the places her answer names: a map under it
    var links: [AnswerLink] = []        // the rows of his day and trips her answer points at: chips under it
}

struct TalkDay: Identifiable, Equatable {
    let id: String                      // `YYYY-MM-DD`
    var title: String
    var rows: [TalkRow]
}

enum TalkStream {
    /// a gap this long (seconds) between two rows, or a new day, puts the time above the next row
    static let gap = 15.0 * 60

    /// The conversation rows to list. While a talk is on show, the rows it brought are its live rows' business: listing both would say everything twice.
    static func shown(_ conv: [ConversationRow], base: Int, talking: Bool) -> [ConversationRow] {
        talking ? conv.filter { $0.seq <= base } : conv
    }

    /// The talk is over and the conversation has its rows: the live rows have done their job.
    static func handedOver(_ conv: [ConversationRow], base: Int, talking: Bool) -> Bool {
        !talking && conv.contains { $0.seq > base }
    }

    /// The conversation by day, oldest first. A row of his with files and no words is listed for its pictures.
    static func convDays(_ rows: [ConversationRow], cal: Calendar, now: Date) -> [TalkDay] {
        var out: [TalkRow] = []
        for (i, r) in rows.enumerated() {
            let text = r.isHis ? r.text.trimmingCharacters(in: .whitespacesAndNewlines) : AnswerText.visible(r.text)
            guard !text.isEmpty || (r.isHis && !r.attachments.isEmpty), let at = time(r.ts, cal) else { continue }
            // a spoken line before his next words was the whole answer; only one that more of hers follows (or may follow) is a lead-in
            let leadIn = r.isLeadIn && !(i + 1 < rows.count && rows[i + 1].isHis)
            out.append(TalkRow(id: "c\(r.seq)", his: r.isHis, text: text, pics: r.isHis ? SentPics.items(r.attachments) : [], leadIn: leadIn, at: at.timeIntervalSince1970,
                               steps: r.isHis || leadIn ? nil : r.steps, recordId: r.id.isEmpty ? nil : r.id, reactions: r.reactions, places: r.isHis || leadIn ? [] : r.places, links: r.isHis || leadIn ? [] : r.links))
        }
        return group(out, [], cal: cal, now: now)
    }

    /// A message he can put a reaction on: it has a record id and is words or pictures, not a card, the typing line, a failure or the chip.
    static func reactable(_ r: TalkRow) -> Bool {
        r.recordId != nil && r.status == nil && r.card == nil && r.about == nil && !r.failed && (!r.text.isEmpty || !r.pics.isEmpty)
    }

    /// The days with the frames since the conversation was read laid over each row's reactions
    static func reacted(_ days: [TalkDay], _ overlay: ReactionOverlay) -> [TalkDay] {
        if overlay.isEmpty { return days }
        return days.map { day in
            var day = day
            for i in day.rows.indices {
                guard let id = day.rows[i].recordId else { continue }
                let merged = overlay.merged(day.rows[i].reactions, target: id)
                if merged != day.rows[i].reactions { day.rows[i].reactions = merged }
            }
            return day
        }
    }

    /// What 拷贝 copies from a row: the words as they read, without markup; nil for a row with no words (the typing line, a card, the chip, a failure)
    static func copyText(_ r: TalkRow) -> String? {
        guard r.status == nil, r.card == nil, r.about == nil, !r.failed else { return nil }
        let t = r.his ? r.text : AnswerBlocks.plain(r.text)
        return t.isEmpty ? nil : t
    }

    /// How much of each row shows in the reading zone under the dial (the panel shut), by the order the rows are listed in: the newest round, from his last line on, in full; the round before it
    /// (from his line before that), dimmer; everything older not at all. Nothing shows until the talk has a row of its own, so the last round of the conversation does not flash up as the newest.
    static func reading(_ rows: [TalkRow]) -> [Double] {
        guard rows.contains(where: \.current) else { return rows.map { _ in 0 } }
        let cur = rows.lastIndex(where: \.his) ?? 0
        let prev = rows[..<cur].lastIndex(where: \.his) ?? cur
        return rows.indices.map { $0 >= cur ? 1 : $0 >= prev ? 0.5 : 0 }
    }

    /// The days with the live talk after them, under today's date. A chip put on before his first line (问她) waits after the days, where the line will go: it is not a row of the talk
    /// yet, so the dial's reading zone does not take the conversation's last round for the newest.
    static func compose(_ days: [TalkDay], live: [LiveTurn], about: About? = nil, overlay: ReactionOverlay = ReactionOverlay(), cal: Calendar, now: Date) -> [TalkDay] {
        if live.isEmpty {
            guard let about else { return reacted(days, overlay) }
            return reacted(group([TalkRow(id: "about", his: false, text: about.title, at: now.timeIntervalSince1970, about: about)], days, cal: cal, now: now), overlay)
        }
        var rows = live.map(row)
        if let about { rows.insert(TalkRow(id: "about", his: false, text: about.title, at: rows[0].at, current: true, about: about), at: 0) }
        return reacted(group(rows, days, cal: cal, now: now), overlay)
    }
    static func row(_ t: LiveTurn) -> TalkRow {
        TalkRow(id: "l\(t.id)", his: t.his, text: t.text, reveal: t.reveal, pics: t.pics, leadIn: t.leadIn, growing: t.growing, at: t.at.timeIntervalSince1970, current: true, card: t.card, status: t.status, steps: t.steps, failed: t.failed, send: t.send, recordId: t.recordId, places: t.places, links: t.links)
    }

    /// `rows` after `days`: each row joins the last day when it is on that day, and the time goes above a row after a long gap or a new day
    private static func group(_ rows: [TalkRow], _ days: [TalkDay], cal: Calendar, now: Date) -> [TalkDay] {
        var days = days
        var prev = days.last?.rows.last?.at
        for var r in rows {
            let date = Date(timeIntervalSince1970: r.at), id = dayId(date, cal)
            if days.last?.id != id { days.append(TalkDay(id: id, title: title(date, cal, now), rows: [])); prev = nil }
            if prev == nil || r.at - prev! >= gap { r.stamp = clock(date, cal) }
            prev = r.at
            days[days.count - 1].rows.append(r)
        }
        return days
    }

    // MARK: dates

    /// today, yesterday, then the date and weekday
    static func title(_ date: Date, _ cal: Calendar, _ now: Date) -> String {
        let d = cal.startOfDay(for: date), t = cal.startOfDay(for: now)
        let diff = cal.dateComponents([.day], from: d, to: t).day ?? 0
        if diff == 0 { return "今天" }
        if diff == 1 { return "昨天" }
        let c = cal.dateComponents([.month, .day, .weekday], from: date)
        return "\(c.month!)月\(c.day!)日 周" + ["日", "一", "二", "三", "四", "五", "六"][c.weekday! - 1]
    }
    static func dayId(_ date: Date, _ cal: Calendar) -> String {
        let c = cal.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year!, c.month!, c.day!)
    }
    static func clock(_ date: Date, _ cal: Calendar) -> String {
        let c = cal.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", c.hour!, c.minute!)
    }

    /// the brain's local time `2026-10-09T23:52:02` (fractions and zone suffix ignored), in the calendar's zone
    static func time(_ ts: String, _ cal: Calendar) -> Date? {
        parser.timeZone = cal.timeZone
        return parser.date(from: String(ts.prefix(19)))
    }
    private static let parser: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f
    }()
}
