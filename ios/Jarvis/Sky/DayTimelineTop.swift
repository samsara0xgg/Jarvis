import SwiftUI

// Her sentence, the overview bar under it and the day's numbers under that: the part of the timeline that follows his finger. It is a view of its own because the minute under a finger
// changes on every frame and whatever reads that minute is drawn again on every frame: kept here, that is this and nothing else of the screen, not the model of the day and not the list.
// While a talk is on show (`ui.tlTalk`) the three give way to it, the way they give way to the readout of a finger: Sky/DayTimelineTalk.swift draws it in their place.

private let ink = SetColor.ink
private let muted = SetColor.muted

struct TLTopView: View {
    let ui: StarUI
    let model: DayTimelineModel?
    let bar: DayTimelineModel?                  // what the bar, its readout and its bracket read (the model, or its other drawing from where he was), made once with the model and not on every frame of a finger
    let scene: TLBarScene                       // and the bar of it
    @Binding var byPlace: Bool                  // the bar is drawn from where he was and not from what he was doing: the switch at its right end
    var loading = false                         // the day is being asked for: its bar is an empty track with a soft light going along it
    @Environment(\.accessibilityReduceMotion) private var reduce
    @State private var lastScrub = 0.0                                               // where his finger was last: what the readout shows while it fades out
    @State private var lastStretch: String?
    @State private var lastLens = TLLens.trip                                        // the lens that was on last: what fades out when it goes
    @State private var ghost: TLBarScene?                                            // the bar as it was when the brain wrote a tidied day, which fades off the new one (`tl.tidied`)
    @State private var ghostGone = false
    @State private var ghostTask: Task<Void, Never>?

    private var tl: TimelineState { ui.tl }

    var body: some View {
        let holding = tl.scrub != nil, talk = ui.tlTalk
        let said = bar.map { $0.readout(at: tl.scrub ?? lastScrub) }          // once for the readout and for VoiceOver's value, on every frame of a finger
        return ZStack(alignment: .topLeading) {
            // her sentence, and in its place while a finger is on the bar the readout of the minute under it: they cross, blurred (`fadeThrough`), so the two are never readable together
            Group {          // each animation below is on what it moves, not on the whole of the header
                if let line = model?.line {
                    Text(line).font(.system(size: 15)).lineSpacing(3).foregroundStyle(ink.opacity(0.9)).lineLimit(2)
                        .frame(width: TLLayout.barW, height: TLLayout.lineH, alignment: .topLeading)
                        .fadeThrough(on: !holding && !talk, reduce: reduce)
                        .accessibilityHidden(talk)
                        .id(line).transition(TLMotion.swapWords(reduce: reduce))
                        .offset(x: 30, y: TLLayout.lineY)
                }
            }
            .animation(TLMotion.dayChange(reduce: reduce), value: model?.line)
            if let said {
                readout(said).fadeThrough(on: holding, reduce: reduce)
                    .offset(x: 30, y: TLLayout.lineY)
                    .allowsHitTesting(false).accessibilityHidden(!holding)
            }
            Group {
                if model == nil && loading {
                    TLTrack(loading: true, reduce: reduce).id(tl.day).transition(.opacity).offset(x: 30, y: TLLayout.barY)
                }
            }
            .animation(TLMotion.dayChange(reduce: reduce), value: model == nil)
            if let m = model, let bar {
                let lens = tl.activeLens(in: m)
                ZStack(alignment: .topLeading) {
                    TLBarView(scene: scene, bracket: bracket(bar), scrub: tl.scrub, lens: lens != nil, lensColor: (lens ?? lastLens).color, lensMarks: m.marks(of: lens ?? lastLens),
                              reveal: tl.reveal, reduce: reduce, breathe: ui.tlHeaderShown,
                              sweeping: tl.tidy == .running && tl.day == tl.today && !reduce, ghost: ghost, ghostGone: ghostGone)
                        .id(byPlace).transition(barSwap)          // the other drawing of the day comes in over the one that goes
                }
                .animation(holding ? .easeOut(duration: 0.08) : .easeInOut(duration: 0.15), value: holding)       // the hairline and its time come and go with the finger
                .modifier(TLBarScrub(span: m.span, changed: { t, x in scrubbed(t, x, m, bar) }, ended: lifted))
                .accessibilityElement().accessibilityLabel("一天的概览")
                .accessibilityHint("拖动可以翻到那个时间，上下滑动一段一段地看")
                .accessibilityValue(tl.scrub != nil ? said.map(spoken) ?? "" : "")
                .accessibilityAdjustableAction { nudge($0, m) }
                .fadeThrough(on: !talk, reduce: reduce).allowsHitTesting(!talk).accessibilityHidden(talk)
                .id(tl.day).transition(barTransition)
                .offset(x: 30, y: TLLayout.barY)
                if m.hasPlaces {
                    TLBarMode(byPlace: $byPlace, reduce: reduce)
                        .opacity(tl.reveal ? 1 : 0).animation(TLMotion.arrival(after: 0.7, reveal: tl.reveal, reduce: reduce), value: tl.reveal)          // with the bar's growth
                        .fadeThrough(on: !talk && !holding, reduce: reduce).allowsHitTesting(!talk)
                        .id(tl.day).transition(barTransition)
                        .offset(x: 30 + TLLayout.barW + 3, y: TLLayout.barY + (TLLayout.barH - TLBarMode.height) / 2)
                }
                if !m.chips.isEmpty || !m.body.isEmpty {
                    TLChips(chips: m.chips, health: m.body, active: lens) { l in
                        Haptic.selection()
                        withAnimation(.easeOut(duration: 0.15)) { tl.lens = tl.lens == l ? nil : l }
                    }
                    .equatable()
                    .fadeThrough(on: !talk, reduce: reduce).allowsHitTesting(!talk).accessibilityHidden(talk)
                    .id(tl.day).transition(barTransition)
                    .offset(y: TLLayout.chipsY - (TLLayout.chipsHit - TLLayout.chipsH) / 2)
                }
            }
        }
        .onDisappear { lastStretch = nil; tl.follow(nil, in: nil) }          // a finger that was on the bar when this went away is not on it when it comes back
        .onChange(of: tl.lens) { _, l in if let l { lastLens = l } }
        .onChange(of: tl.tidied) { _, s in if s != nil { holdGhost() } }
        .onChange(of: tl.reply?.serial) { _, _ in releaseGhost() }
        .onChange(of: tl.scrub) { _, t in if let t { lastScrub = t } }
        // only once she is in her corner, and gone as soon as she leaves it: she glides across where they stand
        .opacity(ui.tlHome ? 1 : 0).allowsHitTesting(ui.tlHome)
        .animation(.easeOut(duration: ui.tlHome ? (reduce ? TLMotion.reducedFade : 0.3) : 0.06), value: ui.tlHome)
    }

    /// A tidied day was written: the bar as it is now stays over what comes in, until the next reading of the day is in (or, if it does not come, for ten seconds).
    private func holdGhost() {
        ghostTask?.cancel()
        ghost = scene; ghostGone = false
        ghostTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 10_000_000_000)
            if !Task.isCancelled { ghost = nil }
        }
    }

    /// the next reading is in: the old drawing fades off it, and is let go
    private func releaseGhost() {
        guard ghost != nil, !ghostGone else { return }
        ghostTask?.cancel()
        withAnimation(.easeInOut(duration: reduce ? TLMotion.reducedFade : 0.45)) { ghostGone = true }
        ghostTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 600_000_000)
            if !Task.isCancelled { ghost = nil }
        }
    }

    /// the minute under his finger in words, where her sentence is: the stretch with its hours and length, under it what else was going on
    private func readout(_ r: TLReadout) -> some View { TLReadoutLines(r: r, second: r.second) }

    /// the minute under VoiceOver's step as it is said: the stretch, and what else was going on
    private func spoken(_ r: TLReadout) -> String { r.head + "，" + r.second }

    /// VoiceOver's up and down on the bar: the next stretch or the one before it, held the way a finger holds a minute and let go after a pause
    private func nudge(_ direction: AccessibilityAdjustmentDirection, _ m: DayTimelineModel) {
        let forward: Bool
        switch direction {
        case .increment: forward = true
        case .decrement: forward = false
        @unknown default: return
        }
        guard let t = scene.step(from: tl.scrub, forward: forward) else { return }
        tl.follow(t, in: m)
        tl.holdThenRelease()
    }

    /// the stretch under his finger, else the span of the row he has open, on the bar (`m` is the bar's own view of the day)
    private func bracket(_ m: DayTimelineModel) -> ClosedRange<Double>? {
        if let t = tl.scrub { return m.seg(at: t).map { $0.t0...$0.t1 } }
        guard let id = tl.openRow, let e = m.entries.first(where: { $0.id == id }), e.kind != .now else { return nil }
        return e.t0...max(e.t0, e.t1 ?? e.t0)
    }

    /// a finger on the bar: the list follows it, with a tick of the phone for each stretch of the bar (`bar`, the day as the bar draws it) it crosses
    private func scrubbed(_ t: Double, _ x: Double, _ m: DayTimelineModel, _ bar: DayTimelineModel) {
        tl.follow(t, in: m)
        tl.finger = TLGaze.bar(x: x)
        let s = bar.stretch(at: t)
        if lastStretch != s { lastStretch = s; Haptic.selection() }
    }

    /// the finger lifted, or was taken away
    private func lifted() {
        lastStretch = nil
        withAnimation(.easeOut(duration: 0.2)) { tl.follow(nil, in: nil) }
    }

    /// The other drawing of the bar arrives faster than the one it replaces leaves (140 ms, 280 ms), so that the two never both stand half faded and the bar dips in between: the new one is
    /// whole while the old one still goes behind it. Under Reduce Motion a plain cross-fade.
    private var barSwap: AnyTransition {
        reduce ? .opacity : .asymmetric(insertion: .opacity.animation(.easeOut(duration: 0.14)), removal: .opacity.animation(.easeOut(duration: 0.28)))
    }

    /// another day's bar comes in from the way time goes, a hair, as the list does
    private var barTransition: AnyTransition { TLMotion.page(slide: TLMotion.barSlide * Double(tl.slide), reduce: reduce) }
}

/// A finger on the day bar: the minute under it (the bar is `TLLayout.barW` wide and covers `span`) on every move, and a call when it lifts or is taken away (a call, the app leaving).
/// The home timeline and the footprint page each give it what they do with the minute.
struct TLBarScrub: ViewModifier {
    let span: ClosedRange<Double>
    let changed: (_ minute: Double, _ x: Double) -> Void
    let ended: () -> Void
    @GestureState private var touching = false

    /// the minute at `x` points from the bar's left edge, kept on the bar
    static func minute(x: Double, span: ClosedRange<Double>) -> Double { span.lowerBound + clamp(x / TLLayout.barW, 0, 1) * (span.upperBound - span.lowerBound) }

    func body(content: Content) -> some View {
        content
            .gesture(DragGesture(minimumDistance: 0)
                .updating($touching) { _, on, _ in on = true }          // goes back to false when the touch ends or is taken away, which lets the readout go
                .onChanged { v in changed(Self.minute(x: v.location.x, span: span), v.location.x) })
            .onChange(of: touching) { _, on in if !on { ended() } }
    }
}

/// The two lines that say the minute under a finger: the stretch with its hours and length (the name gives way when it is too long), and a line of what else. They follow the finger at once.
struct TLReadoutLines: View {
    let r: TLReadout
    let second: String

    var body: some View {
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
            Text(second).font(.system(size: 13)).foregroundStyle(muted).lineLimit(1).frame(height: 15, alignment: .leading)
        }
        .frame(width: TLLayout.barW, height: TLLayout.lineH, alignment: .topLeading)
        .contentTransition(.identity)           // the words follow the finger at once: a change of them is not a fade of its own, which would show the last stretch's words through the new ones
    }
}

/// 在干嘛 | 在哪: what the day bar is drawn from, a small switch in the margin at the bar's right end. 干嘛 is the bar of what he was doing (the default), 在哪 the bar of the places he stayed
/// in and the trips between them; the list under it is the same either way.
struct TLBarMode: View {
    @Binding var byPlace: Bool
    var reduce = false
    @State private var held: Double?            // where a finger has the glass, 0 (干嘛) to 1 (在哪), while it holds it

    static let width = 24.0, cell = 26.0, height = 2 * cell + 4

    /// A switch the way iOS draws one now: a drop of glass over the side that is on. A finger can take the glass and slide it up and down (it swells a little while held); the bar
    /// changes the moment it crosses the middle, with a tick, and on release it settles on the nearer side with a spring. A tap on a side sends the glass there.
    var body: some View {
        let at = held ?? (byPlace ? 1 : 0)
        ZStack(alignment: .top) {
            drop.scaleEffect(held == nil || reduce ? 1 : 1.12).offset(y: 2 + at * Self.cell)
            VStack(spacing: 0) {
                word("干嘛", on: at < 0.5)
                word("在哪", on: at >= 0.5)
            }
            .padding(2)
            .allowsHitTesting(false)
        }
        .frame(width: Self.width, height: Self.height, alignment: .top)
        .background(Capsule().fill(Color.white.opacity(0.06)))
        .contentShape(Capsule().inset(by: -6))
        .gesture(slide)
        .accessibilityElement()
        .accessibilityLabel("总条按")
        .accessibilityValue(byPlace ? "在哪" : "在干嘛")
        .accessibilityAdjustableAction { d in set(d == .increment) }
        .accessibilityAddTraits(.isButton)
    }

    /// the glass: Liquid Glass where the system has it, a frosted pill before it
    @ViewBuilder private var drop: some View {
        let size = CGSize(width: Self.width - 4, height: Self.cell)
        if #available(iOS 26.0, *) {
            Color.clear.frame(width: size.width, height: size.height).glassEffect(.regular.interactive(), in: Capsule())
        } else {
            Capsule().fill(.ultraThinMaterial).overlay(Capsule().strokeBorder(Color.white.opacity(0.18), lineWidth: 0.5)).frame(width: size.width, height: size.height)
        }
    }

    private func word(_ w: String, on: Bool) -> some View {
        Text(w).font(.system(size: 9.5, weight: .semibold)).foregroundStyle(on ? ink : muted).lineLimit(1).fixedSize()
            .frame(width: Self.width - 4, height: Self.cell)
    }

    /// a drag of no length is a tap: the side under it; a longer one carries the glass
    private var slide: some Gesture {
        DragGesture(minimumDistance: 0)
            .onChanged { v in
                guard abs(v.translation.height) > 3 || held != nil else { return }
                let f = Self.place(v.location.y)
                if (f >= 0.5) != byPlace { set(f >= 0.5) }          // crossing the middle is the change
                withAnimation(.interactiveSpring(response: 0.18, dampingFraction: 0.86)) { held = f }
            }
            .onEnded { v in
                let side = held == nil ? Self.place(v.location.y) >= 0.5 : Self.place(v.location.y + (v.predictedEndLocation.y - v.location.y) * 0.3) >= 0.5
                set(side)
                withAnimation(reduce ? .easeOut(duration: TLMotion.reducedFade) : .spring(response: 0.34, dampingFraction: 0.72)) { held = nil }
            }
    }

    /// where on the switch a finger at `y` (in its own points) holds the glass, 0 to 1
    static func place(_ y: Double) -> Double { clamp((y - 2 - cell / 2) / cell, 0, 1) }

    private func set(_ place: Bool) {
        guard place != byPlace else { return }
        Haptic.selection()
        withAnimation(reduce ? .easeOut(duration: TLMotion.reducedFade) : .bouncy(duration: 0.35, extraBounce: 0)) { byPlace = place }          // the glass goes as it does when a finger lets it go
    }
}

extension StarUI {
    /// the top of the timeline (her sentence, the bar, the day's numbers) is on show: she is in her corner and no panel covers it. Whatever moves in it (the warm line for now breathes at
    /// 30 frames a second) is still while it is not.
    var tlHeaderShown: Bool { tlHome && !panelOpen }
}

// MARK: - the day's numbers

/// a number of a page of its own (the footprint's distance and top speed) in the look of the day's numbers; nothing is brought forward by pressing it
struct TLPlainChip: Equatable, Identifiable {
    var symbol: String
    var color: RGB
    var word: String
    var value: String
    var id: String { word }
}

/// The row of the day's numbers under the hours: a chip for each, pressing one brings that kind forward (and pressing it again lets it go). Sideways when there are more than fit.
struct TLChips: View, Equatable {
    let chips: [TLChip]
    let health: [TLBody]                // what Health reported, after them
    let active: TLLens?
    var extra: [TLPlainChip] = []       // a page's own numbers, after the chips and before what Health reported
    let toggle: (TLLens) -> Void

    static func == (a: TLChips, b: TLChips) -> Bool { a.chips == b.chips && a.health == b.health && a.active == b.active && a.extra == b.extra }

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(chips) { c in chip(c) }
                ForEach(extra) { c in face(c.symbol, c.color, c.word, c.value, on: false) }
                if !health.isEmpty { TLBodyValues(values: health).padding(.leading, chips.isEmpty ? 0 : 8) }        // after the numbers, quiet
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
        return Button { toggle(c.lens) } label: { face(c.lens.symbol, c.lens.color, c.word, c.value, on: on) }
        .buttonStyle(PressScale())
        .accessibilityLabel(c.word + " " + c.value)
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    private func face(_ symbol: String, _ color: RGB, _ word: String, _ value: String, on: Bool) -> some View {
        HStack(spacing: 5) {
            Image(systemName: symbol).font(.system(size: 11, weight: .semibold)).foregroundStyle(color.color).frame(width: 12)
            Text(word).font(.system(size: 11.5)).foregroundStyle(ink.opacity(0.86))
            Text(value).font(AT.mono(11.5)).foregroundStyle(ink)
        }
        .fixedSize()
        .padding(.horizontal, 9).frame(height: TLLayout.chipsH)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(on ? 0.13 : 0.06)))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(ink.opacity(on ? 0.6 : 0), lineWidth: 1))
        .animation(.easeOut(duration: 0.15), value: on)
    }
}

extension TimelineState {
    /// the lens that is on, if this day has that kind at all
    func activeLens(in m: DayTimelineModel) -> TLLens? {
        lens.flatMap { l in m.chips.contains { $0.lens == l } ? l : nil }
    }
}

/// The change of what the header says: what goes and what comes cross for 300 ms (a spring that does not bounce) and are blurred while they do, so that the two are never readable together
/// and no moment is left with nothing on show. With Reduce Motion a plain 120 ms fade.
extension View {
    func fadeThrough(on: Bool, reduce: Bool) -> some View {
        opacity(on ? 1 : 0).blur(radius: on || reduce ? 0 : 4)
            .animation(reduce ? .easeOut(duration: TLMotion.reducedFade) : .smooth(duration: 0.3), value: on)
    }
}
