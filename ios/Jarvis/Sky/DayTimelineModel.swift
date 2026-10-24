// words: legacy
import Foundation

// The whole day as one list for the home screen (Sky/DayTimeline.swift draws it), built from the brain's answer for that day (`DayReply`).
// One story: sleep, then one row for each stretch of what he was doing (Sky/DayTimelineDoing.swift; a brain that serves none gets the places he stayed in as groups with what happened
// there as rows, and the trips between the groups as rows of their own), a line for now, and what is still to come after it. Plain values only, so the order, the words and the places on the day can be tested without a screen.
// The words of each row are in Sky/DayTimelineRows.swift. Times are minutes from the day's own midnight by the wall clock of the brain's zone (a stay that began the evening before
// starts below 0).

/// The minutes of one day of the brain's: counted by the wall clock of the zone the brain wrote the day in (`DayReply.tz`) from that day's midnight, so 09:00 is 540 on a day with a clock
/// change as on any other, and a phone in another zone reads the same day. A time before the day began is below 0, one after it 1440 or more.
struct TLClock {
    let cal: Calendar
    let origin: Date                    // the midnight that begins the day, in the brain's zone

    init(_ r: DayReply) {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: r.tz) ?? Cal.cur.cal.timeZone
        cal = c
        origin = c.startOfDay(for: Date(timeIntervalSince1970: Double(r.start_ms) / 1000))
    }
    /// the day of a reply that has no `DayReply` (the footprint's): its zone and the instant its day begins or any instant in it
    init(tz: String, startMs: Int64) {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: tz) ?? Cal.cur.cal.timeZone
        cal = c
        origin = c.startOfDay(for: Date(timeIntervalSince1970: Double(startMs) / 1000))
    }
    func minute(_ at: Date) -> Double { Cal.wall(at, in: cal, from: origin) }
    func minute(ms: Int64) -> Double { minute(Date(timeIntervalSince1970: Double(ms) / 1000)) }

    /// The instant (epoch ms) the wall clock shows `minute` minutes after this day's midnight: the inverse of `minute(ms:)`, so 09:00 is minute 540 on a day with a clock change as on any
    /// other. A minute the clock skips has no instant of its own; the one it repeats is its first.
    func ms(minute: Double) -> Int64 {
        let days = Int((minute / DAY).rounded(.down)), rest = minute - Double(days) * DAY, whole = Int(rest.rounded(.down))
        let midnight = cal.date(byAdding: .day, value: days, to: origin) ?? origin
        var c = cal.dateComponents([.year, .month, .day], from: midnight)
        c.hour = whole / 60; c.minute = whole % 60
        let at = (cal.date(from: c) ?? midnight).addingTimeInterval((rest - Double(whole)) * 60)
        return Int64((at.timeIntervalSince1970 * 1000).rounded())
    }

    /// How a day `n` days before this one is said: 昨天, 前天, then by its date (`10月15日`).
    func before(_ n: Int) -> String {
        if n == 1 { return "昨天" }
        if n == 2 { return "前天" }
        guard let d = cal.date(byAdding: .day, value: -n, to: origin) else { return "\(n) 天前" }
        let c = cal.dateComponents([.month, .day], from: d)
        return "\(c.month ?? 1)月\(c.day ?? 1)日"
    }
}

/// the heights of the list's rows, in design points: fixed, so where a row stands is known without asking the view. A row someone can touch is at least 44 high.
enum TLMetrics {
    static let row = 46.0, rowDetail = 60.0, stay = 44.0, now = 0.0, note = 44.0
    /// the place over a group of stretches: its dot, its name, its hours (only a long press is on it)
    static let header = 40.0
    /// a stop on the footprint page's outing, or the stretch with no fixes: a line inside the outing, not a row to touch
    static let sub = 34.0
    /// between one group (a place, a run of trips) and the next, and above the line for now on a day of stretches
    static let gap = 12.0, nowGap = 0.0
}

/// 1 小时 40 分, 2 小时, and under an hour 18 分钟: the length of a span, never with a clock's colon
func tlDur(_ minutes: Int) -> String {
    let m = max(0, minutes)
    if m < 60 { return "\(m) 分钟" }
    return m % 60 == 0 ? "\(m / 60) 小时" : "\(m / 60) 小时 \(m % 60) 分"
}

/// 7小时50分, 35分钟: a length where room is short (the day's numbers, a line of sleep's stages): the same words without their spaces
func tlHM(_ minutes: Int) -> String { tlDur(minutes).replacingOccurrences(of: " ", with: "") }

/// 12,345: a whole number with its thousands told
func tlGroup(_ n: Int) -> String {
    var out = ""
    for (k, ch) in String(n.magnitude).reversed().enumerated() { if k > 0, k % 3 == 0 { out.append(",") }; out.append(ch) }
    return (n < 0 ? "-" : "") + String(out.reversed())
}

/// $12.40, US$5.00, ¥68.00, `12.40 CHF`: an amount with the mark of its currency as the Mac writes it (CAD $, USD US$, CNY ¥, EUR €, GBP £, JPY JP¥); a currency this phone has
/// no mark for is written after the number by its code
func tlMoney(_ cents: Int, _ currency: String?) -> String {
    let c = cents.magnitude, num = tlGroup(Int(c / 100)) + "." + String(format: "%02d", Int(c % 100)), sign = cents < 0 ? "-" : ""
    let code = (currency ?? "").trimmingCharacters(in: .whitespaces).uppercased()
    let marks = ["CAD": "$", "USD": "US$", "CNY": "¥", "EUR": "€", "GBP": "£", "JPY": "JP¥"]
    if code.isEmpty { return sign + num }
    return marks[code].map { sign + $0 + num } ?? sign + num + " " + code
}

/// a whole number from a double, which a malformed item could make anything: not a number is 0, and it is kept to a size a clock and a list can hold
func tlInt(_ x: Double) -> Int { x.isFinite ? Int(max(-1e9, min(1e9, x.rounded()))) : 0 }

/// What opening a row offers to do with it. Each exists only where it works today.
enum TLAction: Hashable {
    case ask                // hand it to her: the row becomes the chip of the next turn, and the composer opens
    case rename             // a place he keeps, under another name
    case name               // a place he has no name for yet: the first one
    case acknowledge        // a reminder that rang: he knows
    case confirm            // an event his mail made: it is his
    case deny               // an event his mail made: it is not
    case sayWhat            // a stretch of the day: what he was doing, in his own words (the last of them, and the rare one)
}

/// the kinds of place he can name a place as: the brain's own roles (jarvis/state/places.py), which are three
enum TLRole: String, CaseIterable {
    case home, school, other

    var word: String {
        switch self {
        case .home: "家"
        case .school: "学校"
        case .other: "其他"
        }
    }
}

/// how the rail on the left of the list runs through a row: a place's, as a line in its hue, a trip's, dashed, or (the footprint page) a stretch with no fixes, dotted
enum TLRail: Equatable {
    case none
    case place(RGB)
    case trip
    case gap
}

/// where a stay was, for the small map when it is opened, and the place he keeps it is in
struct TLPlace: Equatable {
    var lat: Double?, lng: Double?
    var text: String?               // the phone's own words for the place
    var keptId: String?             // the place he keeps it is in, and the name he gave it
    var keptName: String?
    var onMap: Bool { lat != nil && lng != nil }
    /// the place can be given its first name: he keeps none here, and the stay has the point the brain keeps a place at
    var canName: Bool { keptId == nil && onMap }
    /// the place can be renamed: he keeps it, and its id is one that can stand in a path (the brain's are slugs; one with a slash or a space is not offered, not guessed at, and `undo` is
    /// the brain's own route there)
    var canRename: Bool { keptId.map { $0 != "undo" && !$0.isEmpty && $0.count <= 128 && $0.unicodeScalars.allSatisfy { $0.isASCII && (CharacterSet.alphanumerics.contains($0) || "-_.:".unicodeScalars.contains($0)) } } ?? false }
}

/// one of what he spent the time of a stretch of Mac work on: a project, an app
struct TLTop: Equatable {
    var name: String
    var minutes: Int
    var share: Double               // of the biggest of its list: 1 for that one
}
struct TLWork: Equatable {
    var projects: [TLTop], apps: [TLTop]
    var active: Int?                // minutes at it
    var total: Int?                 // minutes of the whole stretch
}

/// one stretch of a night as Health recorded it
struct TLStage: Equatable {
    enum Kind: Equatable {
        case deep, core, rem, awake, inBed, unspecified
        init?(name: String) {
            switch name {
            case "deep": self = .deep
            case "core": self = .core
            case "rem": self = .rem
            case "awake": self = .awake
            case "in_bed": self = .inBed
            case "unspecified": self = .unspecified
            default: return nil         // a stage this phone does not know: not drawn
            }
        }
        /// which of two stages that cover one instant (two sources) the night shows: awake, then REM, deep, core, a stage with no name, and the time in bed last
        var rank: Int {
            switch self {
            case .awake: 5
            case .rem: 4
            case .deep: 3
            case .core: 2
            case .unspecified: 1
            case .inBed: 0
            }
        }
    }
    var kind: Kind
    var t0: Double, t1: Double
}
extension TLStage {
    /// The night's stages as one timeline of blocks that do not overlap inside `lo...hi`: an instant that two stages cover (two sources) takes the one that ranks higher (`Kind.rank`), and
    /// what no stage covers is `unspecified`. What the night adds up to, the hypnogram and the bar's shading all read this, so they never disagree.
    static func blocks(_ stages: [TLStage], in lo: Double, _ hi: Double) -> [TLStage] {
        guard lo.isFinite, hi.isFinite, hi > lo else { return [] }
        var cuts: Set<Double> = [lo, hi]
        for s in stages { for t in [s.t0, s.t1] where t > lo && t < hi { cuts.insert(t) } }
        let edges = cuts.sorted()
        var out: [TLStage] = []
        for (a, b) in zip(edges, edges.dropFirst()) {
            let mid = (a + b) / 2
            let kind = stages.filter { $0.t0 <= mid && mid < $0.t1 }.max { $0.kind.rank < $1.kind.rank }?.kind ?? .unspecified
            if let last = out.last, last.kind == kind { out[out.count - 1].t1 = b } else { out.append(TLStage(kind: kind, t0: a, t1: b)) }
        }
        return out
    }
}
struct TLSleep: Equatable {
    var stages: [TLStage]                   // the night as one timeline of blocks that do not overlap, cut to the night (`TLStage.blocks`)
    var deep = 0, core = 0, rem = 0         // minutes in each, inside the night
    var awake = 0                           // how many times he was awake
    var summary: String?                    // `深睡 1:10 · 核心 4:30 · REM 1:40 · 醒 3 次`
    var short: String?                      // `深睡 1:10 · REM 1:40`
}

/// one Claude Code session of the day
struct TLSession: Equatable, Identifiable {
    let id: String
    var t0: Double, t1: Double
    var time: String
    var title: String?                      // none for a session nobody named: the Mac says `Claude Code` alone, and so does the phone
    var project: String?, account: String?  // `A` or `B`
    var right: String                       // how long
    var recap: String?                      // Claude Code's latest recap of it
    var prompts: Int?                       // what he typed in it (0: it ran by itself); none from a brain that does not say

    /// what its row and its line in a group are called
    var name: String { title ?? "没起名的会话" }
    /// what the glance and the readout say of it
    var told: String { name }
}

/// one line of the list
struct TLEntry: Identifiable, Equatable {
    enum Kind: Equatable {
        case sleep, stay, trip, pause, talk, picture, event, reminder, departure, work, call, payment, claude, workout, code, now
        case media, messages, phoneCall, screen, away           // what his devices sync (ADR 0305) and what he wrote in TimeSink: only ever a line inside a stretch (`insideOnly`)
        case segment                                            // a stretch of what he was doing (the brain's doing lane): the list's row up to now
        /// a kind that is a line inside a stretch of the day and never a row of the old list, which is what a day with no stretches still draws
        var insideOnly: Bool { [.media, .messages, .phoneCall, .screen, .away].contains(self) }
    }
    let id: String
    var kind: Kind
    var t0: Double
    var t1: Double?                 // when it ended, or now while it goes on; none for a moment (and for what has no end yet)
    var time = ""                   // the fixed column at the left, `HH:mm`
    var title: String
    var detail: String?             // the one muted line under the title
    var more: [String] = []         // what opening the row says that its own title and line do not, a line each
    var symbol = ""                 // an SF Symbol, drawn filled once it has happened and as an outline while it has not
    var right = ""                  // the mono text at the right: how long, a point's value, or how long until it
    var color: RGB?                 // a stay: the colour of its place
    var kept = false                // a stay in a place he keeps (a stay in none is a neutral haze)
    var strongRight = false         // the mono text at the right is the point of the row (what a payment came to), so it is not muted
    var later = false               // not yet: after the line for now
    var soon = false                // later, and within two hours: the text at the right is the time left
    var going = false               // today, begun, no end yet: it goes on, and its end is now
    var ride = false                // a trip on a bus
    var waiting = false             // waits on him (a reminder that rang, an event from his mail he has not confirmed): the amber `等你`
    var commitment: String?         // an event his mail made that he has not said is his: its hex, which is what the brain's route takes
    var attachment: String?         // a picture: the brain's name for it
    var about: About?               // the chip it gives a talk while it is the row opened (ADR 0214): only an item of a kind the brain's host knows
    var actions: [TLAction] = []    // what opening it offers
    var lens: TLLens?               // the kind of the day's numbers it is counted in (a stay, an event and the rest are in none)
    var barName: String?            // the word it wears inside the overview bar when there is room: the place, the route, the way he moved, how long he slept, the event
    var barIcon: String?            // and the symbol, for when the word does not fit
    var head: String?               // a stretch (the night, a place, a trip) as the readout under a finger opens with it: `家 · 09:05–11:25 · 2 时 20 分`
    var glance: String?             // what the readout says of it when it was going on at the minute under a finger: `Jarvis · Xcode`, `A&W · $12.40`
    var group: String?              // the place it belongs to (its stay's id)
    var rail = TLRail.none          // the rail through its icon, and whether it comes in from the row above and goes on to the one below
    var railIn = false, railOut = false
    var onWay = false               // something that happened while he was on the way: it stands on the trip's rail
    var gap = 0.0                   // the blank above it, between one group and the next
    var place: TLPlace?             // a stay: where it was
    var work: TLWork?               // work: what it was spent on
    var sleep: TLSleep?             // a night: its stages
    var sessions: [TLSession] = []  // Claude Code: its session, or, for one row that stands for several, each of them
    var spans: [TLMark] = []        // where it is on the bar when that is not one span
    var cut = false                 // its own words are cut short at the right of the columns (`cutShort`, measured once when the day is built, not whenever a row asks)
    var unknown = false             // a stretch nobody could say anything of: its title is `没有记录`, in the muted colour
    var unsure = false              // a stretch she is not sure of: a question mark after its title
    var commute = false             // a stretch on the way somewhere: it opens to its trip (Sky/RowStage.swift)
    var ask: TLAsk?                 // a stretch: what it asks him, in one line when it is opened
    var lenses: Set<TLLens> = []    // a stretch: the kinds of the day's numbers that have something inside it (a talk too, which is not drawn)
    var at: String?                 // a stretch: the place he was at, which its line says when no group under that place's name holds it
    var note: String?               // a stretch: the model's one line about it, in its line
    var lines: [TLLine] = []        // a stretch: what it says opened, a line each, in time order
    var knows: String?              // a stretch: how she knows what he was doing, and what she went by
    var header = false              // a place over a group of stretches (a `stay`): a dot, a name and its hours, and nothing to open
    var gapRail = TLRail.none       // the rail through the blank above it: a trip's, reaching out to the group before or after it
    var token: Int?                 // a stop of an outing (the footprint page): its number in a ring, in the place of the circle
    var ribbon: [RGB?] = []         // an outing (the footprint page): its speed along it, left to right, the colour of each slice; none where he stood still or nothing was recorded
    var tapOnly = false             // a tap opens a page of the screen's own (an outing), so it opens and the long press offers no menu

    /// the height of the row without the gap above it
    var base: Double {
        switch kind {
        case .stay: header ? TLMetrics.header : TLMetrics.stay
        case .now: TLMetrics.now
        case .pause where token != nil || rail == .gap: TLMetrics.sub
        default: hasLine ? TLMetrics.rowDetail : TLMetrics.row
        }
    }
    var height: Double { base + gap }
    /// the muted line under the title
    var hasLine: Bool { detail != nil }
    /// one row that stands for several Claude Code sessions
    var holdsSessions: Bool { kind == .code && sessions.count > 1 }
    /// a tap on it opens something: what its line does not say, the picture, a map, what can be done with it, or its own words whole where the row cuts them short
    var opens: Bool {
        switch kind {
        case .now, .pause: false
        default: !header && (kind == .picture || kind == .segment || !more.isEmpty || !actions.isEmpty || place != nil || work != nil || sleep != nil || holdsSessions || cut || tapOnly)
        }
    }
    /// Opened where it stands (the phone's list), it says at least two things its line does not, or asks him something, or has something to do that needs its buttons: a stretch with two
    /// lines or more (a run, what happened, what took him off it) or a question; the night's stages; the picture, the map, the Mac's projects, the sessions; what waits on him; words of
    /// its own cut short. A row that would open to one line or to nothing does not open: a tap frames its time on the bar.
    var expands: Bool {
        switch kind {
        case .now, .pause: return false
        case .segment: return ask != nil || lines.count >= 2
        case .sleep: return sleep?.stages.isEmpty == false || more.count >= 2
        default:
            let doing: Set<TLAction> = [.rename, .name, .acknowledge, .confirm, .deny]
            return !header && (tapOnly || kind == .picture || place?.onMap == true || work != nil || holdsSessions || cut || more.count >= 2 || actions.contains { doing.contains($0) })
        }
    }
    /// a tap on it in the phone's list does something: opens it where it stands, or frames its time on the bar
    var touchable: Bool { kind != .now && kind != .pause && !header }
}

/// A kind of thing the day's numbers bring forward: pressing its chip dims the rest of the bar and the list.
enum TLLens: CaseIterable, Equatable {
    case sleep, trip, workout, work, call, talk, payment, picture

    /// the word on its chip: the same on the Mac
    var word: String {
        switch self {
        case .sleep: "睡"
        case .trip: "路上"
        case .workout: "运动"
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
        case .workout: "figure.run"
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
        case .workout: RGB(156, 224, 120)
        case .work: RGB(108, 156, 255)
        case .call: RGB(111, 224, 180)
        case .talk: RGB(172, 124, 255)
        case .payment: RGB(240, 132, 218)
        case .picture: RGB(255, 240, 220)
        }
    }
}

/// one of the day's numbers: `睡 8:05`, `路上 3:04`, `运动 45 分`, `Mac 2:20`, `通话 18 分`, `和她 2 次`, `花 $12.40`, `照片 1`
struct TLChip: Equatable, Identifiable {
    let lens: TLLens
    let value: String
    var minutes: Int?               // a chip that is a length (the phone writes it `2 小时 15 分`; `value` is the iPad's and the footprint's `2小时15分`)
    var symbol: String?             // a page's own icon for it, in the place of the lens's (the footprint's trip chip wears the day's main way of moving)
    var id: String { word }
    var word: String { lens.word }
}

/// What Health reported for the day, as the quiet values after the chips: `8,240 步`, `静息 58`, `HRV 42`
struct TLBody: Equatable, Identifiable {
    var prefix: String, value: String, suffix: String
    var id: String { prefix + suffix }
}

/// the day's weather for the header: `多云 14° / 8°`
struct TLWeather: Equatable {
    var word: String?, symbol: String?
    var high: Int?, low: Int?
    var temps: String { [high, low].compactMap { $0.map { "\($0)°" } }.joined(separator: " / ") }
    var text: String { [word, temps.isEmpty ? nil : temps].compactMap { $0 }.joined(separator: " ") }
}
/// when the sun rose and set, in minutes from the day's midnight (a tap on the weather shows it: TLSunLine)
struct TLSun: Equatable {
    var rise: Double?, set: Double?
}

/// An item he marked or she picked as what mattered (`highlights` of `GET /inherent/day`), at the minute its item or stretch begins; one whose id joins nothing is not here.
struct TLStar: Equatable, Identifiable {
    let id: String
    var t: Double
    var label: String
    var hers: Bool                  // she picked it (`by` her); his marks are the rest
}

/// A stretch of the place line under the bar: a place he keeps (its colour and name), or a trip (dashed). Where he was somewhere he does not keep, or nowhere is known, there is none.
struct TLPlaceBit: Equatable, Identifiable {
    enum Kind: Equatable { case place(RGB, name: String), trip }
    let id: String
    var t0: Double, t1: Double
    var kind: Kind
}

/// something that was going on, for the readout to say of the minute under a finger
struct TLTold: Equatable {
    var kind: TLEntry.Kind
    var t0: Double, t1: Double
    var text: String
}

/// What a finger on the bar reads out: the stretch of the day under it, and what else was going on there.
struct TLReadout: Equatable {
    var hue: RGB?                   // the colour of the stretch (a dot); none when there is no stretch
    var strong = true               // a place he keeps or a trip: the dot is full
    var head: String                // `家 · 09:05–11:25 · 2 时 20 分`
    var elsewhere: [String]         // up to four things that were going on, the most telling first
    var ahead = false               // it has not happened yet
    /// the head split where the words that never change begin (` · 09:05–11:25 · 2 时 20 分`), so that when it is too long it is the name that gives way
    var name: String { head.components(separatedBy: " · ").first ?? head }
    var tail: String { String(head.dropFirst(name.count)) }
    /// the line under it on a phone: the first two things, or that there was nothing
    var second: String { elsewhere.isEmpty ? (ahead ? "这段时间没有安排" : "这段时间没有别的记录") : elsewhere.prefix(2).joined(separator: " · ") }
}

/// where something of a lens's kind is on the bar: a span, or a moment (its end is its start)
struct TLMark: Equatable {
    var t0: Double, t1: Double
}

/// a span of the day on the overview bar
struct TLSeg: Equatable {
    enum Kind: Equatable { case place(RGB, kept: Bool), move, sleep, doing(RGB?) }         // a stretch of what he was doing has its category's colour, or none (a hole)
    var id: String
    var t0: Double, t1: Double
    var kind: Kind
    /// what is on top where spans overlap
    var priority: Int { switch kind { case .sleep: 3; case .move: 2; case .place, .doing: 1 } }
}

struct DayTimelineModel: Equatable {
    var date: String
    var line: String?               // her sentence about the day
    var entries: [TLEntry]
    var segs: [TLSeg]
    var span: ClosedRange<Double>   // what the overview bar covers: from its start (`axisStart`) to 24:00
    var now: Double?                // the minute of now, on today only
    var missing: String?            // "还没读到：Mac 的时间"
    var chips: [TLChip] = []        // the day's numbers, in a fixed order, only the ones that are not nothing
    var body: [TLBody] = []         // what Health reported, after the chips
    var weather: TLWeather?
    var sun: TLSun?
    var waiting: [String] = []      // the rows that wait on him, in the order of the day
    var told: [TLTold] = []         // what the readout can say of a minute: what each thing of the day was doing, a Claude Code session each
    var lensMarks: [TLLens: [TLMark]] = [:]     // where the things of each kind are on the bar
    var stars: [TLStar] = []        // the items he marked and she picked, where they are on the bar
    var places: [TLPlaceBit] = []   // the place line under the bar: the places he keeps, and the trips between them

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
    /// where the row stands, and where its own line begins under the blank above it
    func top(of id: String) -> Double? { entries.firstIndex { $0.id == id }.map { offsets[$0] } }

    /// Where the list rests on today, from its top: the line for now about 60% of the way down `room`, and a row beginning at the top rather than half of one (the row
    /// boundary nearest to that, 8 pt down: the blank under a row's last line, so the row above leaves nothing in the list's fade). Another day rests at its top.
    func restY(room: Double) -> Double {
        guard let i = nowIndex else { return 0 }
        let want = offsets[i] + TLMetrics.now / 2 - 0.6 * room
        return max(0, (offsets.min { abs($0 - want) < abs($1 - want) } ?? 0) - 8)
    }

    /// The part of the day the list shows scrolled to `y` with `room` of it in view, as the bar frames it while he scrolls: from the minute at the top of what shows to the minute at its foot,
    /// each read off the row there as a part of its hours (a header, a moment and what is still to come are no hours of their own). Once the line for now is in view or above it, what
    /// shows reaches now and is carried past it by the room under that line. None: nothing of the day shows.
    func window(y: Double, room: Double) -> ClosedRange<Double>? {
        let offs = offsets, top = y, foot = y + room
        var a: Double?, z: Double?
        for (i, e) in entries.enumerated() where e.kind != .now && !e.later && !e.header {
            guard let end = e.t1, end > e.t0 else { continue }
            let y0 = offs[i] + e.gap, y1 = offs[i] + e.height
            guard y1 > top + 8, y0 < foot - 8 else { continue }
            let t0 = max(e.t0, span.lowerBound), t1 = max(t0, end)
            let s = t0 + (t1 - t0) * clamp((top - y0) / max(1, y1 - y0), 0, 1), f = t0 + (t1 - t0) * clamp((foot - y0) / max(1, y1 - y0), 0, 1)
            a = min(a ?? s, s); z = max(z ?? f, f)
        }
        if let n = now, let i = nowIndex, offs[i] < foot { a = min(a ?? n, n); z = max(z ?? n, n + min(130, (foot - offs[i]) / 1.2)) }
        guard let a, let z, z > a else { return nil }
        return a...min(z, span.upperBound)
    }

    /// the minute `t` as the bar has it: no earlier than its start and no later than 24:00
    func onBar(_ t: Double) -> Double { clamp(t, span.lowerBound, span.upperBound) }

    /// the entry that covers minute `t`: the last one begun by then (of those begun together, the later in the list), else the first
    func entry(at t: Double) -> TLEntry? {
        let list = entries.filter { $0.kind != .now }
        var best: Int?
        for (k, e) in list.enumerated() where e.t0 <= t && (best.map { list[$0].t0 <= e.t0 } ?? true) { best = k }
        return best.map { list[$0] } ?? list.first
    }
    /// the stretch (a stay, a trip, the night) minute `t` is in: the night is on top of a trip, a trip of a stay
    func seg(at t: Double) -> TLSeg? {
        var best: TLSeg?
        for s in segs where s.t0 <= t && t < s.t1 && s.priority > (best?.priority ?? 0) { best = s }
        return best
    }
    /// its id, or none
    func stretch(at t: Double) -> String { seg(at: t)?.id ?? "" }
    /// the row to light for a finger at minute `t`: the one that covers it. Past now only what was planned is lit (what began before now is not what goes on then),
    /// and where nothing was recorded only what goes on through the hole.
    func lit(at t: Double) -> TLEntry? {
        guard let e = entry(at: t) else { return nil }
        if let n = now, t > n, e.t0 <= n { return nil }
        if seg(at: t) == nil, t > max(e.t1 ?? e.t0, e.t0) { return nil }
        return e
    }

    /// the row of the stretch now is in, as the readout finds it: a stretch that is still going on ends exactly at now, which `seg(at:)` (half-open) misses, and `entry(at:)` would pick a moment
    /// row begun later. Where now is in no stretch, the row `lit(at:)` lights.
    func nowRow() -> TLEntry? {
        guard let n = now else { return nil }
        if let s = seg(at: n) ?? seg(at: n.nextDown), let e = entries.first(where: { $0.id == s.id }) { return e }
        return lit(at: n)
    }

    /// What a finger on the bar at minute `t` reads out. The stretch there (a place, a trip, the night) opens it, or how long nothing was recorded, or that it has not happened;
    /// then what else was going on: what covers `t`, else what is within 10 minutes of it, the most telling first (work, Claude Code, a call, a talk, a workout, a picture, a payment,
    /// an event, Claude, a reminder, a bus, the night's stages).
    func readout(at minute: Double) -> TLReadout {
        let t = onBar(minute)
        var hue: RGB?, strong = true, head: String, gist: String?
        let ahead = now.map { t > $0 } ?? false
        if ahead {
            head = "还没到"
        } else if let s = seg(at: t) ?? seg(at: t.nextDown), let e = entries.first(where: { $0.id == s.id }) {          // the end of a stretch with nothing after it (a stay up to now) is still in it
            switch s.kind {
            case .place(let c, let kept): hue = c; strong = kept
            case .move: hue = TLPaint.trip
            case .sleep: hue = TLPaint.sleep
            case .doing(let c): hue = c; strong = c != nil
            }
            head = e.head ?? e.title
            if e.kind == .segment { gist = e.detail }          // a stretch's own line says what else there was, in words
        } else {
            let lo = segs.filter { $0.t1 <= t }.map(\.t1).max() ?? span.lowerBound
            let hi = max(lo, min(segs.filter { $0.t0 > t }.map(\.t0).min() ?? DAY, now ?? DAY))          // a hole ends no earlier than it begins
            head = "没有记录 · \(clock(lo))–\(tlEnd(hi)) · \(tlDur(tlInt(hi - lo)))"
        }
        func gap(_ x: TLTold) -> Double { t < x.t0 ? x.t0 - t : t > x.t1 ? t - x.t1 : 0 }
        var near = told.filter { gap($0) == 0 }
        if near.isEmpty { near = told.filter { gap($0) <= 10 } }
        let order: [TLEntry.Kind] = [.work, .code, .call, .talk, .workout, .picture, .payment, .event, .claude, .reminder, .departure, .sleep]
        func rank(_ x: TLTold) -> Int { order.firstIndex(of: x.kind) ?? 99 }
        near.sort { (rank($0), gap($0), $0.t0) < (rank($1), gap($1), $1.t0) }
        return TLReadout(hue: hue, strong: strong, head: head, elsewhere: gist.map { [$0] } ?? near.prefix(4).map(\.text), ahead: ahead)
    }

    /// where the things of a lens's kind are on the bar
    func marks(of lens: TLLens) -> [TLMark] { lensMarks[lens] ?? [] }

    /// where the things of each lens's kind are, from the day's items as rows, whether the list has them as rows or not (a talk is none, and a stretch has its things inside it)
    static func lensMarks(_ list: [TLEntry]) -> [TLLens: [TLMark]] {
        var out: [TLLens: [TLMark]] = [:]
        for e in list { if let l = e.lens { out[l, default: []] += e.spans.isEmpty ? [TLMark(t0: e.t0, t1: max(e.t0, e.t1 ?? e.t0))] : e.spans } }
        return out
    }

    // MARK: building

    /// `now` is the minute of now when this is today (`TimelineState.nowMinute`). `topic` names a talk by what it was about. `acked`, `renamed` and `done` are what he has just done to a
    /// reminder, to a place he keeps, to what his mail made and to a place he named, which the brain's next answer will say too (an event he said is not his is left out). An item that
    /// cannot be placed (a time that is not one, an end before its start, an id taken already) is left out, and so is one of a kind this phone does not know. The minutes are those of
    /// the brain's day (`TLClock`).
    static func build(_ r: DayReply, now: Double?, topic: @escaping (String) -> String? = { _ in nil },
                      acked: Set<String> = [], renamed: [String: String] = [:], done: TLDone = TLDone()) -> DayTimelineModel {
        let wall = TLClock(r)
        func m(_ ms: Int64) -> Double { wall.minute(ms: ms) }
        let frame = TLFrame(now: now, m: m, before: wall.before, topic: topic, acked: acked, renamed: renamed, confirmed: done.confirmed, named: done.named)
        // what a day can hold, with a month either side: a time beyond it is not a time of this day's list
        let reach = 30 * DAY, window = 31.0 * 86_400_000
        var taken: Set<String> = ["now"]
        // what he was doing, as stretches of the day: with some, the list is those up to now; with none (an older brain, a day it cannot serve) it is the day's records as it always was
        let doings = doingStretches(r, now: now, m)
        let segmented = !doings.isEmpty
        var items: [DayItem] = [], rows: [TLEntry] = []
        for i in r.items {
            guard !i.id.isEmpty, !done.dropped.contains(i.id), abs(Double(i.start_ms) - Double(r.start_ms)) <= window, i.end_ms.map({ abs(Double($0) - Double(r.start_ms)) <= window }) ?? true,
                  taken.insert(i.id).inserted else { continue }
            let t0 = m(i.start_ms), t1 = i.end_ms.map(m)
            guard t0.isFinite, abs(t0) <= reach, t1.map({ $0.isFinite && abs($0) <= reach && $0 >= t0 }) ?? true, let e = row(i, t0: t0, t1: t1, frame),
                  segmented || !e.kind.insideOnly else { continue }
            items.append(i); rows.append(e)
        }

        // what the bar is made of, and what the day's numbers add up
        var segs: [TLSeg] = []
        var minutes: [TLLens: Double] = [:], counts: [TLLens: Int] = [:]
        var talkMinutes = 0.0, talkOpen = false
        var spent: [String: (cents: Int, n: Int)] = [:]                              // money out, by currency
        func seg(_ id: String, _ a: Double, _ b: Double, _ kind: TLSeg.Kind) {
            let lo = max(0, a), hi = min(DAY, b)
            guard hi > lo else { return }
            segs.append(TLSeg(id: id, t0: lo, t1: hi, kind: kind))
        }
        func length(_ x: Double) -> Double { x.isFinite ? max(0, min(x, 3 * DAY)) : 0 }
        for k in rows.indices {
            let i = items[k]
            let span = rows[k].t1.map { max(0, $0 - rows[k].t0) } ?? 0
            switch rows[k].kind {
            case .sleep:
                rows[k].lens = .sleep; minutes[.sleep, default: 0] += length(i.minutes ?? span)
                if let end = rows[k].t1 { seg(rows[k].id, rows[k].t0, end, .sleep) }
            case .stay:
                // on another day a stay with no end goes on to that day's midnight; a day of stretches has the bar drawn from what he was doing, and its places in the list only
                if !segmented, let hue = rows[k].color, let end = rows[k].t1 ?? (now == nil ? DAY : nil) { seg(rows[k].id, rows[k].t0, end, .place(hue, kept: rows[k].kept)) }
            case .trip:
                rows[k].lens = .trip; minutes[.trip, default: 0] += length(span)
                if !segmented, let end = rows[k].t1 { seg(rows[k].id, rows[k].t0, end, .move) }
            case .work: rows[k].lens = .work; minutes[.work, default: 0] += length(i.active_s.map { $0 / 60 } ?? span)      // the time he was at it, not the whole stretch
            case .code: rows[k].lens = .work                                                                                // lit with the Mac's work, not added to it
            case .call: rows[k].lens = .call; minutes[.call, default: 0] += length(span)
            case .workout: rows[k].lens = .workout; minutes[.workout, default: 0] += length(span)
            case .talk:
                rows[k].lens = .talk; counts[.talk, default: 0] += 1
                if rows[k].t1 != nil { talkMinutes += span } else { talkOpen = true }          // a talk with no end is only counted
            case .picture: rows[k].lens = .picture; counts[.picture, default: 0] += 1
            case .payment where i.direction == "out":
                rows[k].lens = .payment
                if let c = i.amount?.cents { let key = tlText(i.currency)?.uppercased() ?? ""; spent[key] = ((spent[key]?.cents ?? 0) + abs(c), (spent[key]?.n ?? 0) + 1) }
            default: break
            }
        }

        // in the order of the day; at the same minute a night comes first, then the place, then the trip, then what happened (a stretch, where the list is stretches, after the night)
        func rank(_ k: TLEntry.Kind) -> Int { k == .sleep ? 0 : k == .stay || k == .segment ? 1 : k == .trip ? 2 : 3 }
        func inOrder(_ list: [TLEntry]) -> [TLEntry] { list.enumerated().sorted { ($0.element.t0, rank($0.element.kind), $0.offset) < ($1.element.t0, rank($1.element.kind), $1.offset) }.map(\.element) }
        var entries = segmented ? segmentList(doings, items: items, rows: rows, now: now, frame) : arrange(inOrder(rows), now: now)
        if segmented { entries = inOrder(entries) }
        if let now {
            var line = TLEntry(id: "now", kind: .now, t0: now, title: "现在")
            line.time = clock(now)
            entries.insert(line, at: entries.firstIndex { $0.t0 > now } ?? entries.count)
        }
        if segmented {
            for e in entries where e.kind == .segment { if let end = e.t1 { seg(e.id, e.t0, end, .doing(e.color)) } }
            entries = grouped(entries, frame)
        } else {
            space(&entries)
        }
        for k in entries.indices { entries[k].actions = actions(of: entries[k], now: now); entries[k].cut = entries[k].cutShort }

        // the night that ended on this day says when he woke; what began before the bar's start brings it back
        let wake = items.compactMap { $0.kind == "sleep" ? $0.end_ms.map(m) : nil }.filter { $0 >= 0 && $0 < DAY }.min()
        let early = segmented ? doings.map(\.t0) : zip(items, rows).filter { axisKinds.contains($0.0.kind) }.map { $0.1.t0 }
        var chips: [TLChip] = []
        for l in TLLens.allCases {
            switch l {
            case .talk, .picture:
                if let n = counts[l], n > 0 {
                    // the phone says how long he talked when every talk has its start and end (and some length); the count stays as the iPad's and the fallback's
                    let timed = l == .talk && !talkOpen && talkMinutes >= 1
                    chips.append(TLChip(lens: l, value: l == .talk ? "\(n) 次" : "\(n)", minutes: timed ? tlInt(talkMinutes) : nil))
                }
            case .payment:
                // the currency he paid in most often (the larger sum when it is a tie); money in another one adds an ellipsis
                if let main = spent.max(by: { ($0.value.n, $0.value.cents, $1.key) < ($1.value.n, $1.value.cents, $0.key) }) {
                    chips.append(TLChip(lens: l, value: tlMoney(main.value.cents, main.key.isEmpty ? nil : main.key) + (spent.count > 1 ? "…" : "")))
                }
            default:
                let n = tlInt(minutes[l] ?? 0)
                if n > 0 { chips.append(TLChip(lens: l, value: tlHM(n), minutes: n)) }
            }
        }
        // what each thing of the day was doing, for the readout: each item as its own row says it (a talk too, which is no row of the list, and each session of Claude Code)
        let told = rows.compactMap { e in e.glance.map { TLTold(kind: e.kind, t0: e.t0, t1: max(e.t1 ?? e.t0, e.t0), text: $0) } }
        var model = DayTimelineModel(date: r.date, line: tlText(r.line?.text), entries: entries, segs: segs,
                                     span: axisStart(wake: wake, early: early, now: now)...DAY, now: now, missing: missingText(r.missing), chips: chips)
        model.body = bodyValues(r.summary)
        model.weather = weatherOf(r.weather)
        model.sun = sunOf(r.weather, m)
        model.waiting = entries.filter(\.waiting).map(\.id)
        model.told = told
        model.lensMarks = lensMarks(rows)
        model.stars = starsOf(r.highlights, rows: rows, stretches: doings, span: model.span)
        model.places = placesOf(rows, now: now)
        return model
    }

    // MARK: the stars and the place line

    /// Where each highlight is on the bar: the start of the item it names, or of the stretch of that id (an id that joins neither is left out, and so is one off the bar). An item both
    /// marked and picked is one star, his.
    static func starsOf(_ highlights: [Highlight], rows: [TLEntry], stretches: [TLStretch], span: ClosedRange<Double>) -> [TLStar] {
        var at: [String: Double] = [:]
        for e in rows { at[e.id] = e.t0 }
        for s in stretches { at[s.span.id] = at[s.span.id] ?? s.t0 }
        var out: [TLStar] = []
        for h in highlights {
            guard let t = at[h.id], span.contains(t) else { continue }
            let hers = h.by == "her"
            if let k = out.firstIndex(where: { $0.id == h.id }) { if !hers { out[k].hers = false }; continue }
            out.append(TLStar(id: h.id, t: t, label: h.label, hers: hers))
        }
        return out.sorted { $0.t < $1.t }
    }

    /// The place line: each stay in a place he keeps with the place's colour and name, each trip, in time order and clipped to what has happened; a stay that goes on to now, or (another day) to
    /// midnight, ends there. Where two overlap the later begins where the earlier ends.
    static func placesOf(_ rows: [TLEntry], now: Double?) -> [TLPlaceBit] {
        var out: [TLPlaceBit] = []
        var from = 0.0
        for e in rows.sorted(by: { $0.t0 < $1.t0 }) where !e.later {
            let kind: TLPlaceBit.Kind
            switch e.kind {
            case .stay where e.kept: kind = .place(e.color ?? PlaceTint.lineNeutral, name: e.title)
            case .trip: kind = .trip
            default: continue
            }
            guard let end = e.t1 ?? (e.kind == .stay && now == nil ? DAY : nil) else { continue }          // a trip on another day with no end has no length
            let t0 = max(e.t0, from), t1 = min(end, now ?? DAY)
            guard t1 > t0 else { continue }
            if let last = out.last, last.kind == kind, last.t1 >= t0 - 0.5 { out[out.count - 1].t1 = t1 } else { out.append(TLPlaceBit(id: e.id, t0: t0, t1: t1, kind: kind)) }
            from = t1
        }
        return out
    }

    // MARK: grouping

    /// The entries in the order of the day, put into groups. A stop under 15 minutes, in no place he keeps and not still going, between two ways he went, is a row of the trip and not a
    /// place. What began while he was in a place is that place's, and the day's Claude Code sessions in one place are one row. The places and the trips carry their rails, and a trip
    /// says where it began and where it went by the places next to it. What is still to come within two hours says how long it is until it.
    private static func arrange(_ sorted: [TLEntry], now: Double?) -> [TLEntry] {
        var list = sorted
        // a short stop is a row of the way
        let ways = list.indices.filter { list[$0].kind == .stay || list[$0].kind == .trip }
        for (n, k) in ways.enumerated() where list[k].kind == .stay {
            let before = n > 0 ? list[ways[n - 1]].kind : nil, after = n + 1 < ways.count ? list[ways[n + 1]].kind : nil
            guard let t1 = list[k].t1, !list[k].going, !list[k].kept, t1 - list[k].t0 < 15, before == .trip, after == .trip else { continue }
            list[k].kind = .pause
            list[k].title = "停了 " + tlDur(max(1, tlInt(t1 - list[k].t0)))
            list[k].detail = list[k].barName                     // what the phone called the place, if it did
            list[k].symbol = "circle.fill"
            list[k].right = ""; list[k].more = []; list[k].color = nil; list[k].place = nil; list[k].about = nil
        }
        // what began while he was in a place is that place's: the latest place that holds its start (what is still to come is no place's)
        let stays = list.indices.filter { list[$0].kind == .stay }
        let nested: Set<TLEntry.Kind> = [.sleep, .talk, .picture, .event, .reminder, .departure, .work, .call, .payment, .claude, .workout, .code]
        var members: [Int: [Int]] = [:]
        var holder: [Int: Int] = [:]
        for k in list.indices where nested.contains(list[k].kind) && !list[k].later {
            guard let s = stays.last(where: { list[$0].t0 <= list[k].t0 && list[k].t0 < (list[$0].going ? .infinity : list[$0].t1 ?? .infinity) }) else { continue }
            holder[k] = s; members[s, default: []].append(k)
        }
        // the sessions of Claude Code in one place are one row, which opens to them
        var stands: [Int: TLEntry] = [:]
        var gone: Set<Int> = []
        for s in stays {
            let codes = (members[s] ?? []).filter { list[$0].kind == .code }
            guard codes.count > 1 else { continue }
            let sessions = codes.flatMap { list[$0].sessions }
            var g = TLEntry(id: "code-group-" + list[s].id, kind: .code, t0: sessions.map(\.t0).min() ?? list[codes[0]].t0, title: "\(codes.count) 个会话")
            g.time = clock(g.t0); g.t1 = sessions.map(\.t1).max(); g.symbol = TLSymbol.claude; g.lens = .work
            let titled = sessions.sorted { $0.t1 > $1.t1 }.compactMap(\.title).prefix(2)      // the two newest that have a title
            g.detail = titled.isEmpty ? nil : titled.joined(separator: " · ")
            g.sessions = sessions
            g.spans = sessions.map { TLMark(t0: $0.t0, t1: $0.t1) }
            var union = 0.0, edge = -Double.infinity
            for x in sessions.sorted(by: { $0.t0 < $1.t0 }) { union += max(0, x.t1 - max(x.t0, edge)); edge = max(edge, x.t1) }
            g.right = union >= 1 ? tlDur(tlInt(union)) : ""
            stands[codes[0]] = g; gone.formUnion(codes.dropFirst())
        }
        // her talks are no rows (the conversation holds them, yilun 10-20): the place a talk began in is lit with them, so their chip brings it forward
        var out: [TLEntry] = []
        for k in list.indices where holder[k] == nil && !gone.contains(k) && list[k].kind != .talk {
            var e = stands[k] ?? list[k]
            if (members[k] ?? []).contains(where: { list[$0].kind == .talk }) { e.lenses.insert(.talk) }
            out.append(e)
            guard e.kind == .stay else { continue }
            for x in members[k] ?? [] where !gone.contains(x) && list[x].kind != .talk { var y = stands[x] ?? list[x]; y.group = e.id; out.append(y) }
        }
        // what happened while he was on the way belongs to the trip
        let legs = out.filter { $0.kind == .trip || $0.kind == .pause }
        let apart: Set<TLEntry.Kind> = [.stay, .trip, .pause, .sleep, .now]
        for k in out.indices where out[k].group == nil && !apart.contains(out[k].kind) && !out[k].later {
            out[k].onWay = legs.contains { $0.t0 <= out[k].t0 && out[k].t0 < ($0.t1 ?? $0.t0) }
        }
        // the rails: a place's runs from its dot through its rows, the trips' is dashed through all of a run
        let hues = Dictionary(out.compactMap { e in e.kind == .stay ? e.color.map { (e.id, $0) } : nil }, uniquingKeysWith: { a, _ in a })
        func way(_ k: Int) -> Bool { out.indices.contains(k) && (out[k].kind == .trip || out[k].kind == .pause || out[k].onWay) }
        for k in out.indices {
            if out[k].kind == .stay {
                out[k].rail = hues[out[k].id].map { .place($0) } ?? .none
                out[k].railOut = out.indices.contains(k + 1) && out[k + 1].group == out[k].id
            } else if let g = out[k].group {
                out[k].rail = hues[g].map { .place($0) } ?? .none
                out[k].railIn = true
                out[k].railOut = out.indices.contains(k + 1) && out[k + 1].group == g
            } else if way(k) {
                out[k].rail = .trip
                out[k].railIn = way(k - 1); out[k].railOut = way(k + 1)
            }
        }
        // a trip says where it began and where it went by the places next to it in the day (a place the phone has no name for is not one)
        for k in out.indices where out[k].kind == .trip && !out[k].ride {
            func named(_ range: [Int]) -> String? {
                guard let j = range.first(where: { out[$0].kind == .stay || out[$0].kind == .trip || out[$0].kind == .pause }), out[j].kind == .stay, out[j].barName != nil else { return nil }
                return out[j].title
            }
            let from = named(Array(out.indices.prefix(k).reversed())), to = named(Array(out.indices.dropFirst(k + 1)))
            let line = from != nil && to != nil ? "\(from ?? "") → \(to ?? "")" : from.map { "从 \($0) 出发" } ?? to.map { "到 \($0)" }
            out[k].more = line.map { [$0] } ?? []
        }
        soon(&out, now: now)
        return out
    }

    /// what is still to come within two hours says how long it is until it (a point with a value of its own keeps it)
    static func soon(_ list: inout [TLEntry], now: Double?) {
        guard let now else { return }
        for k in list.indices where list[k].later && !list[k].strongRight && list[k].t0 - now <= 120 {
            list[k].right = "还有 " + tlDur(max(1, tlInt((list[k].t0 - now).rounded(.up)))); list[k].soon = true
        }
    }

    /// the blank between one group and the next: above a place, above the first leg of a run of trips, and above a row that is in no place after a place or a run
    private static func space(_ list: inout [TLEntry]) {
        for k in list.indices.dropFirst() {
            let p = list[k - 1], e = list[k]
            if e.kind == .now || p.kind == .now { continue }
            let afterBlock = p.group != nil || p.kind == .stay || p.rail == .trip
            switch e.kind {
            case .stay: list[k].gap = TLMetrics.gap
            case .trip, .pause: if !e.railIn { list[k].gap = TLMetrics.gap }
            default: if e.group == nil && afterBlock && !(e.rail == .trip && e.railIn) { list[k].gap = TLMetrics.gap }
            }
        }
    }

    /// what opening a row offers: each only where it works today
    static func actions(of e: TLEntry, now: Double?) -> [TLAction] {
        var a: [TLAction] = []
        switch e.kind {
        case .stay: if e.place?.canRename == true { a.append(.rename) } else if e.place?.canName == true { a.append(.name) }
        case .event: if e.commitment != nil { a += [.confirm, .deny] }
        case .reminder: if e.waiting { a.append(.acknowledge) }
        default: break
        }
        if e.about != nil { a.append(.ask) }
        if !e.header, TLSayWhat.range(of: e, now: now) != nil { a.append(.sayWhat) }
        return a
    }

    // MARK: the rest of the day's numbers

    private static func bodyValues(_ s: DaySummary?) -> [TLBody] {
        guard let s else { return [] }
        var out: [TLBody] = []
        if let v = s.steps, v.isFinite, v >= 0 { out.append(TLBody(prefix: "", value: tlGroup(tlInt(v)), suffix: " 步")) }
        if let v = s.resting_hr, v.isFinite, v > 0 { out.append(TLBody(prefix: "静息 ", value: "\(tlInt(v))", suffix: "")) }
        if let v = s.hrv, v.isFinite, v > 0 { out.append(TLBody(prefix: "HRV ", value: "\(tlInt(v))", suffix: "")) }
        return out
    }

    static func weatherOf(_ w: DayWeather?) -> TLWeather? {
        guard let w else { return nil }
        let words = weatherWords(w.condition)
        let hi = w.high.flatMap { $0.isFinite ? tlInt($0) : nil }, lo = w.low.flatMap { $0.isFinite ? tlInt($0) : nil }
        return words == nil && hi == nil && lo == nil ? nil : TLWeather(word: words?.word, symbol: words?.symbol, high: hi, low: lo)
    }

    /// when the sun rose and set on `day` (the header's weather says it when it is tapped)
    static func sun(of r: DayReply) -> TLSun? { let c = TLClock(r); return sunOf(r.weather) { c.minute(ms: $0) } }

    /// when the sun rose and set, where that is on this day
    private static func sunOf(_ w: DayWeather?, _ m: (Int64) -> Double) -> TLSun? {
        guard let w else { return nil }
        func inside(_ ms: Int64?) -> Double? { ms.map(m).flatMap { $0.isFinite && $0 >= 0 && $0 < DAY ? $0 : nil } }
        let rise = inside(w.sunrise_ms), set = inside(w.sunset_ms)
        return rise == nil && set == nil ? nil : TLSun(rise: rise, set: set)
    }

    /// what, starting before the bar's start, brings the start back: where he went and what he did, never a stay alone
    private static let axisKinds: Set<String> = ["move", "ride", "talk", "picture", "event", "work", "call", "payment", "workout", "code"]

    /// Where the bar starts, in minutes: the later of 06:00 and the hour before he woke (`wake`, the end of the night that ended on this day: none is 06:00), so the
    /// waking day gets the width. Something in `early` (when the things of the day began) before that moves it back to its hour, and on today (`now`) it is
    /// never later than the hour now is in, so that the line for now is always on the bar.
    static func axisStart(wake: Double?, early: [Double], now: Double? = nil) -> Double {
        var lo = 360.0
        if let wake { lo = max(lo, ((wake - 60) / 60).rounded(.down) * 60) }
        if let first = early.filter({ $0 >= 0 && $0 < lo }).min() { lo = (first / 60).rounded(.down) * 60 }
        if let now { lo = min(lo, max(0, (now / 60).rounded(.down) * 60)) }
        return lo
    }

    /// the footnote under the list: what has not been read, named by what it gives the day (the dial words it its own way: `BrainLink.missingText`)
    static func missingText(_ missing: [String]) -> String? {
        let names = ["phone": "手机的记录", "reminders": "提醒", "mac": "Mac 的时间", "talks": "对话", "calendar": "日历", "departures": "公交"]
        let m = missing.compactMap { names[$0] }
        return m.isEmpty ? nil : "还没读到：" + m.joined(separator: "、")
    }
}
