import Foundation

// The week as she and the phone know it, and what the phone saw minute by minute.
// Sample data of the prototype (Thursday 10.8, 21:14). A later change wires real data in behind the same shapes.

enum Lane { case you, her }
enum St: Int { case wait = 0, work = 1, next = 2, done = 3 }   // raw value = sort order of focusSet
/// What a star looks like: the lane and state folded with whether its time has come yet.
enum Kd { case next, lit, done, wait, work }

final class Ev {
    let id: String
    var t: Double
    var d: Double?
    var lane: Lane
    var st: St
    var title: String
    var meta: String
    var note: String?
    var s: String?
    var src: String?
    var mag: Double?
    var approve = false, draft = false, call = false
    /// from the brain's day, not the prototype
    var live = false
    /// what waits on the owner behind this star, from the brain (Brain/Waiting)
    var card: WaitingCard?
    /// a talk star: when the talk began (ms), for its opened row to show the talk
    var talkStart: Int64?
    /// the brain's item kind and start (ms), for telling it what is open (Brain/About)
    var kind: String?, startMs: Int64?
    /// what mattered that day (Brain/RealDay): the colour of the soft glow round its bigger star
    var spot: RGB?
    var born: Double = -1e9, flash: Double = -1e9, ign: Double = -1e9
    // what the last frame drew, for hit tests and trails
    var on = false, focus = false
    var kd: Kd = .next
    var v = 0.0, r = 0.0, an = 0.0, x = 0.0, y = 0.0, pr = 1.0, ps = 1.0

    init(_ id: String, _ t: Double, _ lane: Lane, _ st: St, _ title: String, _ meta: String, d: Double? = nil, s: String? = nil, src: String? = nil,
         mag: Double? = nil, note: String? = nil) {
        self.id = id; self.t = t; self.lane = lane; self.st = st; self.title = title; self.meta = meta
        self.d = d; self.s = s; self.src = src; self.mag = mag; self.note = note
    }
    func copy(id newId: String? = nil) -> Ev {
        let e = Ev(newId ?? id, t, lane, st, title, meta, d: d, s: s, src: src, mag: mag, note: note)
        e.approve = approve; e.draft = draft; e.call = call
        return e
    }
}

private func D(_ n: Double) -> Double { n * DAY }

func makeEvents() -> [Ev] {
    var e: [Ev] = []
    func add(_ x: Ev) { e.append(x) }
    add(Ev("m1", D(-3) + 380, .her, .done, "晨报", "周一 06:20"))
    add(Ev("m2", D(-3) + 600, .you, .done, "投了 3 家公司", "周一 10:00", s: "投递", src: "Mac"))
    add(Ev("m3", D(-3) + 840, .you, .done, "专注 · 简历", "周一 14:00–17:00", d: 180, s: "简历", src: "Mac"))
    add(Ev("m4", D(-3) + 1200, .her, .done, "整理投递记录", "周一 20:00"))
    add(Ev("t1", D(-2) + 380, .her, .done, "晨报", "周二 06:20"))
    add(Ev("t2", D(-2) + 665, .her, .done, "收到一面邀请", "周二 11:05 · 从 Gmail 里挑出来的", s: "邀请"))
    add(Ev("t3", D(-2) + 930, .her, .done, "约好一面时间", "周二 15:30"))
    add(Ev("t4", D(-2) + 1080, .you, .done, "健身", "周二 18:00–19:00", d: 60, s: "健身", src: "健康"))
    add(Ev("w1", D(-1) + 380, .her, .done, "晨报", "周三 06:20"))
    add(Ev("w2", D(-1) + 600, .you, .done, "准备一面", "周三 10:00–12:00", d: 120, s: "准备", src: "Mac"))
    add(Ev("w3", D(-1) + 900, .you, .done, "作品集首页", "周三 15:00–17:30", d: 150, s: "作品集", src: "Mac"))
    add(Ev("w4", D(-1) + 1260, .her, .done, "一面资料包", "周三 21:00 · 6 页"))
    // today: your ring from the phone, hers from her
    add(Ev("brief", 380, .her, .done, "晨报", "06:20 · 天气、日程、3 封新信", s: "晨报"))
    add(Ev("wake", 470, .you, .done, "起床", "07:50 · 闹钟响了两次", s: "起床", src: "闹钟"))
    add(Ev("out", 515, .you, .done, "出门 · 32 路公交", "08:35–09:05 · 戴着耳机", d: 30, s: "出门", src: "定位"))
    add(Ev("lib", 545, .you, .done, "在图书馆专注", "09:05–11:50 · 作品集网站，两个半小时", d: 165, s: "图书馆", src: "定位"))
    add(Ev("cv", 620, .her, .done, "简历第二版", "10:20 · 按你的意思改完了", s: "简历"))
    add(Ev("lunch", 735, .you, .done, "和 Mia 吃午饭", "12:15 · Café Lino", d: 40, s: "午饭", src: "日历"))
    add(Ev("iv1", 780, .you, .done, "一面 · Teams", "13:00–14:00 · 在家", d: 60, s: "一面", src: "日历", mag: 1.35, note: "我记了 6 条笔记。最后那个关于测试覆盖率的问题，你答得很好。"))
    add(Ev("notes", 850, .her, .done, "一面笔记", "14:10 · 6 条，写进了记忆", s: "笔记"))
    add(Ev("share", 980, .you, .done, "你分享给我一个职位", "16:20 · Lumen Labs · iOS 实习", s: "分享", src: "分享"))
    add(Ev("job", 983, .her, .done, "存进了求职表", "16:23 · 截止 10.20，我会提前提醒你", s: "求职表"))
    add(Ev("gym", 1060, .you, .done, "健身", "17:40–18:30 · 力量训练 · 410 千卡", d: 50, s: "健身", src: "健康"))
    add(Ev("ask", 1120, .her, .done, "你在 Mac 上让我写感谢信", "18:40", s: "交代"))
    add(Ev("dinner", 1170, .you, .done, "晚饭", "19:30 · 在家", s: "晚饭", src: "定位"))
    let mac = Ev("mac", 1240, .her, .wait, "Mac 上的会话在等你", "20:40 · 想跑一遍测试", s: "Mac"); mac.approve = true; add(mac)
    add(Ev("mail", 1250, .her, .work, "整理这周的求职邮件", "7/12 · 2 封要你看", d: 50, s: "邮件"))
    let thanks = Ev("thanks", 1266, .her, .wait, "感谢信写好了，等你点头", "21:06 · 发给 Rachel Lin", s: "感谢信"); thanks.draft = true; add(thanks)
    let mia = Ev("mia", 1290, .you, .wait, "给 Mia 回电话", "21:30 · 你下午在 Mac 上说的", s: "回电话"); mia.call = true; add(mia)
    add(Ev("sleep", 1410, .you, .next, "睡觉", "23:30 · 明早有二面", s: "睡觉"))
    add(Ev("night", 1500, .her, .next, "夜里整理今天的记忆", "01:00", s: "记忆"))
    // tomorrow and the weekend
    add(Ev("brief2", D(1) + 390, .her, .next, "晨报", "明天 06:30", s: "晨报"))
    add(Ev("alarm", D(1) + 450, .you, .next, "起床 · 闹钟", "明天 07:30", s: "闹钟"))
    add(Ev("remind", D(1) + 540, .her, .next, "提醒你出门", "明天 09:00", s: "提醒"))
    add(Ev("leave", D(1) + 560, .you, .next, "出门 · 公交 32 分钟", "明天 09:20", d: 32, s: "出门", note: "下雨概率 60%，带伞。"))
    add(Ev("iv2", D(1) + 600, .you, .next, "二面 · 市中心", "明天 10:00–11:00", d: 60, s: "二面", mag: 1.6, note: "资料和作品集放在「二面」文件夹里。路上可以再看一遍第 3 页。"))
    add(Ev("f1", D(1) + 840, .you, .next, "专注 · 作品集网站", "明天 14:00–16:00", d: 120, s: "专注"))
    add(Ev("leo", D(1) + 1140, .you, .next, "和 Leo 吃饭", "明天 19:00", s: "Leo"))
    add(Ev("hike", D(2) + 540, .her, .wait, "天气不错，去爬山？", "周六 09:00 · 我的提议", s: "爬山？"))
    add(Ev("week", D(3) + 1260, .her, .next, "整理这一周的记忆", "周日 21:00", s: "一周"))
    return e
}

/// Threads of one story, drawn as faint constellations.
let CONS: [(String, String)] = [("t2", "t3"), ("t3", "w2"), ("w2", "w4"), ("w4", "iv1"), ("iv1", "notes"), ("notes", "ask"), ("ask", "thanks"), ("share", "job"),
                                ("mac", "mail"), ("brief2", "alarm"), ("alarm", "remind"), ("remind", "leave"), ("leave", "iv2")]

let SUM: [Int: String] = [-3: "投了 3 家公司，下午专注改简历三小时。", -2: "收到一面邀请，约好了时间，晚上健身。", -1: "准备一面，写完作品集首页。",
                          0: "一面，在图书馆专注两个半小时，健身。还有三件等你。", 1: "二面，下午专注，晚上和 Leo 吃饭。", 2: "空着。天气好，我提议去爬山。", 3: "我会整理这一周的记忆。"]

struct Talk {
    var q: String
    var a: String
    var add: Ev? = nil
    var go: String? = nil
    var replay = false
}
func makeTalks() -> [Talk] {
    [Talk(q: "明早八点提醒我带上作品集", a: "好，明早八点提醒你。", add: Ev("port", D(1) + 480, .her, .next, "提醒：带上作品集", "明天 08:00 · 你刚说的", s: "作品集")),
     Talk(q: "我明天几点出门？", a: "九点二十。坐 32 路，路上三十二分钟。可能下雨，带伞。", go: "leave"),
     Talk(q: "今天我都做了什么？", a: "睡了七个多小时。上午在图书馆专注了两个半小时，中午和 Mia 吃饭，下午一面，傍晚去健身。走了八千四百步。", replay: true)]
}
let DRAFT = "Hi Rachel, thank you for today’s conversation. Hearing how your team tests firmware on real controllers made me even more excited about the role."

// MARK: - what the phone saw: where you were, minute by minute

let KINDS = ["sleep", "home", "place", "food", "bus", "walk", "gym"]
private let LV: [String: Double] = ["sleep": 0, "home": 0.16, "place": 0.1, "food": 0.14, "bus": 0.36, "walk": 0.8, "gym": 0.95]
let PLACE: [String: String] = ["sleep": "睡着", "home": "在家", "place": "图书馆", "food": "Café Lino", "bus": "32 路公交上", "walk": "路上", "gym": "健身房"]
private typealias Seg = (Double, Double, String)
private let TODAY_SEG: [Seg] = [(0, 40, "home"), (40, 470, "sleep"), (470, 515, "home"), (515, 545, "bus"), (545, 710, "place"), (710, 735, "walk"), (735, 775, "food"),
                                (775, 1050, "home"), (1050, 1060, "walk"), (1060, 1110, "gym"), (1110, 1125, "walk"), (1125, DAY, "home")]

private func plan(_ d: Int) -> [Seg] {
    if d == 0 { return TODAY_SEG }
    var r = Rng(4001 + (d + 60) * 7919)
    let wd = (((3 + d) % 7) + 7) % 7, off = wd >= 5
    var s: [Seg] = []
    let sl = 20 + (r.next() * 70).rounded()
    let wk = 430 + (r.next() * 55).rounded() + (off ? 55 : 0)
    s.append((0, sl, "home")); s.append((sl, wk, "sleep"))
    var t = wk
    if d == -3 || d == -1 || (d != -2 && !off && r.next() < 0.7) {
        let lv = t + 30 + (r.next() * 25).rounded(), back = lv + 30 + 170 + (r.next() * 120).rounded()
        s.append((t, lv, "home")); s.append((lv, lv + 30, "bus")); s.append((lv + 30, back, "place")); s.append((back, back + 30, "bus")); t = back + 30
    }
    if d == -2 || (d != -3 && d != -1 && r.next() < 0.42) {
        let g = d == -2 ? 1080 : max(t + 30, 1040 + (r.next() * 40).rounded())
        s.append((t, g - 10, "home")); s.append((g - 10, g, "walk")); s.append((g, g + 55, "gym")); s.append((g + 55, g + 65, "walk")); t = g + 65
    }
    if off && r.next() < 0.65 {
        let w = max(t + 30, 620 + (r.next() * 260).rounded())
        s.append((t, w, "home")); s.append((w, w + 75, "walk")); t = w + 75
    }
    s.append((t, DAY, "home"))
    return s
}

/// `col` and `lum` are what he was doing at each sample (`RingTone`); empty in the sample world, whose ring is plain `.lit`
struct DayAct { var act: [Double]; var kind: [Int]; var pal = KINDS; var col: [RGB] = []; var lum: [Double] = [] }
private var actCache: [Int: DayAct] = [:]
/// the ring's edges softened: two box passes
func smoothAct(_ o: DayAct) -> DayAct {
    func box(_ x: [Double]) -> [Double] {
        var a = x
        for _ in 0..<2 {
            var b = [Double](repeating: 0, count: a.count)
            for i in a.indices {
                var s = 0.0, n = 0.0
                for j in -2...2 { let q = i + j; if q >= 0 && q < a.count { s += a[q]; n += 1 } }
                b[i] = s / n
            }
            a = b
        }
        return a
    }
    var r = o
    r.act = box(o.act)
    if !o.col.isEmpty {                 // colours blend where what he was doing changes
        let (cr, cg, cb) = (box(o.col.map(\.r)), box(o.col.map(\.g)), box(o.col.map(\.b)))
        r.col = o.col.indices.map { RGB(cr[$0], cg[$0], cb[$0]) }
        r.lum = box(o.lum)
    }
    return r
}
func dayAct(_ d: Int) -> DayAct {
    if Cal.cur.real { return RealDay.shared.act(d) }
    if let o = actCache[d] { return o }
    let segs = plan(d), N = 289
    var raw = [Double](repeating: 0, count: N), kind = [Int](repeating: 0, count: N)
    for i in 0..<N {
        let m = min(Double(i * 5), DAY - 1)
        let sg = segs.first(where: { m >= $0.0 && m < $0.1 }) ?? (0, 0, "home")
        kind[i] = KINDS.firstIndex(of: sg.2) ?? 0
        let fi = Double(i)
        raw[i] = LV[sg.2]! * (sg.2 == "gym" ? 0.74 + 0.26 * abs(sin(fi * 1.9)) : sg.2 == "walk" ? 0.9 + 0.1 * sin(fi * 2.3) : 1)
    }
    let o = smoothAct(DayAct(act: raw, kind: kind))
    actCache[d] = o
    return o
}
func clearActCache() { actCache = [:] }
func actAt(_ t: Double) -> Double {
    let d = floorDay(t), x = (t - Double(d) * DAY) / 5, i = min(287, Int(x.rounded(.down))), o = dayAct(d)
    return o.act[i] + (o.act[i + 1] - o.act[i]) * (x - Double(i))
}
/// the colour and brightness of the thread at minute t
func toneAt(_ t: Double) -> (col: RGB, lum: Double) {
    let d = floorDay(t), x = (t - Double(d) * DAY) / 5, i = min(287, Int(x.rounded(.down))), o = dayAct(d)
    if o.col.isEmpty { return (.lit, 1) }
    let k = x - Double(i), a = o.col[i], b = o.col[i + 1]
    return (RGB(lerp(a.r, b.r, k), lerp(a.g, b.g, k), lerp(a.b, b.b, k)), lerp(o.lum[i], o.lum[i + 1], k))
}
func kindAt(_ t: Double) -> String {
    let d = floorDay(t), o = dayAct(d)
    return o.pal[o.kind[min(288, Int(((t - Double(d) * DAY) / 5).rounded(.down)))]]
}
/// the words for a place on the ring: the prototype's kinds, or what the phone reported ("~" keys), or nothing in a gap
func placeText(_ key: String) -> String {
    key.hasPrefix("~") ? String(key.dropFirst()) : key == "gap" ? "" : PLACE[key] ?? ""
}
/// the ring is dim and dashed where you were asleep or the phone said nothing
func isQuiet(_ key: String) -> Bool { key == "sleep" || key == "gap" }
struct DayStats { var steps: Int; var sleep: Int; var gym: Int }
func dayStats(_ d: Int) -> DayStats {
    if d == 0 { return DayStats(steps: 8420, sleep: 430, gym: 50) }
    var walk = 0.0, gym = 0.0, sleep = 0.0, bus = 0.0
    for (a, b, k) in plan(d) { if k == "walk" { walk += b - a }; if k == "gym" { gym += b - a }; if k == "sleep" { sleep += b - a }; if k == "bus" { bus += b - a } }
    var r = Rng(77 + d * 13)
    let steps = ((3200 + walk * 104 + gym * 36 + bus * 14 + r.next() * 900) / 10).rounded() * 10
    return DayStats(steps: Int(steps), sleep: Int(sleep), gym: Int(gym))
}
func statLine(_ d: Int) -> String {
    if Cal.cur.real { return RealDay.shared.statLine(d) }
    let s = dayStats(d)
    let f = NumberFormatter(); f.numberStyle = .decimal; f.locale = Locale(identifier: "en_US")
    return "\(f.string(from: NSNumber(value: s.steps)) ?? "") 步 · 睡 \(durText(s.sleep))\(s.gym != 0 ? " · 健身 \(durText(s.gym))" : "")"
}

// MARK: - the month

struct MiniEv { var t: Double; var lane: Lane; var st: St; var star = false }
func monthDays(_ d: Int) -> [MiniEv]? {
    if Cal.cur.real || (d >= -3 && d <= 3) { return nil }
    var r = Rng(900 + (d + 50) * 31)
    var out = [MiniEv(t: 380, lane: .her, st: d < 0 ? .done : .next)]
    let n = d < 0 ? 2 + Int((r.next() * 3).rounded(.down)) : Int((r.next() * 2.2).rounded(.down))
    for _ in 0..<n {
        let t = 540 + (r.next() * 720).rounded()
        out.append(MiniEv(t: t, lane: r.next() < 0.55 ? .you : .her, st: d < 0 ? .done : .next))
    }
    if d == 8 { out.append(MiniEv(t: 1140, lane: .you, st: .next, star: true)) }    // Leo's birthday
    if d == 12 { out.append(MiniEv(t: 600, lane: .her, st: .next, star: true)) }    // Lumen Labs closes
    if d == -5 { out.append(MiniEv(t: 840, lane: .you, st: .done, star: true)) }    // a first call, last week
    return out
}

// MARK: - the sky: a still field of stars and the Milky Way

struct SkyStar { var x: Double, y: Double, R: Double, a: Double, b: Double, t: Double; var tw = false, trail = false }
let RS = 418.0   // the star disc's radius
let BAND = 0.58
let BU = (cos(BAND), sin(BAND))
private func gauss(_ r: inout Rng) -> Double { (r.next() + r.next() + r.next() + r.next() - 2) / 0.577 }
let STARS: [SkyStar] = {
    var r = Rng(23), out: [SkyStar] = []
    for i in 0..<960 {
        var x: Double, y: Double
        if i < 280 {
            let s = (r.next() * 2 - 1) * RS, o = 86 + gauss(&r) * 46
            x = BU.0 * s - BU.1 * o; y = BU.1 * s + BU.0 * o
        } else {
            let rr = RS * sqrt(r.next()), a = r.next() * TAU
            x = rr * cos(a); y = rr * sin(a)
        }
        let R = hypot(x, y)
        if R > RS - 2 || R < 44 { continue }
        out.append(SkyStar(x: x, y: y, R: R, a: atan2(y, x), b: pow(r.next(), 3.6), t: r.next()))
    }
    out.sort { $0.b > $1.b }
    for i in out.indices { out[i].tw = i >= 4 && i < 26; out[i].trail = i < 56 }
    return out
}()
func starRGB(_ s: SkyStar) -> RGB { s.t < 0.24 ? RGB(190, 208, 255) : s.t > 0.84 ? RGB(255, 226, 200) : RGB(232, 236, 255) }

// MARK: - daylight, around the scale

let SUN: [(Double, RGB, Double)] = [(0, RGB(52, 62, 150), 0.12), (380, RGB(52, 62, 150), 0.12), (425, RGB(236, 128, 140), 0.38), (446, RGB(255, 182, 138), 0.52),
                                    (520, RGB(255, 236, 206), 0.34), (1010, RGB(255, 236, 206), 0.34), (1100, RGB(255, 172, 118), 0.52), (1123, RGB(234, 120, 128), 0.42),
                                    (1185, RGB(80, 74, 168), 0.16), (1440, RGB(52, 62, 150), 0.12)]
/// colour and alpha of the daylight at minute t
func sunAt(_ t: Double) -> (RGB, Double) {
    let m = ((t.truncatingRemainder(dividingBy: DAY)) + DAY).truncatingRemainder(dividingBy: DAY)
    var i = 0
    while SUN[i + 1].0 <= m { i += 1 }
    let (m0, c0, a0) = SUN[i], (m1, c1, a1) = SUN[i + 1], f = (m - m0) / (m1 - m0)
    return (RGB(c0.r + (c1.r - c0.r) * f, c0.g + (c1.g - c0.g) * f, c0.b + (c1.b - c0.b) * f), a0 + (a1 - a0) * f)
}
let FADE: [Double] = [0, 0.012, 0.026, 0.045, 0.07, 0.1, 0.135, 0.17, 0.2]
