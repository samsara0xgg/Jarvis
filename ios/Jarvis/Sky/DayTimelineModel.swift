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

/// 7:50, 1:05, and under an hour 18 分: a length as the chips and the bar write it
func tlHM(_ minutes: Int) -> String {
    let m = max(0, minutes)
    return m < 60 ? "\(m) 分" : "\(m / 60):" + String(format: "%02d", m % 60)
}

/// $12.40, ¥68.00, `AUD 5.00`: an amount with the mark of its currency (a currency this phone has no mark for is written by its code)
func tlMoney(_ cents: Int, _ currency: String?) -> String {
    let whole = String(abs(cents) / 100), frac = String(format: "%02d", abs(cents) % 100)
    var grouped = ""
    for (k, ch) in whole.reversed().enumerated() { if k > 0, k % 3 == 0 { grouped.append(",") }; grouped.append(ch) }
    let num = String(grouped.reversed()) + "." + frac, sign = cents < 0 ? "-" : ""
    let code = (currency ?? "").trimmingCharacters(in: .whitespaces).uppercased()
    let marks = ["CAD": "$", "USD": "$", "CNY": "¥", "EUR": "€", "GBP": "£", "JPY": "JP¥", "HKD": "HK$"]
    if code.isEmpty { return sign + num }
    return sign + (marks[code] ?? code + " ") + num
}

/// one line of the list
struct TLEntry: Identifiable, Equatable {
    enum Kind: Equatable { case sleep, stay, trip, talk, picture, event, reminder, departure, work, call, payment, claude, now }
    let id: String
    var kind: Kind
    var t0: Double
    var t1: Double?                 // when it ended, or now while it goes on; none for a moment (and for what has no end yet)
    var time = ""                   // the fixed column at the left, `HH:mm`
    var title: String
    var detail: String?             // the one muted line under the title
    var more: [String] = []         // what opening the row says in full, a line each
    var symbol = ""                 // an SF Symbol, drawn filled once it has happened and as an outline while it has not
    var right = ""                  // the mono text at the right: how long, and for a stay its hours too
    var color: RGB?                 // a stay: the colour of its place
    var kept = false                // a stay in a place he keeps (a stay in none is a neutral haze)
    var strongRight = false         // the mono text at the right is the point of the row (what a payment came to), so it is not muted
    var lit = false                 // marked as what mattered: a lit star before the title
    var later = false               // not yet: after the line for now
    var talkStart: Int64?           // a talk: when it began, for 全部 ›
    var attachment: String?         // a picture: the brain's name for it
    var about: About?               // the chip it gives a talk while it is the row opened (ADR 0214): only an item of a kind the brain's host knows
    var lens: TLLens?               // the kind of the day's numbers it is counted in (a stay, an event and the rest are in none)
    var barName: String?            // the word it wears inside the overview bar when there is room: the place, the route, the way he moved, how long he slept, the event
    var barIcon: String?            // and the symbol, for when the word does not fit
    var head: String?               // a stretch (the night, a place, a trip) as the readout under a finger opens with it: `家 · 09:05–11:25 · 2 时 20 分`
    var glance: String?             // what the readout says of it when it was going on at the minute under a finger: `Jarvis · Xcode`, `A&W · $12.40`

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

/// an item that mattered (a highlight of the day) and when it was
struct TLStar: Equatable, Identifiable {
    let id: String
    let t: Double
}

/// A kind of thing the day's numbers bring forward: pressing its chip dims the rest of the bar and the list.
enum TLLens: CaseIterable, Equatable {
    case sleep, trip, work, call, talk, payment, picture

    /// the word on its chip: the same on the Mac
    var word: String {
        switch self {
        case .sleep: "睡"
        case .trip: "路上"
        case .work: "Mac"
        case .call: "通话"
        case .talk: "和她"
        case .payment: "花"
        case .picture: "照片"
        }
    }
    var symbol: String {
        switch self {
        case .sleep: "moon.fill"
        case .trip: "figure.walk"
        case .work: "laptopcomputer"
        case .call: "phone.fill"
        case .talk: "bubble.left.fill"
        case .payment: "creditcard.fill"
        case .picture: "photo.fill"
        }
    }
    /// its colour on the chip's symbol and, when it is the lens, on the bar
    var color: RGB {
        switch self {
        case .sleep: RGB(112, 120, 205)
        case .trip: RGB(196, 208, 255)
        case .work: RGB(108, 156, 255)
        case .call: RGB(111, 224, 180)
        case .talk: RGB(172, 124, 255)
        case .payment: RGB(240, 132, 218)
        case .picture: RGB(255, 240, 220)
        }
    }
}

/// one of the day's numbers: `睡 8:05`, `路上 3:04`, `Mac 2:20`, `通话 18 分`, `和她 2 次`, `花 $12.40`, `照片 1`
struct TLChip: Equatable, Identifiable {
    let lens: TLLens
    let value: String
    var id: String { word }
    var word: String { lens.word }
}

/// What a finger on the bar reads out: the stretch of the day under it, and what else was going on there.
struct TLReadout: Equatable {
    var hue: RGB?                   // the colour of the stretch (a dot); none when there is no stretch
    var strong = true               // a place he keeps or a trip: the dot is full
    var head: String                // `家 · 09:05–11:25 · 2 时 20 分`
    var elsewhere: [String]         // up to four things that were going on, the most telling first
    var ahead = false               // it has not happened yet
    /// the line under it on a phone: the first two things, or that there was nothing
    var second: String { elsewhere.isEmpty ? (ahead ? "这段时间没有安排" : "这段时间没有别的记录") : elsewhere.prefix(2).joined(separator: " · ") }
}

/// where something of a lens's kind is on the bar: a span, or a moment (its end is its start)
struct TLMark: Equatable {
    var t0: Double, t1: Double
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

struct DayTimelineModel: Equatable {
    var date: String
    var line: String?               // her sentence about the day
    var entries: [TLEntry]
    var segs: [TLSeg]
    var span: ClosedRange<Double>   // what the overview bar covers: from its start (`axisStart`) to 24:00
    var now: Double?                // the minute of now, on today only
    var missing: String?            // "还没读到：Mac 的时间"
    var stars: [TLStar] = []        // what mattered, in the order of the day: a lit star on the bar for each
    var chips: [TLChip] = []        // the day's numbers, in a fixed order, only the ones that are not nothing

    /// the hours the bar is ticked at: every third hour inside its span (06 09 12 15 18 21 24)
    var ticks: [Int] { stride(from: Int((span.lowerBound / 180).rounded(.up)) * 3, through: 24, by: 3).map { $0 } }

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

    /// Where the list rests on today, from its top: the line for now about 60% of the way down `room`, and a row beginning at the top rather than half of one (the row
    /// boundary nearest to that, 8 pt down: the blank under a row's last line, so the row above leaves nothing in the list's fade). Another day rests at its top.
    func restY(room: Double) -> Double {
        guard let i = nowIndex else { return 0 }
        let want = offsets[i] + TLMetrics.now / 2 - 0.6 * room
        return max(0, (offsets.min { abs($0 - want) < abs($1 - want) } ?? 0) - 8)
    }

    /// the entry that covers minute `t`: the last one begun by then, else the first
    func entry(at t: Double) -> TLEntry? {
        let list = entries.filter { $0.kind != .now }
        return list.last { $0.t0 <= t } ?? list.first
    }
    /// the stretch (a stay, a trip, the night) minute `t` is in: the night is on top of a trip, a trip of a stay
    func seg(at t: Double) -> TLSeg? {
        var best: TLSeg?
        for s in segs where s.t0 <= t && t < s.t1 && s.priority > (best?.priority ?? 0) { best = s }
        return best
    }
    /// its id, or none
    func stretch(at t: Double) -> String { seg(at: t)?.id ?? "" }

    /// What a finger on the bar at minute `t` reads out. The stretch there (a place, a trip, the night) opens it, or how long nothing was recorded, or that it has not happened;
    /// then what else was going on: what covers `t`, else what is within 10 minutes of it, the most telling first (work, a call, a talk, a picture, a payment, an event, Claude, a reminder, a bus).
    func readout(at t: Double) -> TLReadout {
        var hue: RGB?, strong = true, head: String
        let ahead = now.map { t > $0 } ?? false
        if ahead {
            head = "还没到"
        } else if let s = seg(at: t), let e = entries.first(where: { $0.id == s.id }) {
            switch s.kind {
            case .place(let c, let kept): hue = c; strong = kept
            case .move: hue = TLPaint.trip
            case .sleep: hue = TLPaint.sleep
            }
            head = e.head ?? e.title
        } else {
            let lo = segs.filter { $0.t1 <= t }.map(\.t1).max() ?? span.lowerBound
            let hi = min(segs.filter { $0.t0 > t }.map(\.t0).min() ?? DAY, now ?? DAY)
            head = "没有记录 · \(clock(lo))–\(clock(hi)) · \(tlDur(Int((hi - lo).rounded())))"
        }
        func gap(_ e: TLEntry) -> Double { let end = max(e.t1 ?? e.t0, e.t0); return t < e.t0 ? e.t0 - t : t > end ? t - end : 0 }
        let told = entries.filter { $0.glance != nil }
        var near = told.filter { gap($0) == 0 }
        if near.isEmpty { near = told.filter { gap($0) <= 10 } }
        let order: [TLEntry.Kind] = [.work, .call, .talk, .picture, .payment, .event, .claude, .reminder, .departure]
        near.sort { (order.firstIndex(of: $0.kind) ?? 99, gap($0), $0.t0) < (order.firstIndex(of: $1.kind) ?? 99, gap($1), $1.t0) }
        return TLReadout(hue: hue, strong: strong, head: head, elsewhere: near.prefix(4).compactMap(\.glance), ahead: ahead)
    }

    /// where the things of a lens's kind are on the bar
    func marks(of lens: TLLens) -> [TLMark] {
        entries.filter { $0.lens == lens }.map { TLMark(t0: $0.t0, t1: max($0.t0, $0.t1 ?? $0.t0)) }
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
        var minutes: [TLLens: Double] = [:], counts: [TLLens: Int] = [:]            // what the day's numbers add up
        var spent: [String: (cents: Int, n: Int)] = [:]                              // money out, by currency

        for i in r.items {
            let t0 = m(i.start_ms), t1 = i.end_ms.map(m)
            let begun = now.map { t0 <= $0 } ?? true
            let later = now.map { t0 > $0 } ?? false
            let running = t1 == nil && begun && now != nil          // today, begun, no end yet: it goes on
            let end = t1 ?? (running ? now : nil)
            let span = end.map { max(0, $0 - t0) }
            let dur = span.flatMap { $0 >= 1 ? tlDur(Int($0.rounded())) : nil }
            var e = TLEntry(id: i.id, kind: .work, t0: t0, title: "")
            e.time = clock(t0); e.t1 = end; e.later = later
            switch i.kind {
            case "sleep":
                e.kind = .sleep
                e.title = "睡觉"
                e.symbol = "moon.fill"
                e.detail = rangeText(t0, t1, running: running)
                e.right = i.minutes.map { tlDur(Int($0.rounded())) } ?? dur ?? ""
                e.barName = (i.minutes ?? span).map { tlHM(Int($0.rounded())) }; e.barIcon = "moon.fill"
                if let end { segs.append(TLSeg(id: i.id, t0: max(0, t0), t1: min(DAY, end), kind: .sleep)) }
            case "stay":
                e.kind = .stay
                e.title = i.ringName?.name ?? "没名字的地方"
                if let k = i.kept_place {
                    e.color = PlaceTint.line(role: k.role, id: k.id); e.kept = true
                } else { e.color = PlaceTint.lineNeutral }
                e.barName = i.ringName?.name
                e.barIcon = ["home": "house.fill", "school": "graduationcap.fill"][i.kept_place?.role ?? ""] ?? "mappin"
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
                    e.barName = text(i.route); e.barIcon = "bus.fill"
                    e.detail = trail.isEmpty ? nil : trail          // one stop is that stop alone, none no line at all
                    e.more = [stops[0].map { "上车 " + $0 }, stops[1].map { "下车 " + $0 }].compactMap { $0 }
                } else {
                    let (word, symbol) = Self.moves[i.activity ?? ""] ?? ("移动", "arrow.right")
                    e.title = word; e.symbol = symbol
                    e.barName = word; e.barIcon = symbol
                }
                if let end { segs.append(TLSeg(id: i.id, t0: max(0, t0), t1: min(DAY, end), kind: .move)) }
            case "talk":
                e.kind = .talk
                let said = topic(i.id)
                e.title = said ?? "和她说话"
                e.symbol = "bubble.left.fill"
                e.detail = "\(i.turns ?? 1) 句"
                e.glance = said ?? "和她说话 · " + e.detail!
                e.right = dur ?? ""
                e.talkStart = i.start_ms
            case "picture":
                e.kind = .picture
                e.title = "照片"
                e.symbol = "photo.fill"
                let lines = (i.note ?? "").split(whereSeparator: \.isNewline).compactMap { text(String($0)) }
                e.detail = lines.first
                e.more = lines
                e.glance = ["照片", e.detail].compactMap { $0 }.joined(separator: " · ")
                e.attachment = i.id.hasPrefix("picture-") ? String(i.id.dropFirst("picture-".count)) : i.id
            case "event":
                e.kind = .event
                let hidden = i.private == true
                e.title = hidden ? "私事" : text(i.title) ?? "日程"
                e.symbol = "calendar.circle.fill"
                e.barName = e.title; e.barIcon = "calendar"; e.glance = e.title
                // a mail made it and he has not said it is his: say so, and where (a private one names no place)
                let parts = [i.confirmed == false && i.source != "plan" ? "从邮件里收的，还没确认" : nil, hidden ? nil : text(i.place)].compactMap { $0 }
                e.detail = parts.isEmpty ? nil : parts.joined(separator: " · ")
                e.more = parts
                e.right = dur ?? ""
            case "reminder":
                e.kind = .reminder
                e.title = text(i.text) ?? "提醒"
                e.glance = "提醒 · " + e.title
                e.symbol = "bell.fill"
                e.detail = i.fired == true ? (i.acknowledged == true ? "已经响过，你确认过了" : "已经响过，你还没确认") : "还没到点"
            case "departure":
                e.kind = .departure
                e.title = text(i.route).map { "\($0) 路 发车" } ?? text(i.title) ?? "发车"
                e.glance = e.title
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
                let tops = [i.projects?.first?.name, i.apps?.first?.name].compactMap(text)
                e.glance = tops.isEmpty ? "在 Mac 上工作" : tops.joined(separator: " · ")
            case "call":
                e.kind = .call
                e.title = "通话"
                e.symbol = "phone.fill"
                e.detail = text(i.app)
                e.glance = ["通话", e.detail].compactMap { $0 }.joined(separator: " · ")
                e.right = dur ?? ""
            case "payment":
                e.kind = .payment
                e.symbol = "creditcard.fill"
                let shop = text(i.counterparty), item = text(i.text), cents = i.amount?.cents
                e.title = shop ?? item ?? "付款"
                e.detail = shop == nil ? nil : item
                // what it came to: money out is the plain amount, money in has a plus
                e.right = cents.map { (i.direction == "in" ? "+" : "") + tlMoney($0, i.currency) } ?? ""
                e.strongRight = true
                e.glance = [e.title, e.right.isEmpty ? nil : e.right].compactMap { $0 }.joined(separator: " · ")
                // opened: which way, the amount (with its currency's code when the mark alone does not say it), and where the import came from
                let way = i.direction == "in" ? "收入" : "支出", code = text(i.currency)?.uppercased()
                let sum = cents.map { tlMoney($0, i.currency) }
                e.more = [sum.map { way + " " + $0 + (code.map { $0.isEmpty || sum!.contains($0) ? "" : " " + $0 } ?? "") }, text(i.source).map { "来自" + (Self.sources[$0] ?? $0) }].compactMap { $0 }
            case "claude_chat":
                e.kind = .claude
                e.symbol = "sparkles"
                let name = text(i.text)
                e.title = name == nil || name == "Claude conversation" ? "Claude 对话" : name!
                e.glance = "Claude · " + e.title
                e.detail = i.messages.map { "\($0) 条消息" }
                e.right = dur ?? ""
            case "claude_day":
                e.kind = .claude
                e.symbol = "sparkles"
                let titles = (i.text ?? "").split(separator: ";").compactMap { text(String($0)) }
                e.title = "Claude 对话小结"
                e.detail = titles.isEmpty ? nil : titles.joined(separator: "、")
                e.more = titles
                e.right = i.chats.map { "\($0) 段" } ?? ""
            default:
                continue            // a kind this phone does not know yet: left out, not guessed at
            }
            switch e.kind {
            case .sleep: e.lens = .sleep; minutes[.sleep, default: 0] += i.minutes ?? span ?? 0
            case .trip: e.lens = .trip; minutes[.trip, default: 0] += span ?? 0
            case .work: e.lens = .work; minutes[.work, default: 0] += i.active_s.map { $0 / 60 } ?? span ?? 0          // the time he was at it, not the whole stretch
            case .call: e.lens = .call; minutes[.call, default: 0] += span ?? 0
            case .talk: e.lens = .talk; counts[.talk, default: 0] += 1
            case .picture: e.lens = .picture; counts[.picture, default: 0] += 1
            case .payment where i.direction == "out":
                e.lens = .payment
                if let c = i.amount?.cents { let k = text(i.currency)?.uppercased() ?? ""; spent[k] = ((spent[k]?.cents ?? 0) + c, (spent[k]?.n ?? 0) + 1) }
            default: break
            }
            switch e.kind {
            case .stay: e.head = e.title + " · " + e.right
            case .sleep: e.head = [e.title, e.detail, e.right.isEmpty ? nil : e.right].compactMap { $0 }.joined(separator: " · ")
            case .trip: e.head = [e.title, rangeText(t0, t1, running: running), e.right.isEmpty ? nil : e.right].compactMap { $0 }.joined(separator: " · ")
            default: break
            }
            if let label = marks[i.id], e.kind != .stay { e.title = label; e.lit = true; e.glance = label }
            if About.kinds.contains(i.kind), About.fits(id: i.id) {
                e.about = About(kind: i.kind, id: i.id, title: e.title, startMs: i.start_ms, mark: Self.mark(i, later: later))
            }
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
        // the night that ended on this day says when he woke; what began before the bar's start brings it back
        let wake = r.items.filter { $0.kind == "sleep" }.compactMap { $0.end_ms.map(m) }.filter { $0 >= 0 && $0 < DAY }.min()
        let early = r.items.filter { Self.axisKinds.contains($0.kind) }.map { m($0.start_ms) }
        // what mattered, at the time of each item that did
        var starred = Set<String>()
        let stars = r.highlights.compactMap { h in entries.first { $0.id == h.id }.flatMap { starred.insert(h.id).inserted ? TLStar(id: h.id, t: $0.t0) : nil } }.sorted { $0.t < $1.t }
        var chips: [TLChip] = []
        for l in TLLens.allCases {
            switch l {
            case .talk, .picture:
                if let n = counts[l], n > 0 { chips.append(TLChip(lens: l, value: l == .talk ? "\(n) 次" : "\(n)")) }
            case .payment:
                // the currency he paid in most often (the larger sum when it is a tie); money in another one adds an ellipsis
                if let main = spent.max(by: { ($0.value.n, $0.value.cents, $1.key) < ($1.value.n, $1.value.cents, $0.key) }) {
                    chips.append(TLChip(lens: l, value: tlMoney(main.value.cents, main.key.isEmpty ? nil : main.key) + (spent.count > 1 ? "…" : "")))
                }
            default:
                let n = Int((minutes[l] ?? 0).rounded())
                if n > 0 { chips.append(TLChip(lens: l, value: tlHM(n))) }
            }
        }
        return DayTimelineModel(date: r.date, line: text(r.line?.text), entries: entries, segs: segs,
                                span: axisStart(wake: wake, early: early)...DAY, now: now, missing: missingText(r.missing), stars: stars, chips: chips)
    }

    /// what, starting before the bar's start, brings the start back: where he went and what he did, never a stay alone
    private static let axisKinds: Set<String> = ["move", "ride", "talk", "picture", "event", "work", "call", "payment"]

    /// Where the bar starts, in minutes: the earlier of 06:00 and the hour before he woke (`wake`, the end of the night that ended on this day: none is 06:00),
    /// never before 00:00. Something in `early` (when the things of the day began) before that moves it back to its hour.
    static func axisStart(wake: Double?, early: [Double]) -> Double {
        var lo = 360.0
        if let wake { lo = min(lo, max(0, ((wake - 60) / 60).rounded(.down) * 60)) }
        if let first = early.filter({ $0 >= 0 && $0 < lo }).min() { lo = (first / 60).rounded(.down) * 60 }
        return lo
    }

    /// the footnote under the list: what has not been read, named by what it gives the day (the dial words it its own way: `BrainLink.missingText`)
    static func missingText(_ missing: [String]) -> String? {
        let names = ["phone": "手机的记录", "reminders": "提醒", "mac": "Mac 的时间", "talks": "对话", "calendar": "日历", "departures": "公交"]
        let m = missing.compactMap { names[$0] }
        return m.isEmpty ? nil : "还没读到：" + m.joined(separator: "、")
    }

    /// the mark the chip wears: the one the dial gives the same item (`StarEngine.kdOf`)
    private static func mark(_ i: DayItem, later: Bool) -> Kd {
        if later { return .next }
        switch i.state {
        case "done": return i.lane == "her" ? .done : .lit
        case "now": return .work
        default: return .next
        }
    }

    /// where an imported payment came from, in the words the Mac uses
    private static let sources = ["alipay": "支付宝", "wechat_pay": "微信", "boc": "中国银行"]

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
