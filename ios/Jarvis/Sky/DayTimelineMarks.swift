// words: legacy
import SwiftUI

// What was important about the day: his star on a row, which he can put on and take off from the open row and from the menu of a long press. The star is on the row at once; the brain is
// told (`POST /inherent/day/highlight`), and the day's highlights it answers with are the phone's from then on. When it will not, the star goes back and the row says why.

/// A value that stands for the brain's own until the brain says the same: until when.
struct TLHeld<T: Equatable>: Equatable {
    var value: T
    var until: Date
}

/// The day's highlights as the brain keeps them (jarvis/state/day_highlights.py): at most five, his marks first and her picks in the slots he leaves, and an item he unmarked is not one,
/// though she picked it.
enum TLMarks {
    static let cap = 5

    /// the list after his word on one item: marked, it is his (under the name she gave it, if she picked it) and her picks give way to it when there are too many; unmarked, it is gone
    static func toggled(_ list: [Highlight], id: String, on: Bool) -> [Highlight] {
        let was = list.first { $0.id == id }
        var out = list.filter { $0.id != id }
        guard on else { return out }
        out.append(Highlight(id: id, label: was?.label ?? "", by: "him"))
        var room = max(0, cap - out.filter { $0.by != "her" }.count)
        out = out.filter { h in
            guard h.by == "her" else { return true }
            defer { room -= 1 }
            return room > 0
        }
        return out
    }
}

extension TimelineState {
    /// how long what he marked shows as the day's before the brain's own answer is the one that does
    static let markHolds = 120.0
    /// how long a note under a row stays
    static let noteFor = 4.0
    static let capNote = "最多标 5 个，先取消一个", markNote = "没标上，稍后再试"

    /// the star button of the open row: it is his mark that it turns on and off, so a star of hers turns into his
    func toggleStar(_ e: TLEntry) { setMark(e, on: !(e.lit && !e.hers)) }

    /// His star on a row, or off it. One word at a time: while one is on its way another waits for it.
    func setMark(_ e: TLEntry, on: Bool) {
        guard e.starrable, !markBusy, let r = reply else { return }
        let day = r.date, now = Date(), was = highlights[day]
        let base = was.flatMap { $0.until > now ? $0.value : nil } ?? r.highlights
        markBusy = true
        notes[e.id] = nil
        withAnimation(tlStarFade(reduce: TLMotion.reduced)) { highlights[day] = TLHeld(value: TLMarks.toggled(base, id: e.id, on: on), until: now.addingTimeInterval(Self.markHolds)) }
        if on { pop[e.id, default: 0] += 1 }
        if on { Haptic.notify(.success) } else { Haptic.impact(.light) }          // marked: the tap of something done, with the star's pop; unmarked: a light one
        Task { @MainActor [self] in
            defer { markBusy = false }
            switch await brain.mark(day, e.id, on) {
            case .done(let list):
                let held = list ?? highlights[day]?.value ?? TLMarks.toggled(base, id: e.id, on: on)
                withAnimation(tlStarFade(reduce: TLMotion.reduced)) { highlights[day] = TLHeld(value: held, until: Date().addingTimeInterval(Self.markHolds)) }
            case .full: back(e, day, was, Self.capNote)
            case .gone: back(e, day, was, Self.markNote); brain.refetch()
            case .failed: back(e, day, was, Self.markNote)
            }
        }
    }

    /// the star goes back to what it was, with a warning in the hand and the reason under the row
    private func back(_ e: TLEntry, _ day: String, _ was: TLHeld<[Highlight]>?, _ why: String) {
        withAnimation(tlStarFade(reduce: TLMotion.reduced)) { highlights[day] = was }
        Haptic.notify(.warning)
        say(why, on: e.id)
    }

    /// A line under a row, for a few seconds. The row is opened to show it, if it was not: he may have done this from the menu of a long press.
    func say(_ text: String, on id: String) {
        withAnimation(.smooth(duration: 0.26)) { if openRow != id { openRow = id }; notes[id] = text }
        Task { @MainActor [self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.noteFor * 1e9))
            if notes[id] == text { withAnimation(.easeOut(duration: 0.2)) { notes[id] = nil } }
        }
    }
}
