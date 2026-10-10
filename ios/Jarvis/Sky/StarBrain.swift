import UIKit

// What he types and attaches, sent to the brain while the phone is paired (Brain/BrainTalk). The prototype's canned replies stay for the unpaired phone.

extension StarEngine {
    /// A line for the status line over the composer (the same line the voice uses); it goes by itself after `ms`, or when `clearNote` is called.
    func note(_ text: String, for ms: Double = 8000) { brainNote = text; brainNoteUntil = nowMs() + ms }
    func clearNote() { brainNote = nil; brainNoteUntil = 0 }

    /// His words and everything attached go to the brain: each file up first, then the words with their ids. Anything that goes wrong is said on the status line and
    /// leaves the words and the files where they were, so he can take the refused one off and send again.
    func sendToBrain(_ words: String) {
        let text = words.trimmingCharacters(in: .whitespacesAndNewlines), items = ui.attach.items
        if (text.isEmpty && items.isEmpty) || sendingToBrain { return }
        endEditing?()
        if let m = AttachUpload.check(count: items.count) { return note(m) }
        sendingToBrain = true
        note("正在发给她…", for: 120_000)
        run { ok in
            defer { self.sendingToBrain = false }
            do {
                var ids: [String] = []
                for part in try await AttachUpload.parts(items, photos: self.ui.attach.photos) {
                    let up = try await BrainLink.shared.call { try await BrainClient.upload(part, bases: $0, token: $1) }
                    guard let id = up.id else { return self.note(AttachUpload.refusal(status: up.status, name: part.name)) }
                    ids.append(id)
                }
                let status = try await BrainLink.shared.call { try await BrainClient.submit(text, attachments: ids, bases: $0, token: $1) }
                guard status == 200 else {
                    return self.note(status == 404 ? "文件过期了，再发一次" : AttachUpload.refusal(status: status, name: ""))
                }
                for it in self.ui.attach.take() { AttachTemp.discard(it.url) }
                self.ui.text = ""
                self.note("发给她了", for: 4000)
            } catch let e as AttachUpload.Refused {
                self.note(e.message)
            } catch let e as BrainError {
                self.note(e.message)
            } catch {
                self.note(BrainError.refused.message)
            }
        }
    }
}
