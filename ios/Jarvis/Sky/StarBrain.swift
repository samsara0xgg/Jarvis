import UIKit

// What he types and attaches, sent to the brain while the phone is paired (Brain/BrainTalk), and her answer as it lands in the conversation.
// The prototype's canned replies stay for the unpaired phone.

/// A typed turn as it went out, kept once the brain has taken it: a failed one is sent again with the same words and the files already up.
struct SentTurn {
    let words: String, shown: String, ids: [String], pics: [SentItem], about: About?
    let his: Int                // the live row of his line; it and everything after it go when the turn is sent again
}

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
    /// `again`: a failed turn sent as it was (its files are up already; the composer is not read).
    func sendToBrain(_ words: String, again: SentTurn? = nil) {
        let text = words.trimmingCharacters(in: .whitespacesAndNewlines), items = again == nil ? ui.attach.items : []
        if (text.isEmpty && items.isEmpty && again == nil) || sendingToBrain { return }
        if let m = AttachUpload.check(count: items.count) { return trouble(m) }
        sendingToBrain = true
        if again == nil { captureAbout() }
        let about = again.map(\.about) ?? ui.about
        note("正在发给她…", for: 120_000)
        let shown = again?.shown ?? text, pics = again?.pics ?? SentPics.items(items), utterance = UUID().uuidString
        if talk != nil { interrupt() }
        run { ok in
            // after the interrupt's own end of the talk (queued before this): his line, and the field empty for the next one
            // a turn that failed leaves its rows (all of them when it is sent again) before the new ones
            let from = again?.his ?? .max
            self.ui.liveTurns.removeAll { $0.failed || $0.id >= from }
            // The talk's base is the newest row before his line. `beginTalk` would ask for it by itself, and that answer can come after the brain has stored his
            // line (a slow link): his line would then be listed twice. The one asked below, before the words go, is used instead.
            let fresh = !self.ui.talking, mine = self.talkId + 1
            self.beginTalk(base: Conversation.shared.rows.last?.seq ?? 0)
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
            self.sentTurn = SentTurn(words: text, shown: shown, ids: ids, pics: self.ui.liveTurns.first { $0.id == his }?.pics ?? pics, about: about, his: his)
            for it in self.ui.attach.take() { AttachTemp.discard(it.url) }
            self.sendingToBrain = false
            guard let seq else { self.talk = nil; self.lingerFrom(nowMs()); return self.note("发给她了", for: 4000) }       // no conversation to read her answer from
            self.clearNote()
            try await self.brainTalk(ok, after: seq, utterance: utterance)
        }
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

    /// Her side of a typed turn the brain took: her thinking look, then her words as they land in the conversation. A lead-in ("我上网查查") is shown and the answer waited for.
    @MainActor private func brainTalk(_ ok: Ok, after seq: Int, utterance: String) async throws {
        sound(.heard, 1); expr = "31"; try await nap(520, ok)
        sound(.think, 1); expr = "30"
        var last = seq, deadline = nowMs() + 90_000, said = false, failed = false
        while nowMs() < deadline {
            try await nap(500, ok)
            let rows = await Conversation.shared.rows(after: last)
            try ok.check()
            let hers = rows.filter { !$0.isHis }
            guard let row = hers.last else {
                if StatusBoard.shared.failed(utterance: utterance) { failed = true; break }         // the host failed the turn: no answer is coming
                continue
            }
            last = rows.last?.seq ?? last
            for r in hers.dropLast() where !AnswerText.visible(r.text).isEmpty { addTurn(his: false, AnswerText.visible(r.text), leadIn: r.isLeadIn, steps: r.isLeadIn ? nil : r.steps) }
            // the steps under the answer: the brain's own summary of the turn, or what the line saw of them until the conversation has it
            let words = AnswerText.visible(row.text)
            startSay(words, leadIn: row.isLeadIn, steps: row.isLeadIn ? nil : row.steps ?? StatusBoard.shared.info)
            if !words.isEmpty { expr = "39"; talk = "speak"; said = true }          // an empty reply is not an answer: the fallback line still stands
            if row.isLeadIn { showStatus(); deadline = nowMs() + 25_000; continue }          // more follows (the steps she takes meanwhile show in a line of their own): it is waited for while this is read
            break
        }
        if failed { showFailed() }
        else if !said { addTurn(his: false, "她还在想，回答会在今天的对话里。"); talk = "speak"; expr = nil }
        else { await speaking?.finished(); try ok.check(); finishSay() }
        endTalk()
        if failed { lingerUntil = nowMs() + Self.lingerFailed }
        await Conversation.shared.refresh()
    }

    /// The turn failed: her place says so (the brain's reason is not shown), with a button that sends the same words again.
    func showFailed() {
        Haptic.notify(.warning)
        let id = addTurn(his: false, "没答出来")
        if let i = ui.liveTurns.lastIndex(where: { $0.id == id }) { ui.liveTurns[i].failed = true }
    }

    /// 重试: the failed turn goes again with its words and files.
    func sendAgain() {
        guard let t = sentTurn else { return }
        sendToBrain(t.words, again: t)
    }
}
