import Foundation

// The brain's days as the dial's own shapes: items become `Ev` stars, and the places, moves and sleep become the ring you walked.
// The item kinds and their fields are docs/spec.html `GET /inherent/day` (ADR 0199; jarvis/state/day_line.py).

/// What the dial draws while the phone is paired. Rebuilt whenever the brain served something new (`StarEngine.adoptBrain`).
final class RealDay {
    static let shared = RealDay()

    /// the ring's pieces, in engine minutes: what you were doing, so the trace can rise where you moved
    private struct Seg { let t0: Double, t1: Double, key: String, level: Double, prio: Int }
    private var segs: [Seg] = []
    private var items: [DayItem] = []
    private var actCache: [Int: DayAct] = [:]
    private var today = 0

    /// Fold the days into stars and rebuild the ring. An item that crosses midnight is in two days under one `id`; the newer answer wins.
    func rebuild(_ days: [String: DayReply], now: Double) -> [Ev] {
        let cal = Cal.cur
        var byId: [String: DayItem] = [:]
        for r in days.values.sorted(by: { $0.now_ms < $1.now_ms }) { for i in r.items { byId[i.id] = i } }
        items = byId.values.sorted { ($0.start_ms, $0.id) < ($1.start_ms, $1.id) }
        today = floorDay(now)
        actCache = [:]
        segs = items.compactMap { seg($0, cal, now) }
        return items.compactMap { star($0, cal, now) }
    }

    // MARK: item -> star

    private func star(_ i: DayItem, _ cal: Cal, _ now: Double) -> Ev? {
        let lane: Lane
        switch i.lane { case "you": lane = .you; case "her": lane = .her; default: return nil }
        let t0 = cal.t(ms: i.start_ms), t1 = i.end_ms.map(cal.t(ms:))
        // done / now / next as the brain said it; something still going on reaches to now
        var st: St = i.state == "done" ? .done : i.state == "now" ? .work : .next
        var d: Double? = t1.map { max(0, $0 - t0) }
        if d == nil, t0 <= now { d = max(1, now - t0) }
        if d == 0 { d = nil }
        let when = i.kind == "reminder" ? dayPrefix(t0, now) + clock(t0) : rangeText(t0, t1, now)
        let title: String, meta: String, s: String, src: String?
        var note: String?
        switch i.kind {
        case "stay":
            let place = i.place.flatMap { $0.isEmpty ? nil : $0 }
            title = place.map { "在\($0)" } ?? "停留"; s = place.map { fit($0, 8) } ?? "停留"; src = "定位"
            meta = when + (t1 == nil ? "" : " · " + durText(Int((d ?? 0).rounded())))
        case "move":
            let (name, label) = Self.moves[i.activity ?? ""] ?? ("移动", "移动")
            title = name; s = label; src = "运动"
            meta = when + (t1 == nil ? "" : " · " + durText(Int((d ?? 0).rounded())))
        case "sleep":
            let asleep = i.minutes.map { Int($0.rounded()) }
            title = asleep.map { "睡了 \(durText($0))" } ?? "睡觉"; s = "睡觉"; src = "健康"
            meta = when
        case "work":
            let p = i.projects?.first?.name, a = i.apps?.first?.name
            title = p.map { "专注 · \($0)" } ?? a.map { "用 \($0) 工作" } ?? "在 Mac 上工作"
            s = fit(p ?? a ?? "工作", 8); src = "Mac"
            meta = when + (i.active_s.map { " · 活跃 \(durText(Int(($0 / 60).rounded())))" } ?? "")
            let parts = [("项目", i.projects), ("应用", i.apps)].compactMap { name, tops -> String? in
                guard let tops, !tops.isEmpty else { return nil }
                return name + " " + tops.map { "\($0.name) \(durText(max(1, Int(($0.seconds / 60).rounded()))))" }.joined(separator: "、")
            }
            note = parts.isEmpty ? nil : parts.joined(separator: " · ")
        case "call":
            title = "通话 · \(i.app ?? "电话")"; s = "通话"; src = i.app ?? "电话"
            meta = when + (t1 == nil ? "" : " · " + durText(Int((d ?? 0).rounded())))
        case "talk":
            let n = i.turns ?? 1, topic = TalkTopics.shared.topic(i.id)
            title = topic ?? (n <= 1 ? "你对我说了一句话" : "和你说了 \(n) 句话"); s = topic.map { fit($0, 8) } ?? "对话"; src = nil
            meta = topic == nil ? when : dayPrefix(t0, now) + clock(t0) + " · 和你说了 \(n) 句"
        case "reminder":
            let text = i.text ?? "提醒"
            title = "提醒：\(text)"; s = fit(text, 8); src = nil
            meta = when
            note = i.fired == true ? (i.acknowledged == true ? "已经响过，你确认过了" : "已经响过，你还没确认") : "还没到点"
            // a reminder that rang and was not acknowledged is waiting on you, for a day
            if i.fired == true, i.acknowledged != true, now - t0 < DAY { st = .wait }
            d = nil
        default:
            return nil          // a kind this phone does not know yet: left out, not guessed at
        }
        let e = Ev(i.id, t0, lane, st, title, meta, d: d, s: s, src: src, note: note)
        e.live = true
        if i.kind == "talk" { e.talkStart = i.start_ms }
        return e
    }

    private static let moves: [String: (String, String)] = [
        "walking": ("走路", "走路"), "running": ("跑步", "跑步"), "cycling": ("骑车", "骑车"), "automotive": ("在路上", "路上"),
    ]
    /// A name on the dial: whole up to `cells` cells (a Chinese character is one, any other about half of one); longer, cut at the last word break in the first `cells - 0.6` if that keeps most of it.
    func fit(_ s: String, _ cells: Double) -> String {
        func cell(_ ch: Character) -> Double { (ch.unicodeScalars.first?.value ?? 0) >= 0x2E80 ? 1 : 0.55 }
        if s.reduce(0, { $0 + cell($1) }) <= cells { return s }
        var w = 0.0, kept: [Character] = []
        for ch in s {
            w += cell(ch)
            if w > cells - 0.6 { break }
            kept.append(ch)
        }
        if let i = kept.lastIndex(where: { " ·-_/、，".contains($0) }), Double(i) >= 0.6 * Double(kept.count) { kept = Array(kept[..<i]) }
        return String(kept).trimmingCharacters(in: .whitespaces) + "…"
    }
    /// 昨天 / 明天 / 周x in front of anything that is not today
    private func dayPrefix(_ t: Double, _ now: Double) -> String {
        let d = floorDay(t), td = floorDay(now)
        return d == td ? "" : d == td - 1 ? "昨天 " : d == td + 1 ? "明天 " : dayName(d) + " "
    }
    private func rangeText(_ t0: Double, _ t1: Double?, _ now: Double) -> String {
        let p = dayPrefix(t0, now)
        guard let t1, t1 - t0 >= 1 else { return p + clock(t0) + (t1 == nil && t0 <= now ? " 起" : "") }
        return p + clock(t0) + "–" + clock(t1)
    }

    // MARK: the ring

    private func seg(_ i: DayItem, _ cal: Cal, _ now: Double) -> Seg? {
        guard i.lane == "you" else { return nil }
        let t0 = cal.t(ms: i.start_ms)
        if t0 > now { return nil }
        let t1 = i.end_ms.map(cal.t(ms:)) ?? now
        switch i.kind {
        case "sleep": return Seg(t0: t0, t1: t1, key: "sleep", level: 0, prio: 3)
        case "move":
            let (name, _) = Self.moves[i.activity ?? ""] ?? ("移动", "")
            let level = ["walking": 0.8, "running": 0.95, "cycling": 0.8, "automotive": 0.36][i.activity ?? ""] ?? 0.5
            return Seg(t0: t0, t1: t1, key: "~" + name, level: level, prio: 2)
        case "stay":
            let p = i.place.flatMap { $0.isEmpty ? nil : $0 }
            return Seg(t0: t0, t1: t1, key: p.map { "~在\($0)" } ?? "~停留", level: 0.12, prio: 1)
        default: return nil
        }
    }

    /// Where you were and how much you moved, every five minutes of day `d`. Gaps in what the phone reported stay gaps.
    func act(_ d: Int) -> DayAct {
        if let a = actCache[d] { return a }
        let N = 289
        var raw = [Double](repeating: 0, count: N), kind = [Int](repeating: 0, count: N), pal = ["gap"]
        let lo = Double(d) * DAY, near = segs.filter { $0.t1 > lo - 1 && $0.t0 < lo + DAY }
        for i in 0..<N {
            let t = lo + min(Double(i * 5), DAY - 1)
            var best: Seg?
            for s in near where s.t0 <= t && t < s.t1 && s.prio > (best?.prio ?? 0) { best = s }
            guard let b = best else { continue }
            if let k = pal.firstIndex(of: b.key) { kind[i] = k } else { pal.append(b.key); kind[i] = pal.count - 1 }
            raw[i] = b.level
        }
        var a = smoothAct(DayAct(act: raw, kind: kind))
        a.pal = pal
        actCache[d] = a
        return a
    }

    // MARK: what a day says

    private func onDay(_ d: Int, _ kind: String) -> [DayItem] {
        let cal = Cal.cur
        return items.filter { $0.kind == kind && floorDay(cal.t(ms: $0.start_ms)) == d }
    }
    /// the night that ended on day d
    private func sleepMinutes(_ d: Int) -> Int {
        let cal = Cal.cur
        return Int(items.filter { $0.kind == "sleep" && ($0.end_ms.map { floorDay(cal.t(ms: $0)) } ?? -9999) == d }.reduce(0.0) { $0 + ($1.minutes ?? 0) }.rounded())
    }
    private func spanMinutes(_ list: [DayItem]) -> Int {
        let cal = Cal.cur, now = cal.nowT()
        return Int(list.reduce(0.0) { $0 + max(0, ($1.end_ms.map(cal.t(ms:)) ?? now) - cal.t(ms: $1.start_ms)) }.rounded())
    }

    /// the mono line under the week's summary
    func statLine(_ d: Int) -> String {
        let sleep = sleepMinutes(d), move = spanMinutes(onDay(d, "move")), work = Int((onDay(d, "work").reduce(0.0) { $0 + ($1.active_s ?? 0) } / 60).rounded())
        return [sleep > 0 ? "睡 \(durText(sleep))" : nil, move > 0 ? "在路上 \(durText(move))" : nil, work > 0 ? "Mac \(durText(work))" : nil]
            .compactMap { $0 }.joined(separator: " · ")
    }
    /// a plain account of what the brain has for the day: counts and times it was told, nothing it had to guess
    func summary(_ d: Int, _ loaded: Bool, today td: Int) -> String {
        if !loaded { return "还没读到这一天。" }
        let stays = onDay(d, "stay").count, talks = onDay(d, "talk").reduce(0) { $0 + ($1.turns ?? 1) }
        let calls = onDay(d, "call").count, reminders = onDay(d, "reminder").count
        let parts = [stays > 0 ? "停留 \(stays) 次" : nil, talks > 0 ? "和我说了 \(talks) 句话" : nil, calls > 0 ? "通话 \(calls) 次" : nil,
                     reminders > 0 ? "\(reminders) 个提醒" : nil].compactMap { $0 }
        if parts.isEmpty { return d > td ? "还没有安排。" : "这一天没有记下什么。" }
        return parts.joined(separator: "，") + "。"
    }
}
