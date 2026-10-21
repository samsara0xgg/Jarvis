import SwiftUI
import UIKit

// The iPad's home, in regular width: across the top her band (the date, her line, the day's bar with every hour named, the place line under it and all the day's
// numbers in one row, with her at the right; a talk and the field she types in open inside it, and it grows for them), under it on the left the phone's own list
// of the day, and on the right her stage: what he is in now and what waits on him, or the stretch he tapped on the left. Compact width (Split View, Slide
// Over) is the phone's home as it is. The rows, the bar, the chips and an opened row are the phone's own views; this file only lays them out at iPad size.

private let ink = SetColor.ink
private let muted = SetColor.muted
private let faint = SetColor.faint
private let amber = Color(red: 1, green: 201 / 255, blue: 143 / 255)

/// The window's root: on an iPad with room the iPad's home, anywhere else the phone's. One engine for both, so a talk goes on when the window changes width.
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
    @Environment(\.horizontalSizeClass) private var width

    var body: some View {
        if UIDevice.current.userInterfaceIdiom == .pad && width == .regular { PadHome(engine: engine) } else { StarChartScreen(engine: engine) }
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

/// The one motion of the iPad's home: 0.3 s with no bounce (her band growing, the stage changing, what comes out of her and goes back into her)
let padMotion = Animation.smooth(duration: 0.3)

struct PadHome: View {
    let engine: StarEngine
    @State private var menu = PadMenu()
    @State private var typing = false                   // the field in her band is open
    @FocusState private var fieldFocus: Bool
    @FocusState private var homeFocus: Bool             // the home takes a hardware keyboard's keys when nothing else does: typing anywhere opens the field
    @State private var herAt = CGPoint.zero             // where she stands, in the home's own space: her menu is laid round it
    @State private var nowInView = true
    @State private var leftAt: Date?
    @State private var talkUntil = Date.distantPast    // her last round stays in her band until he does something else, ten minutes at most (as on the phone)
    @Environment(\.scenePhase) private var phase
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var ui: StarUI { engine.ui }
    private var tl: TimelineState { engine.ui.tl }

    /// the left column is the phone's list at the phone's width
    static let listW = 440.0

    var body: some View {
        GeometryReader { geo in
            TimelineView(.everyMinute) { ctx in
                let built = builtDay(), items = PadMenuItem.shown(tidyShown: tl.tidyShown)
                ZStack(alignment: .topLeading) {
                    VStack(spacing: 0) {
                        PadBand(engine: engine, built: built, width: geo.size.width, talk: ui.tlTalk || (ctx.date < talkUntil && !ui.liveTurns.isEmpty),
                                typing: $typing, focus: $fieldFocus, menu: menu, items: items, herAt: $herAt)
                            .padding(.top, geo.safeAreaInsets.top + 6)
                        HStack(alignment: .top, spacing: 0) {
                            list(built).frame(width: Self.listW)
                            Rectangle().fill(SetColor.line).frame(width: 0.5).padding(.vertical, 12)
                            PadStage(engine: engine, model: built?.model, width: geo.size.width - Self.listW)
                        }
                        .frame(maxHeight: .infinity)
                    }
                    PadHerMenu(engine: engine, menu: menu, items: items, center: herAt)
                }
                .coordinateSpace(name: "padHome")
                .simultaneousGesture(SpatialTapGesture().onEnded { _ in if !ui.tlTalk { talkUntil = .distantPast } })          // he did something else: her last round gives way
                .onChange(of: ui.tlTalk) { _, on in if on { talkUntil = Date().addingTimeInterval(600) } }
                .onChange(of: tl.scrub != nil) { _, on in if on { talkUntil = .distantPast } }
                .onChange(of: typing) { _, on in if on { talkUntil = .distantPast } }
                .onChange(of: ctx.date) { _, _ in tl.rollOver() }
                .onChange(of: built != nil, initial: true) { _, has in reveal(has) }
                .onChange(of: tl.enter) { _, _ in reveal(built != nil) }
            }
            .ignoresSafeArea(.container, edges: .top)
        }
        .background(Color(red: 5 / 255, green: 6 / 255, blue: 13 / 255).ignoresSafeArea())
        .preferredColorScheme(.dark)
        .focusable(!typing)
        .focused($homeFocus)
        .focusEffectDisabled()
        .onKeyPress(phases: .down) { press in typed(press) }
        .onChange(of: fieldFocus) { _, f in
            ui.typing = f
            if !f && ui.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { withAnimation(padMotion) { typing = false }; homeFocus = true }
        }
        .onChange(of: phase) { _, p in
            // away for five minutes, it comes back to now, as the phone's home does
            if p == .background { leftAt = Date() }
            else if p == .active, let t = leftAt { leftAt = nil; if Date().timeIntervalSince(t) > 300 { engine.comeBack() } }
        }
        .onAppear(perform: appear)
        .onReceive(NotificationCenter.default.publisher(for: CardsRequest.notification)) { _ in openCards() }
        .sheet(isPresented: Bindable(ui).settingsOpen) { SettingsSheet(tuneNear: { ui.settingsOpen = false }) }
        .sheet(isPresented: Binding(get: { ui.panelOpen }, set: { if !$0 { engine.closePanel() } })) { PadHistory(engine: engine) }
        .sheet(item: Bindable(tl).sayWhat) { TLSayWhatSheet(tl: tl, s: $0) }
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

    @ViewBuilder private func list(_ built: TLBuilt?) -> some View {
        if let b = built {
            GeometryReader { g in
                TLList(model: b.model, engine: engine, tl: tl, room: g.size.height, lens: tl.activeLens(in: b.model), nowInView: $nowInView)
                    .id(tl.day)
                    .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 12 / max(g.size.height, 1)),
                                                 .init(color: .black, location: 1 - 24 / max(g.size.height, 1)), .init(color: .clear, location: 1)], startPoint: .top, endPoint: .bottom))
            }
        } else {
            Text(empty).font(.system(size: 15)).lineSpacing(4).foregroundStyle(muted)
                .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 30).padding(.top, 28)
                .frame(maxHeight: .infinity, alignment: .top)
        }
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
        HomeModule.shared.kind = .timeline          // the iPad's home is the timeline's: the engine talks in her band, not under a dial
        homeFocus = true
        engine.endEditing = { fieldFocus = false }
        engine.beginEditing = { withAnimation(padMotion) { typing = true }; DispatchQueue.main.async { fieldFocus = true } }
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

    /// a tap on a notification that waits on him: the stage shows what waits
    private func openCards() {
        CardsRequest.pending = false; CardsRequest.card = nil
        tl.openRow = nil
    }

    /// A key of a hardware keyboard with nothing else taking it: a printable one opens the field with it in it.
    private func typed(_ press: KeyPress) -> KeyPress.Result {
        guard !typing, press.modifiers.isDisjoint(with: [.command, .control, .option]) else { return .ignored }
        let s = press.characters
        guard !s.isEmpty, s.unicodeScalars.allSatisfy({ !CharacterSet.controlCharacters.contains($0) && !CharacterSet.newlines.contains($0) }) else { return .ignored }
        ui.text += s
        engine.beginEditing?()
        return .handled
    }

    #if DEBUG
    /// `-padScene menu` her menu out, `-padScene type` the field open with words in it
    private func padScene(_ s: String) {
        switch s {
        case "menu": menu.show(holding: false)
        case "type": ui.text = Words.Pad.field; engine.beginEditing?()
        default: break
        }
    }
    #endif
}

// MARK: - her band

private struct PadBand: View {
    let engine: StarEngine
    let built: TLBuilt?
    let width: Double
    let talk: Bool                      // a round of talk is in her place
    @Binding var typing: Bool
    var focus: FocusState<Bool>.Binding
    let menu: PadMenu
    let items: [PadMenuItem]
    @Binding var herAt: CGPoint
    @State private var lastScrub = 0.0
    @State private var lastStretch: String?
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var ui: StarUI { engine.ui }
    private var tl: TimelineState { engine.ui.tl }
    private let side = 30.0

    var body: some View {
        let m = built?.model, holding = tl.scrub != nil
        let barW = width - 2 * side
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 28) {             // she stays beside the date when her words grow the band
                VStack(alignment: .leading, spacing: 0) {
                    caption(m)
                    date.padding(.top, 2)
                    ZStack(alignment: .topLeading) {
                        if talk {
                            PadTalk(engine: engine, model: m).transition(.opacity)
                        } else if tl.tidy != .idle {
                            Text(tl.tidy.words).font(.system(size: 16)).foregroundStyle(muted).transition(.opacity)          // 理一下 at work, then what came of it
                        } else if let note = ui.voiceNote {
                            Text(note).font(AT.mono(13)).tracking(1.2).foregroundStyle(muted).lineLimit(1).transition(.opacity)
                        } else if holding, let m {
                            let r = m.readout(at: tl.scrub ?? lastScrub)
                            TLReadoutLines(r: r, second: r.second)
                                .environment(\.tlBarWidth, barW - padHerSize - 28).transition(.opacity)
                        } else if let line = m?.line {
                            Text(line).font(.system(size: 16)).lineSpacing(3).foregroundStyle(ink.opacity(0.9)).lineLimit(2)
                                .frame(maxWidth: .infinity, alignment: .leading).transition(.opacity)
                        }
                    }
                    .padding(.top, 10)
                    .animation(padMotion, value: talk)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                HStack(spacing: 18) {
                    PadHer(engine: engine, menu: menu, items: items, center: herAt)
                        .onGeometryChange(for: CGPoint.self) { g in let f = g.frame(in: .named("padHome")); return CGPoint(x: f.midX, y: f.midY) } action: { herAt = $0 }
                    Button { ui.settingsOpen = true } label: {          // settings to her right, as on the phone
                        Image(systemName: "gearshape").font(.system(size: 18)).foregroundStyle(Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.8))
                            .frame(width: 40, height: 40).glass(Circle())
                    }
                    .buttonStyle(PressScale())
                    .accessibilityLabel(Words.Pad.settings)
                }
            }
            .padding(.horizontal, side)
            if typing {
                PadField(engine: engine, focus: focus) { withAnimation(padMotion) { typing = false } }
                    .padding(.leading, side).padding(.trailing, side + padHerSize + 18 + 40 + 28).padding(.top, 12)
                    .transition(.asymmetric(insertion: .scale(scale: 0.2, anchor: .trailing).combined(with: .opacity), removal: .scale(scale: 0.2, anchor: .trailing).combined(with: .opacity)))
            }
            if let built, let m {
                bar(built, m, width: barW).padding(.horizontal, side).padding(.top, 34)
                if !m.chips.isEmpty || !m.body.isEmpty {
                    TLChips(chips: m.chips, health: m.body, active: tl.activeLens(in: m), width: width) { l in
                        Haptic.selection()
                        withAnimation(.easeOut(duration: 0.15)) { tl.lens = tl.lens == l ? nil : l }
                    }
                    .equatable()
                    .padding(.top, 10)
                }
            } else if loading {
                TLTrack(loading: true, reduce: reduce).environment(\.tlBarWidth, barW).padding(.horizontal, side).padding(.top, 34)
            }
        }
        .padding(.bottom, 12)
        .animation(padMotion, value: typing)
        .animation(padMotion, value: talk)
        .onChange(of: tl.scrub) { _, t in if let t { lastScrub = t } }
    }

    /// the day is being asked for: the bar's empty track, with its light going along it
    private var loading: Bool {
        #if DEBUG
        if let f = tl.fixture { return f.loading }
        #endif
        return BrainLink.shared.hasToken && tl.reply == nil
    }

    /// 今天 (or the day's own label) and the weather, quiet, over the date
    private func caption(_ m: DayTimelineModel?) -> some View {
        HStack(spacing: 8) {
            Text(tl.day == tl.today ? Words.Pad.today0 : ui.dateLabel ?? "").font(.system(size: 12.5)).foregroundStyle(muted)
            if let w = m?.weather { TLWeatherLine(w: w, word: true) }
        }
        .frame(height: 16)
    }

    /// the date large, its weekday quiet after it
    private var date: some View {
        let parts = ui.dateText.split(separator: " ", maxSplits: 1).map(String.init)
        return HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(parts.first ?? "").font(.system(size: 34, weight: .semibold)).foregroundStyle(ink)
            if parts.count > 1 { Text(parts[1]).font(.system(size: 22, weight: .medium)).foregroundStyle(muted) }
        }
        .lineLimit(1)
    }

    /// The phone's bar at the band's width, the hours named every hour. A finger on it reads the minute out where her line is and brings the list to it,
    /// as on the phone; the open row is outlined on it.
    private func bar(_ built: TLBuilt, _ m: DayTimelineModel, width: Double) -> some View {
        let lens = tl.activeLens(in: m)
        return TLBarView(scene: built.scene, bracket: bracket(m), scrub: tl.scrub, lens: lens != nil, lensColor: (lens ?? .trip).color, lensMarks: m.marks(of: lens ?? .trip),
                         reveal: true, reduce: reduce, breathe: true, sweeping: tl.tidy == .running && tl.day == tl.today && !reduce)
            .modifier(TLBarScrub(span: m.span, changed: { t, _ in scrubbed(t, m) }, ended: lifted))
            .accessibilityElement().accessibilityLabel(Words.Pad.bar)
            .environment(\.tlBarWidth, width)
            .frame(height: 22 + 18, alignment: .top)
    }

    /// the stretch under his finger, else the span of the row open on the stage
    private func bracket(_ m: DayTimelineModel) -> ClosedRange<Double>? {
        if let t = tl.scrub { return m.seg(at: t).map { $0.t0...$0.t1 } }
        guard let id = tl.openRow, let e = m.entries.first(where: { $0.id == id }), e.kind != .now else { return nil }
        return e.t0...max(e.t0, e.t1 ?? e.t0)
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

// MARK: - a talk in her band

/// The latest round of a talk, where her line was: his heard words in a muted line, hers as she says them. The band grows for it, up to a limit; longer, it scrolls, and a tap opens the history at it.
private struct PadTalk: View {
    let engine: StarEngine
    let model: DayTimelineModel?

    var body: some View {
        let talk = TLTalk.of(engine.ui.liveTurns)
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: 8) {
                if let heard = talk.heard { Text(heard).font(.system(size: 14)).foregroundStyle(muted).lineLimit(2) }
                ForEach(talk.hers) { r in row(r) }
                if talk.unsent != nil {
                    Button { engine.sendAgain(row: talk.unsent.map { "l\($0.id)" }) } label: {
                        Label(Words.Pad.notSent, systemImage: "exclamationmark.circle.fill").font(.system(size: 14)).foregroundStyle(amber)
                    }.buttonStyle(.plain)
                }
                if talk.cards > 0 {
                    Text(Words.Pad.waiting(talk.cards)).font(.system(size: 14, weight: .semibold)).foregroundStyle(amber)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollBounceBehavior(.basedOnSize)
        .frame(maxHeight: 300)
        .fixedSize(horizontal: false, vertical: true)
        .contentShape(Rectangle())
        .onTapGesture { engine.showAnswer(talk.answer) }
        .simultaneousGesture(DragGesture(minimumDistance: 0).onChanged { _ in engine.holdLinger(true) }.onEnded { _ in engine.holdLinger(false) })
    }

    @ViewBuilder private func row(_ r: LiveTurn) -> some View {
        if r.failed {
            HStack(spacing: 12) {
                Text(r.text).font(.system(size: 16)).foregroundStyle(muted)
                Button { engine.sendAgain(row: "l\(r.id)") } label: { Text(Words.Pad.retry).font(.system(size: 14, weight: .semibold)).foregroundStyle(amber) }.buttonStyle(.plain)
            }
        } else if let s = r.status {
            StatusRow(line: s, size: 14)
        } else {
            let blocks = AnswerBlocks.parse(r.text)
            if blocks.contains(where: { if case .paragraph = $0 { false } else { true } }) {
                AnswerView(blocks: blocks, size: 16, lineSpacing: 3)
            } else {
                RevealText(text: styledWords(r.text, base: ink.opacity(0.92)), reveal: r.reveal)
                    .font(.system(size: 16)).lineSpacing(3).frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

// MARK: - the field in her band

/// What he types to her, unrolled from her: the words go as the phone's composer sends them.
private struct PadField: View {
    let engine: StarEngine
    var focus: FocusState<Bool>.Binding
    let done: () -> Void

    var body: some View {
        let ui = engine.ui
        HStack(alignment: .bottom, spacing: 8) {
            TextField(Words.Pad.field, text: Bindable(ui).text, axis: .vertical)
                .font(.system(size: 16)).foregroundStyle(ink).lineLimit(1...5)
                .focused(focus)
                .onSubmit(send)
                .padding(.vertical, 11).padding(.leading, 18)
            Button(action: send) {
                Image(systemName: "arrow.up").font(.system(size: 16, weight: .semibold)).foregroundStyle(Color(red: 11 / 255, green: 13 / 255, blue: 26 / 255))
                    .frame(width: 34, height: 34).background(Circle().fill(Color(red: 238 / 255, green: 241 / 255, blue: 1)))
            }
            .buttonStyle(PressScale())
            .padding(5)
            .accessibilityLabel(Words.Pad.send)
        }
        .glass(RoundedRectangle(cornerRadius: 22), focused: true)
    }

    private func send() {
        let words = engine.ui.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !words.isEmpty else { return }
        if BrainLink.shared.hasToken { engine.sendToBrain(words) } else { engine.send(words) }
        engine.ui.text = ""
        focus.wrappedValue = false
        done()
    }
}

// MARK: - the history

/// The phone's history of the talk, in a sheet at the phone's own size (it is laid out in the phone's design points)
private struct PadHistory: View {
    let engine: StarEngine
    @State private var pull: Double?

    var body: some View {
        GeometryReader { g in
            let k = min(g.size.width / W, g.size.height / (H - StarEngine.panelOpen))
            TalkPanel(engine: engine, ui: engine.ui, pull: $pull)
                .frame(width: W, height: H)
                .coordinateSpace(name: "design")
                .offset(y: -StarEngine.panelOpen)
                .scaleEffect(k, anchor: .topLeading)
                .frame(width: W * k, height: (H - StarEngine.panelOpen) * k, alignment: .topLeading)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Color(red: 11 / 255, green: 13 / 255, blue: 26 / 255).ignoresSafeArea())
        .preferredColorScheme(.dark)
    }
}
