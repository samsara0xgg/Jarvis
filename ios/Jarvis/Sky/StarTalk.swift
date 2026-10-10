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
        ui.talking = true
        setDrawer(false, quiet: true)
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
        return turnSeq
    }
    func setTurn(_ id: Int?, text: String? = nil, alphas: [Double]? = nil) {
        guard let id, let i = ui.liveTurns.lastIndex(where: { $0.id == id }) else { return }
        if let text, ui.liveTurns[i].text != text { ui.liveTurns[i].text = text }
        if let alphas, ui.liveTurns[i].alphas != alphas { ui.liveTurns[i].alphas = alphas }
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
