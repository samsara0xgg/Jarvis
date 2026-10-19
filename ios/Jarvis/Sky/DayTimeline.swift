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
    static let barY = 156.0, barW = 380.0, barH = 16.0
    static let grabberY = 187.0                        // the top of the grabber's 36 pt hit area: its bar sits half way between the hour labels under the overview bar and the list
    static let listTop = 222.0, listBottom = 836.0, fade = 24.0
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

    func scroll(to y: Double, animated: Bool) { request = ScrollRequest(y: y, animated: animated, n: request.n + 1) }

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
    @State private var from = DayTimelineView.empty                                  // the bar as it was before it last changed, for it to morph from
    @State private var morph = 1.0
    @State private var scrub: Double?                                                // the minute under his finger on the overview bar
    @State private var lastStretch: String?
    @State private var nowInView = true
    static let columns = 380
    static let empty = [TLCell](repeating: .track, count: columns)

    private var tl: TimelineState { ui.tl }

    var body: some View {
        let on = HomeModule.shared.isTimeline
        let away = ui.talking || ui.typing || ui.panelOpen || ui.tuning
        _ = tl.version
        return TimelineView(.everyMinute) { _ in
            let shown = shownNow()
            let model: DayTimelineModel? = { if case .model(let m) = shown { return m } else { return nil } }()
            let cells = model?.cells(Self.columns) ?? Self.empty
            ZStack(alignment: .topLeading) {
                header(model, cells: cells)
                    .opacity(ui.panelOpen ? 0 : 1).allowsHitTesting(!ui.panelOpen)
                    .animation(.easeOut(duration: ui.panelOpen ? 0.2 : 0.35), value: ui.panelOpen)
                grabber
                pill(show: (tl.day != tl.today || !nowInView) && !away)
                listArea(shown, away: away)
            }
            .frame(width: W, height: H, alignment: .topLeading)
            .onChange(of: cells) { old, new in
                from = old; morph = 0
                withAnimation(.easeOut(duration: 0.35)) { morph = 1 }
            }
            .onChange(of: isReady(shown), initial: true) { _, ready in release(ready) }
            .onChange(of: tl.enter) { _, _ in release(isReady(shown)) }
            .onChange(of: tl.request) { _, r in if r.y < 0, let m = model { tl.scroll(to: restY(m), animated: true) } }
            .onChange(of: tl.focus) { _, id in if let id { tl.openCard = id; tl.focus = nil } }
        }
        .opacity(on ? 1 : 0)
        .allowsHitTesting(on)
        .animation(.easeOut(duration: 0.35), value: on)
    }

    // MARK: what there is to show

    private func shownNow() -> Shown {
        #if DEBUG
        if let f = tl.fixture {
            guard let r = f.replies[tl.day - tl.today] else { return .sentence("这一天没有记下什么。") }
            return .model(build(r))
        }
        #endif
        let link = BrainLink.shared
        guard link.hasToken else { return .sentence("还没连上大脑，连上以后这里会排出你的一天。") }
        guard let r = link.days[Cal.cur.iso(tl.day)] else { return link.errorMessage.map { .sentence($0) } ?? .loading }
        let m = build(r)
        return m.isEmpty ? .sentence("这一天没有记下什么。") : .model(m)
    }
    private func build(_ r: DayReply) -> DayTimelineModel {
        DayTimelineModel.build(r, day: tl.day, now: tl.nowMinute(), topic: { TalkTopics.shared.topic($0) })
    }
    private func isReady(_ s: Shown) -> Bool { if case .loading = s { return false }; return true }
    /// once the day is there, the bar grows in and the first rows rise (on the next turn, so they have been drawn as they were before)
    private func release(_ ready: Bool) {
        guard ready, !tl.reveal else { return }
        DispatchQueue.main.async { tl.reveal = true }
    }
    /// where the list rests on today: the line for now 60% of the way down what is left under the pinned cards
    private func restY(_ m: DayTimelineModel) -> Double { TLList.restY(m, room: room) }
    private var room: Double { TLLayout.room(pinned: pinnedHeight) }
    private var pinnedHeight: Double { Waiting.shared.cards.isEmpty ? 0 : min(tl.waitH > 0 ? tl.waitH : Double(Waiting.shared.cards.count) * TLMetrics.rowDetail, TLLayout.waitingMax) }

    // MARK: her sentence and the overview bar

    private func header(_ m: DayTimelineModel?, cells: [TLCell]) -> some View {
        ZStack(alignment: .topLeading) {
            if let line = m?.line {
                Text(line).font(.system(size: 15)).lineSpacing(3).foregroundStyle(ink.opacity(0.9)).lineLimit(2)
                    .frame(width: TLLayout.barW, height: TLLayout.lineH, alignment: .topLeading)
                    .id(line).transition(.opacity)
                    .offset(x: 30, y: TLLayout.lineY)
            }
            if let m { bar(m, cells: cells).offset(x: 30, y: TLLayout.barY) }
        }
        .animation(.easeOut(duration: 0.35), value: m?.line)
    }

    private func bar(_ m: DayTimelineModel, cells: [TLCell]) -> some View {
        let lo = m.span.lowerBound, w = m.span.upperBound - lo, bw = TLLayout.barW
        func x(_ t: Double) -> Double { (t - lo) / w * bw }
        return ZStack(alignment: .topLeading) {
            OverviewBar(from: from, to: cells, morph: morph, sweep: reduce || tl.reveal ? 1 : 0)
                .frame(width: bw, height: TLLayout.barH)
                .opacity(reduce && !tl.reveal ? 0 : 1)
                .animation(reduce ? .easeOut(duration: 0.25) : .easeOut(duration: 0.6), value: tl.reveal)
            ForEach([6, 12, 18, 24], id: \.self) { h in
                if Double(h * 60) >= lo {
                    Text(String(format: "%02d", h)).font(AT.mono(10)).foregroundStyle(muted).fixedSize()
                        .offset(x: x(Double(h * 60)) - (h == 24 ? 12 : 6), y: TLLayout.barH + 4)
                }
            }
            if let n = m.now, n >= lo {
                RoundedRectangle(cornerRadius: 0.75).fill(warm).frame(width: 1.5, height: TLLayout.barH + 6)
                    .shadow(color: warm.opacity(0.85), radius: 3)
                    .offset(x: x(n) - 0.75, y: -3)
            }
            if let t = scrub {
                Rectangle().fill(Color.white.opacity(0.85)).frame(width: 1, height: TLLayout.barH + 12).offset(x: x(t) - 0.5, y: -6)
                Text(clock(t)).font(AT.mono(11)).foregroundStyle(ink).padding(.horizontal, 8).frame(height: 20)
                    .background(Capsule().fill(Color(red: 28 / 255, green: 31 / 255, blue: 58 / 255)))
                    .overlay(Capsule().stroke(Color.white.opacity(0.14), lineWidth: 0.5))
                    .fixedSize().offset(x: clamp(x(t) - 22, 0, bw - 44), y: -30)
            }
        }
        .frame(width: bw, height: TLLayout.barH + 22, alignment: .topLeading)
        .contentShape(Rectangle().inset(by: -10))
        .gesture(DragGesture(minimumDistance: 0)
            .onChanged { v in
                let t = lo + clamp(v.location.x / bw, 0, 1) * w
                scrub = t
                if tl.openRow != nil { tl.openRow = nil }
                let s = m.stretch(at: t)
                if lastStretch != s { lastStretch = s; Haptic.selection() }
                if let e = m.entry(at: t), let y = m.top(of: e.id) { tl.scroll(to: y, animated: false) }
            }
            .onEnded { _ in
                lastStretch = nil
                withAnimation(.easeOut(duration: 0.2)) { scrub = nil }
            })
        .accessibilityElement().accessibilityLabel("一天的概览，拖动可以翻到那个时间")
    }

    // MARK: the grabber and the way back

    /// the same grabber as under the dial, between the bar and the list
    private var grabber: some View {
        let hide = ui.typing || ui.panelOpen
        return Capsule().fill(Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.28)).frame(width: 36, height: 5)
            .frame(width: 120, height: 36)
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
        .offset(x: 30, y: TLLayout.grabberY + 5)
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
                LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: 12)
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
            TLList(model: m, engine: engine, tl: tl, room: room, nowInView: $nowInView)
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

// MARK: - the overview bar

/// The day's bar: one cell per column, the colour of what he was doing then. It grows in from the left (`sweep`) and morphs from the bar it was (`morph`).
private struct OverviewBar: View, Animatable {
    var from: [TLCell], to: [TLCell]
    var morph: Double, sweep: Double
    var animatableData: AnimatablePair<Double, Double> {
        get { AnimatablePair(morph, sweep) }
        set { morph = newValue.first; sweep = newValue.second }
    }

    var body: some View {
        Canvas { ctx, size in
            let n = to.count, cw = size.width / Double(n), front = sweep * size.width
            func cell(_ c: Int) -> TLCell {
                if Double(c) * cw >= front { return .track }
                return from.count == n ? .mix(from[c], to[c], morph) : to[c]
            }
            // runs of one colour are one rectangle, so there is no seam between neighbours
            var c = 0
            while c < n {
                let v = cell(c)
                var e = c + 1
                while e < n && cell(e) == v { e += 1 }
                ctx.fill(Path(CGRect(x: Double(c) * cw, y: 0, width: Double(e - c) * cw, height: size.height)),
                         with: .color(Color(.sRGB, red: v.r / 255, green: v.g / 255, blue: v.b / 255, opacity: v.a)))
                c = e
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 5))
    }
}

// MARK: - the list

private struct TLList: View {
    let model: DayTimelineModel
    let engine: StarEngine
    let tl: TimelineState
    let room: Double                    // how tall the list is under the pinned cards
    @Binding var nowInView: Bool
    @State private var pos: ScrollPosition
    @State private var opened: Double   // the room it was opened in, which the first rows' entry is laid out for
    @Environment(\.accessibilityReduceMotion) private var reduce

    init(model: DayTimelineModel, engine: StarEngine, tl: TimelineState, room: Double, nowInView: Binding<Bool>) {
        self.model = model; self.engine = engine; self.tl = tl; self.room = room; _nowInView = nowInView
        _pos = State(initialValue: ScrollPosition(y: Self.restY(model, room: room)))
        _opened = State(initialValue: room)
    }

    /// Today opens with the line for now 60% of the way down; another day opens at its top.
    static func restY(_ m: DayTimelineModel, room: Double) -> Double {
        guard let i = m.nowIndex else { return 0 }
        return max(0, m.offsets[i] + TLMetrics.now / 2 - 0.6 * room)
    }

    /// the first twelve rows in view when it opens rise in one after another; the rest are simply there
    private func ranks() -> [String: Int] {
        let y0 = Self.restY(model, room: opened), offs = model.offsets
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
                    TLRowView(e: e, open: tl.openRow == e.id, first: e.id == model.entries.first?.id, engine: engine) { toggle(e) }
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
            .contentShape(Rectangle())
            .onTapGesture(perform: tap)
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(e.opens ? .isButton : [])
            if open { expansion.padding(.leading, 72).padding(.bottom, 12).transition(.opacity.combined(with: .offset(y: -6))) }
        }
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
            Text(e.right).font(AT.mono(12)).foregroundStyle(muted).lineLimit(1).fixedSize().frame(height: 18).padding(.top, pad)
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
