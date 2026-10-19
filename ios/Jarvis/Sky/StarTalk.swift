import SwiftUI
import CoreLocation

// The talk under the dial: the rows of his words and hers that every kind of talk (voice, typed, the prototype's scripted ones) writes into `ui.liveTurns`,
// and how long they stay. Nothing is cut here; the panel (Sky/StarPanel) decides what fits.

extension StarEngine {
    /// how long a talk stays after her last word, and again after a finger lifts off its rows
    static let linger = 8000.0
    /// a failed turn stays longer, so there is time to tap 重试
    static let lingerFailed = 30_000.0

    /// A talk starts, or the next turn joins the one still on screen. `base` is the conversation's newest row before it, when the caller knows; else it is asked for.
    func beginTalk(base: Int? = nil) {
        lingerUntil = .infinity
        if ui.talking { return }
        talkId += 1
        ui.liveTurns = []; speakId = nil; vHis = nil; vHer = nil; statusTurn = nil
        talkBase = base ?? Conversation.shared.rows.last?.seq ?? 0
        if base == nil && BrainLink.shared.hasToken {
            let id = talkId
            Task { @MainActor in if let s = await Conversation.shared.latestSeq(), id == talkId { talkBase = max(talkBase, s) } }
        }
        autoOpenSpent = false
        ui.talking = true
        wake()
    }

    /// The chip for the item that is open, when a turn is about to go out and the phone is paired (ADR 0214): the star the dial has open, or the row the timeline has open.
    /// He can take it off; it stays off for that item.
    func captureAbout() {
        guard paired, let a = openAbout(), a.id != ui.about?.id, a.id != aboutDropped else { return }
        setAbout(a)
    }
    private var paired: Bool {
        #if DEBUG
        if ui.tl.fixture != nil { return true }         // the timeline's made-up day stands for a paired phone
        #endif
        return BrainLink.shared.hasToken
    }
    /// what is open, as the host is told of it: in the timeline the row opened in the list, as that row shows it; else the star the dial has open
    private func openAbout() -> About? {
        if HomeModule.shared.isTimeline {
            guard let id = ui.tl.openRow, let r = ui.tl.reply else { return nil }
            return ui.tl.model(r).entries.first { $0.id == id }?.about
        }
        guard let id = openId, let e = byId(id), var a = About.of(e) else { return nil }
        a.mark = kdOf(e)
        return a
    }
    func setAbout(_ a: About?) {
        ui.about = a
        MainActor.assumeIsolated { AboutNow.current = a }
    }
    /// the chip's ×
    func dropAbout() { aboutDropped = ui.about?.id; setAbout(nil); Haptic.selection() }

    /// The talk is over; its rows stay for `linger`, and a finger on them keeps them longer.
    func lingerFrom(_ now: Double) { lingerUntil = now + Self.linger }
    func holdLinger(_ on: Bool) {
        lingerHold = on
        if !on { lingerFrom(nowMs()) }
    }

    @discardableResult func addTurn(his: Bool, _ text: String, reveal: Reveal? = nil, pics: [SentItem] = [], leadIn: Bool = false, growing: Bool = false, steps: StepsInfo? = nil, recordId: String? = nil) -> Int {
        // her first words take the place of the typing line, in the same row, so nothing moves
        if !his, let id = statusTurn {
            statusTurn = nil
            if let i = ui.liveTurns.lastIndex(where: { $0.id == id }) {
                ui.liveTurns[i].text = text; ui.liveTurns[i].reveal = reveal; ui.liveTurns[i].leadIn = leadIn; ui.liveTurns[i].steps = steps; ui.liveTurns[i].status = nil; ui.liveTurns[i].recordId = recordId
                openForWritten(text)
                return id
            }
        }
        turnSeq += 1
        let t = LiveTurn(id: turnSeq, his: his, text: text, reveal: reveal, pics: pics, leadIn: leadIn, growing: growing, steps: steps, recordId: recordId)
        withAnimation(.easeOut(duration: 0.25)) { ui.liveTurns.append(t) }
        if !his { openForWritten(text) }
        return turnSeq
    }
    func setTurn(_ id: Int?, text: String? = nil, reveal: Reveal? = nil, growing: Bool? = nil) {
        guard let id, let i = ui.liveTurns.lastIndex(where: { $0.id == id }) else { return }
        StallLog.count("setTurn")
        if let growing, ui.liveTurns[i].growing != growing { ui.liveTurns[i].growing = growing }
        if let text, ui.liveTurns[i].text != text { ui.liveTurns[i].text = text; if !ui.liveTurns[i].his { openForWritten(text) } }
        if let reveal, ui.liveTurns[i].reveal != reveal { ui.liveTurns[i].reveal = reveal }
    }

    /// a document, a list, a table or code needs room: the panel opens by itself, once per talk
    private func openForWritten(_ text: String) {
        if ui.talking && !ui.panelOpen && !autoOpenSpent && AnswerBlocks.isRich(text) { openPanel(auto: true) }
    }

    /// Her words in a row of their own, laid out whole and lit a word at a time (`Reveal`) at `cps` words a second; without `cps` (a typed answer that came whole, not
    /// streamed: Sky/StarBrain `brainTalk`) all at once.
    @discardableResult func startSay(_ text: String, cps: Double? = nil, delay: Double = 0, leadIn: Bool = false, steps: StepsInfo? = nil, recordId: String? = nil) -> Say {
        finishSay()
        if text.isEmpty { return Say("") }       // nothing to say is no row (a row of no height would still take its gap)
        if !leadIn { Haptic.impact(.soft) }
        let n = Reveal.count(String(AnswerText.linked(text).characters)), every = cps.map { 1 / $0 } ?? 0
        let s = cps.map { _ in Say(text, cps: Double(text.count) / (Double(max(n, 1)) * every), delay: delay) } ?? Say("")       // a typed answer is not spoken: no mouth runs on once it is all there
        speaking = s
        speakId = addTurn(his: false, text, reveal: .timed(n, from: Date.timeIntervalSinceReferenceDate + delay / 1000, every: every), leadIn: leadIn, steps: steps, recordId: recordId)
        return s
    }
    func finishSay() {
        speaking?.stop(); speaking = nil
        speakId = nil
    }

    /// every frame: a talk nobody touches goes
    func stepTalk(_ now: Double) {
        if ui.talking && talk == nil && voiceState == .idle && !lingerHold && unsent.isEmpty && now > lingerUntil { ui.talking = false; rowKey = ""; setAbout(nil); aboutDropped = nil }
        put(\.topTime, clock(F))
        syncStatus()
        for e in MainActor.assumeIsolated({ PhoneRows.take() }) { phoneEvent(e) }
        updateStream()
        if ui.panelOpen { updatePanel() }
    }

    /// What the host sent besides her words. A card goes in the talk as a row, after what she has said, and the waiting cards are read again at once so the panel and the dial have it too.
    func phoneEvent(_ e: PhoneEvent) {
        switch e {
        case .card(let c):
            if ui.talking {
                ui.liveTurns.removeAll { $0.card?.brainId == c.brainId }
                turnSeq += 1
                withAnimation(.easeOut(duration: 0.25)) { ui.liveTurns.append(LiveTurn(id: turnSeq, his: false, text: "", card: c)) }
            }
            Task { @MainActor in await Waiting.shared.poll() }
        case .cardGone(let id):
            withAnimation(.easeOut(duration: 0.25)) { ui.liveTurns.removeAll { $0.card?.brainId == id } }
            Task { @MainActor in await Waiting.shared.poll() }
        case .times(let ts): showTimes(ts)
        case .places(let ps):
            // the answer's row has the map; a late one is in the conversation's row and comes with it
            guard ui.talking, let i = ui.liveTurns.lastIndex(where: { !$0.his && !$0.leadIn && $0.card == nil && $0.status == nil && !$0.failed }) else { return }
            withAnimation(.easeOut(duration: 0.25)) { ui.liveTurns[i].places = ps }
        }
    }

    /// Her answer names times (ADR 0215): the dial turns to each, in the order the turn did them and a moment apart, and the star there lights as it arrives. A touch on the dial ends it.
    func showTimes(_ ts: [AnswerTime]) {
        guard ui.talking else { return }
        let id = talkId
        Task { @MainActor in
            for (i, t) in ts.enumerated() {
                if i > 0 { try? await Task.sleep(nanoseconds: 2_200_000_000) }
                guard id == talkId, g == nil else { return }
                seek(Cal.cur.t(ms: t.atMs), true); wake()
                if t.kind == "reminder" { lightRef = (t.ref, nowMs() + 10_000); BrainLink.shared.opened() }       // a reminder she just set is a star of the day once the days are read again
                try? await Task.sleep(nanoseconds: 900_000_000)
                lightStar(t.ref)
            }
        }
    }
    /// the star under that id lights; a star the days have not brought yet lights when they do (`adoptBrain`)
    func lightStar(_ ref: String) {
        guard let e = EV.first(where: { $0.id == ref }) else { return }
        e.flash = nowMs(); e.ign = nowMs(); lightRef = nil
        sound(.tick, 0.8); wake()
    }

    /// The voice session's words as rows: his as he says them (then his line), hers as they come. A new utterance of the same session is new rows.
    /// The recognizer hands his words over a few at a time, a few times a second; they are let in a character at a time, quicker the further
    /// behind, so his bubble grows in a run rather than in jumps.
    func voiceRows(_ v: VoiceShot, _ now: Double) {
        let listening = v.state == .listening
        var his = (listening ? v.partial : v.transcript).trimmingCharacters(in: .whitespacesAndNewlines)
        if listening {
            let n = Double(his.count), dt = max(0, now - vShownAt) / 1000
            vShownAt = now
            vShown = min(n, vShown + dt * (14 + 8 * max(0, n - vShown)))
            his = String(his.prefix(Int(vShown)))
        }
        if !his.isEmpty { if let id = vHis { setTurn(id, text: his, growing: listening) } else { vHis = addTurn(his: true, his, growing: listening) } }
        // he is done and she has not said a word: the typing line, under his
        if v.state == .thinking, v.reply.isEmpty, vHis != nil, vHer == nil { showStatus() }
        if !listening && !v.reply.isEmpty { sayHers(v.reply, of: v.full.isEmpty ? v.reply : v.full) }
    }

    /// Her row holds the whole answer from her first words and her voice lights it: `reply` is how far she has got (Voice/VoiceSession), the words it has started are said from now.
    private func sayHers(_ reply: String, of full: String) {
        if vHerKey != [reply, full] {           // the count only changes with the texts, and counting reads the marks
            vHerKey = [reply, full]
            vHerSaid = Reveal.said(String(AnswerText.linked(reply).characters).count, in: Reveal.words(String(AnswerText.linked(full).characters)))
        }
        let i = vHer.flatMap { id in ui.liveTurns.lastIndex(where: { $0.id == id }) }
        var r = i.flatMap { ui.liveTurns[$0].reveal } ?? Reveal(voiced: true)
        r.say(vHerSaid, at: Date.timeIntervalSinceReferenceDate)
        if let id = vHer { setTurn(id, text: full, reveal: r) } else { vHer = addTurn(his: false, full, reveal: r); Haptic.impact(.soft) }
    }

    /// Her voice is done with this row. What she did not get to say (she was stopped, or he spoke over her) is not on show: the row ends where she did.
    func closeHers() {
        guard let id = vHer, let i = ui.liveTurns.lastIndex(where: { $0.id == id }), let r = ui.liveTurns[i].reveal,
              r.at.count < Reveal.count(String(AnswerText.linked(ui.liveTurns[i].text).characters)) else { return }
        ui.liveTurns[i].text = vHerKey.first ?? ""; ui.liveTurns[i].reveal = nil
    }

    /// A voice answer can have more written than spoken: once the conversation has the row, her row shows what is written.
    func adoptWritten() {
        let id = talkId
        Task { @MainActor in
            await Conversation.shared.refresh()
            guard id == talkId, let row = Conversation.shared.rows.last(where: { !$0.isHis && !$0.isLeadIn && $0.seq > talkBase }),
                  let i = ui.liveTurns.lastIndex(where: { !$0.his && !$0.leadIn }) else { return }
            let shown = AnswerText.visible(row.text)
            if !shown.isEmpty && ui.liveTurns[i].text != shown { ui.liveTurns[i].text = shown; ui.liveTurns[i].reveal = nil }
            ui.liveTurns[i].steps = row.steps ?? MainActor.assumeIsolated { StatusBoard.shared.info }
            if !row.places.isEmpty { ui.liveTurns[i].places = row.places }
        }
    }
}

// MARK: the panel's second height: the whole conversation, over the dial

extension StarEngine {
    static let panelOpen = 104.0
    /// where the panel's top edge rests: under the dial's time, or at the top of the timeline's list
    static var panelHome: Double { HomeModule.shared.isTimeline ? TLLayout.listTop : 540 }
    /// The stream lists this many rows at a time, however long the conversation: every change to the talk (her caption grows 30 times a second) lays out
    /// and compares what is listed, so the cost has to stay the same with 50 rows held or 5000.
    static let streamPage = 60

    func openPanel(auto: Bool = false, quiet: Bool = false) {
        wake()
        if auto { autoOpenSpent = true }
        guard !ui.panelOpen else { return }
        StallLog.mark("panel open")
        if !quiet { sound(.open, 0.7); Haptic.selection() }
        ui.panelOpen = true; panelSince = nowMs()
        if BrainLink.shared.hasToken { Task { @MainActor in await Conversation.shared.refresh() } }
        updatePanel(force: true)
    }
    func closePanel(quiet: Bool = false) {
        guard ui.panelOpen else { return }
        wake()
        if ui.talking { autoOpenSpent = true }      // he closed it during this talk: it does not open itself again
        if ui.typing { endEditing?() }              // the panel goes with the keyboard, and the keyboard with the panel
        StallLog.mark("panel close")
        if !quiet { sound(.close, 0.7); Haptic.selection() }
        ui.panelOpen = false; panelSince = nowMs()
    }
    /// the live rows are taller than the talk's place under the dial: the panel opens by itself, once per talk
    func measureLive(_ h: Double) {
        if ui.talking && !ui.panelOpen && !autoOpenSpent && h > 836 - Self.panelHome - 16 { openPanel(auto: true) }
    }
    /// A talk star's `全部 ›`: the panel, at the first row of the talk (or the nearest after it).
    func showTalk(_ startMs: Int64) {
        openPanel()
        Task { @MainActor in
            if BrainLink.shared.hasToken { await Conversation.shared.refresh() }
            let t = Double(startMs) / 1000 - 1, cal = Cal.cur.cal
            // the stream lists that row: as many rows back as it is
            let rows = TalkStream.shown(Conversation.shared.rows, base: talkBase, talking: ui.talking)
            if let r = rows.first(where: { (TalkStream.time($0.ts, cal)?.timeIntervalSince1970 ?? 0) >= t }) { streamFrom = min(streamFrom ?? .max, r.seq) }
            try? await Task.sleep(nanoseconds: 400_000_000)
            if let r = ui.convDays.flatMap(\.rows).first(where: { $0.at >= t }) { ui.scrollTo = r.id }
        }
    }
    /// He scrolled to the top of the open panel: a page more of the rows held, or the page before the oldest row held from the brain. `done(true)` when rows came in.
    func loadOlder(_ done: @escaping (Bool) -> Void) {
        guard ui.panelOpen else { return done(false) }
        if pageBefore() { return done(true) }
        Task { @MainActor in done(await Conversation.shared.loadOlder() && pageBefore()) }
    }
    /// the stream starts a page earlier, when the conversation holds rows before its first
    private func pageBefore() -> Bool {
        let from = streamFrom ?? .max, older = TalkStream.shown(Conversation.shared.rows, base: talkBase, talking: ui.talking).filter { $0.seq < from }
        guard let first = older.suffix(Self.streamPage).first else { return false }
        streamFrom = first.seq
        return true
    }
    /// the app came back after a long time away: the star chart as it was first opened, on now
    func comeBack() { setLevel(0); goBack(); if HomeModule.shared.isTimeline { ui.tl.goToday() } }

    /// what the app is in, for a stall line: counts and states, no words
    func stallContext() -> String {
        let rows = MainActor.assumeIsolated { Conversation.shared.rows.count }
        return "rows=\(rows) days=\(ui.convDays.count) live=\(ui.liveTurns.count) panel=\(ui.panelOpen ? 1 : 0) typing=\(ui.typing ? 1 : 0) talking=\(ui.talking ? 1 : 0)"
            + " talk=\(talk ?? "-") voice=\(voiceState) sending=\(sendingToBrain ? 1 : 0) level=\(ui.level) chip=\(ui.chipOpen ? 1 : 0) kb=\(Int(ui.kbLift))"
    }

    /// The conversation by day, rebuilt only when it, the talk's start or whether a talk is on show changed; and the live rows go once the conversation has them.
    func updateStream() {
        if !ui.panelOpen && streamFrom != nil && nowMs() - panelSince > 600 { streamFrom = nil }       // shut, and done moving: the newest page again
        let seen = (Conversation.shared.version, ui.talking, talkBase, streamFrom ?? -1, ui.panelOpen)
        if seen == streamSeen { return }
        streamSeen = seen
        let rows = Conversation.shared.rows, shown = TalkStream.shown(rows, base: talkBase, talking: ui.talking)
        let listed = streamFrom.map { f in shown.filter { $0.seq >= f } } ?? Array(shown.suffix(Self.streamPage))
        if ui.panelOpen && streamFrom == nil { streamFrom = listed.first?.seq ?? 0; streamSeen.3 = streamFrom! }      // open: what is listed stays listed
        put(\.olderMore, Conversation.shared.hasMore || listed.count < shown.count)
        if TalkStream.handedOver(rows, base: talkBase, talking: ui.talking) {
            // the steps he opened on a live row stay open on the conversation's row of the same answer
            if let open = ui.liveTurns.first(where: { ui.stepsOpen.contains("l\($0.id)") }), let c = rows.last(where: { !$0.isHis && $0.seq > talkBase && $0.steps != nil }) {
                ui.stepsOpen.remove("l\(open.id)"); ui.stepsOpen.insert("c\(c.seq)")
            }
            ui.liveTurns = []
        }
        let days = TalkStream.convDays(listed, cal: Cal.cur.cal, now: Date())
        if days != ui.convDays { ui.convDays = days }
    }

    /// What is pinned over the stream (what waits on him, what she is doing), a few times a second while the panel is open.
    func updatePanel(force: Bool = false) {
        let now = nowMs()
        if !force && now - panelAt < 500 { return }
        panelAt = now
        let today = floorDay(nowT()), ev = EV.filter { $0.born <= now }
        let waits = ev.filter { $0.st == .wait }.sorted { $0.t < $1.t }
        let works = ev.filter { $0.st == .work && floorDay($0.t) == today }.sorted { $0.t < $1.t }
        let key = (waits + works).map { "\($0.id)\($0.st.rawValue)\($0.t)\($0.t > Trev ? "a" : "")" }.joined(separator: ",") + "|" + (openId ?? "") + "|\(epoch)"
        if key != panelKey {
            panelKey = key
            ui.pinned = [("等你的", waits), ("她在做", works)].compactMap { name, list in list.isEmpty ? nil : PinGroup(id: name, name: name, rows: list.map(rowModel)) }
        }
    }
}

#if DEBUG
extension StarEngine {
    /// `-scene open|blocks|failed|pictures|typing|typingSending|typingFailed|typingNote|typingWaiting|typingChipOpen|rest` (and the `steps…` ones of Sky/StarStatus.swift): the talk panel's states, for screenshots (listening, speaking and speakingLong are `-voiceScene`s)
    func runTalkScene(_ name: String) {
        switch name {
        case "rest": setLevel(0)
        case "pictures": runPicturesScene()
        case "reactions", "reactionsBar", "reactionsBarHis", "reactionsLive": runReactionsScene(name)
        case "talkAfter", "twoRounds":
            // a talk that starts after a conversation (the last round of it shows dimmer), and, for twoRounds, a second round of the same talk
            setLevel(0)
            debugConversation()
            beginTalk(); lingerHold = true
            addTurn(his: true, "我下周二想约牙医，帮我看看哪个时间合适")
            addTurn(his: false, "周二上午十点和下午三点都空着。你想约哪一个？")
            if name == "twoRounds" {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                    self.addTurn(his: true, "上午吧")
                    self.addTurn(his: false, "好，我帮你把周二上午十点占上。")
                }
            }
        case "blocks":
            // nested items, a quote and a rule in a written answer, with a bare address
            setLevel(0)
            beginTalk(); lingerHold = true
            addTurn(his: true, "订位的事怎么样了")
            addTurn(his: false, "## 订位\n- 周五晚上\n  - 七点，四位\n    - 靠窗\n  - 八点\n- 周六\n\n> 他们说周五比较满，建议早点到。\n\n---\n\n订位页 https://www.laroux.ca/reservations/book?date=2026-10-16&size=4 现在能开。")
            openPanel(quiet: true)
        case "failed":
            // a typed turn the brain failed: his line, and "没答出来" with 重试 in her place
            setLevel(0)
            debugConversation()
            beginTalk(); lingerHold = true
            addTurn(his: true, "帮我订周五晚上的位子")
            showFailed()
        case "typingSending", "typingFailed":
            // his message just sent from the composer, which is empty: on its way (dimmed, her typing line under it), or not sent, with 点重试 under it
            setLevel(0)
            debugConversation()
            beginTalk(); lingerHold = true
            let his = addTurn(his: true, "司机不知道咋的下车了逆天")
            markSend(his, name == "typingSending" ? .sending : .failed)
            if name == "typingSending" { showStatus() } else { trouble("连不上 Jarvis") }
        case "map", "mapFull":
            // an answer that names three places, from the conversation's own row, with a made-up position of his: the map card under it, and (mapFull) the whole map over it
            setLevel(0)
            let cal = Cal.cur.cal, now = Date(), at = TalkStream.dayId(now, cal) + "T" + TalkStream.clock(now, cal) + ":00"
            let places = #"[{"name":"Willows Beach","lat":48.4417,"lng":-123.2967,"address":"Beach Dr, Victoria","place_id":null,"kind":"place"},{"name":"Cadboro Bay Village","lat":48.4569,"lng":-123.3004,"address":"Sinclair Rd, Victoria","place_id":null,"kind":"place"},{"name":"Gyro Park","lat":48.4553,"lng":-123.3079,"address":null,"place_id":null,"kind":"place"}]"#
            let rows = #"{"rows":[{"seq":1,"source":"allen","ts":"\#(at)","text":"附近有什么适合傍晚散步的海边"},{"seq":2,"source":"jarvis","ts":"\#(at)","text":"找到三个，都不远，傍晚的光线最好。","places":\#(places)}]}"#
            MainActor.assumeIsolated {
                MyPlace.debug = CLLocationCoordinate2D(latitude: 48.4521, longitude: -123.3018)
                Conversation.shared.debugSet(ConversationRow.parse(Data(rows.utf8)) ?? [])
            }
            openPanel(quiet: true)
            if name == "mapFull" {
                let list = AnswerPlace.parse((try? JSONSerialization.jsonObject(with: Data(places.utf8))))
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { MainActor.assumeIsolated { PlaceMapViewer.present(list, me: MyPlace.known()) } }
            }
        case "reveal": runRevealScene()
        case "typedStream": runTypedStreamScene()
        case "steps", "stepsDots", "stepsStep", "stepsDone", "stepsOpen": runStepsScene(name)
        case "open":
            setLevel(0)
            EV.removeAll { $0.st == .wait && !["thanks", "mia"].contains($0.id) }      // two things wait on him
            debugConversation()
            openPanel(quiet: true)
        case "talkStar":
            // a talk of yesterday evening, named by what he asked, opened to its first exchange; `全部 ›` shows it in the conversation
            setLevel(0)
            debugConversation()
            let cal = Cal.cur.cal, d = cal.date(byAdding: .day, value: -1, to: Date())!, at = cal.date(bySettingHour: 18, minute: 12, second: 0, of: d)!
            let e = Ev("talk-debug", Cal.cur.t(at), .her, .done, "明天几点出门？", "昨天 18:12 · 和你说了 2 句", s: "明天几点出门？")
            e.live = true; e.talkStart = Int64(at.timeIntervalSince1970 * 1000)
            EV.append(e)
            TalkTopics.shared.debugSet(e.id, "明天几点出门？", "九点二十。坐 32 路，路上三十二分钟，可能下雨，带伞。")
            openId = e.id; rowKey = ""; seek(e.t)
        case "times":
            // her answer names tomorrow 15:00: the dial turns there and the reminder's star lights (the row is the host's own, read as the phone reads it off /phone/ws)
            setLevel(0)
            let cal = Cal.cur.cal, tomorrow = cal.date(byAdding: .day, value: 1, to: Date())!, at = cal.date(bySettingHour: 15, minute: 0, second: 0, of: tomorrow)!
            let e = Ev("R-debug", Cal.cur.t(at), .her, .next, "提醒：给 Mia 回电话", "明天 15:00", s: "回电话")
            e.live = true; e.born = -1e9; e.kind = "reminder"; e.startMs = Int64(at.timeIntervalSince1970 * 1000)
            EV.append(e)
            beginTalk(); lingerHold = true
            addTurn(his: true, "明天三点提醒我给 Mia 回电话")
            addTurn(his: false, "好，明天下午三点，我提醒你给 Mia 回电话。")
            let ms = Int64(at.timeIntervalSince1970 * 1000)
            let row = #"{"type":"row","event_type":"surface.response_emitted","event_uid":"6f1c2d3e4a5b46788899aabbccddeeff","ts_epoch_ms":1791650000000,"payload":{"turn_id":"T1","text":"好","phase":"final","times":[{"kind":"reminder","at_ms":\#(ms),"label":"给 Mia 回电话","ref":"R-debug"}]}}"#
            if case .row(let r)? = HostFrame(Data(row.utf8)) { MainActor.assumeIsolated { PhoneRows.offer(r) } }
        case "clarify":
            // she asks him two things while she talks: the host's own row, read as the phone reads it off /phone/ws
            setLevel(0)
            beginTalk(); lingerHold = true
            addTurn(his: true, "帮我订周五晚上的位子")
            addTurn(his: false, "好，我要先问你两件事。")
            let row = #"{"type":"row","event_type":"clarification.requested","event_uid":"6f1c2d3e4a5b46788899aabbccddeeff","ts_epoch_ms":1791650000000,"payload":{"clarification_id":"Qdebug0001","turn_id":"T1","question":"哪一天，几位？","fields":[{"label":"哪一天","choices":["周五","周六"]},{"label":"几位","choices":["2 位","4 位"]}]}}"#
            if case .row(let r)? = HostFrame(Data(row.utf8)) { MainActor.assumeIsolated { PhoneRows.offer(r) } }
        case "about":
            // a reminder open when he starts to talk: the chip is the talk's first row, and "它" in his words is that reminder
            setLevel(0)
            let cal = Cal.cur.cal, at = cal.date(bySettingHour: 15, minute: 0, second: 0, of: Date())!
            let e = Ev("rem-debug", Cal.cur.t(at), .her, .next, "提醒：给 Mia 回电话", "15:00", s: "回电话")
            e.live = true; e.kind = "reminder"; e.startMs = Int64(at.timeIntervalSince1970 * 1000)
            EV.append(e)
            openId = e.id; rowKey = ""; seek(e.t)
            beginTalk(); lingerHold = true
            if var a = About.of(e) { a.mark = kdOf(e); setAbout(a) }
            addTurn(his: true, "把它推到明天")
            addTurn(his: false, "好，明天下午三点再提醒你给 Mia 回电话。")
        case "history":
            // 24 newest rows, and a made-up page of 40 before them: scroll to the top and it comes in
            setLevel(0)
            let cal = Cal.cur.cal, now = Date()
            func rows(_ range: ClosedRange<Int>) -> [ConversationRow] {
                range.map { n in
                    let c = now.addingTimeInterval(-Double(124 - n) * 50 * 60)
                    return ConversationRow(seq: n, source: n % 2 == 0 ? "allen" : "jarvis", ts: TalkStream.dayId(c, cal) + "T" + TalkStream.clock(c, cal) + ":00",
                                           text: n % 2 == 0 ? "第 \(n) 行，他说的话" : "<voice>第 \(n) 行，她的回答。</voice>")
                }
            }
            MainActor.assumeIsolated {
                Conversation.shared.debugSet(rows(101...124), hasMore: true)
                Conversation.shared.fakeOlder = { first in ConversationPage(rows: rows(max(1, first - 40)...(first - 1)), hasMore: first - 40 > 1) }
            }
            openPanel(quiet: true)
        case "long":
            // a long history (`-historyN`, 900 by default) with a written answer every tenth row: the panel, the keyboard and the voice over it
            setLevel(0)
            let n0 = UserDefaults.standard.integer(forKey: "historyN"), n = n0 > 0 ? max(10, n0) : 900, cal = Cal.cur.cal, now = Date()
            let doc = "<voice>好的。</voice><document>## 清单\n- 第一件事，写得长一些好让这一行换行\n- 第二件事\n\n| 列 | 值 |\n|---|---|\n| a | 1 |\n| b | 2 |</document>"
            let rows = (1...n).map { i in
                let c = now.addingTimeInterval(-Double(n - i) * 9 * 60)
                return ConversationRow(seq: i, source: i % 2 == 1 ? "allen" : "jarvis", ts: TalkStream.dayId(c, cal) + "T" + TalkStream.clock(c, cal) + ":00",
                                       text: i % 2 == 1 ? "第 \(i) 行，他说的话，有时候长一点，比如问一下明天几点出门" : i % 10 == 0 ? doc : "<voice>第 \(i) 行，她的回答。九点二十出门，坐 32 路，路上三十二分钟。</voice>")
            }
            MainActor.assumeIsolated { Conversation.shared.debugSet(rows) }
            // `-stress keys`: over that history, again and again: the keyboard up, a line typed a character at a time, sent (her canned answer brightens as she says it), the keyboard down
            if UserDefaults.standard.string(forKey: "stress") == "keys" {
                let line = Array("今天晚上几点出门比较好，顺便看看天气")
                func round(_ n: Int) {
                    guard n < 6 else { return }
                    let at = { (s: Double, f: @escaping () -> Void) in DispatchQueue.main.asyncAfter(deadline: .now() + s, execute: f) }
                    at(0) { StallLog.mark("stress round \(n)"); self.beginEditing?() }
                    for i in 1...line.count { at(1 + Double(i) * 0.12) { self.ui.text = String(line[..<i]) } }
                    at(4) { self.send(self.ui.text) }
                    at(9) { self.endEditing?() }
                    at(11) { round(n + 1) }
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { round(0) }
            }
        case "typing", "typingWaiting", "typingChipOpen", "typingGrow", "typingScrolled", "typingDismiss", "typingNote":
            // the keyboard is the real one: the field is focused, and the panel comes up with it
            setLevel(0)
            if name == "typing" { EV.removeAll { $0.st == .wait || $0.st == .work } }
            else { EV.removeAll { $0.st == .wait && !["thanks", "mia"].contains($0.id) } }
            panelKey = ""
            debugConversation()
            beginTalk(); lingerHold = true
            addTurn(his: true, "我下周二想约牙医，帮我看看哪个时间合适")
            let hers = addTurn(his: false, "周二上午十点和下午三点都空着。你想约哪一个？上午的话十点到十一点半我没有别的安排，下午三点之后有一个电话会。")
            beginEditing?()
            let later = { (s: Double, f: @escaping () -> Void) in DispatchQueue.main.asyncAfter(deadline: .now() + s) { f() } }
            if name == "typingGrow" { later(3) { self.setTurn(hers, text: "周二上午十点和下午三点都空着。你想约哪一个？上午的话十点到十一点半我没有别的安排，下午三点之后有一个电话会。\n\n另外周三全天都有空，周四上午有一个面试，我建议避开。如果你想约得近一些，我可以先帮你把周二下午三点占住，等你确认以后再正式发给诊所，这样不会耽误你别的事。") } }
            if name == "typingScrolled" { later(3) { self.ui.scrollTo = "c1" }; later(5) { self.addTurn(his: false, "我在这里，你慢慢看。") } }
            if name == "typingNote" { later(2) { self.trouble("文件过期了，再发一次") } }       // the status line over the composer, with the keyboard up
            if name == "typingDismiss" { later(4) { self.endEditing?() } }
            if name == "typingChipOpen" { later(3) { self.ui.chipOpen = true } }       // as a tap on the chip does
        default: break
        }
    }

    /// made-up conversation rows over two days, the newest with a written answer, for the scenes
    private func debugConversation() {
        let cal = Cal.cur.cal, now = Date(), day = { (back: Int, h: Int, m: Int) -> String in
            let d = cal.date(byAdding: .day, value: -back, to: now)!
            let c = cal.date(bySettingHour: h, minute: m, second: 0, of: d)!
            return TalkStream.dayId(c, cal) + "T" + TalkStream.clock(c, cal) + ":00"
        }
        let doc = "<voice>好，我把这周的安排列在下面了。</voice><document>## 本周安排\n- 周五 10:00 周会\n- 周六 09:00 爬山，我会提前看天气\n- 周日 21:00 整理这一周的记忆\n\n先做这三件：\n1. 把首页的最后两张图做完\n2. 再投两份简历\n3. 给 Mia 回电话\n\n| 日期 | 事项 | 时间 | 地点 | 备注 |\n|---|---|---|---|---|\n| 周五 | 周会 | 10:00 | 线上 | 提前十分钟进会议室 |\n| 周六 | 爬山 | 09:00 | 西山步道 | 带伞和水壶，前一天看天气 |\n| 周日 | 整理记忆 | 21:00 | 在家 | 我先整理，你再补充 |\n\n```swift\nlet next = plan.first(where: { !$0.done })\n```</document>"
        let rows: [(Bool, String, String)] = [
            (true, day(1, 18, 12), "明天几点出门？"),
            (false, day(1, 18, 12), "<voice>九点二十。坐 32 路，路上三十二分钟，可能下雨，带伞。</voice>"),
            (true, day(1, 18, 14), "那后天呢"),
            (false, day(1, 18, 14), "后天上午十点你约了牙医，九点钟出门。"),
            (true, day(0, 9, 5), "帮我整理一下这周的安排"),
            (false, day(0, 9, 6), "<voice>我先看一下日历。</voice>"),
            (false, day(0, 9, 6), doc),
        ]
        MainActor.assumeIsolated {
            Conversation.shared.debugSet(rows.enumerated().map { ConversationRow(seq: $0.offset + 1, source: $0.element.0 ? "allen" : "jarvis", ts: $0.element.1, text: $0.element.2) })
        }
    }
}
#endif
