import Foundation

// The words of each kind of item as one row of the timeline: what it is called, the muted line under it, what stands at the right, and what opening it says in full. They are the
// Mac's (desktop/resonance/src/dayView.ts) except where the phone has the room to say more. Plain values only, so each row can be tested without a screen.
// A field that is not there makes a row without it; a kind this phone does not know has no row (`row` answers nil).

/// What he has just done that the brain's next answer will say too, and the day shows before it does.
struct TLDone: Equatable {
    var highlights: [Highlight]?                // the day's highlights as they stand after his mark; none: the brain's own
    var confirmed: Set<String> = []             // the events his mail made that he has said are his (by the item's id)
    var dropped: Set<String> = []               // and those he has said are not: the day has no row for them
    var named: [String: KeptPlace] = [:]        // the stays he has just given a place's first name, by the stay's id
}

/// what a row needs to know of the day it is in
struct TLFrame {
    let now: Double?                            // the minute of now, on today only
    let m: (Int64) -> Double                    // epoch milliseconds to minutes from the day's own midnight
    let before: (Int) -> String                 // how a day that many days before this one is said: 昨天, 前天, 10月15日
    let topic: (String) -> String?              // what a talk was about
    let marks: [String: String]                 // the labels of what mattered, by item (an empty one is a star with no words of its own)
    var hers: Set<String> = []                  // and which of them are her picks and not his marks
    var acked: Set<String> = []                 // the reminders he has just said he knows, before the brain's next answer says it
    var renamed: [String: String] = [:]         // the places he has just given another name (by their id), likewise
    var confirmed: Set<String> = []             // the events his mail made that he has just said are his
    var named: [String: KeptPlace] = [:]        // the stays he has just named a place at, by the stay's id
}

/// the text of an optional string, or nil when it is empty
func tlText(_ s: String?) -> String? {
    s.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.flatMap { $0.isEmpty ? nil : $0 }
}

/// the end of a span as the Mac writes it: `24:00` at the day's end, `次日 02:10` past it
func tlEnd(_ t: Double) -> String { t == DAY ? "24:00" : (t > DAY ? "次日 " : "") + clock(t) }

/// 3.2 公里, 800 米
func tlDistance(_ meters: Double) -> String? {
    guard meters.isFinite, meters > 0 else { return nil }
    return meters >= 1000 ? String(format: "%.1f 公里", meters / 1000) : "\(tlInt(meters)) 米"
}

extension DayTimelineModel {
    /// The hex of a commitment's id (`commitment-<hex>@<day>`), which is what the brain's route is asked with: hex digits and nothing else, so that it can stand in a path.
    static func commitmentHex(_ id: String) -> String? {
        let head = "commitment-"
        guard id.hasPrefix(head), let at = id.firstIndex(of: "@") else { return nil }
        let hex = id[id.index(id.startIndex, offsetBy: head.count)..<at]
        return !hex.isEmpty && hex.utf8.count <= 64 && hex.utf8.allSatisfy { (48...57).contains($0) || (65...70).contains($0) || (97...102).contains($0) } ? String(hex) : nil
    }

    /// One item as a row, or nil for a kind this phone does not know. `t0` and `t1` are its times in minutes from the day's midnight (`t1` nil: it has no end yet).
    static func row(_ i: DayItem, t0: Double, t1: Double?, _ f: TLFrame) -> TLEntry? {
        let now = f.now
        let later = now.map { t0 > $0 } ?? false
        // what happens in one moment has no length and does not go on; the rest, begun today with no end yet, goes on
        let instant = ["reminder", "picture", "departure", "payment"].contains(i.kind)
        let running = !instant && t1 == nil && now != nil && !later
        let end = t1 ?? (running ? now : nil)
        let span = end.map { max(0, $0 - t0) }
        let dur = span.flatMap { $0 >= 1 ? tlDur(tlInt($0)) : nil }
        var e = TLEntry(id: i.id, kind: .work, t0: t0, title: "")
        e.time = clock(t0); e.t1 = end; e.later = later; e.going = running
        switch i.kind {
        case "sleep":
            e.kind = .sleep
            e.title = "睡觉"
            e.symbol = "moon.fill"
            e.detail = rangeText(t0, t1, running: running, f)
            let asleep = i.minutes ?? span
            e.right = asleep.map { tlDur(tlInt($0)) } ?? ""
            e.barIcon = "moon.fill"          // the moon alone on the bar; how long is in the readout
            e.sleep = sleepInfo(i, t0: t0, end: end, f)
            e.glance = e.sleep?.short
        case "stay":
            e.kind = .stay
            // the place he keeps it in (or has just named it as: the brain's next answer will say the same), and the name he gave it, which a rename he has just made changes before the brain's next answer does
            let kept = i.kept_place ?? f.named[i.id]
            let given = kept.flatMap { f.renamed[$0.id] }.flatMap(tlText) ?? tlText(kept?.name)
            let ring = (kept != nil ? given.map { (name: $0, kept: true) } : nil) ?? i.ringName
            e.title = ring?.name ?? "没名字的地方"
            let hue: RGB
            if let k = kept {
                hue = PlaceTint.line(role: k.role, id: k.id); e.kept = true
            } else { hue = PlaceTint.lineNeutral }
            e.color = hue
            e.barName = ring?.name
            e.barIcon = ["home": "house.fill", "school": "graduationcap.fill"][kept?.role ?? ""] ?? "mappin"
            // a stay still on has its start and how long it has gone; one that ended, its hours and its length
            e.right = [t1 == nil ? "\(clockDay(t0, f)) 起" : rangeText(t0, t1, running: false, f), dur].compactMap { $0 }.joined(separator: " · ")
            let where_ = tlText(i.place)
            e.more = [where_, e.kept ? "常去的地方 · " + (given ?? e.title) : nil].compactMap { $0 }
            // where it was: coordinates that are not on the earth are not coordinates
            let onEarth = i.lat.flatMap { la in i.lng.map { lo in la.isFinite && lo.isFinite && (-90...90).contains(la) && (-180...180).contains(lo) } } ?? false
            if onEarth || where_ != nil || kept != nil {
                e.place = TLPlace(lat: onEarth ? i.lat : nil, lng: onEarth ? i.lng : nil, text: where_, keptId: kept?.id, keptName: given)
            }
        case "move", "ride":
            e.kind = .trip
            e.right = dur ?? ""
            if i.kind == "ride" {
                e.ride = true
                e.symbol = "bus.fill"
                let stops = [tlText(i.board_stop), tlText(i.alight_stop)]
                let trail = stops.compactMap { $0 }.joined(separator: " → ")
                let route = routeText(i.route)
                e.title = route.map { "\($0) 路" } ?? "公交"
                e.barName = route; e.barIcon = "bus.fill"
                e.detail = trail.isEmpty ? nil : trail          // one stop is that stop alone, none no line at all
                // opened it says what the line does not: with two stops the line says both, with one it is only that stop, so which one it was is said here
                let one = stops.compactMap { $0 }.count == 1
                e.more = [one ? stops[0].map { "上车 " + $0 } : nil, one ? stops[1].map { "下车 " + $0 } : nil, tlText(i.credit).map { "实时：" + $0 },
                          tlText(i.ended_reason).flatMap { endings[$0] }].compactMap { $0 }
            } else {
                let (word, symbol) = moves[i.activity ?? ""] ?? ("移动", "arrow.right")
                e.title = word; e.symbol = symbol
                e.barName = word; e.barIcon = symbol
            }
        case "talk":
            e.kind = .talk
            let said = f.topic(i.id)
            e.title = said ?? "和她说话"
            e.symbol = "bubble.left.fill"
            if let n = i.turns, n > 0 { e.detail = "\(n) 句" }
            e.glance = said ?? ["和她说话", e.detail].compactMap { $0 }.joined(separator: " · ")
            e.right = dur ?? ""
            e.talkStart = i.start_ms
            e.turns = i.turns.flatMap { $0 > 0 ? $0 : nil }
        case "picture":
            e.kind = .picture
            e.title = "照片"
            e.symbol = "photo.fill"
            let lines = (i.note ?? "").split(whereSeparator: \.isNewline).compactMap { tlText(String($0)) }
            e.detail = lines.first
            e.more = Array(lines.dropFirst())           // the line says the first, so opened it says the rest
            e.glance = ["照片", e.detail].compactMap { $0 }.joined(separator: " · ")
            e.attachment = tlText(i.attachment) ?? (i.id.hasPrefix("picture-") ? String(i.id.dropFirst("picture-".count)) : i.id)
        case "event":
            e.kind = .event
            let hidden = i.private == true
            e.title = hidden ? "私事" : tlText(i.title) ?? "日程"
            e.symbol = "calendar.circle.fill"
            e.barName = e.title; e.barIcon = "calendar"; e.glance = e.title
            // a mail made it and he has not said it is his (the brain's answer says so, and so does he, before the brain does): say so, and where (a private one names no place)
            let mine = i.source == "mail" && (i.confirmed == true || f.confirmed.contains(i.id))
            let unconfirmed = i.source == "mail" && i.confirmed == false && !f.confirmed.contains(i.id)
            let said = mine ? "你确认了" : nil
            // where it came from (none: his calendar), that it is not his yet or that he said it is, and where; a private one says no more
            let word = i.source.map { eventSources[$0] } ?? "日历"
            let parts = hidden ? [] : [word, unconfirmed ? "还没确认" : said, tlText(i.place)].compactMap { $0 }
            e.detail = parts.isEmpty ? nil : parts.joined(separator: " · ")
            // opened it says what the line does not: the hours (the row has when it began), and for a private one, whose line says nothing, where it came from and that he has said it is his
            e.more = [t1 != nil && (span ?? 0) >= 1 ? rangeText(t0, t1, running: false, f) : nil, hidden ? "来自" + (eventSources[tlText(i.source) ?? ""] ?? "日历") : nil,
                      hidden ? said : nil].compactMap { $0 }
            e.right = dur ?? ""
            e.waiting = unconfirmed && (now.map { (t1 ?? t0) > $0 } ?? false)
            e.commitment = unconfirmed ? commitmentHex(i.id) : nil
        case "reminder":
            e.kind = .reminder
            e.title = tlText(i.text) ?? "提醒"
            e.glance = "提醒 · " + e.title
            e.symbol = "bell.fill"
            e.detail = i.fired == true ? (i.acknowledged == true || f.acked.contains(i.id) ? "你知道了" : "响过了") : later ? "还没到点" : nil
            e.waiting = i.fired == true && i.acknowledged == false && !f.acked.contains(i.id)
        case "departure":
            e.kind = .departure
            let route = tlText(i.route)
            e.title = tlText(i.title) ?? route.map { "\($0) 路" } ?? "公交"
            e.glance = e.title
            e.symbol = "bus.fill"
            // a point: where it goes under its name, and when to leave where the length would be
            let leave = i.leave_at_ms.map { clock(f.m($0)) + " 出门" }
            e.detail = tlText(i.to).map { "去 " + $0 }
            e.right = leave ?? ""
            e.strongRight = leave != nil
            e.more = [route.map { "路线 " + $0 }, tlText(i.board_stop).map { "在 " + $0 + " 上车" }].compactMap { $0 }
        case "work":
            e.kind = .work
            e.symbol = "laptopcomputer"
            e.title = "在 Mac 上工作"
            let projects = tops(i.projects), apps = tops(i.apps)
            let lead = [projects.first?.name, apps.first?.name].compactMap { $0 }
            e.detail = lead.isEmpty ? nil : lead.joined(separator: " · ")
            // the time he was at it is the point of the row; the stretch it spans is said when it is opened
            let active = i.active_s.flatMap { $0 >= 1 && $0.isFinite ? tlDur(tlInt($0 / 60)) : nil }
            e.right = active.map { "活跃 " + $0 } ?? dur ?? ""
            e.work = TLWork(projects: projects, apps: apps, active: i.active_s.flatMap { $0.isFinite && $0 >= 1 ? tlInt($0 / 60) : nil }, total: span.map { tlInt($0) })
            e.more = [[active.map { "活跃 " + $0 }, dur.map { "共 " + $0 }].compactMap { $0 }.joined(separator: " · ")].filter { !$0.isEmpty }
            e.glance = lead.isEmpty ? "在 Mac 上工作" : lead.joined(separator: " · ")
        case "call":
            e.kind = .call
            let app = tlText(i.app)
            e.title = app.map { "通话 · " + $0 } ?? "通话"
            e.symbol = "phone.fill"
            e.glance = e.title
            e.right = dur ?? ""
        case "payment":
            e.kind = .payment
            e.symbol = "creditcard.fill"
            let shop = tlText(i.counterparty), item = tlText(i.text), cents = i.amount?.cents, via = sources[tlText(i.source) ?? ""]
            e.title = shop ?? item ?? "付款"
            let detail = [shop != nil && item != shop ? item : nil, via].compactMap { $0 }.joined(separator: " · ")
            e.detail = detail.isEmpty ? nil : detail
            // what it came to: money out is the plain amount, money in has a plus
            let sum = cents.map { tlMoney(abs($0), i.currency) }
            e.right = sum.map { (i.direction == "in" ? "+" : "") + $0 } ?? ""
            e.strongRight = true
            e.glance = [e.title, e.right.isEmpty ? nil : e.right].compactMap { $0 }.joined(separator: " · ")
            // opened: which way, the amount (with its currency's code where the mark alone does not say it: a `$` or a `¥`), and where the import came from
            let way = i.direction == "in" ? "收入" : "支出", code = tlText(i.currency)?.uppercased()
            let both = sum.map { s in way + " " + s + (code.flatMap { c in s.hasPrefix("$") || s.hasPrefix("¥") ? " " + c : nil } ?? "") }
            e.more = [both, via == nil ? tlText(i.source).map { "来自" + $0 } : nil].compactMap { $0 }
        case "messages":
            e.kind = .messages
            e.symbol = "message.fill"
            e.title = tlText(i.conversation) ?? tlText(i.app) ?? "消息"
            e.detail = "发 \(max(0, i.sent ?? 0)) · 收 \(max(0, i.received ?? 0))"
            e.right = dur ?? ""
            e.glance = e.title
        case "phone_call":
            e.kind = .phoneCall
            e.symbol = "phone.fill"
            e.title = tlText(i.person) ?? "电话"
            e.detail = [["sent": "打出", "received": "打进"][i.direction ?? ""], tlText(i.app)].compactMap { $0 }.joined(separator: " · ")
            if e.detail?.isEmpty == true { e.detail = nil }
            // a call is told in seconds when it was under a minute
            e.right = i.duration_s.flatMap { $0.isFinite && $0 >= 1 ? ($0 < 60 ? "\(tlInt($0)) 秒" : tlDur(tlInt($0 / 60))) : nil } ?? dur ?? ""
            e.glance = e.title
        case "screen":
            e.kind = .screen
            e.symbol = i.device == "iPad" ? "ipad" : "iphone"
            e.title = "用 " + (tlText(i.device) ?? "手机")
            let apps = tops(i.apps).prefix(2).map { $0.name + " " + tlDur($0.minutes) }
            e.detail = apps.isEmpty ? nil : apps.joined(separator: " · ")
            e.right = i.active_s.flatMap { $0 >= 1 && $0.isFinite ? tlDur(tlInt($0 / 60)) : nil } ?? dur ?? ""
            e.glance = e.title
        case "media":
            e.kind = .media
            e.symbol = "music.note"
            e.title = tlText(i.app) ?? "播放"
            let top = i.titles?.first.flatMap { t in tlText(t.title).map { [$0, tlText(t.artist)].compactMap { $0 }.joined(separator: " · ") } }
            e.detail = top
            e.right = i.active_s.flatMap { $0 >= 1 && $0.isFinite ? tlDur(tlInt($0 / 60)) : nil } ?? dur ?? ""
            e.glance = e.title
        case "away_note":
            e.kind = .away
            e.symbol = "square.and.pencil"
            e.title = tlText(i.title) ?? "离开电脑时写的"
            e.detail = "TimeSink 里你写的"
            e.right = dur ?? ""
            e.glance = e.title
        case "claude_chat":
            e.kind = .claude
            e.symbol = "sparkles"
            let name = tlText(i.text)
            e.title = name == nil || name == "Claude conversation" ? "Claude 对话" : name ?? "Claude 对话"
            e.glance = "Claude · " + e.title
            e.detail = "和 Claude"
            let said = i.messages.flatMap { $0 > 0 ? "\($0) 条" : nil }
            e.right = said ?? dur ?? ""
        case "claude_day":
            e.kind = .claude
            e.symbol = "sparkles"
            let titles = (i.text ?? "").split(separator: ";").compactMap { tlText(String($0)) }
            e.title = "Claude 日报"
            let n = i.chats.flatMap { $0 > 0 ? "\($0) 个对话" : nil }
            e.detail = ([n].compactMap { $0 } + titles).joined(separator: " · ")
            if e.detail?.isEmpty == true { e.detail = nil }
            e.more = titles
        case "workout":
            e.kind = .workout
            let (word, symbol) = workoutWords(i.activity ?? i.text)
            e.title = word; e.symbol = symbol
            let facts = [i.distance_m.flatMap(tlDistance), i.energy_kcal.flatMap { $0.isFinite && $0 > 0 ? "\(tlInt($0)) 千卡" : nil },
                         i.avg_hr.flatMap { $0.isFinite && $0 > 0 ? "平均心率 \(tlInt($0))" : nil }].compactMap { $0 }
            e.detail = facts.isEmpty ? nil : facts.joined(separator: " · ")
            e.right = dur ?? ""
            e.glance = [word, i.distance_m.flatMap(tlDistance)].compactMap { $0 }.joined(separator: " · ")
        case "code":
            e.kind = .code
            e.symbol = "terminal.fill"
            let project = tlText(i.project), account = tlText(i.account)?.uppercased()
            let s = TLSession(id: i.id, t0: t0, t1: max(t0, end ?? t0), time: e.time, title: tlText(i.title), project: project, account: account, right: dur ?? "", recap: tlText(i.detail))
            e.title = s.name
            e.detail = [project, account].compactMap { $0 }.joined(separator: " · ")
            if e.detail?.isEmpty == true { e.detail = nil }
            e.right = dur ?? ""
            e.more = tlText(i.detail).map { [$0] } ?? []
            e.glance = s.told
            e.sessions = [s]
        default:
            return nil              // a kind this phone does not know yet: left out, not guessed at
        }
        // a stretch (the night, a place, a trip) as the readout under a finger opens with it
        switch e.kind {
        case .stay: e.head = e.title + " · " + e.right
        case .sleep: e.head = [e.title, e.detail, e.right.isEmpty ? nil : e.right].compactMap { $0 }.joined(separator: " · ")
        case .trip: e.head = [e.title, rangeText(t0, t1, running: running, f), e.right.isEmpty ? nil : e.right].compactMap { $0 }.joined(separator: " · ")
        default: break
        }
        // what mattered: its label is its name (the brain writes `kind HH:MM` when nobody did, which says less than the row does: a star and no new name)
        if let label = f.marks[i.id] {
            e.lit = true; e.hers = f.hers.contains(i.id)
            if e.kind != .stay, !label.isEmpty {
                e.detail = [e.title, e.detail].compactMap { $0 }.joined(separator: " · ")
                e.title = label; e.glance = label
            }
        }
        if About.kinds.contains(i.kind), About.fits(id: i.id) {
            e.about = About(kind: i.kind, id: i.id, title: e.title, startMs: i.start_ms, mark: mark(i, later: later))
        }
        return e
    }

    // MARK: pieces of the words

    /// the night's stages in minutes from the day's midnight, and what they add up to; nil when the night has none this phone knows
    private static func sleepInfo(_ i: DayItem, t0: Double, end: Double?, _ f: TLFrame) -> TLSleep? {
        let stages: [TLStage] = (i.stages ?? []).compactMap { s in
            guard let k = TLStage.Kind(name: s.stage), s.end_ms > s.start_ms else { return nil }
            let a = f.m(s.start_ms), b = f.m(s.end_ms)
            guard a.isFinite, b.isFinite, abs(a) <= 30 * DAY, abs(b) <= 30 * DAY, b > a else { return nil }
            return TLStage(kind: k, t0: a, t1: b)
        }
        guard !stages.isEmpty else { return nil }
        // The night is flattened once, inside the block: each instant takes one stage (`TLStage.blocks`), so two sources that both saw the same hour do not count it twice. What he slept,
        // how often he woke, the hypnogram and the shading of the bar all read these blocks.
        let hi = end ?? stages.map(\.t1).max() ?? t0
        let night = TLStage.blocks(stages, in: t0, hi)
        func asleep(_ k: TLStage.Kind) -> Int { tlInt(night.filter { $0.kind == k }.reduce(0) { $0 + ($1.t1 - $1.t0) }) }
        var s = TLSleep(stages: night)
        s.deep = asleep(.deep); s.core = asleep(.core); s.rem = asleep(.rem)
        s.awake = night.filter { $0.kind == .awake }.count
        let parts = [s.deep > 0 ? "深睡 " + tlHM(s.deep) : nil, s.core > 0 ? "核心 " + tlHM(s.core) : nil, s.rem > 0 ? "REM " + tlHM(s.rem) : nil,
                     s.awake > 0 ? "醒 \(s.awake) 次" : nil].compactMap { $0 }
        s.summary = parts.isEmpty ? nil : parts.joined(separator: " · ")
        let short = [s.deep > 0 ? "深睡 " + tlHM(s.deep) : nil, s.rem > 0 ? "REM " + tlHM(s.rem) : nil, s.awake > 0 ? "醒 \(s.awake) 次" : nil].compactMap { $0 }
        s.short = short.isEmpty ? nil : short.joined(separator: " · ")
        return s
    }

    /// up to five of the biggest of a list of what he worked on, as bars: the longest is the whole width
    private static func tops(_ list: [DayItem.Top]?) -> [TLTop] {
        let sorted = (list ?? []).filter { tlText($0.name) != nil && $0.seconds.isFinite && $0.seconds >= 0 }.sorted { $0.seconds > $1.seconds }.prefix(5)
        let big = sorted.first?.seconds ?? 0
        return sorted.map { TLTop(name: tlText($0.name) ?? "", minutes: max(1, tlInt($0.seconds / 60)), share: big > 0 ? $0.seconds / big : 0) }
    }

    /// what a workout is called and drawn as, from the name the phone gave it
    static func workoutWords(_ activity: String?) -> (String, String) {
        knownWorkout(activity) ?? ("运动", "figure.mixed.cardio")
    }
    /// the same for a workout whose name the table has a word for, else none
    private static func knownWorkout(_ activity: String?) -> (String, String)? {
        let a = (activity ?? "").lowercased()
        let table: [(String, String, String)] = [("run", "跑步", "figure.run"), ("walk", "步行", "figure.walk"), ("cycl", "骑车", "bicycle"), ("bik", "骑车", "bicycle"),
                                                 ("hik", "徒步", "figure.hiking"), ("swim", "游泳", "figure.pool.swim"), ("strength", "力量训练", "dumbbell.fill"), ("yoga", "瑜伽", "figure.yoga")]
        return table.first { a.contains($0.0) }.map { ($0.1, $0.2) }
    }

    /// a bus route as the rows write it: `27`, `14→7` (the arrow without spaces)
    static func routeText(_ raw: String?) -> String? {
        tlText(raw).map { $0.replacingOccurrences(of: #"\s*→\s*"#, with: "→", options: .regularExpression) }
    }

    /// What a span of the doing lane says he was doing, in the words the Mac uses too, or none (no title, or a blank one). A model's phrase or his own answer (`inferred`, `stated`) is shown
    /// as given. The program (`observed`) titles a span from the facts under it, never from the place, so its title is the project or session (做 …), the route (`27 路`), the app of a call
    /// (… 通话, the one fact whose span has no category of its own), the workout (跑步), or the night (睡觉).
    static func doingWords(_ s: DaySpan) -> String? {
        guard let title = tlText(s.title) else { return nil }
        guard s.basis == "observed" else { return title }
        switch s.category {
        case "work": return "做 " + title
        case "commute": return (routeText(title) ?? title) + " 路"
        case "exercise": return knownWorkout(title)?.0 ?? title
        case "sleep": return "睡觉"
        case "unknown": return title + " 通话"
        default: return title
        }
    }

    /// the day's weather as words and a symbol: WeatherKit's condition as the brain words it (`mostly cloudy`), read without case or separators, in the few the Mac has (`frigid` and
    /// `hot` it has no word for either: only the temperatures are said)
    static func weatherWords(_ condition: String?) -> (word: String, symbol: String)? {
        weatherTable[(condition ?? "").lowercased().filter { $0 >= "a" && $0 <= "z" }]
    }
    private static let weatherTable: [String: (word: String, symbol: String)] = {
        let drizzle = ("小雨", "cloud.drizzle.fill"), rain = ("雨", "cloud.rain.fill"), storm = ("雷雨", "cloud.bolt.rain.fill"), snow = ("雪", "cloud.snow.fill")
        let fog = ("雾", "cloud.fog.fill"), wind = ("风", "wind")
        return ["clear": ("晴", "sun.max.fill"), "mostlyclear": ("少云", "cloud.sun.fill"), "partlycloudy": ("多云", "cloud.sun.fill"), "mostlycloudy": ("多云", "cloud.fill"), "cloudy": ("阴", "cloud.fill"),
                "drizzle": drizzle, "sunshowers": drizzle, "freezingdrizzle": drizzle, "rain": rain, "heavyrain": rain, "freezingrain": rain,
                "isolatedthunderstorms": storm, "scatteredthunderstorms": storm, "thunderstorms": storm, "strongstorms": storm, "tropicalstorm": storm, "hurricane": storm,
                "snow": snow, "heavysnow": snow, "flurries": snow, "sunflurries": snow, "sleet": snow, "wintrymix": snow, "blizzard": snow, "blowingsnow": snow, "hail": snow,
                "foggy": fog, "haze": fog, "smoky": fog, "blowingdust": fog, "breezy": wind, "windy": wind]
    }()

    /// where an imported payment came from, in the words the Mac uses
    private static let sources = ["alipay": "支付宝", "wechat_pay": "微信", "boc": "中国银行"]
    /// where an event came from (none: his calendar)
    private static let eventSources = ["mail": "邮件", "said": "你说的", "plan": "计划"]
    /// how a ride ended, in plain words
    private static let endings = ["left_vehicle": "下车了", "passed_stop": "过了要下的站，就算到了", "said": "你说了下车", "schedule_end": "这趟车到点了，就算结束", "replaced": "换了另一趟车"]

    static let moves: [String: (String, String)] = [
        "walking": ("步行", "figure.walk"), "running": ("跑步", "figure.run"), "cycling": ("骑车", "bicycle"), "automotive": ("开车", "car.fill"),
    ]

    /// the mark the chip wears: the one the dial gives the same item (`StarEngine.kdOf`)
    static func mark(_ i: DayItem, later: Bool) -> Kd {
        if later { return .next }
        switch i.state {
        case "done": return i.lane == "her" ? .done : .lit
        case "now": return .work
        default: return .next
        }
    }

    /// `09:05–11:25`; a stay that began on an earlier day says which, and one still on reads `18:50 起`
    static func rangeText(_ t0: Double, _ t1: Double?, running: Bool, _ f: TLFrame) -> String {
        guard let t1, !running else { return clockDay(t0, f) + " 起" }
        return clockDay(t0, f) + "–" + tlEnd(t1)
    }
    /// a time of this day as `09:05`, and one before it with its day: `昨天 22:00`, `前天 22:00`, `10月15日 22:00`
    static func clockDay(_ t: Double, _ f: TLFrame) -> String {
        guard t < 0 else { return clock(t) }
        return f.before(Int((-t / DAY).rounded(.up))) + " " + clock(t)
    }
}
