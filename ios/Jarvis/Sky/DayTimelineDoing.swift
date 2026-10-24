// words: legacy
import Foundation

// The day's list as one row for each stretch of what he was doing (the brain's doing lane, ADR 0302; docs in .claude/dispatch/doing-rows-spec.md). The brain cuts the waking day into
// stretches back to back and says what he did in each, so a row is a stretch: its time, what he was doing, a line in words, and how long. Two or more stretches in a row at a place he
// keeps are one group under the place's name, its rail through them; a trip is a group of its own, and so is every other row. Opened in place, a stretch says one thing a line in time
// order: each run of his attention with a dot of its colour, what was on his phone alongside, what happened in it with its icon, and what took him off it; a stretch she is not sure of asks
// him in one line. The night stays as it was drawn and so does what is still to come; what waits on him keeps a row of its own at its time. The phone makes no words and no cuts of its
// own: what the brain serves is what is drawn.

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

/// One line inside an opened stretch: its time, a dot for a run of his attention or an icon for anything else, its words, and at the right how long or what it came to.
struct TLLine: Equatable, Identifiable {
    enum Mark: Equatable {
        case dot(RGB?)              // a run of his attention, in its category's colour (none: one with no category)
        case claude(cloud: Bool)    // a Claude Code session: the Claude mark, with a cloud on its corner for one a bridge ran for a claude.ai thread
        case icon(String)           // anything else, by its symbol
        case drift                  // what took him off it: an empty ring
    }
    /// a line whose words are the timeline's own (`Words.Timeline`), put together where it is drawn
    enum Said: Equatable {
        case drift(minutes: Int, switches: Int)
        case onDevice(String, String)           // the device it was on, and what
        case more(Int)                          // the runs past the first few
        case background(Int)                    // Claude Code sessions that ran by themselves
        case session(cloud: Bool, prompts: Int?)          // a Claude Code session by its title (`title`; empty: it has none), a bridge's for a claude.ai thread or one of the Mac's own
        case received(String, people: Int, count: Int)          // messages that only came in, of one app at about the same time
    }
    let id: String
    var time: String?               // `HH:mm`; none for what has no time of its own (what was on his phone alongside, the drift)
    var mark: Mark
    var title = ""
    var said: Said?
    var detail: String?
    var right = ""                  // how long it was inside the stretch, what it came to, or nothing
    var strong = false              // `right` is the point of the line (what a payment came to)
    var attachment: String?         // a picture: the brain's name for it
    var span: ClosedRange<Double>?  // its time on the bar, framed for a moment when it is tapped
}

/// What a stretch asks him, in one line: the brain's question with its options, else (she is not sure, and asks nothing) her words for it put to him with one answer, 是, or (nothing was
/// recorded) a line to fill in. Writing a line of his own is always the last.
struct TLAsk: Equatable {
    var text: String?               // the brain's question; none: `Words.Timeline.isIt` her words, or for a hole `fillIn`
    var options: [String] = []      // the brain's answers, each a chip
    var yes: String?                // with no options: the words a 是 stands for (the stretch's own title)
    var hole = false
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
            out.append(segmentRow(st, inside: inside, places: rows, f))
        }
        return out
    }

    /// One stretch as a row. Its title is the words the Mac uses for what he was doing (`doingWords`), else `没有记录`; its line says where he was (dropped when the stretch is in a group
    /// under its place) and the model's line about it. `places` are the day's rows, among which the stays name the place the brain points to (`place_id`, a place he keeps).
    static func segmentRow(_ st: TLStretch, inside: [(item: DayItem, row: TLEntry)], places: [TLEntry], _ f: TLFrame) -> TLEntry {
        let s = st.span, words = doingWords(s), hole = words == nil
        // an observed call (the one fact the program titles without a category) wears the phone and a call's colour, as on the Mac; a hole wears nothing of its place: the group says where
        let call = s.category == "unknown" && s.basis == "observed" && !hole
        let cat = TLCategory.of(hole ? nil : call ? "social" : s.category)
        var e = TLEntry(id: s.id, kind: .segment, t0: st.t0, title: words ?? "没有记录")
        e.time = clock(st.t0); e.t1 = st.t1; e.going = st.going
        e.unknown = hole
        e.right = tlDur(tlInt(st.t1 - st.t0))
        // a trip wears the way he went (a bus for a ride), else the trip's symbol
        e.symbol = s.category == "commute" ? inside.first { $0.row.kind == .trip }?.row.symbol ?? cat.symbol : call ? "phone.fill" : cat.symbol
        e.color = cat.color
        // where he was: the place he keeps that the brain points to (its stay names it and gives its hue), else the stay that holds most of the stretch
        let kept = s.place_id.flatMap { id in inside.first { $0.row.place?.keptId == id }?.row ?? places.first { $0.kind == .stay && $0.place?.keptId == id } }
        if let kept, let id = s.place_id { e.group = id; e.rail = .place(kept.color ?? PlaceTint.lineNeutral) }
        e.at = kept?.title ?? mainStay(inside, st)?.barName
        e.note = tlText(s.detail)
        e.detail = line(e, solo: true)
        e.unsure = !hole && (s.question != nil || (s.basis == "inferred" && s.confidence == "low"))
        e.commute = s.category == "commute"
        if hole || e.unsure {
            let options = (s.question?.options ?? []).compactMap { tlText($0) }
            e.ask = TLAsk(text: tlText(s.question?.text), options: options, yes: hole || !options.isEmpty ? nil : tlText(s.title), hole: hole)
        }
        e.knows = knows(s)
        for x in inside { if let l = x.row.lens { e.lenses.insert(l) } }
        e.lines = lines(s, st, inside: inside, f)
        // the readout's head and the bar's word: what he was doing, its hours and its length; a hole wears a question mark
        let when = [s.end_ms == nil ? "\(clockDay(st.t0, f)) 起" : rangeText(st.t0, st.t1, running: false, f), e.right].joined(separator: " · ")
        e.head = e.title + " · " + when
        e.barName = words
        e.barIcon = hole ? "questionmark" : e.symbol
        // 问她: the stretch is put to her as the thing the brain can be told about that it is mostly made of, the place first
        let at = inside.first { $0.row.kind == .stay && $0.row.place?.keptId != nil && $0.row.place?.keptId == s.place_id }
        let first = inside.first { $0.row.kind != .talk && $0.row.kind != .sleep && About.kinds.contains($0.item.kind) }
        if let a = at ?? first, About.kinds.contains(a.item.kind), About.fits(id: a.item.id) {
            e.about = About(kind: a.item.kind, id: a.item.id, title: e.title, startMs: s.start_ms, mark: mark(a.item, later: false))
        }
        return e
    }

    /// a stretch's line: where he was, unless the group it is in says it (`solo`), and the model's line about it
    static func line(_ e: TLEntry, solo: Bool) -> String? {
        let parts = [solo ? e.at : nil, e.note].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// the stay among what is inside a stretch that holds most of it
    private static func mainStay(_ inside: [(item: DayItem, row: TLEntry)], _ st: TLStretch) -> TLEntry? {
        func held(_ r: TLEntry) -> Double { max(0, min(r.t1 ?? st.t1, st.t1) - max(r.t0, st.t0)) }
        return inside.map { $0.row }.filter { $0.kind == .stay && held($0) > 0 }.max { held($0) < held($1) }
    }

    /// The runs of his attention a stretch has that are drawn as lines: the first eight, the rest are one line
    static let runsShown = 8

    /// What a stretch says opened, a line each: each run of his attention (its lead, what it was made of, how long), what was on his phone or tablet alongside, what happened in it in time
    /// order (a talk, a place and the night are not: the place is the group, the night is a row), and last what took him off it. Where his attention is known, the Mac's work blocks and his
    /// screens are not lines of their own: the runs say them.
    private static func lines(_ s: DaySpan, _ st: TLStretch, inside: [(item: DayItem, row: TLEntry)], _ f: TLFrame) -> [TLLine] {
        let att = s.attention
        let threads = Dictionary((att?.threads ?? []).map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        let runs = (att?.rows ?? []).compactMap { r -> (row: DayAttention.Row, t0: Double, t1: Double)? in
            let a = f.m(r.start_ms), b = f.m(r.end_ms)
            return a.isFinite && b.isFinite && b > a ? (r, a, b) : nil
        }.sorted { $0.t0 < $1.t0 }
        func minutes(_ ms: Double?) -> Int { tlInt((ms ?? 0) / 60_000) }
        var timed: [(t: Double, line: TLLine)] = []
        for (k, x) in runs.prefix(runsShown).enumerated() {
            let t = threads[x.row.thread], name = tlText(t?.name) ?? x.row.thread
            let made = (t?.keys ?? []).compactMap { tlText($0.name) }.filter { $0 != name }.prefix(2)
            timed.append((x.t0, TLLine(id: "\(s.id)-run-\(k)", time: clock(x.t0), mark: .dot(TLCategory.of(t?.category).color), title: name,
                                       detail: made.isEmpty ? nil : made.joined(separator: " · "), right: tlDur(max(1, minutes(x.row.ms ?? (x.t1 - x.t0) * 60_000))), span: x.t0...x.t1)))
        }
        let moments: Set<TLEntry.Kind> = runs.isEmpty ? [.talk, .stay, .sleep] : [.talk, .stay, .sleep, .work, .screen]
        let (folded, foldedLines) = folds(s, st, inside: inside)
        timed += foldedLines
        for x in inside where !moments.contains(x.row.kind) && !folded.contains(x.item.id) {
            timed.append((max(x.row.t0, st.t0), line(x.row, in: st)))
        }
        var out = timed.enumerated().sorted { ($0.element.t, $0.offset) < ($1.element.t, $1.offset) }.map { $0.element.line }
        if runs.count > runsShown {
            out.append(TLLine(id: "\(s.id)-more", mark: .icon("ellipsis"), said: .more(runs.count - runsShown)))
        }
        // what his phone or tablet had in front while he was at the Mac: the three that held it longest, a minute or more
        for (k, t) in (att?.alongside ?? []).filter({ minutes($0.ms) >= 1 }).sorted(by: { ($0.ms ?? 0) > ($1.ms ?? 0) }).prefix(3).enumerated() {
            let device = t.`where` == "iPad" ? "iPad" : "iPhone"
            out.append(TLLine(id: "\(s.id)-along-\(k)", mark: .icon(device == "iPad" ? "ipad" : "iphone"), said: .onDevice(device, tlText(t.name) ?? t.id), right: tlDur(minutes(t.ms))))
        }
        // what took him off the runs, said and not judged: its minutes, how often he left, and the two things that took most of it
        if !runs.isEmpty {
            let off = minutes(runs.reduce(0) { $0 + ($1.row.others_ms ?? 0) }), away = runs.reduce(0) { $0 + max(0, $1.row.switches ?? 0) }
            let leads = Set(runs.map { $0.row.thread }), along = Set((att?.alongside ?? []).map(\.id))
            let others = (att?.threads ?? []).filter { !leads.contains($0.id) && !along.contains($0.id) && minutes($0.ms) >= 1 }
                .sorted { ($0.ms ?? 0) > ($1.ms ?? 0) }.prefix(2).map { "\(tlText($0.name) ?? $0.id) \(minutes($0.ms))" }
            if off > 0 || away > 0 {
                out.append(TLLine(id: "\(s.id)-drift", mark: .drift, said: .drift(minutes: off, switches: away), detail: others.isEmpty ? nil : others.joined(separator: " · ")))
            }
        }
        return out
    }

    /// What a stretch says in fewer lines than its items. Claude Code, never by the repo it ran in: the sessions a Remote Control bridge ran for one claude.ai thread
    /// (`remote_session`) are one line, by the thread's name (`thread_title`, else a session's `title`); each other session he typed in (or the brain does not say) is a line by its
    /// title; the ones that ran by themselves (`prompts` 0) one line. Messages that only came in, of one app within 10 minutes of each other, one line.
    private static func folds(_ s: DaySpan, _ st: TLStretch, inside: [(item: DayItem, row: TLEntry)]) -> (ids: Set<String>, lines: [(t: Double, line: TLLine)]) {
        func clamp(_ r: TLEntry) -> (Double, Double) { let a = max(r.t0, st.t0); return (a, max(a, min(r.t1 ?? a, st.t1))) }
        var ids: Set<String> = [], out: [(t: Double, line: TLLine)] = []
        let codes = inside.filter { $0.row.kind == .code }
        ids.formUnion(codes.map(\.item.id))
        func named(_ xs: [(item: DayItem, row: TLEntry)]) -> String { xs.lazy.compactMap { tlText($0.item.thread_title) }.first ?? xs.lazy.compactMap { tlText($0.item.title) }.first ?? "" }
        for (thread, xs) in Dictionary(grouping: codes.filter { tlText($0.item.remote_session) != nil }, by: { tlText($0.item.remote_session) ?? "" }) {
            let spans = xs.map { clamp($0.row) }.sorted { $0.0 < $1.0 }
            var union = 0.0, edge = -Double.infinity
            for (a, b) in spans { union += max(0, b - max(a, edge)); edge = max(edge, b) }
            let t0 = spans[0].0, told = xs.compactMap(\.item.prompts)
            out.append((t0, TLLine(id: "\(s.id)-thread-\(thread)", time: clock(t0), mark: .claude(cloud: true), title: named(xs), said: .session(cloud: true, prompts: told.isEmpty ? nil : told.reduce(0, +)),
                                   right: union >= 1 ? tlDur(tlInt(union)) : "", span: t0...edge)))
        }
        let own = codes.filter { tlText($0.item.remote_session) == nil }
        for x in own where x.item.prompts.map({ $0 > 0 }) ?? true {
            var l = line(x.row, in: st)
            l.mark = .claude(cloud: false); l.title = named([x]); l.said = .session(cloud: false, prompts: x.item.prompts); l.detail = nil
            out.append((clamp(x.row).0, l))
        }
        let alone = own.filter { ($0.item.prompts ?? 1) <= 0 }
        if let first = alone.map({ clamp($0.row).0 }).min() {
            out.append((first, TLLine(id: "\(s.id)-code-alone", mark: .claude(cloud: false), said: .background(alone.count))))
        }
        // bursts that only came in: by app, in time order, each within 10 minutes of the one before
        let heard = inside.filter { $0.row.kind == .messages && ($0.item.sent ?? 0) <= 0 && ($0.item.received ?? 0) > 0 }.sorted { $0.row.t0 < $1.row.t0 }
        for (app, xs) in Dictionary(grouping: heard, by: { tlText($0.item.app) ?? "" }) where !app.isEmpty {
            var runs: [[(item: DayItem, row: TLEntry)]] = []
            for x in xs {
                if let last = runs.last?.last, x.row.t0 - (last.row.t1 ?? last.row.t0) <= 10 { runs[runs.count - 1].append(x) } else { runs.append([x]) }
            }
            for run in runs where run.count > 1 {
                let (t0, _) = clamp(run[0].row), t1 = run.map { clamp($0.row).1 }.max() ?? t0
                let people = Set(run.map { tlText($0.item.conversation) ?? $0.item.id }).count, count = run.reduce(0) { $0 + max(0, $1.item.received ?? 0) }
                out.append((t0, TLLine(id: "\(s.id)-heard-\(run[0].item.id)", time: clock(t0), mark: .icon("message.fill"), said: .received(app, people: people, count: count), span: t0...t1)))
                ids.formUnion(run.map(\.item.id))
            }
        }
        return (ids, out.sorted { $0.t < $1.t })
    }

    /// An item as a line inside a stretch: what its own row says, and at the right how long of it was in the stretch when it began before or went on after it, else what its row says there
    /// (how long, what it came to). Its time is when it began, or the stretch's start for what began before it.
    private static func line(_ row: TLEntry, in st: TLStretch) -> TLLine {
        let t0 = max(row.t0, st.t0), end = row.t1.map { min($0, st.t1) }
        var l = TLLine(id: row.id, time: clock(t0), mark: .icon(row.symbol), title: row.title, detail: row.detail, right: row.right, strong: row.strongRight, attachment: row.attachment,
                       span: t0...max(t0, end ?? t0))
        if row.kind != .messages, let full = row.t1, full - row.t0 >= 1, row.t0 < st.t0 || full > st.t1 {          // messages say how many, not how long
            l.right = tlDur(tlInt((end ?? t0) - t0)); l.strong = false
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

    // MARK: groups

    /// A day of stretches as groups. Two or more stretches in a row at one place he keeps are one group under a header with the place's dot, its name, its hours and how long, with the
    /// place's rail from the dot through them (what waits on him at a time inside the run is in the group too, on the rail); their lines no longer say the place. A trip is a group of its
    /// own, its dashed rail reaching out through the blanks either side of it; every other row is a group of its own, with no rail. A group has 12 pt of blank above it, the line for now
    /// 10, and what is still to come follows it with none.
    static func grouped(_ list: [TLEntry], _ f: TLFrame) -> [TLEntry] {
        var out: [TLEntry] = []
        var k = 0
        while k < list.count {
            // a run at one place he keeps: its stretches, and what happened inside them
            if list[k].kind == .segment, let id = list[k].group, case .place(let hue) = list[k].rail {
                var run = [k], stretches = 1, end = list[k].t1 ?? list[k].t0, j = k + 1
                while j < list.count {
                    let x = list[j]
                    if x.kind == .segment {
                        guard x.group == id else { break }
                        stretches += 1; end = x.t1 ?? x.t0
                    } else if x.kind == .now || x.later || x.kind == .sleep || x.t0 >= end {
                        break
                    }
                    run.append(j); j += 1
                }
                if stretches >= 2 {
                    let first = list[k], last = list[run.filter { list[$0].kind == .segment }.last ?? k]
                    let name = first.at ?? ""
                    var h = TLEntry(id: "place-" + first.id, kind: .stay, t0: first.t0, title: name)
                    h.header = true; h.time = first.time; h.t1 = last.t1; h.going = last.going
                    h.color = hue; h.kept = true; h.group = id
                    let on = last.going || last.t1 == nil
                    h.right = [rangeText(first.t0, last.t1, running: on, f), on ? nil : tlDur(tlInt((last.t1 ?? first.t0) - first.t0))].compactMap { $0 }.joined(separator: " · ")
                    h.rail = .place(hue); h.railOut = true
                    h.about = first.about.flatMap { $0.kind == "stay" ? About(kind: $0.kind, id: $0.id, title: name, startMs: $0.startMs, mark: $0.mark) : nil }          // 问她 of the place
                    h.gap = out.isEmpty ? 0 : TLMetrics.gap
                    out.append(h)
                    for (n, i) in run.enumerated() {
                        var e = list[i]
                        if e.kind == .segment { e.detail = line(e, solo: false) }
                        e.rail = .place(hue); e.railIn = true; e.railOut = n < run.count - 1
                        e.gap = 0
                        out.append(e)
                    }
                    k = j
                    continue
                }
            }
            var e = list[k]
            let trip = e.kind == .segment && e.symbol != "" && e.color == TLPaint.trip
            if e.kind == .segment && !trip { e.rail = .none }
            if trip { e.rail = .trip; e.railIn = true; e.railOut = true }
            e.gap = out.isEmpty || e.later ? 0 : e.kind == .now ? TLMetrics.nowGap : TLMetrics.gap
            if let p = out.last, p.rail == .trip, p.kind == .segment, e.gap > 0 { e.gapRail = .trip }          // the trip's rail goes on down through the blank after it
            if trip && e.gap > 0 { e.gapRail = .trip }
            out.append(e)
            k += 1
        }
        return out
    }
}
