// words: legacy
import Foundation

// The day's list as one row for each stretch of what he was doing (the brain's doing lane, ADR 0302; docs in .claude/dispatch/doing-rows-spec.md). The brain cuts the waking day into
// stretches back to back and says what he did in each, so a row is a stretch: its time, what he was doing, where, and how long, with small icons for what is inside it (pictures,
// payments, calls, messages). Opened, it says the model's line about it, what is inside it a line each, and how she knows. The night stays as it was drawn and so does what is still to
// come; what waits on him keeps a row of its own at its time. The phone makes no words and no cuts of its own: what the brain serves is what is drawn.

/// How a category of the doing lane is drawn: its colour on the row's icon and on the bar (none: a hole, like where nothing was recorded), and its symbol. The same table is the Mac's.
struct TLCategory: Equatable {
    var color: RGB?
    var symbol: String

    static func of(_ name: String?) -> TLCategory {
        switch name {
        case "work": TLCategory(color: RGB(91, 140, 255), symbol: "laptopcomputer")
        case "study": TLCategory(color: RGB(155, 123, 255), symbol: "book.fill")
        case "meal": TLCategory(color: RGB(240, 176, 79), symbol: "fork.knife")
        case "rest": TLCategory(color: RGB(111, 168, 200), symbol: "sofa.fill")
        case "sleep": TLCategory(color: TLPaint.sleep, symbol: "moon.fill")
        case "exercise": TLCategory(color: RGB(76, 195, 138), symbol: "figure.run")
        case "commute": TLCategory(color: TLPaint.trip, symbol: "arrow.right")         // a trip is drawn as its way (a bus for a ride): the row takes the symbol from the trip inside it
        case "errand": TLCategory(color: RGB(232, 135, 90), symbol: "bag.fill")
        case "social": TLCategory(color: RGB(233, 121, 166), symbol: "person.2.fill")
        case "home": TLCategory(color: RGB(168, 159, 145), symbol: "house.fill")
        default: TLCategory(color: nil, symbol: "questionmark")                          // unknown, and one this phone has not heard of
        }
    }
}

/// what is inside a stretch that is told in numbers, under its title: pictures, payments, calls (a call on the Mac and one on the phone), messages
struct TLCounts: Equatable {
    var pictures = 0, payments = 0, calls = 0, messages = 0

    var isEmpty: Bool { pictures + payments + calls + messages == 0 }
    /// the symbol, the word VoiceOver says and the number of each that is not none, in this order
    var shown: [(symbol: String, word: String, n: Int)] {
        [("photo.fill", "照片", pictures), ("creditcard.fill", "付款", payments), ("phone.fill", "通话", calls), ("message.fill", "消息", messages)].filter { $0.2 > 0 }.map { (symbol: $0.0, word: $0.1, n: $0.2) }
    }
}

/// one line inside an opened stretch
struct TLLine: Equatable, Identifiable {
    let id: String
    var symbol: String
    var title: String
    var detail: String?
    var right: String               // how long it was inside the stretch, what it came to, or the time of a moment
    var strong = false              // `right` is the point of the line (what a payment came to)
    var attachment: String?         // a picture: the brain's name for it
}

/// a stretch of the doing lane in minutes from the day's midnight
struct TLStretch {
    let span: DaySpan
    let t0: Double, t1: Double
    let going: Bool                 // today, and no end yet: it goes on to now
}

extension DayTimelineModel {
    /// The doing lane's stretches up to now, in time order: `doing` and `unknown` (a plan is after now and is not what he did). One with no end goes on to now, or to midnight on another day;
    /// one that reaches past now ends there, and one that begins at or after it is not drawn.
    static func doingStretches(_ r: DayReply, now: Double?, _ m: (Int64) -> Double) -> [TLStretch] {
        r.spans.compactMap { s -> TLStretch? in
            guard s.lane == "doing", s.kind == "doing" || s.kind == "unknown" else { return nil }
            let a = m(s.start_ms), b = min(s.end_ms.map(m) ?? now ?? DAY, now ?? .infinity)
            guard a.isFinite, b.isFinite, b > a, now.map({ a < $0 }) ?? true else { return nil }
            return TLStretch(span: s, t0: a, t1: b, going: s.end_ms == nil && now != nil)
        }.sorted { $0.t0 < $1.t0 }
    }

    /// The list when it is stretches: the night (every sleep, as it is drawn), a row for each stretch, and what is still to come or waits on him, as rows of their own. A talk is no row.
    /// `rows` are the day's items as rows, `items` the items they are of, in the same order.
    static func segmentList(_ stretches: [TLStretch], items: [DayItem], rows: [TLEntry], now: Double?, _ f: TLFrame) -> [TLEntry] {
        var index: [String: Int] = [:]
        for (k, i) in items.enumerated() { index[i.id] = k }
        var out = rows.filter { $0.kind == .sleep || ($0.kind != .talk && ($0.later || $0.waiting)) }
        soon(&out, now: now)
        var taken = Set(out.map(\.id))
        for st in stretches where taken.insert(st.span.id).inserted {
            var seen = Set<String>()
            let inside = (st.span.item_ids ?? []).compactMap { id -> (item: DayItem, row: TLEntry)? in
                guard seen.insert(id).inserted, let k = index[id] else { return nil }          // an id that is not among the day's items is skipped
                return (items[k], rows[k])
            }
            let place = st.span.place_id.flatMap { index[$0] }.flatMap { rows[$0].kind == .stay ? rows[$0].barName : nil }
            out.append(segmentRow(st, inside: inside, place: place, f))
        }
        return out
    }

    /// One stretch as a row. Its title is the words the Mac uses for what he was doing (`doingWords`), else `不知道在干嘛`; under it the place he was at (the stay the brain points to) and
    /// `不太确定` when the brain is not sure, and the numbers of what is inside.
    static func segmentRow(_ st: TLStretch, inside: [(item: DayItem, row: TLEntry)], place: String?, _ f: TLFrame) -> TLEntry {
        let s = st.span, words = doingWords(s), cat = TLCategory.of(s.category)
        var e = TLEntry(id: s.id, kind: .segment, t0: st.t0, title: words ?? "不知道在干嘛")
        e.time = clock(st.t0); e.t1 = st.t1; e.going = st.going
        e.unknown = words == nil
        e.right = tlDur(tlInt(st.t1 - st.t0))
        // a trip wears the way he went: a bus for a ride, else the trip's symbol; an observed call (the one fact the program titles without a category) wears the phone, as on the Mac
        e.symbol = s.category == "commute" ? inside.first { $0.row.kind == .trip }?.row.symbol ?? cat.symbol
            : s.category == "unknown" && s.basis == "observed" && words != nil ? "phone.fill" : cat.symbol
        e.color = cat.color
        e.rail = .place(cat.color ?? PlaceTint.lineNeutral)
        let under = [place, s.confidence == "low" ? "不太确定" : nil].compactMap { $0 }.joined(separator: " · ")
        e.detail = under.isEmpty ? nil : under
        e.note = tlText(s.detail)
        e.knows = knows(s)
        for x in inside {
            switch x.row.kind {
            case .picture: e.counts.pictures += 1
            case .payment: e.counts.payments += 1
            case .call, .phoneCall: e.counts.calls += 1
            case .messages: e.counts.messages += 1
            default: break
            }
            if let l = x.row.lens { e.lenses.insert(l) }
        }
        e.lines = inside.enumerated().filter { ![.talk, .stay, .sleep].contains($0.element.row.kind) }
            .sorted { ($0.element.row.t0, $0.offset) < ($1.element.row.t0, $1.offset) }.map { line($0.element.row, in: st) }
        // the readout's head and the bar's word: what he was doing, its hours and its length; a hole wears no icon
        let when = [s.end_ms == nil ? "\(clockDay(st.t0, f)) 起" : rangeText(st.t0, st.t1, running: false, f), e.right].joined(separator: " · ")
        e.head = e.title + " · " + when
        e.barName = words
        e.barIcon = cat.color == nil ? nil : e.symbol
        // 问她: the stretch is put to her as the thing the brain can be told about that it is mostly made of, the place first
        let named = inside.first { $0.item.id == s.place_id }, first = inside.first { $0.row.kind != .talk && $0.row.kind != .sleep && About.kinds.contains($0.item.kind) }
        if let a = named ?? first, About.kinds.contains(a.item.kind), About.fits(id: a.item.id) {
            e.about = About(kind: a.item.kind, id: a.item.id, title: e.title, startMs: s.start_ms, mark: mark(a.item, later: false))
        }
        return e
    }

    /// An item as a line inside a stretch: what its own row says, and at the right how long of it was in the stretch when it began before or went on after it, else what its row says there
    /// (how long, what it came to), else the time. A moment has its time.
    private static func line(_ row: TLEntry, in st: TLStretch) -> TLLine {
        var l = TLLine(id: row.id, symbol: row.symbol, title: row.title, detail: row.detail, right: row.right.isEmpty ? row.time : row.right, strong: row.strongRight, attachment: row.attachment)
        if let end = row.t1, end - row.t0 >= 1, row.t0 < st.t0 || end > st.t1 {
            l.right = tlDur(tlInt(min(end, st.t1) - max(row.t0, st.t0))); l.strong = false
        }
        return l
    }

    /// how she knows what he was doing: who said it, then what she went by
    private static func knows(_ s: DaySpan) -> String? {
        let who = ["stated": "你说的", "observed": "记录里看到的", "inferred": "她猜的"][s.basis ?? ""]
        let clues = (s.clues ?? []).compactMap { tlText($0.text) }.joined(separator: "；")
        switch (who, clues.isEmpty) {
        case (let w?, false): return w + "：" + clues
        case (let w?, true): return w
        case (nil, false): return clues
        case (nil, true): return nil
        }
    }
}
