// words: legacy
import UIKit

// What he types and attaches, sent to the brain while the phone is paired (Brain/BrainTalk), and her answer as it lands in the conversation.
// The prototype's canned replies stay for the unpaired phone.

/// The rows a typed turn's words stream into, before the conversation has them (a lead-in line, then the answer).
struct TypedRows { var lead: Int?, answer: Int? }

/// A typed message as it went out: a failed one is sent again with the same words and the files already up. `utterance` is the message's id, made once: every
/// send of it (the line, the HTTP route, a second try) carries it, and the brain writes one turn for it. `taken`: the brain has the message; an answer that failed
/// after that is asked again as a new message, not as this one.
struct SentTurn {
    let words: String, shown: String, ids: [String], pics: [SentItem], about: About?
    let his: Int                // the live row of his line; when a taken one is sent again it and everything after it go
    let utterance: String
    var taken = true
}

/// How the words of a message ended: the brain `taken` them; `refused` them for a reason that sending again does not change; or they are `lost`, and the brain may or may not have them
/// (a send that got no answer): the message stays on screen, not sent, and goes again under its id.
enum Delivery: Equatable {
    case taken(record: String?)
    case refused(String)
    case lost(String)
}

extension StarEngine {
    /// A line for the status line over the composer (the same line the voice uses); it goes by itself after `ms`, or when `clearNote` is called.
    func note(_ text: String, for ms: Double = 8000) { brainNote = text; brainNoteUntil = nowMs() + ms }
    /// a note that says something went wrong
    func trouble(_ text: String) { note(text); Haptic.notify(.warning) }
    func clearNote() { brainNote = nil; brainNoteUntil = 0 }

    /// His words and everything attached go to the brain: the field empties at the tap, his line shows at once (dimmed while it is on its way), and under it hers, the typing dots
    /// (the board's steps then say what she is doing). Each file goes up first, then the words with the message's id over the live line (PhoneLink); a brain that cannot take them
    /// there gets POST /inherent/submit with the same id. The id is made once per message, so however many times the network makes the send happen, the brain has one turn of it.
    /// A refusal takes his line back, says why on the status line and leaves the words and the files where they were, so he can take the refused one off and send again. A send that
    /// got no answer is not taken back (the brain may have it): the line stays with 没发出去, 点重试, and goes again under the same id. Once the brain has taken the turn, her answer is waited for.
    /// `again`: a message sent as it was (its files are up already; the composer is not read): one not sent goes under its first id, a taken one whose answer failed is a new message.
    func sendToBrain(_ words: String, again: SentTurn? = nil) {
        let text = words.trimmingCharacters(in: .whitespacesAndNewlines), items = again == nil ? ui.attach.items : []
        if (text.isEmpty && items.isEmpty && again == nil) || sendingToBrain { return }
        if let m = AttachUpload.check(count: items.count) { return trouble(m) }
        sendingToBrain = true
        if again == nil { captureAbout() }
        let about = again.map(\.about) ?? ui.about
        note("正在发给她…", for: 120_000)
        let shown = again?.shown ?? text, pics = again?.pics ?? SentPics.items(items)
        let utterance = again.flatMap { $0.taken ? nil : $0.utterance } ?? UUID().uuidString
        if again == nil, ui.text.trimmingCharacters(in: .whitespacesAndNewlines) == text { ui.text = "" }       // his words are the message now: the field is empty at the tap
        if talk != nil { interrupt() }
        run { ok in
            // after the interrupt's own end of the talk (queued before this): his line
            // a turn that failed leaves its rows (all of them when it is sent again) before the new ones; a message not sent keeps its row, and the ones after it
            let reuse = again.flatMap { $0.taken ? nil : $0.his }, from = again?.his ?? .max
            if let reuse { self.unsent[reuse] = nil }
            self.ui.liveTurns.removeAll { $0.failed || (reuse == nil && $0.id >= from) }
            // The talk's base is the newest row before his line. `beginTalk` would ask for it by itself, and that answer can come after the brain has stored his
            // line (a slow link): his line would then be listed twice. The one asked below, before the words go, is used instead.
            let fresh = !self.ui.talking, mine = self.talkId + 1
            self.beginTalk(base: Conversation.shared.rows.last?.seq ?? 0)
            self.talk = "listen"
            let his = reuse.flatMap { r in self.ui.liveTurns.contains { $0.id == r } ? r : nil } ?? self.addTurn(his: true, shown, pics: pics)
            self.markSend(his, .sending)
            var record: String?                 // the conversation record of his line, which the host's `said` names (ADR 0256)
            StatusBoard.shared.begin(utterance: utterance)
            self.showStatus()
            @MainActor func settle() {
                self.sendingToBrain = false
                self.dropStatus()
                StatusBoard.shared.end()
                if self.talk == "listen" { self.talk = nil; self.lingerFrom(nowMs()) }
            }
            @MainActor func fail(_ m: String) {
                self.ui.liveTurns.removeAll { $0.id == his }
                self.unsent[his] = nil
                settle()
                if self.ui.text.isEmpty { self.ui.text = words }
                self.trouble(m)
            }
            // her answer is the first row of hers past the newest one now: asked while the files go up
            async let latest = Conversation.shared.latestSeq()
            let seq: Int?
            var ids = again?.ids ?? []
            do {
                for (k, part) in try await AttachUpload.parts(items, photos: self.ui.attach.photos).enumerated() {
                    let up = try await BrainLink.shared.call { try await BrainClient.upload(part, bases: $0, token: $1) }
                    guard let id = up.id else { return fail(AttachUpload.refusal(status: up.status, name: part.name)) }
                    ids.append(id)
                    await self.keepPicture(items[k], id: id, row: his, at: k)
                }
                StallLog.mark("send: files up")
                seq = await latest
                if fresh, let seq, self.talkId == mine { self.talkBase = max(self.talkBase, seq) }
                StallLog.mark("send: latest seq")
            } catch let e as AttachUpload.Refused {
                return fail(e.message)
            } catch let e as BrainError {
                return fail(e.message)
            } catch {
                return fail(BrainError.refused.message)
            }
            let sent = SentTurn(words: text, shown: shown, ids: ids, pics: self.ui.liveTurns.first { $0.id == his }?.pics ?? pics, about: about, his: his, utterance: utterance, taken: false)
            self.unsent[his] = sent
            switch await self.deliver(utterance, text, ids, about) {
            case .taken(let id):
                record = id
                if let id, let i = self.ui.liveTurns.firstIndex(where: { $0.id == his }) { self.ui.liveTurns[i].recordId = id }
            case .refused(let m):
                if self.ui.following != nil { self.follow(nil) }          // a topic the brain does not know refuses the say (422 / bad_say): its bar goes
                return fail(m)
            case .lost(let m):
                self.markSend(his, .failed)
                settle()
                return self.trouble(m)
            }
            StallLog.mark("send: taken")
            self.unsent[his] = nil
            self.markSend(his, .sent)
            var done = sent
            done.taken = true
            self.sentTurn = done
            for it in self.ui.attach.take() { AttachTemp.discard(it.url) }
            self.sendingToBrain = false
            guard let seq else { self.talk = nil; self.lingerFrom(nowMs()); return self.note("发给她了", for: 4000) }       // no conversation to read her answer from
            self.clearNote()
            try await self.brainTalk(ok, after: seq, utterance: utterance, record: record)
        }
    }

    /// The words out: over the live line, or when it cannot take them (no socket, an older brain, no answer after the second try) POST /inherent/submit. Both carry the
    /// message's id, so a send that got through unseen and is made again is the same turn.
    @MainActor static func deliverToBrain(utterance: String, text: String, ids: [String], about: About?) async -> Delivery {
        switch await PhoneLink.shared.sendTyped(utterance: utterance, text: text, attachments: ids, about: about?.json) {
        case .taken(_, let id): return .taken(record: id)
        case .refused(let m): return .refused(m)
        case .useSubmit:
            do {
                let status = try await BrainLink.shared.call { try await BrainClient.submit(text, attachments: ids, about: about, utterance: utterance, bases: $0, token: $1) }
                // the host takes the item whole or refuses the turn: say so, and leave the chip for him to take off
                guard status == 200 else {
                    return .refused(status == 404 ? "文件过期了，再发一次" : status == 422 && about != nil ? "她没接住上面的那一项，去掉它再发" : AttachUpload.refusal(status: status, name: ""))
                }
                return .taken(record: nil)
            } catch let e as BrainError {
                return .lost(e.message)
            } catch {
                return .lost(BrainError.refused.message)
            }
        }
    }

    /// his line's state: on its way, not sent, or with the brain
    func markSend(_ row: Int, _ state: SendState) {
        if let i = ui.liveTurns.firstIndex(where: { $0.id == row }), ui.liveTurns[i].send != state { ui.liveTurns[i].send = state }
    }

    /// A picture of his is up: this phone keeps the original it picked or took under the brain's id (off the main thread, before the temp copy goes), and his line in the talk
    /// learns the id, so the picture there opens full screen at once. A library photo's original may come from iCloud: it is kept when it arrives.
    @MainActor private func keepPicture(_ it: AttachItem, id: String, row: Int, at k: Int) async {
        if let i = ui.liveTurns.firstIndex(where: { $0.id == row }), ui.liveTurns[i].pics.indices.contains(k) { ui.liveTurns[i].pics[k].attachment = id }
        guard it.kind == .photo else { return }
        if let small = it.thumbnail { PictureStore.shared.seed(id: id, image: small) }
        if let url = it.url { await PictureStore.shared.keep(id: id, copying: url) }
        else if let asset = it.assetID {
            let photos = ui.attach.photos
            Task { if let data = await photos.originalData(id: asset) { await PictureStore.shared.keep(id: id, data: data) } }
        }
    }

    /// Her side of a typed turn the brain took: her thinking look, then her words as the line brings them, each piece the moment it comes, and the conversation's
    /// record of them once it lands (which the rows then hold). A lead-in ("我上网查查") is shown and the answer waited for.
    /// `record` is the conversation record of his line: a reaction of hers on it, other than the program's eyes, is her whole answer (ADR 0256).
    @MainActor private func brainTalk(_ ok: Ok, after seq: Int, utterance: String, record: String?) async throws {
        sound(.heard, 1); expr = "31"; try await nap(520, ok)
        sound(.think, 1); expr = "30"
        var last = seq, deadline = nowMs() + 90_000, said = false, failed = false, reacted = false, read = 0.0
        var live = TypedRows()
        while nowMs() < deadline {
            try await nap(50, ok)
            if let w = StatusBoard.shared.words(utterance: utterance), streamTyped(w, into: &live) { expr = "39"; talk = "speak"; said = true }
            // the conversation is read every half second, and at once when the host has ended the turn
            guard nowMs() - read >= 500 || StatusBoard.shared.over(utterance: utterance) else { continue }
            read = nowMs()
            let rows = await Conversation.shared.rows(after: last)
            try ok.check()
            let hers = rows.filter { !$0.isHis }
            guard let row = hers.last else {
                if StatusBoard.shared.failed(utterance: utterance) { failed = true; break }         // the host failed the turn: no answer is coming
                if Reactions.shared.answered(record) { reacted = true; break }                      // she reacted and wrote nothing: no text row is coming either
                continue
            }
            last = rows.last?.seq ?? last
            for r in hers.dropLast() where !AnswerText.visible(r.text).isEmpty { placeTyped(r, live: &live, last: false) }
            if placeTyped(row, live: &live, last: true) { expr = "39"; talk = "speak"; said = true }          // an empty reply is not an answer: the fallback line still stands
            if row.isLeadIn { showStatus(); deadline = nowMs() + 25_000; continue }          // more follows (the steps she takes meanwhile show in a line of their own): it is waited for while this is read
            break
        }
        switch WaitEnd.of(failed: failed, said: said, reacted: reacted) {
        case .failed: showFailed()
        case .fallback: addTurn(his: false, "她还在想，回答会在今天的对话里。"); talk = "speak"; expr = nil
        case .quiet: break                      // her answer is the reaction on his line; `endTalk` takes the typing line away
        case .spoke: await speaking?.finished(); try ok.check(); finishSay()
        }
        endTalk()
        if failed { lingerUntil = nowMs() + Self.lingerFailed; tlUntil = lingerUntil }
        await Conversation.shared.refresh()
    }

    /// Her words of a typed turn as far as the line has brought them: the lead-in line and the answer each in a row of their own, the new words fading in as they come.
    /// True when there is a word on show.
    @MainActor private func streamTyped(_ w: (lead: String, answer: String), into live: inout TypedRows) -> Bool {
        for (lead, raw) in [(true, w.lead), (false, w.answer)] {
            let text = AnswerText.visible(raw)
            guard !text.isEmpty else { continue }
            let n = Reveal.count(String(AnswerText.linked(text).characters)), t = Date.timeIntervalSinceReferenceDate
            if let id = lead ? live.lead : live.answer {
                guard let i = ui.liveTurns.lastIndex(where: { $0.id == id }), ui.liveTurns[i].text != text else { continue }
                var r = ui.liveTurns[i].reveal ?? Reveal()
                r.say(n, at: t)
                setTurn(id, text: text, reveal: r)
            } else {
                finishSay()
                var r = Reveal()
                r.say(n, at: t)
                let id = addTurn(his: false, text, reveal: r, leadIn: lead)
                if lead { live.lead = id } else { live.answer = id; Haptic.impact(.soft) }
            }
        }
        return live.answer != nil || live.lead != nil
    }

    /// A record of hers from the conversation. The row her words streamed into takes it (words the line had not brought fade in, the rest stays as it is); a record
    /// that streamed nowhere shows whole, the `last` one in the talk's say. True when it has words.
    @MainActor @discardableResult private func placeTyped(_ r: ConversationRow, live: inout TypedRows, last: Bool) -> Bool {
        let words = AnswerText.visible(r.text), recordId = r.id.isEmpty ? nil : r.id
        // the steps under the answer: the brain's own summary of the turn, or what the line saw of them until the conversation has it
        let steps = r.isLeadIn ? nil : r.steps ?? StatusBoard.shared.info
        if let id = r.isLeadIn ? live.lead : live.answer, let i = ui.liveTurns.lastIndex(where: { $0.id == id }) {
            var rv = ui.liveTurns[i].reveal ?? Reveal()
            rv.say(Reveal.count(String(AnswerText.linked(words).characters)), at: Date.timeIntervalSinceReferenceDate)
            setTurn(id, text: words, reveal: rv)
            ui.liveTurns[i].leadIn = r.isLeadIn; ui.liveTurns[i].steps = steps; ui.liveTurns[i].recordId = recordId
        } else if last {
            startSay(words, leadIn: r.isLeadIn, steps: steps, recordId: recordId)
        } else {
            addTurn(his: false, words, leadIn: r.isLeadIn, steps: r.isLeadIn ? nil : r.steps, recordId: recordId)
        }
        return !words.isEmpty
    }

    #if DEBUG
    /// `-scene typedStream`: a typed turn whose answer streams in as the host's own rows: a line before a tool call, the step, then the answer a sentence at a time,
    /// and the conversation's record taking the row at the end (it must not move or light again).
    func runTypedStreamScene() {
        setLevel(0)
        beginTalk(); lingerHold = true
        addTurn(his: true, "帮我看看明天天气，再说说我这周剩下还有什么安排")
        let board = MainActor.assumeIsolated { StatusBoard.shared }
        MainActor.assumeIsolated { board.begin(utterance: "scene"); board.bind(utterance: "scene", turn: "T1") }
        showStatus()
        var n = 0
        func frame(_ json: String) { MainActor.assumeIsolated { if case .row(let r)? = HostFrame(Data(json.utf8)) { board.feed(.row(r)) } else if case .step(let f)? = HostFrame(Data(json.utf8)) { board.feed(.step(f)) } } }
        func chunk(_ text: String) {
            n += 1
            let body = String(data: try! JSONSerialization.data(withJSONObject: ["type": "row", "event_type": "surface.response_chunk", "event_uid": String(format: "%032x", n), "ts_epoch_ms": 1791650015000,
                                                                             "payload": ["turn_id": "T1", "text": text, "phase": "final"]]), encoding: .utf8)!
            frame(body)
        }
        let parts = ["我来查明天的天气和这周剩下的日程。", "\n\n明天多云间晴，最低八度，最高十八度，", "带件薄外套就行。\n\n", "这周剩下的安排：从明天到周日，日历、待办和提醒里都是空的。\n\n", "要是想定个提醒，跟我说一声。"]
        let at = [1.0, 3.6, 4.0, 4.6, 5.2]
        let later = { (s: Double, f: @escaping () -> Void) in DispatchQueue.main.asyncAfter(deadline: .now() + s) { f() } }
        for (p, t) in zip(parts, at) { later(t) { chunk(p) } }
        later(1.3) { frame(#"{"type":"step","turn_id":"T1","step":1,"tool":"weather","label":"查天气","state":"running","started_ms":1791650003000,"ended_ms":null}"#) }
        later(3.3) { frame(#"{"type":"step","turn_id":"T1","step":1,"tool":"weather","label":"查天气","state":"done","started_ms":1791650003000,"ended_ms":1791650005000}"#) }
        later(5.4) { frame(#"{"type":"row","event_type":"surface.response_emitted","event_uid":"ffffffffffffffffffffffffffffffff","ts_epoch_ms":1791650016000,"payload":{"turn_id":"T1","text":"x","phase":"final"}}"#) }
        var live = TypedRows()
        let tick = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { _ in
            MainActor.assumeIsolated { if let w = board.words(utterance: "scene") { _ = self.streamTyped(w, into: &live) } }
        }
        later(5.8) {
            tick.invalidate()
            let record: [String: Any] = ["rows": [["seq": 9, "id": "r9", "source": "jarvis", "ts": "2026-10-15T09:00:00", "text": parts.joined(), "steps": [["label": "查天气", "ms": 2000]], "steps_ms": 2000, "steps_summary": "查了天气"]]]
            if let row = ConversationRow.parse(try! JSONSerialization.data(withJSONObject: record))?.first { MainActor.assumeIsolated { _ = self.placeTyped(row, live: &live, last: true) } }
        }
    }
    #endif

    /// The turn failed: her place says so (the brain's reason is not shown), with a button that sends the same words again.
    func showFailed() {
        Haptic.notify(.warning)
        let id = addTurn(his: false, "没答出来")
        if let i = ui.liveTurns.lastIndex(where: { $0.id == id }) { ui.liveTurns[i].failed = true }
    }

    /// 重试: the message under the button (`row`) goes again under its first id if it was not sent, else the failed turn goes again with its words and files as a new message.
    func sendAgain(row: String? = nil) {
        if let row, let n = Int(row.dropFirst()), let t = unsent[n] { return sendToBrain(t.words, again: t) }
        guard let t = sentTurn else { return }
        sendToBrain(t.words, again: t)
    }
}
