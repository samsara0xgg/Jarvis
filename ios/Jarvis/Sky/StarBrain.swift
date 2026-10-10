import UIKit

// What he types and attaches, sent to the brain while the phone is paired (Brain/BrainTalk), and her answer as it lands in the conversation.
// The prototype's canned replies stay for the unpaired phone.

extension StarEngine {
    /// A line for the status line over the composer (the same line the voice uses); it goes by itself after `ms`, or when `clearNote` is called.
    func note(_ text: String, for ms: Double = 8000) { brainNote = text; brainNoteUntil = nowMs() + ms }
    func clearNote() { brainNote = nil; brainNoteUntil = 0 }

    /// His words and everything attached go to the brain: each file up first, then the words with their ids. Anything that goes wrong is said on the status line and
    /// leaves the words and the files where they were, so he can take the refused one off and send again. Once the brain has taken the turn, her answer is waited for.
    func sendToBrain(_ words: String) {
        let text = words.trimmingCharacters(in: .whitespacesAndNewlines), items = ui.attach.items
        if (text.isEmpty && items.isEmpty) || sendingToBrain { return }
        endEditing?()
        if let m = AttachUpload.check(count: items.count) { return note(m) }
        sendingToBrain = true
        note("正在发给她…", for: 120_000)
        let shown = text.isEmpty ? AttachModel.caption(items) : text, pics = SentPics.items(items)
        if talk != nil { interrupt() }
        run { @MainActor ok in
            let seq: Int?
            do {
                var ids: [String] = []
                for part in try await AttachUpload.parts(items, photos: self.ui.attach.photos) {
                    let up = try await BrainLink.shared.call { try await BrainClient.upload(part, bases: $0, token: $1) }
                    guard let id = up.id else { self.sendingToBrain = false; return self.note(AttachUpload.refusal(status: up.status, name: part.name)) }
                    ids.append(id)
                }
                seq = await Conversation.shared.latestSeq()      // her answer is the first row of hers past this one
                let status = try await BrainLink.shared.call { try await BrainClient.submit(text, attachments: ids, bases: $0, token: $1) }
                guard status == 200 else {
                    self.sendingToBrain = false
                    return self.note(status == 404 ? "文件过期了，再发一次" : AttachUpload.refusal(status: status, name: ""))
                }
            } catch let e as AttachUpload.Refused {
                self.sendingToBrain = false; return self.note(e.message)
            } catch let e as BrainError {
                self.sendingToBrain = false; return self.note(e.message)
            } catch {
                self.sendingToBrain = false; return self.note(BrainError.refused.message)
            }
            for it in self.ui.attach.take() { AttachTemp.discard(it.url) }
            self.ui.text = ""
            self.sendingToBrain = false
            guard let seq else { return self.note("发给她了", for: 4000) }       // no conversation to read her answer from
            self.clearNote()
            SentPics.pending = pics
            try await self.brainTalk(ok, shown: shown, pics: pics, after: seq)
        }
    }

    /// Her side of a typed turn: his line shown, her thinking look, then her words as they land in the conversation. A lead-in ("我上网查查") is shown and the answer waited for.
    @MainActor private func brainTalk(_ ok: Ok, shown: String, pics: [SentItem], after seq: Int) async throws {
        setDrawer(false, quiet: true)
        talk = "listen"; ui.talking = true; ui.capYou = shown; ui.capPics = pics; ui.capHer = ""; ui.capHerAlpha = []; ui.capOut = false
        sound(.heard, 1); expr = "31"; try await nap(520, ok)
        sound(.think, 1); expr = "30"
        var last = seq, deadline = nowMs() + 90_000, said = false
        while nowMs() < deadline {
            try await nap(1000, ok)
            let rows = await Conversation.shared.rows(after: last)
            try ok.check()
            if !SentPics.pending.isEmpty, let his = rows.first(where: { $0.isHis }) { SentPics.byRow[his.seq] = SentPics.pending; SentPics.pending = [] }
            guard let row = rows.last(where: { !$0.isHis }) else { continue }
            last = rows.last?.seq ?? last
            speaking?.stop()
            let say = Say(Self.head(AnswerText.visible(row.text)), cps: 12); speaking = say
            expr = "39"; talk = "speak"; said = true
            if row.isLeadIn { deadline = nowMs() + 25_000; continue }          // more follows: it is waited for while this is read
            break
        }
        if !said { ui.capHer = "她还在想，回答会在今天的对话里。"; ui.capHerAlpha = Array(repeating: 1, count: ui.capHer.count); talk = "speak"; expr = nil }
        else { await speaking?.finished(); try ok.check(); speaking = nil }
        try await nap(2400, ok)     // her words stay long enough to read
        endTalk()
        await Conversation.shared.refresh()
    }

    /// The beginning of a long answer, as much as the caption holds: a Chinese character is one cell, a Latin letter about half. The whole of it is in the drawer.
    private static func head(_ s: String, cells: Double = 70) -> String {
        var w = 0.0, n = 0
        for ch in s {
            w += (ch.unicodeScalars.first?.value ?? 0) >= 0x2E80 ? 1 : 0.55
            if w > cells { return String(s.prefix(n)) + "…" }
            n += 1
        }
        return s
    }
}
