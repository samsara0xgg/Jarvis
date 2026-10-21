// words: legacy
import SwiftUI
import Observation

// The whole day as the middle of the home screen (Settings › 首页 › 中间显示 › 时间线). Her sentence about the day, an overview bar of where he was, and under it the day as one
// list: sleep, the places he stayed in as groups with what happened there as rows, the trips between them, and a line for now. The words and the order are
// Sky/DayTimelineModel.swift; this file draws them and moves them. Everything is in the chrome's 440 x 956 design points.

private let ink = SetColor.ink
private let muted = SetColor.muted
private let faint = SetColor.faint
private let warm = Color(red: 1, green: 240 / 255, blue: 220 / 255)               // the warm white of now
private let amber = Color(red: 1, green: 201 / 255, blue: 143 / 255)               // what waits on him, and nothing else
private let hair = Color(red: 157 / 255, green: 180 / 255, blue: 1).opacity(0.12)
private let iconColor = Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.82)

enum TLLayout {
    static let lineY = 108.0, lineH = 40.0             // her sentence: two lines of 15 pt
    static let barY = 176.0, barW = 380.0, barH = 22.0, barRadius = 6.0       // 28 pt over it for now's time (and, under a finger, the time in its chip), 12 pt under it for the hours
    static let chipsY = 220.0, chipsH = 24.0, chipsHit = 36.0                    // the day's numbers: chips of 24 pt (the first 8 pt under the hours) in a row of 36 that takes the touch
    static let pillsY = 248.0                          // the row of the pills (the way back, what waits) between the chips and the list
    /// the list runs on into the glass bar at the foot (Sky/HomeBar.swift) and has faded out 12 pt into it, before its rounded ends: rows show through the top of the glass, and
    /// nothing of them round its ends or under it
    static let listTop = 272.0, fade = 24.0, fadeTop = 12.0, listBottom = HomeBar.top + 12 - fade
    /// what the list gives up at its foot while the composer is out at rest (the bar has given way to it): its fade ends 8 pt over the field
    static var composerRoom: Double { listBottom + fade - (H - StarUI.restGap - 48 - 8) }
    static var viewport: Double { listBottom - listTop }
    /// what is left of the list's height under `pinned` points of cards (and the gap under them)
    static func room(pinned: Double) -> Double { viewport - (pinned > 0 ? pinned + 4 : 0) }
    /// the pinned cards of what waits on him are never taller than this
    static let waitingMax = 260.0
    /// The black under the list as (y, opacity): none at the bar, `strength` by 24 pt into the list, and back to none at the foot (48 pt, under the list's own fade). Both ramps are eased (smoothstep)
    /// in `steps` steps, so that no edge shows where they start or end.
    static func scrim(strength: Double = 0.8, steps: Int = 12) -> [(y: Double, alpha: Double)] {
        func eased(_ t: Double) -> Double { t * t * (3 - 2 * t) }
        let from = barY, full = listTop + 24, end = listBottom + fade, out = end - 48
        let down = (0...steps).map { i -> (y: Double, alpha: Double) in let t = Double(i) / Double(steps); return (from + t * (full - from), strength * eased(t)) }
        let up = (0...steps).map { i -> (y: Double, alpha: Double) in let t = Double(i) / Double(steps); return (out + t * (end - out), strength * (1 - eased(t))) }
        return down + up
    }
}

// MARK: - what the timeline remembers

@Observable
final class TimelineState {
    /// the day on show, as a day number of `Cal.cur`; never after today
    var day = 0
    /// bumps whenever the timeline opens afresh (at launch, and when it is switched to): the entry motion runs once the day is there
    var enter = 0
    /// false from the moment it opens until its day has arrived: the overview bar has not grown in yet and the first rows have not risen
    var reveal = false
    /// which way the last change of day went: +1 to an earlier day (the page moves right), -1 to a later one
    var slide = 1
    var openRow: String?                // the row whose page is open over the list, one at a time
    var lens: TLLens?                   // the day's number he pressed: the bar and the list bring that kind forward
    var scrub: Double?                  // the minute under his finger on the overview bar
    @ObservationIgnored var finger: CGPoint?        // and where that finger is, in the chrome's design points: where her eyes look (Sky/DayTimelineGaze.swift); nothing draws from it
    var hot: String?                    // the row that covers it, lit in the list
    @ObservationIgnored private var seen: String?       // the row the list was last sent to while the finger is down
    @ObservationIgnored private var scrubHold: Task<Void, Never>?
    @ObservationIgnored private var turning: String?        // the row a finger on the bar has reached that the open page will turn to, and the rest that waits for it
    @ObservationIgnored private var rest: Task<Void, Never>?
    var openCard: String?               // the waiting card opened over the list
    var focus: String?                  // a waiting card the engine asks to bring up
    var version = 0                     // bumps when the brain's days were adopted again: talk topics may have come
    var request = ScrollRequest()       // where the list is asked to go
    let swipe = TLSwipe()               // how far a swipe back has the row's page to the right
    var waitH = 0.0                     // how tall the pinned cards over the list are
    // what a row does when it is opened (Sky/DayTimelineActions.swift)
    var acked: [String: Date] = [:]                 // the reminders he has just said he knows, until when the row says so before the brain's next answer does (`ackHolds`)
    var renamed: [String: Renamed] = [:]            // the places he keeps that he has just renamed, by the place's id
    struct Renamed: Equatable { var name: String; var until: Date }
    var renaming: String?                           // the row whose place is being renamed (or named for the first time): its field is open
    var renameBusy = false
    var renameNote: String?                         // why it did not go
    var nameRole = TLRole.other                     // the kind of place he is naming, which the chips under its field say
    var undo: PlaceUndo?                            // the rename he can take back for a few seconds
    @ObservationIgnored var undoBusy = false        // taking it back is on its way to the brain
    // what he says of what his mail made, and of a place he names (Sky/DayTimelineCommit.swift, Sky/DayTimelineActions.swift)
    var confirmed: [String: Date] = [:]             // the events he has just said are his, until when the row says so before the brain's next answer does (`commitHolds`)
    var dropped: [String: Date] = [:]               // and those he has just said are not: the day has no row for them for as long
    var named: [String: TLHeld<KeptPlace>] = [:]    // the stays he has just named a place at, by the stay's id, as the brain keeps the place
    // what he marks (Sky/DayTimelineMarks.swift)
    var highlights: [String: TLHeld<[Highlight]>] = [:]     // the day's highlights as they stand after his mark, by the day's date, until the brain's own say the same (`markHolds`)
    var markBusy = false                            // his word on a mark is on its way to the brain: another waits for it
    var notes: [String: String] = [:]               // why what he did to a row did not go, by row: said under the row while it is open
    var pop: [String: Int] = [:]                    // bumps when a row has just been starred: its star in the open row bursts
    // what he says of a stretch (Sky/DayTimelineSayWhat.swift)
    var sayWhat: TLSayWhat?                         // the stretch the sheet is open for
    var sayBusy = false                             // his words are on their way to the brain
    var sayNote: String?                            // why they did not go
    @ObservationIgnored var brain = TLBrain.live    // where his words go (a test or a debug scene gives another)
    // 理一下 (Sky/DayTimelineTidy.swift)
    var tidy = TLTidy.idle                          // what the button says
    var tidied: UUID?                               // the brain wrote a new run: the serial of the reading of today the bar was drawn from, which it fades from once the next one is in
    @ObservationIgnored var tidyHold: Task<Void, Never>?
    @ObservationIgnored var tidyHolds = (nothing: 2.0, failed: 3.0)         // how long the button keeps what it says of an answer that changed nothing, and of one that failed
    // her cards (Sky/DayTimelineArc.swift)
    var arcMounted = false                          // the cards are on screen: from the moment she puts them out until they have gone back into her
    var arcOpen = false                             // and out of her (false while they come out, and as they go back)
    var arcHover: TLHerMenu.Kind?                   // the card a finger that is still down is over
    var arcPicked: TLHerMenu.Kind?                  // the card chosen: it pulses before they go back
    var arcHolding = false                          // the finger that held her down is still down
    @ObservationIgnored var arcTask: Task<Void, Never>?
    var flash: String?                              // the row just scrolled to: it flashes
    var sunSaid = false                             // the weather in the header was just tapped: it says when the sun rose and set, in its place
    @ObservationIgnored var sunHide: Task<Void, Never>?
    struct ScrollRequest: Equatable { var y = 0.0, animated = false, glide = false, n = 0 }          // `glide`: a short spring, for a finger carrying the list from row to row
    @ObservationIgnored private var pending: Int?
    #if DEBUG
    struct Fixture { var replies: [Int: DayReply]; var now: Double; var loading = false }       // by days from today; `loading`: the day is still being asked for
    var fixture: Fixture?               // made-up days and a fixed now (Sky/StarTimelineScene.swift)
    #endif

    /// the day number of today as of the last tick of the minute (`rollOver`): what shows today reads it through `today`, so that midnight draws it again
    var dayStamp = floorDay(NOW1)
    var today: Int { _ = dayStamp; return floorDay(NOW1) }       // the engine's own now: the phone's today, or the sample world's Thursday
    /// the minute of now in the brain's day `r`, if now is in it
    func nowMinute(in r: DayReply) -> Double? {
        #if DEBUG
        if let f = fixture { return day == today ? f.now : nil }
        #endif
        let minute = TLClock(r).minute(Cal.now())
        return minute >= 0 && minute < DAY ? minute : nil
    }

    /// what the brain told of the day on show (a debug build's made-up day stands in for it)
    var reply: DayReply? {
        #if DEBUG
        if let f = fixture { return f.replies[day - today] }
        #endif
        return BrainLink.shared.days[Cal.cur.iso(day)]
    }
    /// everything a day is made of on the screen: the reading of it, the minute, what was said of talks, and what he has just done to it that holds
    private struct BuiltKey: Equatable {
        var reply: UUID, minute: Double?, version: Int, topics: Int
        var acked: Set<String>, renamed: [String: String], done: TLDone
    }
    @ObservationIgnored private var kept: (key: BuiltKey, built: TLBuilt)?

    /// The day on show as the screen has it. It is made again only when something it is made of has changed (`BuiltKey`), not on every run of the screen's body, which a keyboard rising, a
    /// pull of the history and a ticking minute all ask for.
    func built(_ r: DayReply) -> TLBuilt {
        let now = Date(), minute = nowMinute(in: r)
        let acked = Set(acked.filter { $0.value > now }.keys), renamed = renamed.compactMapValues { $0.until > now ? $0.name : nil }
        let done = TLDone(highlights: highlights[r.date].flatMap { $0.until > now ? $0.value : nil },
                          confirmed: Set(confirmed.filter { $0.value > now }.keys), dropped: Set(dropped.filter { $0.value > now }.keys),
                          named: named.compactMapValues { $0.until > now ? $0.value : nil })
        let key = BuiltKey(reply: r.serial, minute: minute.map { $0.rounded(.down) }, version: version, topics: TalkTopics.shared.version, acked: acked, renamed: renamed, done: done)
        if let k = kept, k.key == key { return k.built }
        let b = TLBuilt(DayTimelineModel.build(r, now: minute, topic: { TalkTopics.shared.topic($0) }, acked: acked, renamed: renamed, done: done))
        kept = (key, b)
        return b
    }
    func model(_ r: DayReply) -> DayTimelineModel { built(r).model }

    func scroll(to y: Double, animated: Bool, glide: Bool = false) { request = ScrollRequest(y: y, animated: animated, glide: glide, n: request.n + 1) }

    /// A finger on the overview bar at minute `t` (none: it has lifted): the list goes to the row that covers it and lights it. With a row's page open the list stays where it is, for the way
    /// back, and the page turns to the row that covers `t` (one that opens to something) once the finger rests on it for 150 ms, or lifts: while it moves only the bar's bracket follows it, so
    /// that no page is swapped for every row it crosses. A minute off the bar is the bar's start or end.
    func follow(_ minute: Double?, in m: DayTimelineModel?) {
        let t = minute.map { m?.onBar($0) ?? $0 }
        scrub = t
        if t == nil { finger = nil }
        guard let t, let m else { hot = nil; seen = nil; turnPage(to: nil, now: true); return }
        if openRow != nil {
            hot = nil; seen = nil
            if let e = m.lit(at: t), e.opens { turnPage(to: e.id == openRow ? nil : e.id) }          // over no row that opens, what it reached last still waits
            return
        }
        hot = m.lit(at: t)?.id
        guard let e = m.entry(at: t), e.id != seen else { return }        // the same row as a moment ago: the list is already there
        seen = e.id
        if let y = m.top(of: e.id) { scroll(to: y, animated: false, glide: true) }
    }

    /// The page a finger on the bar has reached, turned to after a rest of 150 ms on it, or at once when `now` (the finger lifts); none: the finger is where the page is, or has gone.
    private func turnPage(to id: String?, now: Bool = false) {
        if now, let id = turning, openRow != nil { openRow = id }
        guard id != turning else { return }
        turning = id; rest?.cancel(); rest = nil
        guard let id else { return }
        rest = Task { @MainActor [self] in
            try? await Task.sleep(nanoseconds: 150_000_000)
            if !Task.isCancelled, turning == id { turning = nil; if openRow != nil { openRow = id } }
        }
    }

    /// VoiceOver's step holds a minute on the bar the way a finger would, and lets it go after a pause (each step starts the pause again)
    func holdThenRelease(for seconds: Double = 3) {
        scrubHold?.cancel()
        scrubHold = Task { @MainActor [self] in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1e9))
            if !Task.isCancelled { withAnimation(.easeOut(duration: 0.2)) { follow(nil, in: nil) } }
        }
    }

    /// The timeline opens afresh: the bar and the rows are put back to before their entry, with no motion, and run it when the day is there.
    func open() {
        enter += 1
        var t = Transaction(); t.disablesAnimations = true
        withTransaction(t) { reveal = false }
    }

    /// What was held on a day that is not the one on show any more, or on the timeline when it is left: the row opened, the minute under a finger, the field of a rename (which keeps
    /// the composer away while it is open) and the sun's words.
    func letGo() {
        openRow = nil; renaming = nil; renameNote = nil; sunSaid = false
        closeArc()
        follow(nil, in: nil)
    }

    /// The rows the list has now: a row opened or being renamed that is not among them any more (the day was asked for again and the place is not in it) is let go.
    func keep(only ids: Set<String>) {
        if let id = openRow, !ids.contains(id) { openRow = nil }
        if let id = renaming, !ids.contains(id) { renaming = nil; renameNote = nil }
    }

    /// Another day. The page's direction is set first and the page changes on the next turn, so the page that leaves slides the same way as the one that comes.
    func setDay(_ d: Int) {
        let from = pending ?? day
        guard d != from, d <= today else { return }
        pending = d; slide = d < from ? 1 : -1; letGo()
        DispatchQueue.main.async { [self] in
            pending = nil
            withAnimation(TLMotion.dayChange(reduce: TLMotion.reduced)) { day = d }
        }
    }
    /// a swipe to another day, with a tick of the phone when there is one to go to (none past today)
    func swipe(_ delta: Int) {
        let from = pending ?? day
        setDay(from + delta)
        if (pending ?? day) != from { Haptic.selection() }
    }

    /// A drag along the day: mostly sideways, and either 40 pt far or flung (it would have gone on past 150 pt: `predicted`, where it would come to rest) it is a day before (dragged right) or a
    /// day after (dragged left); a drag up and down is the list's own.
    static func swipe(dx: Double, dy: Double, predicted: Double = 0) -> Int? {
        guard abs(dx) > abs(dy) * 1.8, abs(dx) > 40 || abs(predicted) > 150 else { return nil }
        return dx > 0 ? -1 : 1
    }

    /// A drag on a row's page is the way back when it starts to the right and mostly sideways (`dx`, `dy` of its first 10 pt); else it is the page's own scroll.
    static func backSwipeStarts(dx: Double, dy: Double) -> Bool { dx > 0 && dx > abs(dy) * 1.2 }

    /// A swipe back lets go as the way back when it went far enough to the right (80 pt, or its fling would have: 200 pt) and mostly sideways.
    static func backSwipe(dx: Double, dy: Double, predicted: Double) -> Bool {
        dx > 0 && dx > abs(dy) * 1.2 && (dx > 80 || predicted > 200)
    }

    /// The tick of the minute asks whether a new day has begun (midnight, or the phone was asleep past it). If so, and the day on show was the one that ended, it moves to the new today;
    /// what was held on it is let go, and the days are asked for again (the one that ended is final now, and the new today is not yet in).
    @discardableResult func rollOver() -> Bool {
        let now = floorDay(NOW1)
        guard now != dayStamp else { return false }
        let ended = dayStamp
        dayStamp = now
        #if DEBUG
        let madeUp = fixture != nil
        #else
        let madeUp = false
        #endif
        if !madeUp { BrainLink.shared.opened() }
        guard (pending ?? day) == ended else { return true }
        setDay(now)
        return true
    }

    /// back to now: today with the line for now in view
    func goToday() {
        openRow = nil
        if (pending ?? day) != today { setDay(today) } else { request = ScrollRequest(y: -1, animated: true, n: request.n + 1) }       // y < 0: where now rests
    }
}

/// A day as the screen draws it: its model, and the bar's drawing of it (from what he was doing), which the body of the screen asks for on every run
struct TLBuilt {
    let model: DayTimelineModel
    let scene: TLBarScene

    init(_ m: DayTimelineModel) { model = m; scene = m.scene }
}

// MARK: - the screen

private enum Shown {
    case loading, sentence(String), model(TLBuilt)
    /// which of the three, for what animates when it changes
    var kind: Int { switch self { case .loading: 0; case .sentence: 1; case .model: 2 } }
}

struct DayTimelineView: View {
    let engine: StarEngine
    let ui: StarUI
    @Binding var pull: Double?
    @Environment(\.accessibilityReduceMotion) private var reduce
    @State private var nowInView = true

    private var tl: TimelineState { ui.tl }

    var body: some View {
        let on = HomeModule.shared.isTimeline
        let away = ui.panelOpen || ui.tuning          // a talk and the keyboard leave the day where it is: it is the history, over the day, that takes its place
        _ = tl.version
        return TimelineView(.everyMinute) { context in
            let shown = shownNow()
            let built: TLBuilt? = { if case .model(let b) = shown { return b } else { return nil } }()
            let model = built?.model
            ZStack(alignment: .topLeading) {
                scrim()
                ZStack(alignment: .topLeading) {
                    TLTopView(ui: ui, model: model, scene: built?.scene ?? .empty, loading: !isReady(shown))
                    TLTalkArea(engine: engine, ui: ui, model: model)            // a talk takes the place of her sentence, the bar and the chips
                }
                .modifier(HistoryOver(ui: ui, pull: $pull)).allowsHitTesting(!ui.panelOpen)
                listArea(shown, away: away)
                undoToast(away: away)
                TLWaitPill(count: away ? 0 : model?.waiting.count ?? 0) { if let model { tl.revealWaiting(in: model) } }
                    .frame(width: W - 60, alignment: .leading).offset(x: 30, y: TLLayout.pillsY + 2)
                TLNowPill(tl: tl, nowInView: $nowInView, away: away)
                // while he types to her, a touch anywhere on the day (a tap, or a drag up or down) lets the keyboard go and does nothing else, as in Messages: the sky under it,
                // which did this before the day covered it, no longer gets the touch
                if ui.typing {
                    Color.clear.contentShape(Rectangle())
                        .gesture(DragGesture(minimumDistance: 0).onEnded { _ in engine.endEditing?() })
                        .accessibilityHidden(true)
                }
            }
            .frame(width: W, height: H, alignment: .topLeading)
            .onChange(of: context.date) { _, _ in tl.rollOver() }          // the day may have turned
            .onChange(of: hasDay(shown), initial: true) { _, has in release(has) }
            .onChange(of: tl.enter) { _, _ in release(hasDay(shown)) }
            .onChange(of: ui.tlHome) { _, _ in release(hasDay(shown)) }
            .onChange(of: tl.focus) { _, id in if let id { tl.openCard = id; tl.focus = nil } }
            .onChange(of: tl.day) { _, _ in engine.releaseTalk() }          // another day: her answer about this one has been seen
        }
        .opacity(on ? 1 : 0)
        .allowsHitTesting(on)
        .animation(.easeOut(duration: on ? 0.35 : 0.15), value: on)         // out quickly: she is about to glide down across it
        .sheet(item: Bindable(tl).sayWhat) { TLSayWhatSheet(tl: tl, s: $0) }
    }

    // MARK: what there is to show

    private func shownNow() -> Shown {
        #if DEBUG
        if tl.fixture != nil {
            if tl.fixture?.loading == true { return .loading }
            guard let r = tl.reply else { return .sentence("这一天没有记下什么。") }
            return .model(tl.built(r))
        }
        #endif
        let link = BrainLink.shared
        guard link.hasToken else { return .sentence("还没连上大脑，连上以后这里会排出你的一天。") }
        guard let r = tl.reply else { return link.errorMessage.map { .sentence($0) } ?? .loading }
        let b = tl.built(r)
        return b.model.isEmpty ? .sentence("这一天没有记下什么。") : .model(b)
    }
    private func isReady(_ s: Shown) -> Bool { if case .loading = s { return false }; return true }
    /// the day is there with a bar to grow: a sentence in its place (no brain yet, nothing recorded) has none, and the day that comes after it grows in as the first one does
    private func hasDay(_ s: Shown) -> Bool { if case .model = s { return true }; return false }
    /// once the day is there and she is in her corner, the bar grows in and the first rows rise (on the next turn, so they have been drawn as they were before)
    private func release(_ ready: Bool) {
        guard ready, ui.tlHome, !tl.reveal else { return }
        DispatchQueue.main.async { tl.reveal = true }
    }
    private var room: Double { TLLayout.room(pinned: pinnedHeight) }
    private var pinnedHeight: Double { Waiting.shared.cards.isEmpty ? 0 : min(tl.waitH > 0 ? tl.waitH : Double(Waiting.shared.cards.count) * TLMetrics.rowDetail, TLLayout.waitingMax) }

    /// From the bar down the sky is covered by black, none at the bar and 0.8 by 24 pt into the list, so the record sits on near-black as it does on the Mac (the stars nearly gone
    /// under the list) while the header, her, the date, her sentence and the bar keep the sky; it goes as it came at the foot of the list.
    private func scrim() -> some View {
        let top = TLLayout.barY, h = TLLayout.listBottom + TLLayout.fade - top
        return LinearGradient(stops: TLLayout.scrim().map { .init(color: .black.opacity($0.alpha), location: ($0.y - top) / h) }, startPoint: .top, endPoint: .bottom)
            .frame(width: W, height: h).offset(y: top)
            .modifier(HistoryOver(ui: ui, pull: $pull, tuning: true))
            .allowsHitTesting(false)
    }

    /// a rename he can take back, for a few seconds, over the bar at the foot
    private func undoToast(away: Bool) -> some View {
        ZStack {
            if let u = tl.undo, !away { TLUndoToast(undo: u) { tl.undoRename() }.transition(.move(edge: .bottom).combined(with: .opacity)) }
        }
        .frame(width: W, height: 48)
        .offset(y: HomeBar.top - 56)
    }

    // MARK: the list, and what waits above it

    private func listArea(_ shown: Shown, away: Bool) -> some View {
        VStack(spacing: 0) {
            waiting
            ZStack(alignment: .top) {
                page(shown).id(tl.day).transition(slideTransition)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            // a swipe along the page is a day before or after, whatever the page is (a list, a sentence, or nothing yet), so no day is a dead end; a drag up and down is the list's own
            .contentShape(Rectangle())
            .simultaneousGesture(DragGesture(minimumDistance: 24).onEnded { v in
                if tl.openRow == nil, let d = TimelineState.swipe(dx: v.translation.width, dy: v.translation.height, predicted: v.predictedEndTranslation.width) { tl.swipe(d) }          // not over a row's page, whose edge swipe is the way back
            })
        }
        .frame(width: W, height: TLLayout.viewport + TLLayout.fade, alignment: .top)
        .modifier(FootFade(ui: ui, head: Waiting.shared.cards.isEmpty ? 0 : pinnedHeight + 4))
        // the history takes the place of the list as it comes up: the list goes up and out with it, and comes back when the history has gone
        .modifier(HistoryOver(ui: ui, pull: $pull, tuning: true, lift: 6))
        .allowsHitTesting(!away)
        .offset(y: TLLayout.listTop)
    }

    private var slideTransition: AnyTransition { TLMotion.page(slide: TLMotion.daySlide * Double(tl.slide), reduce: reduce) }

    /// What the page of a day is: nothing yet, a sentence, or the list. The day's data arriving (or its sentence) is not a pop: it comes in as the first rows of a day do, a fade from 6 pt below.
    @ViewBuilder private func page(_ shown: Shown) -> some View {
        Group {
            switch shown {
            case .loading: Color.clear
            case .sentence(let s):
                Text(s).font(.system(size: 15)).lineSpacing(4).foregroundStyle(muted)
                    .frame(width: TLLayout.barW, alignment: .leading).padding(.top, 28).padding(.horizontal, 30)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .transition(arrival)
            case .model(let b):
                TLPager(model: b.model, engine: engine, tl: tl, room: room, lens: tl.activeLens(in: b.model), nowInView: $nowInView)
                    .transition(arrival)
            }
        }
        .animation(TLMotion.dayChange(reduce: reduce), value: shown.kind)
    }

    /// not the first day of the timeline's opening, whose rows rise by themselves once the bar has grown (`tl.reveal`)
    private var arrival: AnyTransition { !tl.reveal ? .identity : reduce ? .opacity : .opacity.combined(with: .offset(y: TLMotion.riseOffset)) }

    /// what waits on him: amber rows pinned over the list, each opening into its card; as tall as they are, up to a limit, then they scroll
    @ViewBuilder private var waiting: some View {
        let cards = Waiting.shared.cards
        if !cards.isEmpty {
            ScrollView(showsIndicators: false) {
                VStack(spacing: 0) {
                    ForEach(cards) { c in
                        TLWaitingRow(card: c, open: tl.openCard == c.id) {
                            withAnimation(reduce ? .easeOut(duration: TLMotion.reducedFade) : .spring(response: 0.35, dampingFraction: 0.86)) { tl.openCard = tl.openCard == c.id ? nil : c.id }
                        }
                    }
                }
                .padding(.horizontal, 30)
                .onGeometryChange(for: Double.self) { Double($0.size.height) } action: { h in
                    withAnimation(reduce ? nil : .spring(response: 0.35, dampingFraction: 0.86)) { tl.waitH = h }
                }
            }
            .scrollBounceBehavior(.basedOnSize)
            .frame(height: pinnedHeight)
            .overlay(alignment: .bottom) { Rectangle().fill(hair).frame(height: 0.5) }
            .padding(.bottom, 4)
        }
    }
}

/// What the history's rising does to what is under it: it fades (and the list goes up `lift` pt) as the history comes up. While a finger holds the history's top edge it follows it, a frame at
/// a time with no animation of its own; let go, it goes on as the history does (`panelSpring`, at the finger's speed). `tuning`: the near view's panel is as open as the history.
private struct HistoryOver: ViewModifier {
    let ui: StarUI
    @Binding var pull: Double?
    var tuning = false
    var lift = 0.0
    @Environment(\.accessibilityReduceMotion) private var reduce

    func body(content: Content) -> some View {
        let open = max(pull != nil ? panelOpenness(panelTop(ui, pull)) : ui.panelOpen ? 1 : 0, tuning && ui.tuning ? 1 : 0)
        content.opacity(1 - open).offset(y: reduce ? 0 : -lift * open)
            .animation(pull != nil ? nil : panelSpring(reduce, velocity: ui.panelRelease), value: open)
    }
}

/// The one mask of the list's area: its top fades in (12 pt, under the cards of what waits, `head` pt) and its foot fades out at the foot of the screen, or over the composer while it is out, and goes up
/// with it when the keyboard (or what he is sending) raises it: no row shows through the field. A modifier of its own, so that the keyboard rising draws this and not the whole screen.
private struct FootFade: ViewModifier {
    let ui: StarUI
    let head: Double

    func body(content: Content) -> some View {
        let field = ui.composerOut ? TLLayout.composerRoom : 0
        content.mask(alignment: .top) {
            VStack(spacing: 0) {
                if head > 0 { Color.black.frame(height: head) }
                LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: TLLayout.fadeTop)
                Color.black.frame(height: max(0, TLLayout.viewport - field - ui.kbLift - ui.attach.rise - head - TLLayout.fadeTop))          // (a keyboard so high that the two fades meet puts the foot's under the top's)
                LinearGradient(colors: [.black, .clear], startPoint: .top, endPoint: .bottom).frame(height: TLLayout.fade)
                Color.clear
            }
        }
    }
}

/// The way back, in the look of the one under the dial, on the row of the pills at the right: on today 回到现在 when the line for now is out of view, on another day 回到今天. Now arriving is
/// a firm tap of the phone.
private struct TLNowPill: View {
    let tl: TimelineState
    @Binding var nowInView: Bool
    let away: Bool
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        let today = tl.day == tl.today
        let show = (!today || !nowInView) && !away
        Button { Haptic.impact(.rigid); tl.goToday() } label: {
            TLBackLabel(text: today ? Words.Home.backToNow : Words.Home.backToToday).id(today).transition(TLMotion.swapWords(reduce: reduce))
                .contentShape(Rectangle().inset(by: -9))
        }
        .buttonStyle(PressScale())
        .frame(width: W - 60, alignment: .trailing)
        .opacity(show ? 1 : 0).scaleEffect(show || reduce ? 1 : 0.92).allowsHitTesting(show)
        .animation(.easeOut(duration: 0.25), value: show)
        .offset(x: 30, y: TLLayout.pillsY + 2)
    }
}

// MARK: - the list, and the page of a row over it

/// The list, and over it the page of the row he opened (Sky/DayTimelineDetail.swift): it comes in from the right while the list goes a little to the left and fades. A swipe to the right from
/// anywhere on the page takes it back the way a pushed page goes back on iOS: the page follows the finger the whole way (back past where it began too: it stops at its place, it never sticks),
/// the list comes out from under it at a third of the finger's pace, and on release the page goes on at the finger's own speed. The list stays where it is under the page, so the way back is to
/// the same place in it.
private struct TLPager: View {
    let model: DayTimelineModel
    let engine: StarEngine
    let tl: TimelineState
    let room: Double
    let lens: TLLens?
    @Binding var nowInView: Bool
    @State private var releasing = false        // a swipe let go and its page is on its way, out or back in
    @GestureState private var sideways: Bool?   // what a drag on the page is, decided once as it starts: the way back (true), or the page's own scroll (false); none between drags
    @State private var turn: Edge?              // the edge the page of a step comes in from: below for the next row, above for the one before (none: a turn from the bar, a cross-fade)
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var swipe: TLSwipe { tl.swipe }

    /// This body does not read the swipe: a finger moving the page draws `Swiped` (below) and not the pager, with its list, its page and their closures, on every frame.
    var body: some View {
        let page = tl.openRow.flatMap { id in model.entries.first { $0.id == id } }
        ZStack(alignment: .top) {
            TLList(model: model, engine: engine, tl: tl, room: room, lens: lens, nowInView: $nowInView)
                .modifier(Swiped(swipe: swipe, list: true, open: page != nil, reduce: reduce))
                .allowsHitTesting(page == nil).accessibilityHidden(page != nil)
            if let page {
                // a step to the row before or after pushes only the page's body up or down under its fixed head; a turn from the bar is a cross-fade in the same place
                TLDetailPage(e: page, tl: tl, engine: engine, back: goBack, step: { step($0, from: page) }, steps: steps(from: page), turn: turn)
                    .modifier(Swiped(swipe: swipe, list: false, open: true, reduce: reduce))
                    .transition(reduce ? .opacity : .modifier(active: Crossing(back: 1, list: false, reduce: false), identity: Crossing(back: 0, list: false, reduce: false)))
                    .simultaneousGesture(swipeBack)
            }
        }
        .animation(TLMotion.detail(reduce: reduce), value: tl.openRow)
        .onChange(of: tl.openRow) { _, id in
            if let r = tl.renaming, r != id { tl.cancelRename() }          // the page with its field open went: the field goes with it
            if !releasing { swipe.drag = 0 }
        }
        // a swipe the system took away (the page's scroll won it, a call came in) ends with no release: the page goes back to its place
        .onChange(of: sideways) { _, s in if s == nil, !releasing, swipe.drag > 0 { withAnimation(TLMotion.detail(reduce: reduce)) { swipe.drag = 0 } } }
    }

    private func goBack() { tl.openRow = nil }

    /// the rows that open, in the list's order: what a step goes through
    private var opening: [TLEntry] { model.entries.filter(\.opens) }

    private func steps(from e: TLEntry) -> (up: Bool, down: Bool) {
        guard let i = opening.firstIndex(where: { $0.id == e.id }) else { return (false, false) }
        return (i > 0, i < opening.count - 1)
    }

    private func step(_ d: Int, from e: TLEntry) {
        let rows = opening
        guard let i = rows.firstIndex(where: { $0.id == e.id }), rows.indices.contains(i + d) else { return }
        Haptic.selection()
        turn = d > 0 ? .bottom : .top          // the way is set first and the row changes on the next turn, so the body that leaves goes by the same way as the one that comes
        DispatchQueue.main.async { withAnimation(TLMotion.step(reduce: reduce)) { tl.openRow = rows[i + d].id } completion: { turn = nil } }
    }

    private static let slop = 10.0

    private var swipeBack: some Gesture {
        DragGesture(minimumDistance: Self.slop, coordinateSpace: .named("design"))
            .updating($sideways) { v, s, _ in if s == nil { s = TimelineState.backSwipeStarts(dx: v.translation.width, dy: v.translation.height) } }
            .onChanged { v in
                guard sideways ?? TimelineState.backSwipeStarts(dx: v.translation.width, dy: v.translation.height), !reduce else { return }
                if releasing { releasing = false }
                swipe.drag = max(0, v.translation.width - Self.slop)          // the gesture fires 10 pt in: start from there, or the page jumps by that on its first frame
            }
            .onEnded { v in
                if reduce {          // under Reduce Motion the page does not follow: only where the swipe lets go counts
                    if TimelineState.backSwipe(dx: v.translation.width, dy: v.translation.height, predicted: v.predictedEndTranslation.width) { withAnimation(TLMotion.detail(reduce: true)) { goBack() } }
                    return
                }
                let drag = swipe.drag
                guard drag > 0 else { return }          // not a swipe back: the page's own scroll
                let goes = TimelineState.backSwipe(dx: v.translation.width, dy: 0, predicted: v.predictedEndTranslation.width)
                releasing = true
                let to = goes ? W : 0
                let speed = abs(to - drag) > 1 ? v.velocity.width / (to - drag) : 0          // the finger's own speed, in parts of the way left per second
                withAnimation(TLMotion.release(speed: speed)) { swipe.drag = to } completion: {
                    var t = Transaction(); t.disablesAnimations = true
                    withTransaction(t) { if goes { goBack() }; swipe.drag = 0 }
                    releasing = false
                }
            }
    }
}

/// How far a swipe back has the page to the right. It is an object of its own and not state of the pager, so that the pager's body, which has the list, the page and the closures of both,
/// is not run again on every frame of a finger.
@Observable final class TLSwipe { var drag = 0.0 }

/// The list and the page as the crossing has them, `back` being 0 with the page all the way in and 1 with the list. It is an animated value, so that the crossing goes through the same curve
/// (`TLMotion.listShows`, `pageShows`) under a finger, on its release and when the page comes or goes by a tap, as the bar's way back; the list at a third of the finger's pace.
private struct Crossing: ViewModifier, Animatable {
    var back: Double
    let list: Bool
    let reduce: Bool
    var animatableData: Double {
        get { back }
        set { back = newValue }
    }
    func body(content: Content) -> some View {
        if list {
            content.offset(x: reduce ? 0 : -TLMotion.detailShift * (1 - back)).opacity(TLMotion.listShows(back))
        } else {
            content.offset(x: back * W).opacity(TLMotion.pageShows(back))
        }
    }
}

/// `Crossing` at the place of the swipe: the list with no page open is all the way in (1)
private struct Swiped: ViewModifier {
    let swipe: TLSwipe
    let list: Bool
    let open: Bool
    let reduce: Bool
    func body(content: Content) -> some View {
        content.modifier(Crossing(back: open ? clamp(swipe.drag / W, 0, 1) : 1, list: list, reduce: reduce))
    }
}

struct TLList: View {
    let model: DayTimelineModel
    let engine: StarEngine
    let tl: TimelineState
    let room: Double                    // how tall the list is under the pinned cards
    let lens: TLLens?                   // the kind brought forward: the rest is dimmed
    @Binding var nowInView: Bool
    @State private var pos: ScrollPosition
    @State private var opened: Double   // the room it was made for, which the first rows' entry is laid out for
    @State private var at = ScrollAt()  // where the list is now (written as it moves, never drawn from)
    @Environment(\.accessibilityReduceMotion) private var reduce

    init(model: DayTimelineModel, engine: StarEngine, tl: TimelineState, room: Double, lens: TLLens?, nowInView: Binding<Bool>) {
        self.model = model; self.engine = engine; self.tl = tl; self.room = room; self.lens = lens; _nowInView = nowInView
        _pos = State(initialValue: ScrollPosition(y: model.restY(room: room)))
        _opened = State(initialValue: room)
    }

    /// the first ten rows in view when it opens rise in one after another; the rest are simply there
    private func ranks(_ offs: [Double]) -> [String: Int] {
        let y0 = model.restY(room: opened)
        var out: [String: Int] = [:]
        for (i, e) in model.entries.enumerated() where offs[i] + e.height > y0 && offs[i] < y0 + opened && out.count < TLMotion.riseCount { out[e.id] = out.count }
        return out
    }

    var body: some View {
        let offs = model.offsets, ranks = ranks(offs)
        let nowTop = model.nowIndex.map { offs[$0] }
        ScrollView(showsIndicators: false) {
            VStack(spacing: 0) {
                ForEach(Array(model.entries.enumerated()), id: \.element.id) { i, e in
                    TLRowView(e: e, dim: dim(e), top: offs[i], room: room, tl: tl, engine: engine, tap: { open(e) })
                        .equatable()
                        .modifier(Rise(rank: ranks[e.id], on: tl.reveal, reduce: reduce))
                        .transition(reduce ? .opacity : TLGrow.transition)          // what is new grows in, and what he says is not his collapses out the same way
                }
                if let n = model.missing {
                    Text(n).font(.system(size: 12.5)).foregroundStyle(faint).frame(maxWidth: .infinity, minHeight: TLMetrics.note, alignment: .leading)
                }
            }
            .padding(.horizontal, 30)
            .animation(TLMotion.listChange(reduce: reduce), value: model.entries.map(\.id))
        }
        // room under the last row, so the line for now can be brought up to 60% of the way down on any day
        .contentMargins(.bottom, 0.4 * room + 40, for: .scrollContent)
        .scrollPosition($pos)
        .onScrollGeometryChange(for: Bool.self) { g in
            guard let ny = nowTop else { return true }
            return ny < g.visibleRect.maxY && ny + TLMetrics.now > g.visibleRect.minY
        } action: { _, v in nowInView = v }
        .onScrollGeometryChange(for: Double.self) { Double($0.contentOffset.y) } action: { _, y in at.y = y }
        .onScrollPhaseChange { _, phase in if phase == .interacting { engine.releaseTalk() } }          // he is at the list: the talk's place has said what it had to
        .modifier(ScrollsOnRequest(tl: tl, model: model, room: room, pos: $pos, at: at))
        .onChange(of: model.entries.map(\.id)) { _, ids in tl.keep(only: Set(ids)) }
        .onAppear { nowInView = true }
    }

    /// under a lens the rows of that kind stay as they are, a place's header goes to 0.6 so the day keeps its shape, the rest to 0.35
    private func dim(_ e: TLEntry) -> Double {
        guard let lens else { return 1 }
        return e.lens == lens || e.lenses.contains(lens) || e.kind == .now ? 1 : e.kind == .stay ? 0.6 : 0.35
    }

    /// A tap on a row opens its page, with a light tick of the phone; the page slides in over the list (`TLPager`)
    private func open(_ e: TLEntry) {
        guard e.opens else { return }
        Haptic.selection()
        engine.releaseTalk()
        if let top = model.top(of: e.id) { engine.glance(at: TLGaze.row(top: top + e.gap, height: e.base, scrolled: at.y)) }        // her eyes go to it once
        tl.openRow = e.id
    }
}

/// where the list is, kept where nothing is drawn from it
private final class ScrollAt { var y = 0.0 }

/// The list goes where it is asked to (`tl.request`): to a place, or with a place under 0 to where now rests (the line for now 60% of the way down what is left under the pinned cards). It is a
/// modifier of its own so that a request, which a finger on the bar makes on nearly every frame, draws this and not the list.
private struct ScrollsOnRequest: ViewModifier {
    let tl: TimelineState
    let model: DayTimelineModel
    let room: Double
    @Binding var pos: ScrollPosition
    let at: ScrollAt                    // where the list is now
    @Environment(\.accessibilityReduceMotion) private var reduce

    func body(content: Content) -> some View {
        content.onChange(of: tl.request) { _, r in
            let y = r.y < 0 ? model.restY(room: room) : r.y
            if r.animated || r.y < 0 { withAnimation(TLMotion.scrollTo(distance: abs(y - at.y))) { pos.scrollTo(y: y) } }
            else if r.glide && !reduce { withAnimation(TLMotion.follow) { pos.scrollTo(y: y) } }
            else { pos.scrollTo(y: y) }
        }
    }
}

/// a row rising in: from 6 pt below over 220 ms, one after another 25 ms apart; with Reduce Motion a plain fade
private struct Rise: ViewModifier {
    let rank: Int?
    let on: Bool
    let reduce: Bool
    func body(content: Content) -> some View {
        let shown = rank == nil || on
        content
            .opacity(shown ? 1 : 0)
            .offset(y: shown || reduce ? 0 : TLMotion.riseOffset)
            .animation(rank.map { TLMotion.rise(rank: $0, reduce: reduce) }, value: on)
    }
}

// MARK: - what waits on him

private struct TLWaitingRow: View {
    let card: WaitingCard
    let open: Bool
    let toggle: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 0) {
                Text("等你").font(tlFigures(12, weight: .semibold)).foregroundStyle(amber).frame(width: 40, height: 18, alignment: .leading)
                MarkShape(kd: .wait).frame(width: 16, height: 16).frame(width: 22, height: 18)
                VStack(alignment: .leading, spacing: 2) {
                    Text(card.title).font(.system(size: 15)).foregroundStyle(amber).lineLimit(1).frame(height: 18, alignment: .leading)
                    if !card.meta.isEmpty { Text(card.meta).font(.system(size: 12.5)).foregroundStyle(muted).lineLimit(1).frame(height: 15, alignment: .leading) }
                }.padding(.leading, 10)
                Spacer(minLength: 8)
                Image(systemName: "chevron.right").font(.system(size: 11, weight: .semibold)).foregroundStyle(muted)
                    .rotationEffect(.degrees(open ? 90 : 0)).frame(height: 18)
            }
            .padding(.top, 9).frame(height: card.meta.isEmpty ? TLMetrics.row : TLMetrics.rowDetail, alignment: .top)
            .contentShape(Rectangle())
            .onTapGesture(perform: toggle)
            if open {
                WaitingCardView(card: card).environment(\.cardCompact, true)
                    .padding(.leading, 72).padding(.bottom, 12).transition(.opacity.combined(with: .offset(y: -6)))
            }
        }
        .accessibilityElement(children: .contain)
    }
}
