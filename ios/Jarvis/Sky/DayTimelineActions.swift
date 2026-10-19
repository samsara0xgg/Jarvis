import SwiftUI

// What a row of the day does when it is opened, each only where it works today: hand it to her, open the conversation at its time, rename a place he keeps (and take that back), say he
// knows a reminder that rang, have her sum up a stretch of Mac work. The model says which of them a row offers (`TLEntry.actions`); this is what each one does.

/// a rename he can take back for a few seconds
struct PlaceUndo: Equatable {
    let change: String              // the brain's name for the change
    let place: String               // the place he keeps
    let was: String                 // what it was called before
    var note: String?               // why taking it back did not go
}

extension TimelineState {
    /// the one turn [让她总结这段] sends, with the work item as what it is about
    static let summarizeWords = "帮我总结一下这段时间我在 Mac 上做了什么"
    /// how long a rename can be taken back, and how long a rename or a reminder he knows shows as his before the brain's own answer has come
    static let undoFor = 5.0, renameHolds = 120.0, ackHolds = 120.0

    /// a debug build's made-up day has no brain to tell: what he does to it stays on the phone
    private var madeUp: Bool {
        #if DEBUG
        return fixture != nil
        #else
        return false
        #endif
    }

    /// what an action of a row does; one the row does not offer does nothing
    func perform(_ a: TLAction, on e: TLEntry, engine: StarEngine) {
        guard e.actions.contains(a) else { return }
        switch a {
        case .ask:
            if renaming != nil { cancelRename() }           // its field has the keyboard and keeps the composer away: asking is for the composer
            if let about = e.about { engine.askAbout(about) }
        case .openTalk:
            if let start = e.talkStart { engine.showTalk(start) }
        case .rename:
            beginRename(e)
        case .acknowledge:
            acknowledge(e)
        case .summarize:
            guard let about = e.about else { return }
            engine.aboutDropped = nil
            engine.setAbout(about)
            engine.sendToBrain(Self.summarizeWords)
        }
    }

    // MARK: a reminder that rang

    /// he said he knows this reminder and the time for the brain's own answer is not up
    func isAcked(_ id: String, at now: Date = Date()) -> Bool { (acked[id] ?? .distantPast) > now }

    /// He knows. The row says so at once and the count of what waits goes down; the brain is told on the card route (`CardAnswer.acknowledge`, which asks for the days again), and if it
    /// does not take it the row waits again. When the brain answers that there is nothing to take (it was not waiting any more) and its next answer still has the reminder waiting, the
    /// row waits again once the time for that answer is up (`ackHolds`), and does not say he knows for good.
    func acknowledge(_ e: TLEntry) {
        guard !isAcked(e.id) else { return }
        withAnimation(.easeOut(duration: 0.25)) { acked[e.id] = Date().addingTimeInterval(Self.ackHolds) }
        if madeUp { Haptic.notify(.success); return }
        let card = WaitingCard(id: "notice-" + e.id, kind: .notice, brainId: e.id, short: "提醒", title: e.title, meta: "", body: "",
                               actions: [CardAction(label: "知道了", answer: .acknowledge)])
        Waiting.shared.answer(card, .acknowledge)
        Task { @MainActor in
            while Waiting.shared.sending.contains(card.id) { try? await Task.sleep(nanoseconds: 100_000_000) }
            if Waiting.shared.failures[card.id] != nil { withAnimation(.easeOut(duration: 0.25)) { _ = acked.removeValue(forKey: e.id) } }
        }
    }

    // MARK: a place he keeps

    /// the field opens in the row, which is opened
    func beginRename(_ e: TLEntry) {
        guard e.place?.canRename == true else { return }
        renameNote = nil
        withAnimation(.smooth(duration: 0.26)) { openRow = e.id; renaming = e.id }
    }
    func cancelRename() {
        renameNote = nil
        withAnimation(.smooth(duration: 0.26)) { renaming = nil }
    }

    /// The name he typed: blanks collapsed as the brain does, and nothing sent when it is empty, too long or the one it has. On success the place wears it at once, the days are asked for
    /// again, and taking it back is on offer for a few seconds.
    func rename(_ e: TLEntry, to raw: String) {
        guard let p = e.place, p.canRename, let id = p.keptId, !renameBusy else { return }
        let name = raw.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        if name.isEmpty { renameNote = "名字不能是空的"; Haptic.notify(.warning); return }
        if name.count > 80 { renameNote = "名字最多 80 个字"; Haptic.notify(.warning); return }
        if name == (p.keptName ?? e.title) { cancelRename(); return }
        let was = p.keptName ?? e.title
        renameBusy = true; renameNote = nil
        Task { @MainActor in
            defer { renameBusy = false }
            let result = madeUp ? PlaceChange.done(change: "change-demo") : await sendRename(id, name)
            switch result {
            case .done(let change):
                renamed[id] = Renamed(name: name, until: Date().addingTimeInterval(Self.renameHolds))
                withAnimation(.smooth(duration: 0.26)) { renaming = nil }
                Haptic.notify(.success)
                if let change { offerUndo(PlaceUndo(change: change, place: id, was: was)) }
                if !madeUp { BrainLink.shared.opened() }
            case .refused(let why):
                renameNote = "没改成：" + why
                Haptic.notify(.warning)
            }
        }
    }

    private func sendRename(_ id: String, _ name: String) async -> PlaceChange {
        do { return try await BrainLink.shared.call { try await BrainClient.renamePlace(id: id, name: name, bases: $0, token: $1) } }
        catch let e as BrainError { return .refused(e.message) }
        catch { return .refused(BrainError.refused.message) }
    }

    private func offerUndo(_ u: PlaceUndo) {
        withAnimation(.easeOut(duration: 0.2)) { undo = u }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(Self.undoFor * 1e9))
            if undo?.change == u.change { withAnimation(.easeOut(duration: 0.2)) { undo = nil } }
        }
    }

    /// 撤销: the toast goes at the tap, so a second tap has nothing to take back (the brain would be asked twice), and the brain takes the change back and the place has its old name
    /// again; when it cannot, the toast comes back for a moment and says why
    func undoRename() {
        guard let u = undo, u.note == nil, !undoBusy else { return }
        undoBusy = true
        withAnimation(.easeOut(duration: 0.2)) { undo = nil }
        Task { @MainActor in
            let result = madeUp ? PlaceChange.done(change: nil) : await sendUndo(u.change)
            undoBusy = false
            switch result {
            case .done:
                renamed[u.place] = Renamed(name: u.was, until: Date().addingTimeInterval(Self.renameHolds))
                Haptic.selection()
                if !madeUp { BrainLink.shared.opened() }
            case .refused(let why):
                var shown = u; shown.note = why
                withAnimation(.easeOut(duration: 0.2)) { undo = shown }
                Haptic.notify(.warning)
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                if undo?.change == u.change { withAnimation(.easeOut(duration: 0.2)) { undo = nil } }
            }
        }
    }

    private func sendUndo(_ change: String) async -> PlaceChange {
        do { return try await BrainLink.shared.call { try await BrainClient.undoPlaceChange(change, bases: $0, token: $1) } }
        catch let e as BrainError { return .refused(e.message) }
        catch { return .refused(BrainError.refused.message) }
    }

    // MARK: the weather

    /// how long the sun's times stay in the header after a tap on the weather
    static let sunFor = 3.0

    /// A tap on the header's weather: when the sun rose and set, in its place, for a few seconds; another tap puts it away at once.
    func saySun(for seconds: Double = TimelineState.sunFor) {
        sunHide?.cancel()
        if sunSaid { withAnimation(.easeOut(duration: 0.2)) { sunSaid = false }; return }
        withAnimation(.easeOut(duration: 0.2)) { sunSaid = true }
        sunHide = Task { @MainActor [self] in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1e9))
            if !Task.isCancelled { withAnimation(.easeOut(duration: 0.2)) { sunSaid = false } }
        }
    }

    // MARK: what waits

    /// the first row that waits on him: the list goes to it and it flashes (a row he had open is closed first, so the place it stands at is where the closed list has it)
    func revealWaiting(in m: DayTimelineModel) {
        guard let id = m.waiting.first, let y = m.top(of: id), let e = m.entries.first(where: { $0.id == id }) else { return }
        openRow = nil
        scroll(to: max(0, y + e.gap - TLLayout.fadeTop - 6), animated: true)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 350_000_000)          // the list is on its way: the flash is where the eye arrives
            flash = id
            try? await Task.sleep(nanoseconds: 80_000_000)
            flash = nil
        }
    }
}
