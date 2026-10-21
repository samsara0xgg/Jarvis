// words: legacy
import SwiftUI

// Her sentence, the overview bar under it and the day's numbers under that: the part of the timeline that follows his finger. It is a view of its own because the minute under a finger
// changes on every frame and whatever reads that minute is drawn again on every frame: kept here, that is this and nothing else of the screen, not the model of the day and not the list.
// While a talk is on show (`ui.tlTalk`) the three give way to it, the way they give way to the readout of a finger: Sky/DayTimelineTalk.swift draws it in their place. While the brain tidies
// today, and for a while after it answers with nothing new or fails, what it says of it is in her sentence's place.

private let ink = SetColor.ink
private let muted = SetColor.muted

struct TLTopView: View {
    let ui: StarUI
    let model: DayTimelineModel?
    let scene: TLBarScene                       // the bar of it, made once with the model and not on every frame of a finger
    var loading = false                         // the day is being asked for: its bar is an empty track with a soft light going along it
    @Environment(\.accessibilityReduceMotion) private var reduce
    @State private var lastScrub = 0.0                                               // where his finger was last: what the readout shows while it fades out
    @State private var lastStretch: String?
    @State private var lastLens = TLLens.trip                                        // the lens that was on last: what fades out when it goes
    @State private var lastTidy = ""                                                 // what was said of the tidying last: what fades out when it goes
    @State private var nowSide: Bool?                                                // which side of now a finger on the bar is: crossing it is a firm tap
    @State private var ghost: TLBarScene?                                            // the bar as it was when the brain wrote a tidied day, which fades off the new one (`tl.tidied`)
    @State private var ghostGone = false
    @State private var ghostTask: Task<Void, Never>?

    private var tl: TimelineState { ui.tl }

    var body: some View {
        let holding = tl.scrub != nil, talk = ui.tlTalk
        let tidying = tl.tidy != .idle && tl.day == tl.today ? tl.tidy.words : nil          // what is said of the brain tidying today, in her sentence's place
        let said = model.map { $0.readout(at: tl.scrub ?? lastScrub) }          // once for the readout and for VoiceOver's value, on every frame of a finger
        return ZStack(alignment: .topLeading) {
            // her sentence, and in its place while a finger is on the bar the readout of the minute under it: they cross, blurred (`fadeThrough`), so the two are never readable together
            Group {          // each animation below is on what it moves, not on the whole of the header
                if let line = model?.line {
                    Text(line).font(.system(size: 15)).lineSpacing(3).foregroundStyle(ink.opacity(0.9)).lineLimit(2)
                        .frame(width: TLLayout.barW, height: TLLayout.lineH, alignment: .topLeading)
                        .fadeThrough(on: !holding && !talk && tidying == nil, reduce: reduce)
                        .accessibilityHidden(talk || tidying != nil)
                        .allowsHitTesting(false)          // she may drift behind it (Sky/DayTimelineSeat.swift): a touch on her there is still on her
                        .id(line).transition(TLMotion.swapWords(reduce: reduce))
                        .offset(x: 30, y: TLLayout.lineY)
                }
            }
            .animation(TLMotion.dayChange(reduce: reduce), value: model?.line)
            Text(tidying ?? lastTidy).font(.system(size: 15)).foregroundStyle(ink.opacity(0.9)).lineLimit(1)
                .frame(width: TLLayout.barW, height: TLLayout.lineH, alignment: .topLeading)
                .id(tidying ?? lastTidy).transition(TLMotion.swapWords(reduce: reduce)).animation(.easeOut(duration: 0.2), value: tidying)
                .fadeThrough(on: tidying != nil && !holding && !talk, reduce: reduce)
                .accessibilityHidden(tidying == nil || talk)
                .allowsHitTesting(false)
                .offset(x: 30, y: TLLayout.lineY)
                .onChange(of: tidying, initial: true) { _, w in if let w { lastTidy = w } }
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
            if let m = model {
                let lens = tl.activeLens(in: m)
                ZStack(alignment: .topLeading) {
                    TLBarView(scene: scene, bracket: bracket(m), scrub: tl.scrub, lens: lens != nil, lensColor: (lens ?? lastLens).color, lensMarks: m.marks(of: lens ?? lastLens),
                              reveal: tl.reveal, reduce: reduce, breathe: ui.tlHeaderShown,
                              sweeping: tl.tidy == .running && tl.day == tl.today && !reduce, ghost: ghost, ghostGone: ghostGone)
                }
                .animation(holding ? .easeOut(duration: 0.08) : .easeInOut(duration: 0.15), value: holding)       // the hairline and its time come and go with the finger
                .modifier(TLBarScrub(span: m.span, changed: { t, x in scrubbed(t, x, m) }, ended: lifted))
                .accessibilityElement().accessibilityLabel("一天的概览")
                .accessibilityHint("拖动可以翻到那个时间，上下滑动一段一段地看")
                .accessibilityValue(tl.scrub != nil ? said.map(spoken) ?? "" : "")
                .accessibilityAdjustableAction { nudge($0, m) }
                .fadeThrough(on: !talk, reduce: reduce).allowsHitTesting(!talk).accessibilityHidden(talk)
                .id(tl.day).transition(barTransition)
                .offset(x: 30, y: TLLayout.barY)
                if !m.chips.isEmpty || !m.body.isEmpty {
                    TLChips(chips: m.chips, health: m.body, active: lens) { l in
                        Haptic.selection()
                        let on = tl.lens != l
                        withAnimation(.easeOut(duration: 0.15)) { tl.lens = on ? l : nil }
                        if on, let r = tl.reply { tl.revealLens(l, in: tl.model(r)) }          // the list goes to the first of that kind (the day as it is now: the chips do not redraw for a change that leaves them as they were)
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

    /// a finger on the bar: the list follows it, with a tick of the phone for each stretch of the bar it crosses, and a firm tap where it crosses now
    private func scrubbed(_ t: Double, _ x: Double, _ m: DayTimelineModel) {
        tl.follow(t, in: m)
        tl.finger = TLGaze.bar(x: x)
        let side = m.now.map { m.onBar(t) >= $0 }
        if let side, let was = nowSide, side != was { Haptic.impact(.rigid); lastStretch = m.stretch(at: t) }
        nowSide = side
        let s = m.stretch(at: t)
        if lastStretch != s { lastStretch = s; Haptic.selection() }
    }

    /// the finger lifted, or was taken away
    private func lifted() {
        lastStretch = nil; nowSide = nil
        withAnimation(.easeOut(duration: 0.2)) { tl.follow(nil, in: nil) }
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
    @Environment(\.tlBarWidth) private var width

    /// the minute at `x` points from the left edge of a bar `width` wide, kept on the bar
    static func minute(x: Double, span: ClosedRange<Double>, width: Double = TLLayout.barW) -> Double { span.lowerBound + clamp(x / width, 0, 1) * (span.upperBound - span.lowerBound) }

    func body(content: Content) -> some View {
        content
            .gesture(DragGesture(minimumDistance: 0)
                .updating($touching) { _, on, _ in on = true }          // goes back to false when the touch ends or is taken away, which lets the readout go
                .onChanged { v in changed(Self.minute(x: v.location.x, span: span, width: width), v.location.x) })
            .onChange(of: touching) { _, on in if !on { ended() } }
    }
}

/// The two lines that say the minute under a finger: the stretch with its hours and length (the name gives way when it is too long), and a line of what else. They follow the finger at once.
struct TLReadoutLines: View {
    let r: TLReadout
    let second: String
    @Environment(\.tlBarWidth) private var width

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
        .frame(width: width, height: TLLayout.lineH, alignment: .topLeading)
        .contentTransition(.identity)           // the words follow the finger at once: a change of them is not a fade of its own, which would show the last stretch's words through the new ones
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
    var lead: [TLPlainChip] = []        // a page's own, before the chips
    var width = W                       // the row's width: the phone's, or the iPad's top band
    let toggle: (TLLens) -> Void

    static func == (a: TLChips, b: TLChips) -> Bool { a.chips == b.chips && a.health == b.health && a.active == b.active && a.extra == b.extra && a.lead == b.lead && a.width == b.width }

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(lead) { c in face(c.symbol, c.color, c.word, c.value, on: false) }
                ForEach(chips) { c in chip(c) }
                ForEach(extra) { c in face(c.symbol, c.color, c.word, c.value, on: false) }
                if !health.isEmpty { TLBodyValues(values: health).padding(.leading, chips.isEmpty ? 0 : 8) }        // after the numbers, quiet
            }
            .padding(.horizontal, 30)
            .frame(height: TLLayout.chipsHit)
        }
        .frame(width: width, height: TLLayout.chipsHit)
        // a chip going off the side goes softly
        .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 24 / width), .init(color: .black, location: 1 - 24 / width), .init(color: .clear, location: 1)],
                             startPoint: .leading, endPoint: .trailing))
    }

    private func chip(_ c: TLChip) -> some View {
        let on = active == c.lens
        return Button { toggle(c.lens) } label: { face(c.symbol ?? c.lens.symbol, c.lens.color, c.word, c.value, on: on) }
        .buttonStyle(PressScale())
        .accessibilityLabel(c.word + " " + c.value)
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    private func face(_ symbol: String, _ color: RGB, _ word: String, _ value: String, on: Bool) -> some View {
        HStack(spacing: 5) {
            Image(systemName: symbol).font(.system(size: 11, weight: .semibold)).foregroundStyle(color.color).frame(width: 12)
            Text(word).font(.system(size: 11.5)).foregroundStyle(ink.opacity(0.86))
            Text(value).font(tlFigures(11.5)).foregroundStyle(ink)
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
