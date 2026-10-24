import SwiftUI
import UIKit

// The iPad's home (跨端UI标准 §11): the phone's four pages, 今天 · 聊天 · 地图 · 设置, switched by the bar at the foot (`ui.page`), and over every page her at the top centre with
// the number of what waits on her, her card under her, and the bar. 今天 in a window wide enough for two columns: the strip (日历, the date under her, ✦ 润色), the day's bar at
// the whole width with the place line and the day's numbers, pinned; under them the phone's list on the left and her stage on the right (what he is in now and what waits on
// him, or the row he picked). Narrower (portrait, Split View) it is one column, the list's rows opening where they stand. 聊天 in a wide window is her topics at the left and
// the conversation at the right (Pad/PadChat.swift); 地图 and 设置 are the phone's own pages for now.
// The rows, the bar, the chips and an opened row are the phone's own views; this file only lays them out at iPad size.

private let ink = SetColor.ink
private let muted = SetColor.muted
private let faint = SetColor.faint
private let amber = Color(red: 1, green: 201 / 255, blue: 143 / 255)

/// The window's root: on an iPad the iPad's home at any width, on a phone the phone's. One engine for both, so a talk goes on when the window changes width.
struct HomeRoot: View {
    @State private var engine = StarEngine()

    var body: some View {
        #if DEBUG
        HomeChoice(engine: engine).modifier(PadDebugFrame())
        #else
        HomeChoice(engine: engine)
        #endif
    }
}

private struct HomeChoice: View {
    let engine: StarEngine

    var body: some View {
        let pad = UIDevice.current.userInterfaceIdiom == .pad
        Group { if pad { PadHome(engine: engine) } else { StarChartScreen(engine: engine) } }
            .onChange(of: pad, initial: true) { _, p in engine.ui.padHome = p }          // the engine lays the history out as the home on screen has it
    }
}

#if DEBUG
/// A simulator cannot be turned or split from the command line: `-padFrame landscape` lays the home out at the iPad's landscape size on its side,
/// `-padFrame split` in a Split View third (compact width) beside an empty pane.
private struct PadDebugFrame: ViewModifier {
    func body(content: Content) -> some View {
        switch UserDefaults.standard.string(forKey: "padFrame") {
        case "landscape":
            GeometryReader { g in
                content.frame(width: g.size.height, height: g.size.width).rotationEffect(.degrees(-90)).position(x: g.size.width / 2, y: g.size.height / 2)
            }
            .ignoresSafeArea()
        case "split":
            HStack(spacing: 0) {
                content.frame(width: 375).environment(\.horizontalSizeClass, .compact).clipped()
                Color.white.opacity(0.08).frame(width: 6)
                Color.black
            }
        default: content
        }
    }
}
#endif

/// The one motion of the iPad's home: 0.3 s with no bounce (the stage changing, a row's icon flying to it)
let padMotion = Animation.smooth(duration: 0.3)

struct PadHome: View {
    let engine: StarEngine
    @State private var flight = PadFlight()          // a tapped row's icon on its way to the stage's title
    @State private var nowInView = true
    @State private var leftAt: Date?
    @State private var listRoom = 600.0             // how tall the list is: a row opened from the bar in one column is carried up within it
    @State private var chatFolded = false           // 聊天's left column folded away
    @Environment(\.scenePhase) private var phase

    private var ui: StarUI { engine.ui }
    private var tl: TimelineState { engine.ui.tl }

    /// the narrowest window with the list and the stage side by side (13" landscape is 1376, 11" about 1194; portrait and Split View are one column)
    static let wideMin = 1100.0

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width, wide = w >= Self.wideMin, top = geo.safeAreaInsets.top
            TimelineView(.everyMinute) { ctx in
                let built = builtDay()
                ZStack(alignment: .topLeading) {
                    page(built, width: w, height: geo.size.height + geo.safeAreaInsets.bottom + top, top: top, wide: wide)
                    // over every page: her card, her, the number on her, the bar at the foot (跨端UI标准 §1)
                    if ui.page != .talk {
                        HerWordsCard(engine: engine, ui: ui).frame(width: W, height: H)
                            .offset(x: (w - W) / 2, y: top + Self.herY + padHerSize / 2 + 1 - HerWordsCard.top)
                    }
                    PadHer(engine: engine).position(x: w / 2, y: top + Self.herY)
                    HerBadge(engine: engine, ui: ui, seat: (CGPoint(x: W / 2, y: top + Self.herY), padHerSize / 2)).frame(width: W, height: H).offset(x: (w - W) / 2)
                    tabBar(width: w, height: geo.size.height + geo.safeAreaInsets.bottom + top)
                    PadFlightView(flight: flight)
                }
                .frame(width: w, height: geo.size.height + top + geo.safeAreaInsets.bottom, alignment: .topLeading)
                .coordinateSpace(name: "padHome")
                .onChange(of: ctx.date) { _, _ in tl.rollOver() }
                .onChange(of: wide) { _, _ in tl.close() }          // a row open on the stage is not one opened where it stands, nor the other way
                .onChange(of: built != nil, initial: true) { _, has in reveal(has) }
                .onChange(of: tl.enter) { _, _ in reveal(built != nil) }
                .onChange(of: (built?.model.waiting.count ?? 0) + Waiting.shared.cards.count, initial: true) { _, n in tl.waitingCount = n }          // what waits on him: the amber number on her
                .onChange(of: tl.waitingTap) { _, _ in withAnimation(padMotion) { tl.openRow = nil } }          // a tap on that number: the stage shows what waits
            }
            .ignoresSafeArea(.container, edges: [.top, .bottom])
        }
        .background(Color(red: 5 / 255, green: 6 / 255, blue: 13 / 255).ignoresSafeArea())
        .preferredColorScheme(.dark)
        .onChange(of: phase) { _, p in
            // away for five minutes, it comes back to now, as the phone's home does
            if p == .background { leftAt = Date(); engine.releaseTalk() }
            else if p == .active, let t = leftAt { leftAt = nil; if Date().timeIntervalSince(t) > 300 { engine.comeBack() } }
        }
        .onAppear(perform: appear)
        .onReceive(NotificationCenter.default.publisher(for: CardsRequest.notification)) { _ in openCards() }
        .sheet(item: Bindable(tl).sayWhat) { TLSayWhatSheet(tl: tl, s: $0) }
    }

    /// her middle under the status bar, the strip's buttons on the same line (the mock's 58 with a status bar of 24)
    static let herY = 34.0
    /// the bar at the foot with the room under it, as a tab bar controller puts it (62 pt, 21 pt over the foot); a page runs on under it, and a list's room ends where the phone's
    /// does, its 24 pt fade reaching 12 pt into the glass (TLLayout.listBottom)
    static let barH = 83.0, barRoom = barH - 12 + TLLayout.fade

    // MARK: the pages

    @ViewBuilder private func page(_ built: TLBuilt?, width w: Double, height h: Double, top: Double, wide: Bool) -> some View {
        switch ui.page {
        case .day: PadToday(engine: engine, built: built, width: w, top: top, wide: wide, flight: flight, nowInView: $nowInView, listRoom: $listRoom, empty: empty)
        case .talk:
            // ponytail: in one column the phone's page at the phone's width, centred, until narrow windows are revisited
            if wide { PadChat(engine: engine, width: w, height: h, top: top, folded: $chatFolded) }
            else { phonePage { TalkPanel(engine: engine, ui: ui, pull: .constant(nil), page: true) }.offset(x: (w - W) / 2, y: top) }
        // ponytail: the phone's own pages at the phone's width, centred, until each has its two panes
        case .map: phonePage { FootprintDayLayer(engine: engine, ui: ui) }.offset(x: (w - W) / 2)
        case .settings: SettingsSheet(asPage: true, cardUp: ui.herCardOn, tuneNear: { engine.showPage(.day) }).frame(width: W).frame(maxWidth: .infinity).padding(.top, top)
        }
    }

    private func phonePage<P: View>(@ViewBuilder _ p: () -> P) -> some View {
        p().frame(width: W, height: H, alignment: .topLeading).coordinateSpace(name: "design")
    }

    /// The bar at the foot, as on the phone: the system's own on iOS 26 at the mock's width, the drawn one before it
    @ViewBuilder private func tabBar(width w: Double, height h: Double) -> some View {
        if #available(iOS 26.0, *) {
            let shows = ui.barShows
            HomeStockTabs(engine: engine).environment(\.horizontalSizeClass, .compact).frame(width: 460, height: Self.barH)          // the phone's tabs, icon over word
                .position(x: w / 2, y: h - Self.barH / 2)
                .opacity(shows ? 1 : 0).offset(y: shows ? 0 : 16).allowsHitTesting(shows).accessibilityHidden(!shows)
                .animation(.easeOut(duration: shows ? 0.25 : 0.2), value: shows)
        } else {
            HomeTabBar(engine: engine).frame(width: W, height: H).offset(x: (w - W) / 2, y: h - H)
        }
    }

    /// what the brain told of the day on show, as the screen has it (a debug build's made-up day stands in for it); none while it is asked for, or when there is nothing
    private func builtDay() -> TLBuilt? {
        guard let r = tl.reply else { return nil }
        let b = tl.built(r)
        return b.model.isEmpty ? nil : b
    }

    /// the day is there: the rows that rise in at the phone's opening are simply there
    private func reveal(_ has: Bool) {
        guard has, !tl.reveal else { return }
        DispatchQueue.main.async { tl.reveal = true }
    }

    private var empty: String {
        #if DEBUG
        if tl.fixture != nil { return tl.fixture?.loading == true ? "" : Words.Pad.nothing }
        #endif
        let link = BrainLink.shared
        guard link.hasToken else { return Words.Pad.unpaired }
        guard tl.reply != nil else { return link.errorMessage ?? "" }
        return Words.Pad.nothing
    }

    private func appear() {
        HomeModule.shared.kind = .timeline          // the iPad's home is the timeline's
        engine.beginEditing = { engine.showPage(.talk) }          // ponytail: 聊天 has no field yet to take the keyboard; the phone's composer, once a view of its own, brings one
        StallLog.context = { [engine] in engine.stallContext() }
        engine.watchVoice()
        if CardsRequest.pending { openCards() }
        #if DEBUG
        if let sc = UserDefaults.standard.string(forKey: "scene") {
            let delay = UserDefaults.standard.double(forKey: "sceneDelay")
            DispatchQueue.main.asyncAfter(deadline: .now() + (delay > 0 ? delay : 1.5)) { engine.runScene(sc) }
        }
        if let s = UserDefaults.standard.string(forKey: "padScene") { DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) { padScene(s) } }
        #endif
    }

    /// a tap on a notification that waits on him: 今天, and the stage shows what waits
    private func openCards() {
        CardsRequest.pending = false; CardsRequest.card = nil
        engine.showPage(.day)
        tl.openRow = nil
    }

    #if DEBUG
    /// `-padScene row` a row of the list with more to show tapped, `-padScene bar` a block of the bar tapped
    private func padScene(_ s: String) {
        switch s {
        case "topic":           // 看原话 on the first of `-scene 时间线:聊天:话题`'s topics: its last place lit, the ‹ › bar over the field
            // a moment after the page has listed its rows, as a tap would come
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [engine] in
                guard let t = ChatSearchModel.sceneTopics?.first else { return }
                engine.goTrail(ChatTrail(name: t.name, places: t.places.map { .init(recordId: $0.recordId, seq: $0.seq, lines: $0.lines) }, at: t.places.count - 1), to: t.places.count - 1)
            }
            return
        case "fold": chatFolded = true; return          // 聊天's left column folded away
        default: break
        }
        guard let m = builtDay()?.model else { return }
        switch s {
        case "bar":             // a tap on the bar in the middle of the first stretch of the afternoon
            if let e = m.entries.first(where: { $0.kind == .segment && $0.t0 >= 15 * 60 }) { tl.padTapBar((e.t0 + (e.t1 ?? e.t0)) / 2, in: m) }
        case "row":             // a tap on the last row of time on the Mac (else the last row before now that opens on the stage): its icon flies to the stage's title
            if let e = m.entries.last(where: { $0.work != nil }) ?? m.entries.prefix(m.nowIndex ?? m.entries.count).last(where: { $0.expands }) { tl.padTapRow(e) }
        default: break
        }
    }
    #endif
}

extension TimelineState {
    /// A tap on a row of the iPad's list beside the stage: a row with more to show goes on the stage; one with nothing more frames its time on the bar for a moment, and the stage
    /// stays as it is (as on the phone, ADR 0376).
    func padTapRow(_ e: TLEntry) {
        if e.expands { withAnimation(padMotion) { openRow = e.id } } else { flashRow(e) }
    }

    /// A tap on the iPad's bar at `minute`: past now goes back to now; on a block of the row on the stage, the stage's line at that minute lights; on any other block the list goes to
    /// its row, and the row goes on the stage as a tap on it puts it there (one with nothing more frames its block for a moment).
    func padTapBar(_ minute: Double, in m: DayTimelineModel) {
        let t = m.onBar(minute)
        if let n = m.now, t >= n { Haptic.impact(.rigid); goToday(); return }
        guard let s = m.seg(at: t), let e = m.entries.first(where: { $0.id == s.id }) else { return }
        Haptic.selection()
        if e.id == openRow {
            if let l = e.lines.first(where: { $0.span.map { $0.lowerBound <= t && t <= $0.upperBound + 0.5 } ?? false }) { lightLine(l) }
            return
        }
        padTapRow(e)
        if let y = m.top(of: e.id) { scroll(to: max(0, y + e.gap - TLLayout.fadeTop - 6), animated: true) }
    }
}

// MARK: - 今天

/// The day's page: the strip under her, the bar at the whole width with the place line and the day's numbers, pinned; under them the list and, in a wide window, the stage beside it.
/// Laid out at the mock's places (13" landscape), from the top of the window under the status bar.
private struct PadToday: View {
    let engine: StarEngine
    let built: TLBuilt?
    let width: Double
    let top: Double                     // the status bar's height
    let wide: Bool
    let flight: PadFlight
    @Binding var nowInView: Bool
    @Binding var listRoom: Double
    let empty: String
    @State private var lastScrub = 0.0
    @State private var lastStretch: String?
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var ui: StarUI { engine.ui }
    private var tl: TimelineState { engine.ui.tl }
    private let side = 28.0                          // the mock's sides (13" landscape): the bar 28 pt in from each edge
    private static let dateY = 72.0, barY = 120.0, barH = 44.0

    /// the list's width beside the stage: the mock's 668 of 1376
    private var listW: Double { (width * 668 / 1376).rounded() }

    var body: some View {
        let m = built?.model, barW = width - 2 * side
        VStack(alignment: .leading, spacing: 0) {
            Group {
                if let built, let m {
                    bar(built, m, width: barW)
                    if !m.places.isEmpty { TLPlaceLine(places: m.places, axis: TLAxis.fair(built.scene, width: barW)).padding(.top, 8) } else { Color.clear.frame(height: 32) }
                } else if loading {
                    TLTrack(loading: true, reduce: reduce).environment(\.tlBarWidth, barW).environment(\.tlBarHeight, Self.barH).frame(height: Self.barH + 32, alignment: .top)
                } else { Color.clear.frame(height: Self.barH + 32) }
            }
            .padding(.horizontal, side)
            .environment(\.tlBarWidth, barW).environment(\.tlBarHeight, Self.barH).environment(\.tlBarRadius, TLLayout.Today.barRadius)
            if let m, !m.chips.isEmpty || !m.body.isEmpty {
                TLChips(chips: m.chips, health: m.body, active: tl.activeLens(in: m), width: width, whole: true) { l in
                    Haptic.selection()
                    withAnimation(.easeOut(duration: 0.15)) { tl.lens = tl.lens == l ? nil : l }
                }
                .equatable()
                .padding(.leading, side - 30)          // the chips' own 30 pt in, at the mock's 28
                .padding(.top, 5)
            } else { Color.clear.frame(height: 41) }
            HStack(alignment: .top, spacing: 0) {
                if wide {
                    list(m).frame(width: listW).environment(\.padFlight, flight)
                    Rectangle().fill(SetColor.line).frame(width: 0.5).padding(.top, 12).padding(.bottom, 96)
                    PadStage(engine: engine, model: m, width: width - listW, flight: flight)
                } else {
                    list(m)
                }
            }
            .padding(.top, 17)
            .frame(maxHeight: .infinity)
        }
        .padding(.top, top + Self.barY)
        .overlay(alignment: .top) { strip(m).padding(.top, top) }
        .onChange(of: tl.scrub) { _, t in if let t { lastScrub = t } }
    }

    /// 日历 at the left and ✦ 润色 at the right, on her line; the date under her, which gives way to her card and to the minute under a finger on the bar (the phone's `TLTopView`)
    private func strip(_ m: DayTimelineModel?) -> some View {
        let holding = tl.scrub != nil
        return ZStack(alignment: .top) {
            HStack {
                FootprintDayPicker(ui: ui)
                Spacer()
                if tl.tidyShown {
                    TopGlassButton(symbol: "sparkles", label: Words.PhoneToday.polish, on: tl.tidy != .running) { tl.tidyDay() }
                        .allowsHitTesting(tl.tidy != .running)
                }
            }
            .padding(.horizontal, side)
            .padding(.top, PadHome.herY - HomeTop.button / 2)
            (Text(ui.dateText) + Text(ui.dateLabel.map { " · " + $0 } ?? ""))
                .font(.system(size: Look.note)).tracking(0.4).foregroundStyle(Look.ink2).lineLimit(1)
                .fadeThrough(on: !holding && !ui.herCardOn, reduce: reduce)
                .allowsHitTesting(false)
                .padding(.top, Self.dateY)
            if let m {
                let r = m.readout(at: tl.scrub ?? lastScrub)
                TLReadoutLines(r: r, second: r.second, centered: true).environment(\.tlBarWidth, min(width - 240, 640))
                    .fadeThrough(on: holding, reduce: reduce)
                    .allowsHitTesting(false).accessibilityHidden(!holding)
                    .padding(.top, Self.dateY - 2)
            }
        }
        .frame(maxWidth: .infinity)
    }

    /// the day is being asked for: the bar's empty track, with its light going along it
    private var loading: Bool {
        #if DEBUG
        if let f = tl.fixture { return f.loading }
        #endif
        return BrainLink.shared.hasToken && tl.reply == nil
    }

    @ViewBuilder private func list(_ m: DayTimelineModel?) -> some View {
        if let m {
            GeometryReader { g in
                // it runs on to the foot under the bar; its room is what is over the bar, as on the phone
                let room = g.size.height - PadHome.barRoom
                TLList(model: m, engine: engine, tl: tl, room: room, lens: tl.activeLens(in: m), nowInView: $nowInView, inPlace: !wide)
                    .id("\(tl.day) \(wide)")          // the list is laid out for one way of opening its rows
                    .onChange(of: room, initial: true) { _, h in listRoom = h }
                    .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 12 / max(g.size.height, 1)),
                                                 .init(color: .black, location: 1 - 24 / max(g.size.height, 1)), .init(color: .clear, location: 1)], startPoint: .top, endPoint: .bottom))
            }
        } else {
            Text(empty).font(.system(size: 15)).lineSpacing(4).foregroundStyle(SetColor.muted)
                .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 30).padding(.top, 28)
                .frame(maxHeight: .infinity, alignment: .top)
        }
    }

    /// The phone's 44 pt bar at the whole width, hours over it, a name beside each icon where its piece has room, the stars over it (跨端UI标准 §11). A finger held on it reads the
    /// minute out in the date's place and brings the list to it, as on the phone; a tap on a block does what a tap on its row does; the bar frames what the stage shows.
    private func bar(_ built: TLBuilt, _ m: DayTimelineModel, width: Double) -> some View {
        let lens = tl.activeLens(in: m), axis = TLAxis.fair(built.scene, width: width)
        return TLBarView(scene: built.scene, bracket: bracket(m), scrub: tl.scrub, lens: lens != nil, lensColor: (lens ?? .trip).color, lensMarks: m.marks(of: lens ?? .trip),
                         reveal: true, reduce: reduce, breathe: true, sweeping: tl.tidy == .running && tl.day == tl.today && !reduce, fair: true, blink: tl.blink,
                         phone: true, stars: m.stars, names: true)
            .modifier(TLBarScrub(span: m.span, axis: axis, changed: { t, _ in scrubbed(t, m) }, ended: lifted, tap: { t in if wide { tl.padTapBar(t, in: m) } else { tl.tapBar(t, in: m, room: listRoom) } }))
            .accessibilityElement().accessibilityLabel(Words.Pad.bar)
            .frame(height: Self.barH, alignment: .top)
    }

    /// the stretch under his finger, else what the stage shows: the row open on it, or the stretch now is in (in one column only a row opened where it stands)
    private func bracket(_ m: DayTimelineModel) -> ClosedRange<Double>? {
        if let t = tl.scrub { return m.seg(at: t).map { $0.t0...$0.t1 } }
        guard let id = tl.openRow ?? (wide ? m.nowRow()?.id : nil), let e = m.entries.first(where: { $0.id == id }), e.kind != .now else { return nil }
        return e.t0...max(e.t0, e.t1 ?? m.now ?? e.t0)
    }

    private func scrubbed(_ t: Double, _ m: DayTimelineModel) {
        tl.follow(t, in: m)
        let s = m.stretch(at: t)
        if lastStretch != s { lastStretch = s; Haptic.selection() }
    }

    private func lifted() {
        lastStretch = nil
        withAnimation(.easeOut(duration: 0.2)) { tl.follow(nil, in: nil) }
    }
}
