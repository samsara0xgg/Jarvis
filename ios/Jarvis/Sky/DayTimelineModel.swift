import Foundation

// The whole day as one list for the home screen (Sky/DayTimeline.swift draws it), built from the brain's answer for that day (`DayReply`).
// One story: sleep, then the places he stayed in as groups with what happened there as rows, the trips between the groups as rows of their own,
// a line for now, and what is still to come after it. Plain values only, so the order, the words and the places on the day can be tested without a screen.
// Times are minutes from the day's own midnight (a stay that began the evening before starts below 0).

/// the heights of the list's rows, in design points: fixed, so where a row stands is known without asking the view
enum TLMetrics {
    static let sleep = 52.0, stay = 40.0, trip = 36.0, row = 44.0, rowDetail = 52.0, now = 30.0, note = 44.0
}

/// 1 时 40 分, 40 分: the length of a span in the list
func tlDur(_ minutes: Int) -> String {
    let m = max(0, minutes)
    if m < 60 { return "\(m) 分" }
    return m % 60 == 0 ? "\(m / 60) 时" : "\(m / 60) 时 \(m % 60) 分"
}

/// one line of the list
struct TLEntry: Identifiable, Equatable {
    enum Kind: Equatable { case sleep, stay, trip, talk, picture, event, reminder, departure, work, call, now }
    let id: String
    var kind: Kind
    var t0: Double
    var time = ""                   // the fixed column at the left, `HH:mm`
    var title: String
    var detail: String?             // the one muted line under the title
    var more: [String] = []         // what opening the row says in full, a line each
    var symbol = ""                 // an SF Symbol, drawn filled once it has happened and as an outline while it has not
    var right = ""                  // the mono text at the right: how long, and for a stay its hours too
    var color: RGB?                 // a stay: the colour of its place
    var kept = false                // a stay in a place he keeps (a stay in none is a neutral haze)
    var lit = false                 // marked as what mattered: a lit star before the title
    var later = false               // not yet: after the line for now
    var talkStart: Int64?           // a talk: when it began, for 全部 ›
    var attachment: String?         // a picture: the brain's name for it

    var height: Double {
        switch kind {
        case .sleep: TLMetrics.sleep
        case .stay: TLMetrics.stay
        case .trip: detail == nil ? TLMetrics.trip : TLMetrics.rowDetail
        case .now: TLMetrics.now
        default: detail == nil ? TLMetrics.row : TLMetrics.rowDetail
        }
    }
    /// a tap on it opens something: the first exchange of a talk, the picture, or lines that do not fit on one
    var opens: Bool { kind == .talk || kind == .picture || !more.isEmpty }
}

/// a span of the day on the overview bar
struct TLSeg: Equatable {
    enum Kind: Equatable { case place(RGB, kept: Bool), move, sleep }
    var id: String
    var t0: Double, t1: Double
    var kind: Kind
    /// what is on top where spans overlap
    var priority: Int { switch kind { case .sleep: 3; case .move: 2; case .place: 1 } }
}

/// one column of the overview bar
struct TLCell: Equatable {
    var r: Double, g: Double, b: Double, a: Double
    static let track = TLCell(r: 255, g: 255, b: 255, a: 0.06)         // nothing recorded
    static func mix(_ x: TLCell, _ y: TLCell, _ k: Double) -> TLCell {
        TLCell(r: lerp(x.r, y.r, k), g: lerp(x.g, y.g, k), b: lerp(x.b, y.b, k), a: lerp(x.a, y.a, k))
    }
}

struct DayTimelineModel: Equatable {
    var date: String
    var line: String?               // her sentence about the day
    var entries: [TLEntry]
    var segs: [TLSeg]
    var span: ClosedRange<Double>   // what the overview bar covers: from the first thing of the day (or 06:00) to 24:00
    var now: Double?                // the minute of now, on today only
    var missing: String?            // "还没读到：Mac 的时间"

    var isEmpty: Bool { entries.allSatisfy { $0.kind == .now } }
    /// where the line for now stands in the list
    var nowIndex: Int? { entries.firstIndex { $0.kind == .now } }

    // MARK: where things stand

    /// the top of each entry from the top of the list, and the whole height
    var offsets: [Double] {
        var y = 0.0
        return entries.map { e in defer { y += e.height }; return y }
    }
    var height: Double { entries.reduce(0) { $0 + $1.height } + (missing == nil ? 0 : TLMetrics.note) }
    func top(of id: String) -> Double? { entries.firstIndex { $0.id == id }.map { offsets[$0] } }

    /// the entry that covers minute `t`: the last one begun by then, else the first
    func entry(at t: Double) -> TLEntry? {
        let list = entries.filter { $0.kind != .now }
        return list.last { $0.t0 <= t } ?? list.first
    }
    /// which stretch (a stay, a trip, the night, or none) minute `t` is in
    func stretch(at t: Double) -> String {
        var best: TLSeg?
        for s in segs where s.t0 <= t && t < s.t1 && s.priority > (best?.priority ?? 0) { best = s }
        return best?.id ?? ""
    }

    // MARK: the overview bar

    func cells(_ n: Int) -> [TLCell] {
        let lo = span.lowerBound, w = span.upperBound - lo
        return (0..<n).map { c in
            let t = lo + (Double(c) + 0.5) / Double(n) * w
            var best: TLSeg?
            for s in segs where s.t0 <= t && t < s.t1 && s.priority > (best?.priority ?? 0) { best = s }
            guard let s = best else { return .track }
            switch s.kind {
            case .place(let col, let kept): return TLCell(r: col.r, g: col.g, b: col.b, a: kept ? 0.85 : 0.28)
            case .move: return TLCell(r: 196, g: 208, b: 255, a: 0.35)
            case .sleep: return TLCell(r: 112, g: 120, b: 205, a: 0.9)
            }
        }
    }

    // MARK: building

    /// `day` is the day number (0 is the day `Cal.cur` counts from), `now` the minute of now when this is today. `topic` names a talk by what it was about.
    static func build(_ r: DayReply, day: Int, now: Double?, topic: (String) -> String? = { _ in nil }) -> DayTimelineModel {
        let base = Double(day) * DAY
        func m(_ ms: Int64) -> Double { Cal.cur.t(ms: ms) - base }
        func text(_ s: String?) -> String? { s.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.flatMap { $0.isEmpty ? nil : $0 } }
        let marks = Dictionary(r.highlights.map { ($0.id, $0.label) }, uniquingKeysWith: { a, _ in a })
        var out: [(rank: Int, e: TLEntry)] = []
        var segs: [TLSeg] = []

        for i in r.items {
            let t0 = m(i.start_ms), t1 = i.end_ms.map(m)
            let begun = now.map { t0 <= $0 } ?? true
            let later = now.map { t0 > $0 } ?? false
            let running = t1 == nil && begun && now != nil          // today, begun, no end yet: it goes on
            let end = t1 ?? (running ? now : nil)
            let span = end.map { max(0, $0 - t0) }
            let dur = span.flatMap { $0 >= 1 ? tlDur(Int($0.rounded())) : nil }
            var e = TLEntry(id: i.id, kind: .work, t0: t0, title: "")
            e.time = clock(t0); e.later = later
            switch i.kind {
            case "sleep":
                e.kind = .sleep
                e.title = "睡觉"
                e.symbol = "moon.fill"
                e.detail = rangeText(t0, t1, running: running)
                e.right = i.minutes.map { tlDur(Int($0.rounded())) } ?? dur ?? ""
                if let end { segs.append(TLSeg(id: i.id, t0: max(0, t0), t1: min(DAY, end), kind: .sleep)) }
            case "stay":
                e.kind = .stay
                e.title = i.ringName?.name ?? "没名字的地方"
                if let k = i.kept_place {
                    e.color = PlaceTint.line(role: k.role, id: k.id); e.kept = true
                } else { e.color = PlaceTint.lineNeutral }
                // a stay still on has its start and how long it has gone; one that ended, its hours and its length
                e.right = [t1 == nil ? "\(clockDay(t0)) 起" : rangeText(t0, t1, running: false), dur].compactMap { $0 }.joined(separator: " · ")
                // on another day a stay with no end goes on to that day's midnight
                if let end = end ?? (now == nil ? DAY : nil) { segs.append(TLSeg(id: i.id, t0: max(0, t0), t1: min(DAY, end), kind: .place(e.color!, kept: e.kept))) }
            case "move", "ride":
                e.kind = .trip
                e.right = dur ?? ""
                if i.kind == "ride" {
                    e.symbol = "bus.fill"
                    let stops = [text(i.board_stop), text(i.alight_stop)]
                    let trail = stops.compactMap { $0 }.joined(separator: " → ")
                    e.title = text(i.route).map { "\($0) 路" } ?? "公交"
                    e.detail = trail.isEmpty ? nil : trail          // one stop is that stop alone, none no line at all
                    e.more = [stops[0].map { "上车 " + $0 }, stops[1].map { "下车 " + $0 }].compactMap { $0 }
                } else {
                    let (word, symbol) = Self.moves[i.activity ?? ""] ?? ("移动", "arrow.right")
                    e.title = word; e.symbol = symbol
                }
                if let end { segs.append(TLSeg(id: i.id, t0: max(0, t0), t1: min(DAY, end), kind: .move)) }
            case "talk":
                e.kind = .talk
                e.title = topic(i.id) ?? "和她说话"
                e.symbol = "bubble.left.fill"
                e.detail = "\(i.turns ?? 1) 句"
                e.right = dur ?? ""
                e.talkStart = i.start_ms
            case "picture":
                e.kind = .picture
                e.title = "照片"
                e.symbol = "photo.fill"
                let lines = (i.note ?? "").split(whereSeparator: \.isNewline).compactMap { text(String($0)) }
                e.detail = lines.first
                e.more = lines
                e.attachment = i.id.hasPrefix("picture-") ? String(i.id.dropFirst("picture-".count)) : i.id
            case "event":
                e.kind = .event
                let hidden = i.private == true
                e.title = hidden ? "私事" : text(i.title) ?? "日程"
                e.symbol = "calendar.circle.fill"
                // a mail made it and he has not said it is his: say so, and where (a private one names no place)
                let parts = [i.confirmed == false && i.source != "plan" ? "从邮件里收的，还没确认" : nil, hidden ? nil : text(i.place)].compactMap { $0 }
                e.detail = parts.isEmpty ? nil : parts.joined(separator: " · ")
                e.more = parts
                e.right = dur ?? ""
            case "reminder":
                e.kind = .reminder
                e.title = text(i.text) ?? "提醒"
                e.symbol = "bell.fill"
                e.detail = i.fired == true ? (i.acknowledged == true ? "已经响过，你确认过了" : "已经响过，你还没确认") : "还没到点"
            case "departure":
                e.kind = .departure
                e.title = text(i.route).map { "\($0) 路 发车" } ?? text(i.title) ?? "发车"
                e.symbol = "bus.fill"
                let parts = [text(i.board_stop).map { "在\($0)上车" }, text(i.to).map { "去\($0)" }, i.leave_at_ms.map { clock(m($0)) + " 出门" }].compactMap { $0 }
                e.detail = parts.isEmpty ? nil : parts.joined(separator: " · ")
                e.more = parts
            case "work":
                e.kind = .work
                e.symbol = "laptopcomputer"
                let top = i.projects?.first?.name ?? i.apps?.first?.name
                e.title = text(top) ?? "在 Mac 上工作"
                // how long he was at it is the active time, not the whole stretch
                e.right = i.active_s.map { tlDur(Int(($0 / 60).rounded())) } ?? dur ?? ""
                let parts = [("项目", i.projects), ("应用", i.apps)].compactMap { name, tops -> String? in
                    guard let tops, !tops.isEmpty else { return nil }
                    return name + " " + tops.map { "\($0.name) \(tlDur(max(1, Int(($0.seconds / 60).rounded()))))" }.joined(separator: "、")
                }
                e.detail = parts.isEmpty ? nil : parts.joined(separator: " · ")
                e.more = parts
            case "call":
                e.kind = .call
                e.title = "通话"
                e.symbol = "phone.fill"
                e.detail = text(i.app)
                e.right = dur ?? ""
            default:
                continue            // a kind this phone does not know yet: left out, not guessed at
            }
            if let label = marks[i.id], e.kind != .stay { e.title = label; e.lit = true }
            out.append((e.kind == .sleep ? 0 : e.kind == .stay ? 1 : e.kind == .trip ? 2 : 3, e))
        }

        // in the order of the day; at the same minute a night comes first, then the place, then the trip, then what happened
        var seen = Set<String>()
        var entries = out.enumerated().sorted { ($0.element.e.t0, $0.element.rank, $0.offset) < ($1.element.e.t0, $1.element.rank, $1.offset) }
            .map { $0.element.e }.filter { seen.insert($0.id).inserted }
        if let now {
            var line = TLEntry(id: "now", kind: .now, t0: now, title: "现在 " + clock(now))
            line.time = clock(now)
            entries.insert(line, at: entries.firstIndex { $0.t0 > now } ?? entries.count)
        }
        let first = entries.first { $0.kind != .now }?.t0 ?? 360
        return DayTimelineModel(date: r.date, line: text(r.line?.text), entries: entries, segs: segs,
                                span: max(0, min(first, 360))...DAY, now: now, missing: missingText(r.missing))
    }

    /// the footnote under the list: what has not been read, named by what it gives the day (the dial words it its own way: `BrainLink.missingText`)
    static func missingText(_ missing: [String]) -> String? {
        let names = ["phone": "手机的记录", "reminders": "提醒", "mac": "Mac 的时间", "talks": "对话", "calendar": "日历", "departures": "公交"]
        let m = missing.compactMap { names[$0] }
        return m.isEmpty ? nil : "还没读到：" + m.joined(separator: "、")
    }

    private static let moves: [String: (String, String)] = [
        "walking": ("走路", "figure.walk"), "running": ("跑步", "figure.run"), "cycling": ("骑车", "bicycle"), "automotive": ("开车", "car.fill"),
    ]

    /// `09:05–11:25`; a stay that began the evening before says so, and one still on reads `18:50 起`
    private static func rangeText(_ t0: Double, _ t1: Double?, running: Bool) -> String {
        guard let t1, !running else { return clockDay(t0) + " 起" }
        return clockDay(t0) + "–" + clock(t1)
    }
    private static func clockDay(_ t: Double) -> String { (t < 0 ? "昨天 " : "") + clock(t) }
}
