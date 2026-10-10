import Foundation

// What the open talk panel lists: the conversation of record by day, then the talk that is on show and not in the conversation yet.

/// One row of that list, his or hers, with the time to write above it when the gap before it is long.
struct TalkRow: Identifiable, Equatable {
    let id: String                      // `c<seq>` of the conversation, `l<n>` of the live talk
    var his: Bool
    var text: String
    var alphas: [Double] = []
    var pics: [SentItem] = []
    var leadIn = false
    var growing = false
    var at = 0.0                        // seconds since 1970
    var stamp: String?
    var current = false                 // a row of the talk on show
    var card: WaitingCard?              // not words: a card she put up while she talks (ADR 0215)
    var about: About?                   // not words: the chip of the item he has open, the first row of the talk
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

    /// The conversation by day, oldest first. `pics` are what he sent, by the number of his row.
    static func convDays(_ rows: [ConversationRow], pics: [Int: [SentItem]], cal: Calendar, now: Date) -> [TalkDay] {
        var out: [TalkRow] = []
        for (i, r) in rows.enumerated() {
            let text = r.isHis ? r.text.trimmingCharacters(in: .whitespacesAndNewlines) : AnswerText.visible(r.text)
            guard !text.isEmpty, let at = time(r.ts, cal) else { continue }
            // a spoken line before his next words was the whole answer; only one that more of hers follows (or may follow) is a lead-in
            let leadIn = r.isLeadIn && !(i + 1 < rows.count && rows[i + 1].isHis)
            out.append(TalkRow(id: "c\(r.seq)", his: r.isHis, text: text, pics: r.isHis ? pics[r.seq] ?? [] : [], leadIn: leadIn, at: at.timeIntervalSince1970))
        }
        return group(out, [], cal: cal, now: now)
    }

    /// The days with the live talk after them, under today's date.
    static func compose(_ days: [TalkDay], live: [LiveTurn], about: About? = nil, cal: Calendar, now: Date) -> [TalkDay] {
        if live.isEmpty { return days }
        var rows = live.map(row)
        if let about { rows.insert(TalkRow(id: "about", his: false, text: about.title, at: rows[0].at, current: true, about: about), at: 0) }
        return group(rows, days, cal: cal, now: now)
    }
    static func row(_ t: LiveTurn) -> TalkRow {
        TalkRow(id: "l\(t.id)", his: t.his, text: t.text, alphas: t.alphas, pics: t.pics, leadIn: t.leadIn, growing: t.growing, at: t.at.timeIntervalSince1970, current: true, card: t.card)
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
