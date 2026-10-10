import Foundation

// What the engine does with the brain's days: it changes world when the phone is paired, adopts each answer, and asks for the days on screen.

extension StarEngine {
    /// every frame: a few integer compares unless the brain changed something
    func syncBrain() {
        let b = BrainLink.shared
        if b.hasToken != Cal.cur.real { switchWorld(real: b.hasToken) }
        guard Cal.cur.real else { return }
        // answers arrive a few at a time: take them together, a quarter second apart at most
        if b.dataVersion != seenVersion && nowMs() - lastAdopt > 250 { seenVersion = b.dataVersion; lastAdopt = nowMs(); adoptBrain(b) }
        if b.openTick != seenOpen { seenOpen = b.openTick; askDays() }
        let m = qGoal > 1.5
        if m != monthAsked { monthAsked = m; if m { askDays() } }
    }

    /// The sample week and the phone's own day do not mix: pairing swaps the calendar, the clock and the stars together, and unpairing swaps them back.
    func switchWorld(real: Bool) {
        Cal.cur = Cal(real: real)
        clearActCache(); sprites.minis.removeAll()
        EV = real ? [] : makeEvents()
        t0 = Date()
        let n = NOW1
        fs = Spring(n - 330); goal = n; stiff = 2.6; mode = .seek; byHand = false
        F = n - 330; prevF = F; Trev = n; prevRev = n
        qGoal = 0; openId = nil; rowKey = ""; viewKey = ""; drKey = ""; dateD = nil; seenVersion = -1; monthAsked = false
        epoch += 1; wake()
    }

    /// Replace the stars with the brain's. What was already there keeps its fade, so a refresh does not make the sky blink.
    func adoptBrain(_ b: BrainLink) {
        let now = nowMs(), fresh = RealDay.shared.rebuild(b.days, now: nowT())
        let old = Dictionary(EV.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        for e in fresh {
            if let o = old[e.id] { e.born = o.born; e.flash = o.flash; e.ign = o.ign } else { e.born = now }
        }
        EV = fresh
        sprites.minis.removeAll(); epoch += 1; rowKey = ""; drKey = ""; wake()
    }

    /// The day in focus and the week round it (nearest first), today, and the whole month while the month is open. The brain skips what it served since the app was opened.
    func askDays() {
        guard Cal.cur.real else { return }
        let c = Cal.cur, d = floorDay(F)
        var days = Set((d - 3)...(d + 3))
        days.insert(floorDay(nowT()))
        if qGoal > 1.5 { days.formUnion(M_FIRST...M_LAST) }
        BrainLink.shared.ensure(days.sorted { abs($0 - d) < abs($1 - d) }.map(c.iso))
    }

    func monthSummary(_ today: Int) {
        let mine = EV.filter { $0.live && floorDay($0.t) >= M_FIRST && floorDay($0.t) <= M_LAST }
        let recorded = Set(mine.filter { floorDay($0.t) <= today }.map { floorDay($0.t) }).count
        ui.sumMain = recorded > 0 ? "这个月我记下了 \(recorded) 天。" : "这个月还没有记下什么。"
        ui.sumStat = nil
        let wait = mine.filter { $0.st == .wait }.count, next = mine.filter { $0.st == .next }.count
        ui.chips = [(Kd.wait, wait, "件等你"), (.next, next, "件要来")].filter { $0.1 != 0 }.map { ChipModel(kd: $0.0, text: "\($0.1) \($0.2)") }
    }
}
