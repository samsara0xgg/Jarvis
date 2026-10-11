import UIKit
import SwiftUI
import UserNotifications

// What touches, time and her talking do to the dial. A port of the prototype's flows, touch handling, per-frame step and detail panel.

protocol HerDrive: AnyObject {
    func herUpdate(now: Double, dt: Double, expr: String, look: (Double, Double)?, hear: Double, pressed: Bool, say: Double?)
    func herLookAt(_ g: (Double, Double), now: Double, ms: Double)
    func herEffect(_ name: String, now: Double)
}

func voiceLevel(_ t: Double, _ k: Double = 1) -> Double {
    k * clamp(0.25 + 0.75 * pow(0.5 + 0.5 * sin(t * 7.3 + 1.3 * sin(t * 2.1)), 2) * (0.6 + 0.4 * sin(t * 1.7)), 0, 1)
}

/// her words: each character brightens as she says it
final class Say {
    let chars: [Character]
    var litAt: [Double]
    var i = 0
    var stopped = false
    private var task: Task<Void, Never>?
    init(_ text: String, cps: Double = 9, delay: Double = 0) {
        chars = Array(text); litAt = Array(repeating: -1, count: chars.count)
        task = Task { @MainActor [weak self] in
            if delay > 0 { try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000)) }
            while let s = self, !s.stopped, s.i < s.chars.count {
                s.litAt[s.i] = nowMs()
                let ch = s.chars[s.i]; s.i += 1
                let pause = "，。！？；：,.!?".contains(ch) ? 260 : ch.isWhitespace ? 40 : 1000 / cps
                try? await Task.sleep(nanoseconds: UInt64(pause * 1_000_000))
            }
        }
    }
    func finished() async { await task?.value }
    func stop() { stopped = true; task?.cancel() }
    var speaking: Bool { i < chars.count && !stopped && !"，。！？；：,.!?".contains(chars[max(0, i - 1)]) }
    func alphas(_ now: Double) -> [Double] {
        (0..<chars.count).map { j in
            if stopped { return 1 }
            if litAt[j] < 0 { return 0.32 }
            let k = clamp((now - litAt[j]) / 350, 0, 1)
            return 0.32 + 0.68 * (k * k * (3 - 2 * k))
        }
    }
}

/// a one-shot gate for "held" (the finger on her is still down)
final class Gate {
    private var cont: CheckedContinuation<Void, Never>?
    private var fired = false
    func fire() { fired = true; cont?.resume(); cont = nil }
    func wait(timeout: Double) async {
        if fired { return }
        await withTaskGroup(of: Void.self) { g in
            g.addTask { @MainActor in await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in if self.fired { c.resume() } else { self.cont = c } } }
            g.addTask { try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000)) }
            await g.next(); g.cancelAll()
            fire()
        }
    }
}

struct Ok {
    let my: Int
    unowned let eng: StarEngine
    func check() throws { if my != eng.gen { throw CancellationError() } }
}

enum GKind { case press, hold, lens, drag, pinch, none }
struct Gest {
    var kind: GKind
    var p0 = CGPoint.zero
    var her = false
    var a: Double?
    var hist: [(Double, Double)] = []
    var caught = false
    var d0 = 0.0, a0 = 0.0, q0 = 0.0, F0 = 0.0
    var timer: DispatchWorkItem?
}

extension StarEngine {
    // MARK: setting the UI only when a value changed

    func put<T: Equatable>(_ kp: ReferenceWritableKeyPath<StarUI, T>, _ v: T) { if ui[keyPath: kp] != v { ui[keyPath: kp] = v } }

    // MARK: flows

    /// A flow, on the main actor like everything else that touches the engine and the UI: a nonisolated async closure runs on the cooperative pool, and a flow
    /// run there wrote `ui` beside SwiftUI and the display link (and trapped on the first haptic, which asserts the main actor).
    func run(_ fn: @escaping @MainActor (Ok) async throws -> Void) {
        gen += 1
        let my = gen
        Task { @MainActor in
            do { try await fn(Ok(my: my, eng: self)) } catch {}
        }
    }
    func nap(_ ms: Double, _ ok: Ok) async throws {
        try await Task.sleep(nanoseconds: UInt64(ms * 1_000_000))
        try ok.check()
    }
    func seek(_ t: Double, _ slow: Bool = false) { goal = t; mode = .seek; stiff = slow ? 2.6 : 6; byHand = false }
    func setLevel(_ l0: Double) {
        wake()
        let l = clamp(l0.rounded(), -1, 2)
        let was = qGoal.rounded(), d = floorDay(F), today = floorDay(nowT())
        qGoal = l
        if l <= 0 && was > 0 { seek(d == today ? nowT() : Double(d) * DAY + 720, true) }
        if l == 1 && was <= 0 { seek(Double(d) * DAY + 720, true) }
        if l != was { sound(l > was ? .open : .close, 1) }
        openId = nil; rowKey = ""; closePanel(quiet: true)
    }

    // MARK: touch: one finger turns the sky, two pinch between day, week and month

    func onHer(_ p: CGPoint) -> Bool {
        let (x, y) = poleXY()
        return hypot(Double(p.x) - x, Double(p.y) - y) < max(30, mon > 0.5 ? 26 : 46 * herP.2)
    }
    func letGo(_ v: Double) {
        // where an iOS-style deceleration would come to rest, nudged onto the nearest thing worth stopping on
        let end = F + v * 0.42
        var T = end
        if qGoal == 1 { T = ((end - 720) / DAY).rounded() * DAY + 720 }
        else {
            let n = nearestEvent(end, (abs(v) < 40 * z ? 12 : 22) * z)
            T = n ?? (end / 5).rounded() * 5      // nothing near: rest on a five-minute detent
        }
        let dist = T - F
        stiff = abs(dist) > 0.3 && (dist > 0) == (v > 0) && v != 0 ? clamp(abs(v / dist), 1.8, 9) : 6.5
        goal = T; mode = .seek; byHand = true
    }
    private func two2() -> (Double, Double) {
        let v = Array(ptr.values), a = v[0], b = v[1]
        return (hypot(Double(b.x - a.x), Double(b.y - a.y)), atan2(Double(b.y - a.y), Double(b.x - a.x)))
    }

    func pointerDown(_ id: Int, _ p: CGPoint, _ ts: Double) {
        wake()
        ptr[id] = p
        if ptr.count == 1 {
            var ng = Gest(kind: .press, p0: p, her: onHer(p), a: nil, hist: [(ts, fs.value)], caught: false)
            if mode == .seek { if abs(fs.velocity) > 40 * z { ng.caught = true }; mode = .idle; fs.velocity = 0 }
            pressed = ng.her
            if ng.her {
                let w = DispatchWorkItem { [weak self] in
                    guard let s = self, s.g?.kind == .press else { return }
                    s.g?.kind = .hold; s.pressed = false
                    if s.talk == nil { if BrainLink.shared.hasToken { s.micTap() } else { s.holdTalk() } }      // paired, she listens for real; the scripted talks are the unpaired demo
                }
                ng.timer = w; DispatchQueue.main.asyncAfter(deadline: .now() + 0.28, execute: w)
            } else if talk == nil && mon < 0.5 && fl < 0.05 {
                let w = DispatchWorkItem { [weak self] in
                    guard let s = self, let g = s.g, g.kind == .press else { return }
                    s.g?.kind = .lens; s.lensGoal = 1; s.lensTh = atan2(Double(g.p0.y) - CY, Double(g.p0.x) - CX); s.prevLensT = nil
                    if s.openId != nil { s.openId = nil; s.rowKey = "" }
                    s.sound(.open, 0.5)
                }
                ng.timer = w; DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: w)
            }
            g = ng
        } else if ptr.count == 2, let cur = g, cur.kind != .hold {
            cur.timer?.cancel(); lensGoal = 0
            let (d0, a0) = two2()
            g = Gest(kind: .pinch, d0: d0, a0: a0, q0: qs.value, F0: fs.value); qHeld = true; pressed = false; mode = .drag; byHand = true
        }
    }

    /// `pts`: the finger's coalesced positions since the last event, oldest first (at least the current one)
    func pointerMove(_ id: Int, _ pts: [(CGPoint, Double)]) {
        guard ptr[id] != nil, var cur = g, let (p, _) = pts.last else { return }
        ptr[id] = p
        if cur.kind == .press && hypot(Double(p.x - cur.p0.x), Double(p.y - cur.p0.y)) > 7 {
            cur.timer?.cancel(); pressed = false
            if (talk != nil && voiceState == .idle) || mon > 0.5 { cur.kind = .none; g = cur; return }       // a scripted talk drives the dial itself; his voice talk leaves it to his finger
            cur.kind = .drag; mode = .drag; byHand = true; fs.velocity = 0; cur.a = nil
            if openId != nil { openId = nil; rowKey = "" }
        }
        if cur.kind == .lens { lensTh = atan2(Double(p.y) - CY, Double(p.x) - CX) }
        else if cur.kind == .drag {
            for (q2, ts) in pts {
                guard let u = unP(Double(q2.x), Double(q2.y)) else { continue }      // the dial under the finger keeps under it, from above or close up
                let da = cur.a == nil ? 0 : wrapAngle(u.0 - cur.a!); cur.a = u.0
                if u.1 < 30 || da == 0 { continue }
                fs.value -= da / k; cur.hist.append((ts, fs.value))
            }
            while cur.hist.count > 16 { cur.hist.removeFirst() }
        } else if cur.kind == .pinch && ptr.count == 2 {
            let (d, a) = two2()
            qs.value = clamp(cur.q0 - log(d / cur.d0) * 1.0, -1.22, 2.22); qs.velocity = 0; qGoal = qs.value
            if qs.value < 1.4 { fs.value = cur.F0 - wrapAngle(a - cur.a0) / (TAU / (DAY * pow(7, clamp(qs.value, 0, 1)))) }
        }
        g = cur
    }

    func pointerUp(_ id: Int, _ ts: Double, cancelled: Bool) {
        wake()
        guard ptr[id] != nil else { return }
        ptr[id] = nil
        guard let cur = g else { return }
        if cur.kind == .pinch {
            if ptr.count < 2 { qHeld = false; setLevel(clamp(qs.value.rounded(), -1, 2)); if mode == .drag { letGo(0) }; g = Gest(kind: .none) }
            if ptr.isEmpty { g = nil }
            return
        }
        if !ptr.isEmpty { return }
        g = nil; cur.timer?.cancel(); pressed = false
        if cur.kind == .lens { lensGoal = 0; sound(.close, 0.45); if cur.caught { letGo(0) }; return }
        if cancelled { release?.fire(); if cur.kind == .drag { letGo(0) }; return }
        if cur.kind == .hold { release?.fire(); return }
        if cur.kind == .drag {
            let h = cur.hist.filter { ts - $0.0 < 100 }, a = h.first, b = h.last
            letGo(a != nil && b != nil && b!.0 - a!.0 > 8 ? (b!.1 - a!.1) / (b!.0 - a!.0) * 1000 : 0)
            return
        }
        if cur.kind == .press && !cur.caught { tap(cur.p0, cur.her) }
        else if cur.caught { letGo(0) }
    }

    func tap(_ p: CGPoint, _ onH: Bool) {
        if talk != nil { return interrupt() }
        if onH {
            if BrainLink.shared.hasToken { return micTap() }
            return run { ok in try await self.talkFlow(ok, self.nextTalk(), nil, false) }
        }
        if mon > 0.5 { if let d = cellAt(Double(p.x), Double(p.y)) { openDay(d) }; return }
        var best: Ev?, bd = 22.0
        for e in EV where e.on && e.v >= 0.2 {
            let dd = hypot(Double(p.x) - e.x, Double(p.y) - e.y)
            if dd < bd { bd = dd; best = e }
        }
        if best == nil, cam != nil, let h = labelHits.first(where: { p.x >= $0.box.0 - 6 && p.x <= $0.box.2 + 6 && p.y >= $0.box.1 - 6 && p.y <= $0.box.3 + 6 }) { best = byId(h.id) }      // a name is a far bigger target than its star
        if let b = best { seek(b.t); openId = b.id; rowKey = ""; sound(.module, 1) }
        else if openId != nil { openId = nil; rowKey = ""; sound(.close, 0.6) }
    }
    func openDay(_ d: Int) {
        let today = floorDay(nowT())
        fs.value = d == today ? nowT() : Double(d) * DAY + 720; fs.velocity = 0; goal = fs.value; mode = .idle; byHand = false
        qGoal = 0; openId = nil; rowKey = ""; sound(.close, 1)
    }
    func nextTalk() -> Talk { defer { ti += 1 }; return talks[ti % talks.count] }

    func goBack() {
        openId = nil; rowKey = ""; sound(.back, 1)
        if qGoal > 0.5 { let t = nowT(); fs.value = Double(floorDay(t)) * DAY + 720; goal = t; qGoal = 0; seek(t, true); return }
        seek(nowT(), abs(F - nowT()) > 180)
    }

    // MARK: rows

    func rel(_ t: Double) -> String {
        let m = Int((t - nowT()).rounded())
        return m < 60 ? "\(m) 分钟后" : Double(m) < DAY - nowT().truncatingRemainder(dividingBy: DAY) ? "\(Int((Double(m) / 60).rounded())) 小时后" : dayWord(floorDay(t))
    }
    func rowModel(_ e: Ev) -> RowModel {
        let kd = kdOf(e)
        let (right, style): (String, RightStyle) = kd == .wait ? ("看看", .look) : kd == .work ? ("进行中", .mut) : kd == .done ? ("已完成", .mut) : kd == .lit ? (e.src ?? "", .src) : (rel(e.t), .mut)
        var ex: Expand?
        if openId == e.id {
            if let c = e.card { ex = .card(c) } else if let ms = e.talkStart { let x = TalkTopics.shared.exchange(e.id); ex = .talk(his: x?.his ?? "", hers: x?.hers ?? "", start: ms) } else if e.draft { ex = .draft(DRAFT) } else if e.call { ex = .call } else if e.approve { ex = .approve } else if e.id == "hike" { ex = .hike } else if let n = e.note { ex = .note(n) }
        }
        return RowModel(id: e.id, kd: kd, title: e.title, meta: e.meta, right: right, rightStyle: style, expand: ex)
    }
    func rowTap(_ id: String) {
        guard let ev = byId(id) else { return }
        openId = openId == ev.id ? nil : ev.id; rowKey = ""
        sound(openId != nil ? .open : .close, 0.7)
        if openId != nil { seek(ev.d != nil && ev.st == .work ? F : ev.t) }
        herDrive?.herLookAt((0, 0.7), now: nowMs(), ms: 900)
    }
    /// A tapped push names its card: it opens under the dial. One the app has not heard of yet is asked for first; one that is not waiting any more opens nothing (the app is just open).
    @MainActor func openWaitingCard(_ id: String) async {
        if !Waiting.shared.cards.contains(where: { $0.id == id }) { await Waiting.shared.poll() }
        guard Waiting.shared.cards.contains(where: { $0.id == id }) else { return }
        // the engine takes the cards in as stars on its next look, a quarter second at most
        for _ in 0..<10 {
            if let e = EV.first(where: { $0.card?.id == id }) {
                closePanel(quiet: true); setLevel(0); openId = e.id; rowKey = ""; panelKey = ""; seek(e.t)
                return
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
    }
    func nextHint(_ T: Double) -> String {
        let now = nowMs()
        guard let n = EV.filter({ $0.t > T && $0.born <= now }).min(by: { $0.t < $1.t }) else { return "这段时间空着" }
        return "这段时间空着 · 下一件 \(dayWord(floorDay(n.t))) \(clock(n.t)) \(n.title)"
    }

    /// the buttons inside an opened card
    func rowAction(_ c: String) {
        guard let id = openId, let ev = byId(id) else { return }
        let now = nowMs()
        func close() { openId = nil; rowKey = "" }
        if ["send", "call", "yes", "allow"].contains(c) {
            ev.flash = now; ev.ign = now
            if c == "send" { ev.st = .done; ev.title = "感谢信发出去了"; ev.meta = "21:16 · 发给 Rachel Lin" }
            if c == "call" { ev.st = .done; ev.title = "给 Mia 回了电话"; ev.meta = "21:15"; ev.t = min(ev.t, nowT()); ev.src = "电话" }
            if c == "yes" { ev.st = .next; ev.title = "爬山"; ev.meta = "周六 09:00 · 我把路线和天气准备好"; ev.lane = .you }
            if c == "allow" {
                ev.st = .work; ev.title = "Mac 上在跑测试"; ev.meta = "20:40 · 你在手机上允许的"; ev.d = max(4, nowT() - ev.t + 2)
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.6) { [weak self] in
                    ev.st = .done; ev.title = "测试都过了"; ev.meta = "21:17 · 128 个，Mac 那边接着做"; ev.flash = nowMs(); ev.ign = nowMs()
                    self?.rowKey = ""; self?.sound(.done, 1)
                }
            }
            close(); sound(.send, 1)
            if c != "allow" { DispatchQueue.main.asyncAfter(deadline: .now() + 0.22) { [weak self] in self?.sound(.done, 1) } }
            run { ok in self.expr = "10"; self.herDrive?.herEffect("burst", now: nowMs()); try await self.nap(1600, ok); self.expr = nil }
        } else if c == "later" {
            ev.t += 10; ev.meta = "21:40 · 晚了 10 分钟"; ev.flash = now; close(); sound(.tock, 1); seek(ev.t)
        } else if c == "no" || c == "deny" {
            ev.st = .done; ev.title = c == "no" ? "不去爬山" : "没让 Mac 跑测试"; ev.meta = c == "no" ? "周六 · 那就在家歇着" : "20:40 · 它会等你回到电脑前"; close(); sound(.close, 1)
        } else if c == "edit" {
            close(); run { ok in try await self.talkFlow(ok, Talk(q: "最后一句改成，我下周都有空", a: "改好了。"), nil, false) }
        }
    }

    // MARK: the detail under the dial

    func updateDetail() {
        let v = q < 0.5 || lensT != nil ? "day" : q < 1.5 ? "week" : "month"
        if v != viewKey { viewKey = v; rowKey = ""; ui.flip += 1; ui.view = v }
        // turning fast, the cards under the dial hold still and dim, and catch up once it slows enough to read; the time above them keeps up
        let fast = abs(omegaS) > 1.2 && lensT == nil && v != "month", hold = fast && rowKey != ""
        if fast != detFast { detFast = fast; ui.detFast = fast }
        let nT = nowT(), d = floorDay(F), today = floorDay(nT)
        if v == "month" {
            put(\.hm, monthName(dateOf(today).0)); put(\.tagText, nil); put(\.dayText, "\(Cal.cur.year) · 第 \(dateOf(today).1) 天")
            if rowKey != "month" {
                rowKey = "month"; ui.showSum = true
                if Cal.cur.real { monthSummary(today) } else {
                    var steps = 0, sleep = 0, gym = 0, n = 0
                    for i in M_FIRST...today { let s = dayStats(i); steps += s.steps; sleep += s.sleep; gym += s.gym != 0 ? 1 : 0; n += 1 }
                    ui.sumMain = "一面过了，明天二面。这个月健身 \(gym) 次，平均每晚睡 \(durText(Int((Double(sleep) / Double(n)).rounded())))。"
                    ui.sumStat = nil
                    ui.chips = [ChipModel(kd: .lit, text: String(format: "%.1f 万步", Double(steps) / 10000)), ChipModel(kd: .wait, text: "3 件等你"), ChipModel(kd: .next, text: "23 天还空着")]
                }
                ui.rows = []; ui.emptyHint = nil; ui.rowsKey += 1
            }
            return
        }
        if v == "week" {
            let (mo, da) = dateOf(d)
            put(\.hm, "\(mo).\(da)"); put(\.tagText, d == today ? "今天" : nil)
            put(\.dayText, "\(dayName(d)) · \(d == today ? "今天" : d < today ? "过去" : "将来")")
            var n: [St: Int] = [.done: 0, .work: 0, .wait: 0, .next: 0]
            let now = nowMs()
            for e in EV where floorDay(e.t) == d && e.born <= now { n[e.st]! += 1 }
            let key = "w\(d)\(n[.done]!),\(n[.work]!),\(n[.wait]!),\(n[.next]!)|\(epoch)"
            if key != rowKey && !hold {
                rowKey = key; ui.showSum = true
                ui.sumMain = Cal.cur.real ? RealDay.shared.summary(d, BrainLink.shared.days[Cal.cur.iso(d)] != nil, today: today) : SUM[d] ?? ""
                let stat = d <= today ? statLine(d) : ""
                ui.sumStat = stat.isEmpty ? nil : stat
                ui.chips = [(Kd.lit, n[.done]!, "完成"), (.work, n[.work]!, "在做"), (.wait, n[.wait]!, "等你"), (.next, n[.next]!, "要来")].filter { $0.1 != 0 }.map { ChipModel(kd: $0.0, text: "\($0.1) \($0.2)") }
                ui.rows = []; ui.emptyHint = nil; ui.rowsKey += 1
            }
            return
        }
        ui.showSum = false
        let T = lensT ?? F, dT = floorDay(T)       // while the lens is held, the panel reads the time under it
        put(\.hm, clock(T))
        put(\.tagText, abs(T - nT) < 3 ? "现在" : nil)
        let m = ((Int(T.rounded()) % 1440) + 1440) % 1440
        var dt = "\(abs(dT) > 1 ? "\(dateOf(dT).0).\(dateOf(dT).1) \(dayName(dT))" : dayWord(dT)) · \(partOfDay(Double(m)))"
        if T <= Trev + 1 && T >= TRACE0 { let p = placeText(kindAt(T)); if !p.isEmpty { dt += " · \(p)" } }
        if Cal.cur.real, let n = BrainLink.shared.missingNote(Cal.cur.iso(dT)) { dt += " · \(n)" }
        put(\.dayText, dt)
        var list = lensT != nil ? focusSet(T, win: 26 * z / lensM) : focusSet()
        if let oid = openId, let o = byId(oid), lensT == nil { list = [o] }
        let key = list.map { "\($0.id)\($0.st.rawValue)\($0.t > Trev ? "a" : "")" }.joined(separator: ",") + "|" + (openId ?? "") + "|\(epoch)"
        if key != rowKey && !hold {
            rowKey = key
            put(\.rowsCalm, abs(omegaS) < 0.08 && mode != .drag && lensT == nil)
            ui.rows = list.map(rowModel); ui.emptyHint = list.isEmpty ? nextHint(T) : nil; ui.rowsKey += 1
        }
    }

    // MARK: talking to her

    @MainActor func talkFlow(_ ok: Ok, _ sc: Talk, _ held: Gate?, _ typed: Bool, pics: [SentItem] = []) async throws {
        beginTalk()
        talk = "listen"
        var his = typed ? addTurn(his: true, sc.q, pics: pics) : nil
        if !typed {
            expr = "35"; sound(.listen, 1); Haptic.impact(.light)
            try await nap(380, ok)
            let qc = Array(sc.q)
            for i in 1...qc.count {
                if let id = his { setTurn(id, text: String(qc[0..<i])) } else { his = addTurn(his: true, String(qc[0..<i])) }
                hear = 0.3 + 0.7 * voiceLevel(nowMs() / 1000 * 1.3)
                if hear > 0.55 && (sonar.isEmpty || nowMs() - sonar.last! > 360) { sonar.append(nowMs()) }
                try await nap("，。？".contains(qc[i - 1]) ? 170 : 88, ok)
            }
            hear = 0
            if let h = held { await h.wait(timeout: 5000); try ok.check() } else { try await nap(420, ok) }
            release = nil
        }
        sound(.heard, 1); expr = "31"; try await nap(520, ok)
        sound(.think, 1); expr = "30"; try await nap(900, ok)
        expr = "39"; talk = "speak"
        if sc.replay { return try await replayFlow(ok, sc) }
        let say = startSay(sc.a, cps: 12)
        if let add = sc.add {
            let e = add.copy(); e.born = 1e12; e.flash = -1e9; e.ign = -1e9
            sound(.send, 1); flyer = (e, nowMs()); seek(e.t, true)
        }
        if let go = sc.go, let e = byId(go) { openId = go; rowKey = ""; seek(e.t, true); sound(.module, 1) }
        await say.finished(); try ok.check(); finishSay()
        endTalk()
    }
    /// The talk is over, but her words stay where they are for a while (`stepTalk` takes them away).
    func endTalk() {
        if talk != nil { sound(.end, 1) }
        finishSay()
        dropStatus()
        MainActor.assumeIsolated { StatusBoard.shared.end() }
        talk = nil; expr = nil; hear = 0; rowKey = ""
        lingerFrom(nowMs())
    }
    /// the day again: the sky turns from last night to now, and each star lights as it reaches the meridian
    @MainActor func replayFlow(_ ok: Ok, _ sc: Talk) async throws {
        expr = "37"; qGoal = 0; openId = nil
        let a = 10.0, b = nowT(), dur2 = 10500.0, t1 = nowMs()
        mode = .script; byHand = false; replay = true; script = a; prevRev = a
        let say = startSay(sc.a, cps: 5.6, delay: 500)
        do {
            while true {
                let k2 = clamp((nowMs() - t1) / dur2, 0, 1)
                // the night passes quickly, the day at a pace you can follow
                script = k2 < 0.14 ? a + (450 - a) * Ease.inOut(k2 / 0.14) : 450 + (b - 450) * Ease.inOut((k2 - 0.14) / 0.86)
                if k2 >= 1 { break }
                try await nap(30, ok)
            }
        } catch { script = nil; replay = false; throw error }
        script = nil; replay = false
        fs.value = b; fs.velocity = 0; mode = .idle; prevRev = b
        await say.finished(); try ok.check(); finishSay()
        endTalk()
    }
    func holdTalk() {
        let sc = nextTalk(), gate = Gate(); release = gate
        run { ok in try await self.talkFlow(ok, sc, gate, false) }
    }
    func interrupt() {
        speaking?.stop()
        if voiceState != .idle { voiceState = .idle; MainActor.assumeIsolated { VoiceSession.shared.cancel() } }
        sound(.interrupt, 1)
        run { _ in
            self.flyer = nil
            if self.mode == .script { self.mode = .idle; self.fs.velocity = 0 }
            self.script = nil; self.replay = false; self.endTalk()
        }
    }

    // MARK: the composer

    func send(_ text0: String, reply: String? = nil, pics: [SentItem] = []) {
        let text = text0.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty { return }
        StallLog.mark("send \(text.count)ch")
        if reply == nil && BrainLink.shared.hasToken { return sendToBrain(text) }       // paired: the brain answers, not the demo
        ui.text = ""       // the keyboard stays, as in a chat
        let t = nowT()
        let keys = ["提醒", "出门", "今天.*做了什么|回放"]
        var demo = reply == nil ? talks.first { $0.q == text } : nil
        if demo == nil, reply == nil, let i = keys.firstIndex(where: { text.range(of: $0, options: .regularExpression) != nil }) { demo = talks[i] }
        let chars = Array("记下了：\(text)")
        let ttl = chars.count > 18 ? String(chars[0..<17]) + "…" : String(chars)
        var sc: Talk
        if let r = reply { sc = Talk(q: text, a: r) }
        else if var dm = demo { dm.q = text; sc = dm }
        else { sc = Talk(q: text, a: "好，我记下了。", add: Ev("note\(EV.count)", t, .her, .done, ttl, "\(clock(t)) · 你打字说的", s: "记下")) }
        if talk != nil { interrupt() }
        run { ok in try await self.talkFlow(ok, sc, nil, true, pics: pics) }
    }
    func micTap() {
        StallLog.mark("micTap voice=\(voiceState) talk=\(talk ?? "-")")
        if !ui.text.trimmingCharacters(in: .whitespaces).isEmpty { return send(ui.text) }
        endEditing?()
        // a talk that is still on (her typed answer, a scripted one) gives way, then the mic starts: a tap is never only a dismiss
        if talk != nil && voiceState == .idle { interrupt(); run { _ in self.micVoice() } } else { micVoice() }
    }

    // MARK: every frame

    func step(now: Double, dt: Double, scale: Double) {
        px = scale
        syncBrain()
        // zoom
        if !qHeld { crit(&qs, qGoal, 7.5, dt) }
        q = qs.value; z = pow(7, clamp(q, 0, 1)); mon = clamp(q - 1, 0, 1); k = TAU / (DAY * z)
        fl = clamp(-q, 0, 1); setCam(fl)
        amp = 8.5 / (1 + 0.25 * (z - 1))   // a week is read by its days, not its minutes
        // the lens
        crit(&ls, lensGoal, 15, dt); lensM = 1 + (2.5 + 2.5 * smooth(1, 7, z)) * clamp(ls.value, 0, 1)
        // time
        if let sc = script { fs.value = sc; fs.velocity = 0 }
        else if mode == .seek {
            crit(&fs, goal, stiff, dt)
            // it comes to rest once what is left of the turn is under a twentieth of a pixel on the bezel
            let pxm = k * R_BEZ * (cam != nil ? 3 : 1)
            if abs(fs.value - goal) * pxm < 0.05 && abs(fs.velocity) * pxm < 1.5 { fs.value = goal; fs.velocity = 0; mode = .idle }
        }
        F = fs.value
        let dF = F - prevF; prevF = F
        sky -= dF * k; omega = dt > 0 ? -dF * k / dt : 0
        omegaS += (omega - omegaS) * (1 - exp(-dt / 0.07))
        hand += ((byHand && mode != .idle ? 1 : 0) - hand) * (1 - exp(-dt / 0.12))
        Trev = replay && script != nil ? script! : nowT()
        // a star lights when its time comes; what crosses the meridian clicks; the hours tock
        if Trev > prevRev {
            for e in EV where e.live && e.st == .next && e.t <= Trev { e.st = e.d == nil ? .done : .work }     // the brain's day, until it is asked again
            for e in EV where e.t > prevRev && e.t <= Trev && e.born <= now && visT(e.t) > 0.2 {
                e.ign = now
                if now - lastTick > 70 { sound(e.lane == .you ? .tick : .tock, 0.8); lastTick = now }
            }
        }
        prevRev = Trev
        lensT = lensGoal != 0 && ls.value > 0.05 ? F + wrapAngle(lensTh - Double.pi / 2) / k : nil
        if let lt = lensT, let pl = prevLensT, lt != pl {
            for e in EV where e.on && e.v > 0.3 && sgn(e.t - lt) != sgn(e.t - pl) { e.flash = now; if now - lastTick > 55 { sound(.tick, 0.6); lastTick = now } }
        }
        prevLensT = lensT
        // under your finger, slow enough to read, what crosses the meridian glints and clicks; flung fast or sent by a button, it passes quietly
        if abs(dF) > 0.01 && !replay && byHand && abs(omegaS) < 1.2 {
            for e in EV where e.on && e.v > 0.3 && sgn(e.t - F) != sgn(e.t - (F - dF)) { e.flash = now; if now - lastTick > 55 { sound(.tick, 0.7); lastTick = now } }
            let unit = z > 2.6 ? DAY : 60
            if floor(F / unit) != floor((F - dF) / unit) && now - lastTock > 80 && now - lastTick > 40 { sound(.tock, 0.5); lastTock = now }
        }
        // her
        let voiceSay = syncVoice(now)
        let spin = abs(omegaS)
        let focusWait = talk == nil && q < 0.5 && focusSet().contains { $0.st == .wait }
        let said: Double? = speaking.map { $0.speaking ? voiceLevel(now / 1000) : 0 } ?? voiceSay
        // under a sheet she is updated on every sixth frame
        herDt += dt; herN += 1
        herFresh = herN >= (covered ? 6 : 1)
        if herFresh {
            herN = 0
            herDrive?.herUpdate(now: now, dt: herDt, expr: expr ?? (q > 0.5 ? "deep" : focusWait ? "ask" : "02"),
                                look: look ?? (spin > 0.2 ? (clamp(-omegaS * 0.5, -0.8, 0.8), 0.4) : nil), hear: hear, pressed: pressed, say: said)
            herDt = 0
        }
        // close up the middle of the ring is out of sight, so she rises into the empty sky at the upper right, a small sun you can still touch
        herP = cam != nil ? (lerp(CX, 372, camE), lerp(CY, 132, camE), lerp(1, 0.42, camE)) : (CX, CY, 1)
        baseOff = cam != nil || lensM > 1.002           // close up or under the lens, the fixed rings are drawn with everything else
        // the panel under the dial
        updateDetail()
        let at = q < 0.5 && abs(F - nowT()) < 8 && mode != .drag, gone = at || talk != nil
        put(\.showBack, !gone)
        put(\.level, Int(clamp(qGoal.rounded(), -1, 2)))
        put(\.camE, camE)
        let dd = floorDay(F)
        if dd != dateD { dateD = dd; let (mo, da) = dateOf(dd); ui.dateText = "\(mo)月\(da)日 \(dayName(dd))"; askDays() }
        stepTalk(now)
        let ms: MicState = !ui.text.trimmingCharacters(in: .whitespaces).isEmpty ? .send : talk != nil ? .on : .mic
        put(\.mic, ms)
        put(\.chipsOn, ui.typing && ui.text.isEmpty && !ui.panelOpen)       // the open panel has her last words there
    }
    private func sgn(_ x: Double) -> Int { x > 0 ? 1 : x < 0 ? -1 : 0 }
}

#if DEBUG
extension StarEngine {
    /// launch with `-scene 一周` to land on a state (the prototype's scene rail), for screenshots and quick looks
    func runScene(_ name: String) {
        switch name {
        case "说话": run { ok in try await self.talkFlow(ok, self.talks[0], nil, false) }
        case "明天": setLevel(0); if let e = byId("iv2") { openId = e.id; rowKey = ""; seek(e.t, true) }
        case "回放今天": run { ok in try await self.talkFlow(ok, self.talks[2], nil, false) }
        case "凌晨": setLevel(0); seek(Double(floorDay(nowT())) * DAY + 200, true)       // paired: the night's sleep crosses midnight
        case "前天": setLevel(0); seek(Double(floorDay(nowT()) - 2) * DAY + 800, true)    // paired: a day the Mac was missing
        case "近": setLevel(-1)
        // `近@昨天17:43` or `近@21:30`: the near view resting at that time
        case _ where name.hasPrefix("近@"):
            var w = String(name.dropFirst(2)), day = floorDay(nowT())
            if w.hasPrefix("昨天") { day -= 1; w = String(w.dropFirst(2)) }
            let hm = w.split(separator: ":").compactMap { Double($0) }
            guard hm.count == 2 else { return }
            let t = Double(day) * DAY + hm[0] * 60 + hm[1]
            setLevel(-1); fs.value = t; fs.velocity = 0; goal = t; mode = .idle
        case "星环": runRingScene(near: nil)
        case _ where name.hasPrefix("星环近@"): runRingScene(near: String(name.dropFirst(4)))
        case "一周": setLevel(1)
        case "一个月": setLevel(2)
        case "确认": setLevel(0); if let e = byId("thanks") { openId = e.id; rowKey = ""; seek(e.t) }
        case "设置": ui.settingsOpen = true
        case "视角": setLevel(-1); ui.tuning = true
        case "抽屉": openPanel()
        // paired, with cards waiting: `看看:claude` opens that kind's card under the dial, `抽屉:claude` with the panel open (kinds: confirmation, clarification, notice, plugin, claude)
        case _ where name.hasPrefix("看看:") || name.hasPrefix("抽屉:"):
            let kind = String(name.dropFirst(3))
            guard let e = EV.first(where: { $0.card?.kind.rawValue == kind }) else { return }
            setLevel(0); openId = e.id; rowKey = ""; panelKey = ""; seek(e.t)
            if name.hasPrefix("抽屉") { openPanel() }
        // the bus ride's push arriving while the app is open, with made-up ids and words: `骑行:board` or `骑行:alight` opens its card under the dial, `骑行抽屉:board` with the panel open
        case _ where name.hasPrefix("骑行"):
            let board = name.hasSuffix("board")
            let json = board ? #"{"ride":{"kind":"board","candidate_id":"Cdebug01","route":"32"}}"# : #"{"ride":{"kind":"alight","ride_id":"Rdebug01"}}"#
            let c = UNMutableNotificationContent()
            c.title = board ? "上 32 路了吗？" : "快到站了"; c.body = board ? "32 路，下一站 光华路" : "下一站 国贸，准备下车"
            c.userInfo = (try? JSONSerialization.jsonObject(with: Data(json.utf8))) as? [AnyHashable: Any] ?? [:]
            guard let push = RidePush(c) else { return }
            MainActor.assumeIsolated { Waiting.shared.showRide(push) }
            let waits = MainActor.assumeIsolated { Waiting.shared.events(merging: []) }
            for e in waits { e.born = -1e9 }
            EV.append(contentsOf: waits)
            setLevel(0)
            guard let e = waits.first else { return }
            openId = e.id; rowKey = ""; panelKey = ""; seek(e.t)
            if name.hasPrefix("骑行抽屉") { openPanel() }
        // a tap on a waiting push with made-up words and ids, through the same handler as the real one: `推送:claude`, `推送:confirmation`, `推送:question` open that card under the dial, `推送:gone` names one that is not waiting
        case _ where name.hasPrefix("推送:"):
            let kind = String(name.dropFirst(3)), gone = kind == "gone"
            let json = #"{"confirmation": {"id": "Cdebug01", "tool": "t", "action": "发这封邮件", "source": "gmail", "args": {"to": ["a@x.com"]}}, "clarification": {"id": "Qdebug01", "question": "哪一天，几位？", "fields": [{"label": "哪一天", "choices": ["周五", "周六"]}]}, "claude_requests": [{"id": "Rdebug01", "tool": "Bash", "input": {"command": "npm test"}, "cwd": "/Users/a/Projects/jarvis", "always": "", "session_id": "s", "title": "星盘", "project": ""}]}"#
            MainActor.assumeIsolated { Waiting.shared.debugSet(WaitingCards.parse(Data(json.utf8)) ?? []) }
            let waits = MainActor.assumeIsolated { Waiting.shared.events(merging: []) }
            for e in waits { e.born = -1e9 }
            EV.append(contentsOf: waits)
            setLevel(0)
            let c = UNMutableNotificationContent()
            c.threadIdentifier = PushShape.waitingThread
            c.userInfo = ["card": ["kind": gone ? "claude" : kind, "id": gone ? "Rgone" : ["confirmation": "Cdebug01", "question": "Qdebug01"][kind] ?? "Rdebug01"]]
            Task { @MainActor in await PushNotifications.tapped(c) }
        case "放大镜": lensTh = -0.6; lensGoal = 1
        case "打字": ui.typing = true; ui.chipsOn = true
        default: runTalkScene(name)
        }
    }
}
#endif
