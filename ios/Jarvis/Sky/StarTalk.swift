import SwiftUI

// The talk under the dial: the rows of his words and hers that every kind of talk (voice, typed, the prototype's scripted ones) writes into `ui.liveTurns`,
// and how long they stay. Nothing is cut here; the panel (Sky/StarPanel) decides what fits.

extension StarEngine {
    /// how long a talk stays after her last word, and again after a finger lifts off its rows
    static let linger = 8000.0

    /// A talk starts, or the next turn joins the one still on screen. `base` is the conversation's newest row before it, when the caller knows; else it is asked for.
    func beginTalk(base: Int? = nil) {
        lingerUntil = .infinity
        if ui.talking { return }
        talkId += 1
        ui.liveTurns = []; speakId = nil; vHis = nil; vHer = nil
        talkBase = base ?? Conversation.shared.rows.last?.seq ?? 0
        if base == nil && BrainLink.shared.hasToken {
            let id = talkId
            Task { @MainActor in if let s = await Conversation.shared.latestSeq(), id == talkId { talkBase = max(talkBase, s) } }
        }
        autoOpenSpent = false
        ui.talking = true
        wake()
    }

    /// The talk is over; its rows stay for `linger`, and a finger on them keeps them longer.
    func lingerFrom(_ now: Double) { lingerUntil = now + Self.linger }
    func holdLinger(_ on: Bool) {
        lingerHold = on
        if !on { lingerFrom(nowMs()) }
    }

    @discardableResult func addTurn(his: Bool, _ text: String, alphas: [Double] = [], pics: [SentItem] = [], leadIn: Bool = false) -> Int {
        turnSeq += 1
        let t = LiveTurn(id: turnSeq, his: his, text: text, alphas: alphas, pics: pics, leadIn: leadIn)
        withAnimation(.easeOut(duration: 0.25)) { ui.liveTurns.append(t) }
        if !his { openForWritten(text) }
        return turnSeq
    }
    func setTurn(_ id: Int?, text: String? = nil, alphas: [Double]? = nil) {
        guard let id, let i = ui.liveTurns.lastIndex(where: { $0.id == id }) else { return }
        if let text, ui.liveTurns[i].text != text { ui.liveTurns[i].text = text; if !ui.liveTurns[i].his { openForWritten(text) } }
        if let alphas, ui.liveTurns[i].alphas != alphas { ui.liveTurns[i].alphas = alphas }
    }

    /// a document, a list, a table or code needs room: the panel opens by itself, once per talk
    private func openForWritten(_ text: String) {
        if ui.talking && !ui.panelOpen && !autoOpenSpent && AnswerBlocks.isRich(text) { openPanel(auto: true) }
    }

    /// Her words in a row of their own, brightening as they are said. A long answer is said faster, so reading it does not take minutes.
    @discardableResult func startSay(_ text: String, cps: Double? = nil, delay: Double = 0, leadIn: Bool = false) -> Say {
        finishSay()
        let s = Say(text, cps: cps ?? max(12, Double(text.count) / 10), delay: delay)
        speaking = s
        speakId = addTurn(his: false, text, alphas: s.alphas(nowMs()), leadIn: leadIn)
        return s
    }
    func finishSay() {
        speaking?.stop(); speaking = nil
        setTurn(speakId, alphas: []); speakId = nil
    }

    /// every frame: the saying row brightens, and a talk nobody touches goes
    func stepTalk(_ now: Double) {
        if let s = speaking { setTurn(speakId, alphas: s.alphas(now)) }
        if ui.talking && talk == nil && voiceState == .idle && !lingerHold && now > lingerUntil { ui.talking = false; rowKey = "" }
        put(\.topTime, clock(F))
        updateStream()
        if ui.panelOpen { updatePanel() }
    }

    /// The voice session's words as rows: his as he says them (then his line), hers as they come. A new utterance of the same session is new rows.
    func voiceRows(_ v: VoiceShot) {
        let listening = v.state == .listening
        let his = (listening ? v.partial : v.transcript).trimmingCharacters(in: .whitespacesAndNewlines)
        if !his.isEmpty { if let id = vHis { setTurn(id, text: his) } else { vHis = addTurn(his: true, his) } }
        if !listening && !v.reply.isEmpty { if let id = vHer { setTurn(id, text: v.reply) } else { vHer = addTurn(his: false, v.reply) } }
    }

    /// A voice answer can have more written than spoken: once the conversation has the row, her row shows what is written.
    func adoptWritten() {
        let id = talkId
        Task { @MainActor in
            await Conversation.shared.refresh()
            guard id == talkId, let row = Conversation.shared.rows.last(where: { !$0.isHis && !$0.isLeadIn && $0.seq > talkBase }),
                  let i = ui.liveTurns.lastIndex(where: { !$0.his && !$0.leadIn }) else { return }
            let shown = AnswerText.visible(row.text)
            if !shown.isEmpty && ui.liveTurns[i].text != shown { ui.liveTurns[i].text = shown; ui.liveTurns[i].alphas = [] }
        }
    }
}

// MARK: the panel's second height: the whole conversation, over the dial

extension StarEngine {
    static let panelHome = 540.0, panelOpen = 104.0

    func openPanel(auto: Bool = false, quiet: Bool = false) {
        wake()
        if auto { autoOpenSpent = true }
        guard !ui.panelOpen else { return }
        if !quiet { sound(.open, 0.7) }
        ui.panelOpen = true; panelSince = nowMs()
        if BrainLink.shared.hasToken { Task { @MainActor in await Conversation.shared.refresh() } }
        updatePanel(force: true)
    }
    func closePanel(quiet: Bool = false) {
        guard ui.panelOpen else { return }
        wake()
        if ui.talking { autoOpenSpent = true }      // he closed it during this talk: it does not open itself again
        if !quiet { sound(.close, 0.7) }
        ui.panelOpen = false; panelSince = nowMs()
    }
    /// the live rows are taller than the talk's place under the dial: the panel opens by itself, once per talk
    func measureLive(_ h: Double) {
        if ui.talking && !ui.panelOpen && !autoOpenSpent && h > 280 { openPanel(auto: true) }
    }
    /// A talk star's `全部 ›`: the panel, at the first row of the talk (or the nearest after it).
    func showTalk(_ startMs: Int64) {
        openPanel()
        Task { @MainActor in
            if BrainLink.shared.hasToken { await Conversation.shared.refresh() }
            try? await Task.sleep(nanoseconds: 400_000_000)
            let t = Double(startMs) / 1000 - 1
            if let r = ui.convDays.flatMap(\.rows).first(where: { $0.at >= t }) { ui.scrollTo = r.id }
        }
    }
    /// the app came back after a long time away: the star chart as it was first opened, on now
    func comeBack() { setLevel(0); goBack() }

    /// The conversation by day, rebuilt only when it, the talk's start or whether a talk is on show changed; and the live rows go once the conversation has them.
    func updateStream() {
        let seen = (Conversation.shared.version, ui.talking, talkBase)
        if seen == streamSeen { return }
        streamSeen = seen
        let rows = Conversation.shared.rows
        if TalkStream.handedOver(rows, base: talkBase, talking: ui.talking) { ui.liveTurns = [] }
        let days = TalkStream.convDays(TalkStream.shown(rows, base: talkBase, talking: ui.talking), pics: MainActor.assumeIsolated { SentPics.byRow }, cal: Cal.cur.cal, now: Date())
        if days != ui.convDays { ui.convDays = days }
    }

    /// What is pinned over the stream (what waits on him, what she is doing) and the header's line, a few times a second while the panel is open.
    func updatePanel(force: Bool = false) {
        let now = nowMs()
        if !force && now - panelAt < 500 { return }
        panelAt = now
        let nT = nowT(), today = floorDay(nT), ev = EV.filter { $0.born <= now }
        let waits = ev.filter { $0.st == .wait }.sorted { $0.t < $1.t }
        let works = ev.filter { $0.st == .work && floorDay($0.t) == today }.sorted { $0.t < $1.t }
        let key = (waits + works).map { "\($0.id)\($0.st.rawValue)\($0.t)\($0.t > Trev ? "a" : "")" }.joined(separator: ",") + "|" + (openId ?? "") + "|\(epoch)"
        if key != panelKey {
            panelKey = key
            ui.pinned = [("等你的", waits), ("她在做", works)].compactMap { name, list in list.isEmpty ? nil : PinGroup(id: name, name: name, rows: list.map(rowModel)) }
        }
        let next = ev.filter { $0.st == .next && $0.t > nT }.min(by: { $0.t < $1.t })
        put(\.headLine, !waits.isEmpty ? "等你 \(waits.count) 件"
            : next.map { "接下来 \(floorDay($0.t) == today ? "" : dayWord(floorDay($0.t)) + " ")\(clock($0.t)) \($0.title)" } ?? "接下来没有安排")
        put(\.headWaits, !waits.isEmpty)
    }
}

#if DEBUG
extension StarEngine {
    /// `-scene open|typing|rest`: the talk panel's states, for screenshots (listening, speaking and speakingLong are `-voiceScene`s)
    func runTalkScene(_ name: String) {
        switch name {
        case "rest": setLevel(0)
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
        case "typing":
            setLevel(0)
            beginTalk(); lingerHold = true
            addTurn(his: true, "我下周二想约牙医，帮我看看哪个时间合适")
            addTurn(his: false, "周二上午十点和下午三点都空着。你想约哪一个？")
            ui.typing = true; ui.chipsOn = false; ui.kbLift = 336
            openPanel(quiet: true)
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
