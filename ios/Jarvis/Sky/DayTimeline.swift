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
    static let her = CGPoint(x: 47, y: 79)             // where she stands on the top row, and how big (34 → 15)
    static let herScale = 15.0 / R_PS
    static let dateShift = 39.0                        // the date starts this far further right, beside her: its first stroke 8 pt clear of her disc, which ends at 62 (47 + 15)
    static let lineY = 108.0, lineH = 40.0             // her sentence: two lines of 15 pt
    static let barY = 176.0, barW = 380.0, barH = 22.0, barRadius = 6.0       // 28 pt over it for now's time (and, under a finger, the time in its chip), 12 pt under it for the hours
    static let chipsY = 220.0, chipsH = 24.0, chipsHit = 36.0                    // the day's numbers: chips of 24 pt (the first 8 pt under the hours) in a row of 36 that takes the touch
    static let grabberY = 248.0, grabberH = 24.0       // the top of the grabber's hit area: its bar sits half way between the chips and the list
    static let listTop = 272.0, listBottom = 836.0, fade = 24.0, fadeTop = 12.0
    static var viewport: Double { listBottom - listTop }
    /// what is left of the list's height under `pinned` points of cards (and the gap under them)
    static func room(pinned: Double) -> Double { viewport - (pinned > 0 ? pinned + 4 : 0) }
    /// the pinned cards of what waits on him are never taller than this
    static let waitingMax = 260.0
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
    var openRow: String?                // the row opened in the list, one at a time
    var lens: TLLens?                   // the day's number he pressed: the bar and the list bring that kind forward
    var scrub: Double?                  // the minute under his finger on the overview bar
    var hot: String?                    // the row that covers it, lit in the list
    @ObservationIgnored private var seen: String?       // the row the list was last sent to while the finger is down
    @ObservationIgnored private var scrubHold: Task<Void, Never>?
    var openCard: String?               // the waiting card opened over the list
    var focus: String?                  // a waiting card the engine asks to bring up
    var version = 0                     // bumps when the brain's days were adopted again: talk topics may have come
    var request = ScrollRequest()       // where the list is asked to go
    var waitH = 0.0                     // how tall the pinned cards over the list are
    // what a row does when it is opened (Sky/DayTimelineActions.swift)
    var acked: [String: Date] = [:]                 // the reminders he has just said he knows, until when the row says so before the brain's next answer does (`ackHolds`)
    var renamed: [String: Renamed] = [:]            // the places he keeps that he has just renamed, by the place's id
    struct Renamed: Equatable { var name: String; var until: Date }
    var renaming: String?                           // the row whose place is being renamed: its field is open
    var renameBusy = false
    var renameNote: String?                         // why it did not go
    var undo: PlaceUndo?                            // the rename he can take back for a few seconds
    @ObservationIgnored var undoBusy = false        // taking it back is on its way to the brain
    var flash: String?                              // the row just scrolled to: it flashes
    var sunSaid = false                             // the weather in the header was just tapped: it says when the sun rose and set, in its place
    @ObservationIgnored var sunHide: Task<Void, Never>?
    struct ScrollRequest: Equatable { var y = 0.0, animated = false, n = 0 }
    @ObservationIgnored private var pending: Int?
    #if DEBUG
    struct Fixture { var replies: [Int: DayReply]; var now: Double }       // by days from today
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
    /// the day on show as the list has it
    func model(_ r: DayReply) -> DayTimelineModel {
        let now = Date()
        return DayTimelineModel.build(r, now: nowMinute(in: r), topic: { TalkTopics.shared.topic($0) }, acked: Set(acked.filter { $0.value > now }.keys),
                                      renamed: renamed.compactMapValues { $0.until > now ? $0.name : nil })
    }

    func scroll(to y: Double, animated: Bool) { request = ScrollRequest(y: y, animated: animated, n: request.n + 1) }

    /// A finger on the overview bar at minute `t` (none: it has lifted): the list goes to the row that covers it and lights it, and a row he had open is closed.
    func follow(_ t: Double?, in m: DayTimelineModel?) {
        scrub = t
        guard let t, let m else { hot = nil; seen = nil; return }
        if openRow != nil { openRow = nil }
        hot = m.lit(at: t)?.id
        guard let e = m.entry(at: t), e.id != seen else { return }        // the same row as a moment ago: the list is already there
        seen = e.id
        if let y = m.top(of: e.id) { scroll(to: y, animated: false) }
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
    func swipe(_ delta: Int) { setDay((pending ?? day) + delta) }

    /// A drag along the day: far enough and mostly sideways it is a day before (dragged right) or a day after (dragged left); a drag up and down is the list's own.
    static func swipe(dx: Double, dy: Double) -> Int? {
        guard abs(dx) > 60, abs(dx) > abs(dy) * 1.8 else { return nil }
        return dx > 0 ? -1 : 1
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

// MARK: - the screen

private enum Shown { case loading, sentence(String), model(DayTimelineModel) }

struct DayTimelineView: View {
    let engine: StarEngine
    let ui: StarUI
    @Binding var pull: Double?
    @Environment(\.accessibilityReduceMotion) private var reduce
    @State private var nowInView = true

    private var tl: TimelineState { ui.tl }

    var body: some View {
        let on = HomeModule.shared.isTimeline
        let away = ui.talking || ui.typing || ui.panelOpen || ui.tuning
        _ = tl.version
        return TimelineView(.everyMinute) { context in
            let shown = shownNow()
            let model: DayTimelineModel? = { if case .model(let m) = shown { return m } else { return nil } }()
            ZStack(alignment: .topLeading) {
                scrim(away: away)
                TLTopView(ui: ui, model: model, scene: model?.scene ?? .empty)
                    .opacity(ui.panelOpen ? 0 : 1).allowsHitTesting(!ui.panelOpen)
                    .animation(.easeOut(duration: ui.panelOpen ? 0.2 : 0.35), value: ui.panelOpen)
                listArea(shown, away: away)
                undoToast(away: away)
                grabber
                TLWaitPill(count: away ? 0 : model?.waiting.count ?? 0) { if let model { tl.revealWaiting(in: model) } }
                    .frame(width: W - 60, alignment: .leading).offset(x: 30, y: TLLayout.grabberY + 2)
                pill(show: tl.day == tl.today && !nowInView && !away)
            }
            .frame(width: W, height: H, alignment: .topLeading)
            .onChange(of: context.date) { _, _ in tl.rollOver() }          // the day may have turned
            .onChange(of: isReady(shown), initial: true) { _, ready in release(ready) }
            .onChange(of: tl.enter) { _, _ in release(isReady(shown)) }
            .onChange(of: ui.tlHome) { _, _ in release(isReady(shown)) }
            .onChange(of: tl.focus) { _, id in if let id { tl.openCard = id; tl.focus = nil } }
        }
        .opacity(on ? 1 : 0)
        .allowsHitTesting(on)
        .animation(.easeOut(duration: on ? 0.35 : 0.15), value: on)         // out quickly: she is about to glide down across it
    }

    // MARK: what there is to show

    private func shownNow() -> Shown {
        #if DEBUG
        if tl.fixture != nil {
            guard let r = tl.reply else { return .sentence("这一天没有记下什么。") }
            return .model(tl.model(r))
        }
        #endif
        let link = BrainLink.shared
        guard link.hasToken else { return .sentence("还没连上大脑，连上以后这里会排出你的一天。") }
        guard let r = tl.reply else { return link.errorMessage.map { .sentence($0) } ?? .loading }
        let m = tl.model(r)
        return m.isEmpty ? .sentence("这一天没有记下什么。") : .model(m)
    }
    private func isReady(_ s: Shown) -> Bool { if case .loading = s { return false }; return true }
    /// once the day is there and she is in her corner, the bar grows in and the first rows rise (on the next turn, so they have been drawn as they were before)
    private func release(_ ready: Bool) {
        guard ready, ui.tlHome, !tl.reveal else { return }
        DispatchQueue.main.async { tl.reveal = true }
    }
    private var room: Double { TLLayout.room(pinned: pinnedHeight) }
    private var pinnedHeight: Double { Waiting.shared.cards.isEmpty ? 0 : min(tl.waitH > 0 ? tl.waitH : Double(Waiting.shared.cards.count) * TLMetrics.rowDetail, TLLayout.waitingMax) }

    /// From the chips row down the sky is covered by black, none under the hours and 0.8 by the middle of the chips, so the record sits on near-black as it does on the Mac
    /// (the stars nearly gone under the list) while the header, her, the date, her sentence and the bar, keeps the sky; it goes as it came at the foot of the list.
    private func scrim(away: Bool) -> some View {
        let top = TLLayout.chipsY - 6, ramp = 22.0, h = TLLayout.listBottom + TLLayout.fade - top
        return LinearGradient(stops: [.init(color: .black.opacity(0), location: 0), .init(color: .black.opacity(0.8), location: ramp / h),
                                      .init(color: .black.opacity(0.8), location: 1 - TLLayout.fade / h), .init(color: .black.opacity(0), location: 1)],
                              startPoint: .top, endPoint: .bottom)
            .frame(width: W, height: h).offset(y: top)
            .opacity(away ? 0 : 1).animation(.easeOut(duration: away ? 0.2 : 0.35), value: away)
            .allowsHitTesting(false)
    }

    // MARK: the grabber and the way back

    /// the same grabber as under the dial, between the bar and the list
    private var grabber: some View {
        let hide = ui.typing || ui.panelOpen
        return Capsule().fill(Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.28)).frame(width: 36, height: 5)
            .frame(width: 120, height: TLLayout.grabberH)
            .contentShape(Rectangle())
            .gesture(panelDrag(engine, $pull, opens: true, tap: true))
            .opacity(hide ? 0 : 1)
            .allowsHitTesting(!hide)
            .animation(.easeOut(duration: 0.3), value: hide)
            .offset(x: (W - 120) / 2, y: TLLayout.grabberY)
            .accessibilityLabel("展开对话")
    }

    /// 回到现在, in the look of the one under the dial, on the grabber's row at the right: on today, when the line for now is out of view (another day has its way back in the header)
    private func pill(show: Bool) -> some View {
        Button { tl.goToday() } label: { TLBackLabel(text: "回到现在") }
        .buttonStyle(PressScale())
        .frame(width: W - 60, alignment: .trailing)
        .opacity(show ? 1 : 0).scaleEffect(show || reduce ? 1 : 0.92).allowsHitTesting(show)
        .animation(.easeOut(duration: 0.25), value: show)
        .offset(x: 30, y: TLLayout.grabberY + 2)
    }

    /// a rename he can take back, for a few seconds, over the foot of the list
    private func undoToast(away: Bool) -> some View {
        ZStack {
            if let u = tl.undo, !away { TLUndoToast(undo: u) { tl.undoRename() }.transition(.move(edge: .bottom).combined(with: .opacity)) }
        }
        .frame(width: W, height: 48)
        .offset(y: TLLayout.listBottom - 56)
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
                if let d = TimelineState.swipe(dx: v.translation.width, dy: v.translation.height) { tl.swipe(d) }
            })
            .mask(VStack(spacing: 0) {
                LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: TLLayout.fadeTop)
                Color.black
            })
        }
        .frame(width: W, height: TLLayout.viewport + TLLayout.fade, alignment: .top)
        .mask(VStack(spacing: 0) {
            Color.black
            LinearGradient(colors: [.black, .clear], startPoint: .top, endPoint: .bottom).frame(height: TLLayout.fade)
        })
        // a talk takes the place of the list, the way it takes the place of what is under the dial: it goes up and out, and comes back when the talk has gone
        .opacity(away ? 0 : 1).offset(y: away && !reduce ? -6 : 0)
        .allowsHitTesting(!away)
        .animation(.easeOut(duration: away ? 0.2 : 0.35), value: away)
        .offset(y: TLLayout.listTop)
    }

    private var slideTransition: AnyTransition {
        if reduce { return .opacity }
        let d = TLMotion.daySlide * Double(tl.slide)
        return .asymmetric(insertion: .opacity.combined(with: .offset(x: -d)), removal: .opacity.combined(with: .offset(x: d)))
    }

    @ViewBuilder private func page(_ shown: Shown) -> some View {
        switch shown {
        case .loading: Color.clear
        case .sentence(let s):
            Text(s).font(.system(size: 15)).lineSpacing(4).foregroundStyle(muted)
                .frame(width: TLLayout.barW, alignment: .leading).padding(.top, 28).padding(.horizontal, 30)
                .frame(maxWidth: .infinity, alignment: .leading)
        case .model(let m):
            TLList(model: m, engine: engine, tl: tl, room: room, lens: tl.activeLens(in: m), nowInView: $nowInView)
        }
    }

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

// MARK: - the list

private struct TLList: View {
    let model: DayTimelineModel
    let engine: StarEngine
    let tl: TimelineState
    let room: Double                    // how tall the list is under the pinned cards
    let lens: TLLens?                   // the kind brought forward: the rest is dimmed
    @Binding var nowInView: Bool
    @State private var pos: ScrollPosition
    @State private var opened: Double   // the room it was opened in, which the first rows' entry is laid out for
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
                    TLRowView(e: e, open: tl.openRow == e.id, dim: dim(e), top: offs[i], room: room, tl: tl, engine: engine,
                              tap: { toggle(e) }, opened: { ensureVisible(e, height: $0) })
                        .equatable()
                        .modifier(Rise(rank: ranks[e.id], on: tl.reveal, reduce: reduce))
                        .transition(reduce ? .opacity : .asymmetric(insertion: .opacity.combined(with: .scale(scale: 0.96, anchor: .top)), removal: .opacity))
                }
                if let n = model.missing {
                    Text(n).font(.system(size: 12.5)).foregroundStyle(faint).frame(maxWidth: .infinity, minHeight: TLMetrics.note, alignment: .leading)
                }
            }
            .padding(.horizontal, 30)
            .animation(.easeOut(duration: 0.35), value: model.entries.map(\.id))
        }
        // room under the last row, so the line for now can be brought up to 60% of the way down on any day
        .contentMargins(.bottom, 0.4 * room + 40, for: .scrollContent)
        .scrollPosition($pos)
        .onScrollGeometryChange(for: Bool.self) { g in
            guard let ny = nowTop else { return true }
            return ny < g.visibleRect.maxY && ny + TLMetrics.now > g.visibleRect.minY
        } action: { _, v in nowInView = v }
        .onScrollGeometryChange(for: Double.self) { Double($0.contentOffset.y) } action: { _, y in at.y = y }
        .modifier(ScrollsOnRequest(tl: tl, model: model, room: room, pos: $pos))
        // the field of a rename needs the keyboard, which covers the foot of the list: the row goes up under the header
        .onChange(of: tl.renaming) { _, id in
            guard let id, let y = model.top(of: id), let e = model.entries.first(where: { $0.id == id }) else { return }
            tl.scroll(to: max(0, y + e.gap - TLLayout.fadeTop - 6), animated: true)
        }
        .onChange(of: model.entries.map(\.id)) { _, ids in tl.keep(only: Set(ids)) }
        .onAppear { nowInView = true }
    }

    /// under a lens the rows of that kind stay as they are, a place's header goes to 0.6 so the day keeps its shape, the rest to 0.35
    private func dim(_ e: TLEntry) -> Double {
        guard let lens else { return 1 }
        return e.lens == lens || e.kind == .now ? 1 : e.kind == .stay ? 0.6 : 0.35
    }

    /// one row open at a time, with a light tap of the phone as it opens and a spring of about 260 ms (a plain fade with Reduce Motion)
    private func toggle(_ e: TLEntry) {
        guard e.opens else { return }
        let opening = tl.openRow != e.id
        if opening { Haptic.impact(.light) }
        withAnimation(reduce ? .easeOut(duration: TLMotion.reducedFade) : .spring(duration: 0.26, bounce: 0.08)) { tl.openRow = opening ? e.id : nil }
    }

    /// A row opened is as tall as it is (`h`, without the blank above it): when its foot is below the list, the list goes up as far as that takes, never so far that the row's own top is lost.
    private func ensureVisible(_ e: TLEntry, height h: Double) {
        guard tl.openRow == e.id, h > 0, let i = model.entries.firstIndex(where: { $0.id == e.id }) else { return }
        let top = model.offsets[i] + e.gap, bottom = top + h
        let want = min(bottom + 8 - room, top - TLLayout.fadeTop - 6)
        if want > at.y + 0.5 { tl.scroll(to: want, animated: true) }
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

    func body(content: Content) -> some View {
        content.onChange(of: tl.request) { _, r in
            let y = r.y < 0 ? model.restY(room: room) : r.y
            if r.animated || r.y < 0 { withAnimation(.easeOut(duration: 0.45)) { pos.scrollTo(y: y) } } else { pos.scrollTo(y: y) }
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
                Text("等你").font(AT.mono(12)).foregroundStyle(amber).frame(width: 40, height: 18, alignment: .leading)
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
