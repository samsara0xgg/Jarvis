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
    var openCard: String?               // the waiting card opened over the list
    var focus: String?                  // a waiting card the engine asks to bring up
    var version = 0                     // bumps when the brain's days were adopted again: talk topics may have come
    var request = ScrollRequest()       // where the list is asked to go
    var waitH = 0.0                     // how tall the pinned cards over the list are
    struct ScrollRequest: Equatable { var y = 0.0, animated = false, n = 0 }
    @ObservationIgnored private var pending: Int?
    #if DEBUG
    struct Fixture { var replies: [Int: DayReply]; var now: Double }       // by days from today
    var fixture: Fixture?               // made-up days and a fixed now (Sky/StarTimelineScene.swift)
    #endif

    var today: Int { floorDay(NOW1) }                   // the engine's own now: the phone's today, or the sample world's Thursday
    /// the minute of now on the day on show, if that is today
    func nowMinute() -> Double? {
        guard day == today else { return nil }
        #if DEBUG
        if let f = fixture { return f.now }
        #endif
        return NOW1 - Double(day) * DAY
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
        DayTimelineModel.build(r, day: day, now: nowMinute(), topic: { TalkTopics.shared.topic($0) })
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

    /// The timeline opens afresh: the bar and the rows are put back to before their entry, with no motion, and run it when the day is there.
    func open() {
        enter += 1
        var t = Transaction(); t.disablesAnimations = true
        withTransaction(t) { reveal = false }
    }

    /// Another day. The page's direction is set first and the page changes on the next turn, so the page that leaves slides the same way as the one that comes.
    func setDay(_ d: Int) {
        let from = pending ?? day
        guard d != from, d <= today else { return }
        pending = d; slide = d < from ? 1 : -1; openRow = nil
        DispatchQueue.main.async { [self] in
            pending = nil
            withAnimation(.easeOut(duration: 0.35)) { day = d }
        }
    }
    func swipe(_ delta: Int) { setDay((pending ?? day) + delta) }

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
    @GestureState private var touching = false                                       // a finger is on the bar
    @State private var lastScrub = 0.0                                               // where his finger was last: what the readout shows while it fades out
    @State private var lastStretch: String?
    @State private var lastLens = TLLens.trip                                        // the lens that was on last: what fades out when it goes
    @State private var nowInView = true

    private var tl: TimelineState { ui.tl }

    var body: some View {
        let on = HomeModule.shared.isTimeline
        let away = ui.talking || ui.typing || ui.panelOpen || ui.tuning
        _ = tl.version
        return TimelineView(.everyMinute) { _ in
            let shown = shownNow()
            let model: DayTimelineModel? = { if case .model(let m) = shown { return m } else { return nil } }()
            ZStack(alignment: .topLeading) {
                scrim(away: away)
                header(model)
                    .opacity(ui.panelOpen ? 0 : 1).allowsHitTesting(!ui.panelOpen)
                    .animation(.easeOut(duration: ui.panelOpen ? 0.2 : 0.35), value: ui.panelOpen)
                listArea(shown, away: away)
                grabber
                pill(show: (tl.day != tl.today || !nowInView) && !away)
            }
            .frame(width: W, height: H, alignment: .topLeading)
            .onChange(of: isReady(shown), initial: true) { _, ready in release(ready) }
            .onChange(of: tl.enter) { _, _ in release(isReady(shown)) }
            .onChange(of: ui.tlHome) { _, _ in release(isReady(shown)) }
            .onChange(of: tl.request) { _, r in if r.y < 0, let m = model { tl.scroll(to: restY(m), animated: true) } }
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
    /// where the list rests on today: the line for now 60% of the way down what is left under the pinned cards
    private func restY(_ m: DayTimelineModel) -> Double { m.restY(room: room) }
    private var room: Double { TLLayout.room(pinned: pinnedHeight) }
    private var pinnedHeight: Double { Waiting.shared.cards.isEmpty ? 0 : min(tl.waitH > 0 ? tl.waitH : Double(Waiting.shared.cards.count) * TLMetrics.rowDetail, TLLayout.waitingMax) }

    // MARK: her sentence and the overview bar

    private func header(_ m: DayTimelineModel?) -> some View {
        let holding = tl.scrub != nil
        return ZStack(alignment: .topLeading) {
            // her sentence, and in its place while a finger is on the bar the readout of the minute under it: a fade-through, not a cross-fade (the two are never both on show)
            if let line = m?.line {
                Text(line).font(.system(size: 15)).lineSpacing(3).foregroundStyle(ink.opacity(0.9)).lineLimit(2)
                    .frame(width: TLLayout.barW, height: TLLayout.lineH, alignment: .topLeading)
                    .fadeThrough(on: !holding, reduce: reduce)
                    .id(line).transition(.opacity)
                    .offset(x: 30, y: TLLayout.lineY)
            }
            if let m {
                readout(m.readout(at: tl.scrub ?? lastScrub)).fadeThrough(on: holding, reduce: reduce)
                    .offset(x: 30, y: TLLayout.lineY)
                    .allowsHitTesting(false).accessibilityHidden(!holding)
            }
            if let m {
                let lens = activeLens(m)
                TLBarView(scene: m.scene, bracket: bracket(m), scrub: tl.scrub, lens: lens != nil, lensColor: (lens ?? lastLens).color, lensMarks: m.marks(of: lens ?? lastLens),
                          reveal: tl.reveal, reduce: reduce)
                    .animation(holding ? .easeOut(duration: 0.08) : .easeInOut(duration: 0.15), value: holding)       // the hairline and its time come and go with the finger
                    .gesture(scrubGesture(m))
                    .accessibilityElement().accessibilityLabel("一天的概览，拖动可以翻到那个时间")
                    .id(tl.day).transition(barTransition)
                    .offset(x: 30, y: TLLayout.barY)
                if !m.chips.isEmpty {
                    TLChips(chips: m.chips, active: lens) { l in
                        Haptic.selection()
                        withAnimation(.easeOut(duration: 0.15)) { tl.lens = tl.lens == l ? nil : l }
                    }
                    .id(tl.day).transition(barTransition)
                    .offset(y: TLLayout.chipsY - (TLLayout.chipsHit - TLLayout.chipsH) / 2)
                }
            }
        }
        .onChange(of: tl.lens) { _, l in if let l { lastLens = l } }
        .onChange(of: tl.scrub) { _, t in if let t { lastScrub = t } }
        .onChange(of: touching) { _, on in
            guard !on else { return }
            lastStretch = nil
            withAnimation(.easeOut(duration: 0.2)) { tl.follow(nil, in: nil) }
        }
        .animation(.easeOut(duration: 0.35), value: m?.line)
        // only once she is in her corner, and gone as soon as she leaves it: she glides across where they stand
        .opacity(ui.tlHome ? 1 : 0).allowsHitTesting(ui.tlHome)
        .animation(.easeOut(duration: ui.tlHome ? (reduce ? 0.2 : 0.3) : 0.06), value: ui.tlHome)
    }

    /// the minute under his finger in words, where her sentence is: the stretch with its hours and length, under it what else was going on
    private func readout(_ r: TLReadout) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: 7) {
                Circle().fill((r.hue ?? RGB(196, 204, 238)).color(r.hue == nil ? 0 : r.strong ? 1 : 0.5)).frame(width: 8, height: 8)
                    .overlay(Circle().stroke(ink.opacity(r.hue == nil ? 0.4 : 0), lineWidth: 1))
                HStack(spacing: 0) {
                    Text(r.name).lineLimit(1)
                    Text(r.tail).fixedSize().layoutPriority(1)
                }
                .font(.system(size: 15, weight: .semibold)).foregroundStyle(ink)
            }
            .frame(height: 19, alignment: .leading)
            Text(r.second).font(.system(size: 13)).foregroundStyle(muted).lineLimit(1).frame(height: 15, alignment: .leading)
        }
        .frame(width: TLLayout.barW, height: TLLayout.lineH, alignment: .topLeading)
        .contentTransition(.identity)           // the words follow the finger at once: a change of them is not a fade of its own, which would show the last stretch's words through the new ones
    }

    /// the lens that is on, if this day has that kind at all
    private func activeLens(_ m: DayTimelineModel) -> TLLens? {
        tl.lens.flatMap { l in m.chips.contains { $0.lens == l } ? l : nil }
    }

    /// the stretch under his finger, else the span of the row he has open, on the bar
    private func bracket(_ m: DayTimelineModel) -> ClosedRange<Double>? {
        if let t = tl.scrub { return m.seg(at: t).map { $0.t0...$0.t1 } }
        guard let id = tl.openRow, let e = m.entries.first(where: { $0.id == id }), e.kind != .now else { return nil }
        return e.t0...max(e.t0, e.t1 ?? e.t0)
    }

    /// a finger on the bar: the list follows it, with a tick of the phone for each stretch it crosses
    private func scrubGesture(_ m: DayTimelineModel) -> some Gesture {
        let lo = m.span.lowerBound, w = m.span.upperBound - lo
        return DragGesture(minimumDistance: 0)
            .updating($touching) { _, on, _ in on = true }          // goes back to false when the touch ends or is taken away (a call, the app leaving), which lets the readout go
            .onChanged { v in
                let t = lo + clamp(v.location.x / TLLayout.barW, 0, 1) * w
                tl.follow(t, in: m)
                let s = m.stretch(at: t)
                if lastStretch != s { lastStretch = s; Haptic.selection() }
            }
    }

    /// another day's bar comes in from the way time goes, a hair, as the list does
    private var barTransition: AnyTransition {
        if reduce { return .opacity }
        let d = 8.0 * Double(tl.slide)
        return .asymmetric(insertion: .opacity.combined(with: .offset(x: -d)), removal: .opacity.combined(with: .offset(x: d)))
    }

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

    /// 回到现在, in the look of the one under the dial, on the grabber's row at the right
    private func pill(show: Bool) -> some View {
        Button { tl.goToday() } label: {
            HStack(spacing: 6) {
                Circle().fill(Color(red: 1, green: 243 / 255, blue: 214 / 255)).frame(width: 6, height: 6)
                    .shadow(color: Color(red: 1, green: 228 / 255, blue: 170 / 255).opacity(0.7), radius: 4)
                Text("回到现在").font(.system(size: 12.5, weight: .semibold)).foregroundStyle(ink)
            }
            .padding(.leading, 10).padding(.trailing, 12).frame(height: 26).background(Color.white.opacity(0.1), in: Capsule())
        }
        .buttonStyle(PressScale())
        .frame(width: W - 60, alignment: .trailing)
        .opacity(show ? 1 : 0).scaleEffect(show || reduce ? 1 : 0.92).allowsHitTesting(show)
        .animation(.easeOut(duration: 0.25), value: show)
        .offset(x: 30, y: TLLayout.grabberY + 2)
    }

    // MARK: the list, and what waits above it

    private func listArea(_ shown: Shown, away: Bool) -> some View {
        VStack(spacing: 0) {
            waiting
            ZStack(alignment: .top) {
                page(shown).id(tl.day).transition(slideTransition)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
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
        let d = 24.0 * Double(tl.slide)
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
            TLList(model: m, engine: engine, tl: tl, room: room, lens: activeLens(m), nowInView: $nowInView)
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
                            withAnimation(reduce ? .easeOut(duration: 0.2) : .spring(response: 0.35, dampingFraction: 0.86)) { tl.openCard = tl.openCard == c.id ? nil : c.id }
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

/// The change of what the header says as a fade-through: what goes out leaves over 50 ms (ease-in) and what comes in arrives over 90 ms from 40 ms on (ease-out, 2 pt up
/// into its place), so the two are not on show together for more than a frame. With Reduce Motion a plain 120 ms fade.
private extension View {
    func fadeThrough(on: Bool, reduce: Bool) -> some View {
        let enter = reduce ? Animation.easeOut(duration: 0.12) : .easeOut(duration: 0.09).delay(0.04), leave = reduce ? Animation.easeOut(duration: 0.12) : .easeIn(duration: 0.05)
        return opacity(on ? 1 : 0).animation(on ? enter : leave, value: on)
            .offset(y: on || reduce ? 0 : 2).animation(on ? enter : nil, value: on)
    }
}

// MARK: - the day's numbers

/// The row of the day's numbers under the hours: a chip for each, pressing one brings that kind forward (and pressing it again lets it go). Sideways when there are more than fit.
private struct TLChips: View {
    let chips: [TLChip]
    let active: TLLens?
    let toggle: (TLLens) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(chips) { c in chip(c) }
            }
            .padding(.horizontal, 30)
            .frame(height: TLLayout.chipsHit)
        }
        .frame(width: W, height: TLLayout.chipsHit)
        // a chip going off the side goes softly
        .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 24 / W), .init(color: .black, location: 1 - 24 / W), .init(color: .clear, location: 1)],
                             startPoint: .leading, endPoint: .trailing))
    }

    private func chip(_ c: TLChip) -> some View {
        let on = active == c.lens
        return Button { toggle(c.lens) } label: {
            HStack(spacing: 5) {
                Image(systemName: c.lens.symbol).font(.system(size: 11, weight: .semibold)).foregroundStyle(c.lens.color.color).frame(width: 12)
                Text(c.word).font(.system(size: 11.5)).foregroundStyle(ink.opacity(0.86))
                Text(c.value).font(AT.mono(11.5)).foregroundStyle(ink)
            }
            .fixedSize()
            .padding(.horizontal, 9).frame(height: TLLayout.chipsH)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(on ? 0.13 : 0.06)))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(ink.opacity(on ? 0.6 : 0), lineWidth: 1))
            .animation(.easeOut(duration: 0.15), value: on)
        }
        .buttonStyle(PressScale())
        .accessibilityLabel(c.word + " " + c.value)
        .accessibilityAddTraits(on ? .isSelected : [])
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
    @Environment(\.accessibilityReduceMotion) private var reduce

    init(model: DayTimelineModel, engine: StarEngine, tl: TimelineState, room: Double, lens: TLLens?, nowInView: Binding<Bool>) {
        self.model = model; self.engine = engine; self.tl = tl; self.room = room; self.lens = lens; _nowInView = nowInView
        _pos = State(initialValue: ScrollPosition(y: model.restY(room: room)))
        _opened = State(initialValue: room)
    }

    /// the first twelve rows in view when it opens rise in one after another; the rest are simply there
    private func ranks() -> [String: Int] {
        let y0 = model.restY(room: opened), offs = model.offsets
        var out: [String: Int] = [:]
        for (i, e) in model.entries.enumerated() where offs[i] + e.height > y0 && offs[i] < y0 + opened && out.count < 12 { out[e.id] = out.count }
        return out
    }

    var body: some View {
        let ranks = ranks()
        let nowTop = model.nowIndex.map { model.offsets[$0] }
        ScrollView(showsIndicators: false) {
            VStack(spacing: 0) {
                ForEach(model.entries) { e in
                    TLRowView(e: e, open: tl.openRow == e.id, first: e.id == model.entries.first?.id, dim: dim(e), hot: tl.hot == e.id, engine: engine) { toggle(e) }
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
        .onChange(of: tl.request) { _, r in
            guard r.y >= 0 else { return }
            if r.animated { withAnimation(.easeOut(duration: 0.45)) { pos.scrollTo(y: r.y) } } else { pos.scrollTo(y: r.y) }
        }
        // a swipe along the list is a day before or after; a drag up and down is the list's own
        .simultaneousGesture(DragGesture(minimumDistance: 24).onEnded { v in
            let dx = v.translation.width, dy = v.translation.height
            guard abs(dx) > 60, abs(dx) > abs(dy) * 1.8 else { return }
            tl.swipe(dx > 0 ? -1 : 1)
        })
        .onAppear { nowInView = true }
    }

    /// under a lens the rows of that kind stay as they are, a place's header goes to 0.6 so the day keeps its shape, the rest to 0.35
    private func dim(_ e: TLEntry) -> Double {
        guard let lens else { return 1 }
        return e.lens == lens || e.kind == .now ? 1 : e.kind == .stay ? 0.6 : 0.35
    }

    private func toggle(_ e: TLEntry) {
        guard e.opens else { return }
        withAnimation(reduce ? .easeOut(duration: 0.2) : .spring(response: 0.35, dampingFraction: 0.86)) { tl.openRow = tl.openRow == e.id ? nil : e.id }
    }
}

/// a row rising in: from 10 pt below, one after another 30 ms apart; with Reduce Motion a plain fade
private struct Rise: ViewModifier {
    let rank: Int?
    let on: Bool
    let reduce: Bool
    func body(content: Content) -> some View {
        let shown = rank == nil || on
        content
            .opacity(shown ? 1 : 0)
            .offset(y: shown || reduce ? 0 : 10)
            .animation(rank.map { reduce ? Animation.easeOut(duration: 0.25) : Animation.easeOut(duration: 0.4).delay(Double($0) * 0.03) }, value: on)
    }
}

// MARK: - one line of the list

private struct TLRowView: View {
    let e: TLEntry
    let open: Bool
    let first: Bool
    let dim: Double                     // 1, less while a lens is on and this is not of its kind
    let hot: Bool                       // the row that covers the minute under his finger on the bar
    let engine: StarEngine
    let tap: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Group {
                switch e.kind {
                case .stay: stay
                case .now: nowLine
                default: item
                }
            }
            .frame(height: e.height, alignment: .top)
            .background {
                RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(hot ? 0.07 : 0)).padding(.horizontal, -8)
                    .animation(.easeOut(duration: 0.1), value: hot)
            }
            .contentShape(Rectangle())
            .onTapGesture(perform: tap)
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(e.opens ? .isButton : [])
            if open { expansion.padding(.leading, 72).padding(.bottom, 12).transition(.opacity.combined(with: .offset(y: -6))) }
        }
        .opacity(dim)
        .animation(.easeOut(duration: 0.15), value: dim)
    }

    // the title and the like, muted once it is later than now
    private var titleColor: Color { e.later ? ink.opacity(0.58) : ink }
    private var symbolName: String { e.later ? e.symbol.replacingOccurrences(of: ".fill", with: "") : e.symbol }

    /// the place he stayed in: its colour as a dot, its name, and at the right its hours and how long
    private var stay: some View {
        HStack(spacing: 0) {
            Circle().fill((e.color.map { Color(red: $0.r / 255, green: $0.g / 255, blue: $0.b / 255) } ?? muted).opacity(e.kept ? 1 : 0.45)).frame(width: 8, height: 8)
            Text(e.title).font(.system(size: 15.5, weight: .semibold)).foregroundStyle(ink).lineLimit(1).padding(.leading, 8)
            Spacer(minLength: 10)
            Text(e.right).font(AT.mono(12)).foregroundStyle(muted).lineLimit(1).fixedSize().layoutPriority(1)
        }
        .padding(.top, 14)
        .overlay(alignment: .top) { if !first { Rectangle().fill(hair).frame(height: 0.5) } }
    }

    private var nowLine: some View {
        HStack(spacing: 10) {
            Text(e.title).font(AT.mono(11)).foregroundStyle(warm).fixedSize()
            Rectangle().fill(warm.opacity(0.85)).frame(height: 1)
        }
        .frame(maxHeight: .infinity)
    }

    /// time, symbol, title with its muted line, how long. The block of a row is centred in its height; a trip's rail runs the whole height.
    private var item: some View {
        let trip = e.kind == .trip
        let pad = e.kind == .trip ? 9.0 : e.detail == nil ? (e.height - 18) / 2 : 9.0
        return HStack(alignment: .top, spacing: 0) {
            Text(e.time).font(AT.mono(12)).foregroundStyle(muted).frame(width: 40, height: 18, alignment: .leading).padding(.top, pad)
            Group {
                if trip { rail } else { symbol.padding(.top, pad) }
            }.frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    if e.lit { MarkShape(kd: .lit).frame(width: 14, height: 14) }
                    Text(e.title).font(.system(size: 15)).foregroundStyle(titleColor).lineLimit(1)
                }.frame(height: 18, alignment: .leading)
                if let d = e.detail { Text(d).font(.system(size: 12.5)).foregroundStyle(e.later ? muted.opacity(0.8) : muted).lineLimit(1).frame(height: 15, alignment: .leading) }
            }
            .padding(.leading, 10).padding(.top, pad)
            Spacer(minLength: 8)
            Text(e.right).font(AT.mono(12)).foregroundStyle(e.strongRight ? ink : muted).lineLimit(1).fixedSize().frame(height: 18).padding(.top, pad)
        }
    }

    private var symbol: some View {
        Image(systemName: symbolName).font(.system(size: 15)).foregroundStyle(e.later ? iconColor.opacity(0.6) : iconColor).frame(width: 22, height: 18)
    }

    /// a trip is a dashed rail through the symbol's column, with the vehicle on it (level with the title, the rail running on to the foot of a taller row)
    private var rail: some View {
        VStack(spacing: 0) { dashes(9); symbol; dashes(e.height - 27) }
    }
    private func dashes(_ h: Double) -> some View {
        Path { p in p.move(to: .zero); p.addLine(to: CGPoint(x: 0, y: h)) }
            .stroke(Color(red: 196 / 255, green: 208 / 255, blue: 255 / 255).opacity(0.35), style: StrokeStyle(lineWidth: 1, dash: [2, 3]))
            .frame(width: 1, height: h)
    }

    /// what opening it shows: the first exchange of a talk, the picture, or the lines in full
    @ViewBuilder private var expansion: some View {
        switch e.kind {
        case .talk:
            let x = TalkTopics.shared.exchange(e.id)
            ExpandView(ex: .talk(his: x?.his ?? "", hers: x?.hers ?? "", start: e.talkStart ?? 0), engine: engine)
        case .picture:
            TLPicture(attachment: e.attachment, lines: e.more)
        default:
            ExpandView(ex: .note(e.more.joined(separator: "\n")), engine: engine)
        }
    }
}

/// A picture he sent: the picture as the talk shows it (a tap opens it full screen), and what the brain wrote about it. One the brain has swept is a plain line.
private struct TLPicture: View {
    let attachment: String?
    let lines: [String]
    @State private var swept = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let id = attachment {
                if swept {
                    Text("图片已过期").font(.system(size: 13)).foregroundStyle(muted)
                } else {
                    PictureCell(item: SentItem(id: id, attachment: id, isImage: true, title: ""), size: nil) {
                        PictureViewer.present([ViewerPicture(id: id, gone: false, small: PictureStore.shared.peek(id: id, maxPixel: 0))], index: 0)
                    }
                }
            }
            if !lines.isEmpty {
                Text(lines.joined(separator: "\n")).font(.system(size: 13.5)).lineSpacing(3).foregroundStyle(Color(red: 226 / 255, green: 230 / 255, blue: 250 / 255).opacity(0.84))
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .task(id: attachment) {
            guard let id = attachment else { return }
            if case .gone = await PictureStore.shared.source(id: id, gone: false) { swept = true }
        }
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
