import UIKit

// What he types and attaches, sent to the brain while the phone is paired (Brain/BrainTalk), and her answer as it lands in the conversation.
// The prototype's canned replies stay for the unpaired phone.

extension StarEngine {
    /// A line for the status line over the composer (the same line the voice uses); it goes by itself after `ms`, or when `clearNote` is called.
    func note(_ text: String, for ms: Double = 8000) { brainNote = text; brainNoteUntil = nowMs() + ms }
    /// a note that says something went wrong
    func trouble(_ text: String) { note(text); Haptic.notify(.warning) }
    func clearNote() { brainNote = nil; brainNoteUntil = 0 }

    /// His words and everything attached go to the brain: his line shows at once, and under it hers, the typing dots (the board's steps then say what she is doing).
    /// Each file goes up first, then the words with their ids over the live line (PhoneLink); a brain that cannot take them there gets POST /inherent/submit.
    /// Anything that goes wrong takes his line back, says why on the status line and leaves the words and the files where they were, so he can take the refused
    /// one off and send again. Once the brain has taken the turn, her answer is waited for.
    func sendToBrain(_ words: String) {
        let text = words.trimmingCharacters(in: .whitespacesAndNewlines), items = ui.attach.items
        if (text.isEmpty && items.isEmpty) || sendingToBrain { return }
        if let m = AttachUpload.check(count: items.count) { return trouble(m) }
        sendingToBrain = true
        captureAbout()
        let about = ui.about
        note("正在发给她…", for: 120_000)
        let shown = text.isEmpty ? AttachModel.caption(items) : text, pics = SentPics.items(items), utterance = UUID().uuidString
        if talk != nil { interrupt() }
        run { ok in
            // after the interrupt's own end of the talk (queued before this): his line, and the field empty for the next one
            self.beginTalk()
            self.talk = "listen"
            let his = self.addTurn(his: true, shown, pics: pics)
            StatusBoard.shared.begin(utterance: utterance)
            self.showStatus()
            if self.ui.text == words { self.ui.text = "" }
            @MainActor func fail(_ m: String) {
                self.sendingToBrain = false
                self.ui.liveTurns.removeAll { $0.id == his }
                self.dropStatus()
                StatusBoard.shared.end()
                if self.ui.text.isEmpty { self.ui.text = words }
                if self.talk == "listen" { self.talk = nil; self.lingerFrom(nowMs()) }
                self.trouble(m)
            }
            // her answer is the first row of hers past the newest one now: asked while the files go up
            async let latest = Conversation.shared.latestSeq()
            let seq: Int?
            do {
                var ids: [String] = []
                for part in try await AttachUpload.parts(items, photos: self.ui.attach.photos) {
                    let up = try await BrainLink.shared.call { try await BrainClient.upload(part, bases: $0, token: $1) }
                    guard let id = up.id else { return fail(AttachUpload.refusal(status: up.status, name: part.name)) }
                    ids.append(id)
                }
                StallLog.mark("send: files up")
                seq = await latest
                StallLog.mark("send: latest seq")
                switch await PhoneLink.shared.sendTyped(utterance: utterance, text: text, attachments: ids, about: about?.json) {
                case .taken: break
                case .refused(let m): return fail(m)
                case .useSubmit:
                    let status = try await BrainLink.shared.call { try await BrainClient.submit(text, attachments: ids, about: about, bases: $0, token: $1) }
                    guard status == 200 else {
                        // the host takes the item whole or refuses the turn: say so, and leave the chip for him to take off
                        return fail(status == 404 ? "文件过期了，再发一次" : status == 422 && about != nil ? "她没接住上面的那一项，去掉它再发" : AttachUpload.refusal(status: status, name: ""))
                    }
                }
            } catch let e as AttachUpload.Refused {
                return fail(e.message)
            } catch let e as BrainError {
                return fail(e.message)
            } catch {
                return fail(BrainError.refused.message)
            }
            StallLog.mark("send: taken")
            for it in self.ui.attach.take() { AttachTemp.discard(it.url) }
            self.sendingToBrain = false
            guard let seq else { self.talk = nil; self.lingerFrom(nowMs()); return self.note("发给她了", for: 4000) }       // no conversation to read her answer from
            self.clearNote()
            SentPics.pending = pics
            try await self.brainTalk(ok, after: seq)
        }
    }

    /// Her side of a typed turn the brain took: her thinking look, then her words as they land in the conversation. A lead-in ("我上网查查") is shown and the answer waited for.
    @MainActor private func brainTalk(_ ok: Ok, after seq: Int) async throws {
        sound(.heard, 1); expr = "31"; try await nap(520, ok)
        sound(.think, 1); expr = "30"
        var last = seq, deadline = nowMs() + 90_000, said = false
        while nowMs() < deadline {
            try await nap(500, ok)
            let rows = await Conversation.shared.rows(after: last)
            try ok.check()
            if !SentPics.pending.isEmpty, let his = rows.first(where: { $0.isHis }) { SentPics.byRow[his.seq] = SentPics.pending; SentPics.pending = [] }
            let hers = rows.filter { !$0.isHis }
            guard let row = hers.last else { continue }
            last = rows.last?.seq ?? last
            for r in hers.dropLast() { addTurn(his: false, AnswerText.visible(r.text), leadIn: r.isLeadIn, steps: r.isLeadIn ? nil : r.steps) }
            // the steps under the answer: the brain's own summary of the turn, or what the line saw of them until the conversation has it
            startSay(AnswerText.visible(row.text), leadIn: row.isLeadIn, steps: row.isLeadIn ? nil : row.steps ?? StatusBoard.shared.info)
            expr = "39"; talk = "speak"; said = true
            if row.isLeadIn { showStatus(); deadline = nowMs() + 25_000; continue }          // more follows (the steps she takes meanwhile show in a line of their own): it is waited for while this is read
            break
        }
        if !said { addTurn(his: false, "她还在想，回答会在今天的对话里。"); talk = "speak"; expr = nil }
        else { await speaking?.finished(); try ok.check(); finishSay() }
        endTalk()
        await Conversation.shared.refresh()
    }
}
