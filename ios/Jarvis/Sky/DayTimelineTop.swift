// words: legacy
import SwiftUI

// The pinned top of the phone's 今天 page (跨端UI标准 §2–4), under her: the strip (日历, the date, ✦ 润色), the hours, the day's bar with the stars on it, the place line and the day's numbers. It is a
// view of its own because the minute under a finger changes on every frame and whatever reads that minute is drawn again on every frame: kept here, that is this and nothing else of the screen,
// not the model of the day and not the list. A tap on the bar is not a finger held on it: it opens the row of the block tapped, or frames the block for a moment (`TimelineState.tapBar`).
// Under the date her words stand while she talks (Sky/DayTimelineTalk.swift) and, while the brain tidies today, what is said of it (and for a while after it answers with nothing new or fails); the
// bar, the place line and the numbers stand lower by their room (`TLCaption`). A finger on the bar says its minute in the date's place instead, and nothing moves under it.

private let ink = SetColor.ink
private let muted = SetColor.muted

struct TLTopView: View {
    let ui: StarUI
    let model: DayTimelineModel?
    let scene: TLBarScene                       // the bar of it, made once with the model and not on every frame of a finger
    var loading = false                         // the day is being asked for: its bar is an empty track with a soft light going along it
    var room = TLLayout.viewport                // how tall the list is under the pinned cards: how far up it can carry a row the bar opens
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
    private typealias T = TLLayout.Today

    var body: some View {
        let holding = tl.scrub != nil, talk = ui.tlTalk
        let tidying = tl.tidy != .idle && tl.day == tl.today ? tl.tidy.words : nil          // what is said of the brain tidying today, under the date
        let said = model.map { $0.readout(at: tl.scrub ?? lastScrub) }          // once for the readout and for VoiceOver's value, on every frame of a finger
        let shift = TLCaption.shared.shift
        return ZStack(alignment: .topLeading) {
            TopStrip {
                FootprintDayPicker(ui: ui)
            } middle: {
                date.fadeThrough(on: !holding, reduce: reduce).accessibilityHidden(holding).allowsHitTesting(false)          // she may drift behind it: a touch on her there is still on her
            } right: {
                if tl.tidyShown {
                    TopGlassButton(symbol: "sparkles", label: Words.PhoneToday.polish, on: tl.tidy != .running) { tl.tidyDay() }
                        .allowsHitTesting(tl.tidy != .running)
                }
            }
            // what is said of the tidying, under the date; her words while she talks are TLTalkArea's
            Text(tidying ?? lastTidy).font(.system(size: Look.body, weight: .medium)).foregroundStyle(ink).lineLimit(1)
                .frame(width: W - 88, height: TLCaption.tidyLine)
                .id(tidying ?? lastTidy).transition(TLMotion.swapWords(reduce: reduce)).animation(.easeOut(duration: 0.2), value: tidying)
                .fadeThrough(on: tidying != nil && !holding && !talk, reduce: reduce)
                .accessibilityHidden(tidying == nil || talk)
                .allowsHitTesting(false)
                .offset(x: 44, y: T.captionY)
            // the minute under a finger on the bar, in the date's place
            if let said {
                TLReadoutLines(r: said, second: said.second, centered: true).environment(\.tlBarWidth, W - 80)
                    .fadeThrough(on: holding, reduce: reduce)
                    .offset(x: 40, y: HomeTop.lineY - 2)
                    .allowsHitTesting(false).accessibilityHidden(!holding)
            }
            ZStack(alignment: .topLeading) {
                Group {
                    if model == nil && loading {
                        TLTrack(loading: true, reduce: reduce).id(tl.day).transition(.opacity).offset(x: T.side, y: T.barY)
                    }
                }
                .animation(TLMotion.dayChange(reduce: reduce), value: model == nil)
                if let m = model {
                    let lens = tl.activeLens(in: m), axis = TLAxis.fair(scene, width: T.barW)
                    ZStack(alignment: .topLeading) {
                        TLBarView(scene: scene, bracket: bracket(m), scrub: tl.scrub, lens: lens != nil, lensColor: (lens ?? lastLens).color, lensMarks: m.marks(of: lens ?? lastLens),
                                  reveal: tl.reveal, reduce: reduce, breathe: ui.tlHeaderShown,
                                  sweeping: tl.tidy == .running && tl.day == tl.today && !reduce, ghost: ghost, ghostGone: ghostGone, fair: true, blink: tl.blink,
                                  phone: true, stars: m.stars)
                        TLViewportFrame(tl: tl, axis: axis)
                    }
                    .animation(.easeOut(duration: holding ? 0.08 : 0.15), value: holding)       // the hairline comes and goes with the finger
                    .modifier(TLBarScrub(span: m.span, axis: axis, changed: { t, x in scrubbed(t, x, m) }, ended: lifted, tap: { t in tl.tapBar(t, in: m, room: room) }))
                    .accessibilityElement().accessibilityLabel("一天的概览")
                    .accessibilityHint("拖动可以翻到那个时间，上下滑动一段一段地看")
                    .accessibilityValue(tl.scrub != nil ? said.map(spoken) ?? "" : "")
                    .accessibilityAdjustableAction { nudge($0, m) }
                    .id(tl.day).transition(barTransition)
                    .offset(x: T.side, y: T.barY)
                    if !m.places.isEmpty {
                        TLPlaceLine(places: m.places, axis: axis).id(tl.day).transition(barTransition)
                            .opacity(tl.reveal ? 1 : 0).animation(TLMotion.arrival(after: 0.4, reveal: tl.reveal, reduce: reduce), value: tl.reveal)
                            .offset(x: T.side, y: T.placeY)
                    }
                    if !m.chips.isEmpty || !m.body.isEmpty {
                        TLPhoneChips(chips: m.chips, health: m.body, active: lens) { l in
                            Haptic.selection()
                            let on = tl.lens != l
                            withAnimation(.easeOut(duration: 0.15)) { tl.lens = on ? l : nil }
                            if on, let r = tl.reply { tl.revealLens(l, in: tl.model(r)) }          // the list goes to the first of that kind (the day as it is now: the chips do not redraw for a change that leaves them as they were)
                        }
                        .equatable()
                        .id(tl.day).transition(barTransition)
                        .offset(y: T.chipsY - 2)
                    }
                }
            }
            .offset(y: shift)
            .animation(reduce ? .easeOut(duration: TLMotion.reducedFade) : .easeOut(duration: 0.3), value: shift)          // the words under the date come and the rest stands lower, no bounce
        }
        .environment(\.tlBarWidth, T.barW).environment(\.tlBarHeight, T.barH).environment(\.tlBarRadius, T.barRadius)
        .onDisappear { lastStretch = nil; tl.follow(nil, in: nil) }          // a finger that was on the bar when this went away is not on it when it comes back
        .onChange(of: tl.lens) { _, l in if let l { lastLens = l } }
        .onChange(of: tl.tidied) { _, s in if s != nil { holdGhost() } }
        .onChange(of: tl.reply?.serial) { _, _ in releaseGhost() }
        .onChange(of: tl.scrub) { _, t in if let t { lastScrub = t } }
        .onChange(of: shift, initial: true) { _, v in tl.captionLift = v }          // the list stands lower by the same (Sky/DayTimeline.swift)
        .onChange(of: tidying != nil, initial: true) { _, on in TLCaption.shared.tidyH = on ? TLCaption.tidyLine : 0 }
        .onChange(of: tidying, initial: true) { _, w in if let w { lastTidy = w } }
        // only once she is in her corner, and gone as soon as she leaves it: she glides across where they stand
        .opacity(ui.tlHome ? 1 : 0).allowsHitTesting(ui.tlHome)
        .animation(.easeOut(duration: ui.tlHome ? (reduce ? TLMotion.reducedFade : 0.3) : 0.06), value: ui.tlHome)
    }

    /// the date under her, `10月23日 周五`, and after it the label of an all-day event of that day
    private var date: some View {
        (Text(ui.dateText) + Text(ui.dateLabel.map { " · " + $0 } ?? ""))
            .font(.system(size: Look.note)).tracking(0.4).foregroundStyle(Look.ink2).lineLimit(1)
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
        tl.follow(t, in: m, inPlace: true)
        tl.holdThenRelease()
    }

    /// the block of the row open where it stands, on the bar (`m` is the bar's own view of the day)
    private func bracket(_ m: DayTimelineModel) -> ClosedRange<Double>? {
        guard let id = tl.openRow, let e = m.entries.first(where: { $0.id == id }), e.kind != .now else { return nil }
        return e.t0...max(e.t0, e.t1 ?? e.t0)
    }

    /// a finger held on the bar: the list follows it, a row open where it stands staying open, with a tick of the phone for each stretch of the bar it crosses, and a firm tap where it
    /// crosses now
    private func scrubbed(_ t: Double, _ x: Double, _ m: DayTimelineModel) {
        tl.follow(t, in: m, inPlace: true)
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

/// A finger on the day bar: the minute under it (the bar is `TLLayout.barW` wide and covers `span`, where `axis` draws it) on every move, and a call when it lifts or is taken away (a call,
/// the app leaving). The home timeline and the footprint page each give it what they do with the minute. Given `tap`, a touch is a finger held on the bar only once it has stayed 180 ms or
/// moved more than 5 pt: one that lifts before is a tap, at the minute it lifted.
struct TLBarScrub: ViewModifier {
    let span: ClosedRange<Double>
    var axis: TLAxis?
    let changed: (_ minute: Double, _ x: Double) -> Void
    let ended: () -> Void
    var tap: ((_ minute: Double) -> Void)?
    @GestureState private var touching = false
    @State private var touch = Touch()
    @Environment(\.tlBarWidth) private var width

    static let holdFor = 0.18, slop = 5.0

    /// a touch that may still be a tap: where it began, where it is, whether it has become a finger held on the bar, and the wait that makes it one (kept by the modifier and never drawn
    /// from, so a reference)
    final class Touch {
        var start: Double?
        var x = 0.0
        var held = false
        var wait: Task<Void, Never>?
    }

    /// the minute at `x` points from the left edge of a bar `width` wide, kept on the bar
    static func minute(x: Double, span: ClosedRange<Double>, width: Double = TLLayout.barW) -> Double { span.lowerBound + clamp(x / width, 0, 1) * (span.upperBound - span.lowerBound) }
    private func minute(_ x: Double) -> Double { axis?.minute(x) ?? Self.minute(x: x, span: span, width: width) }

    func body(content: Content) -> some View {
        content
            .gesture(DragGesture(minimumDistance: 0)
                .updating($touching) { _, on, _ in on = true }          // goes back to false when the touch ends or is taken away, which lets the readout go
                .onChanged { v in moved(v.location.x, from: v.startLocation.x) }
                .onEnded { v in lifted(v.location.x) })
            .onChange(of: touching) { _, on in
                guard !on else { return }
                if tap == nil { ended() } else { DispatchQueue.main.async { taken() } }         // after `lifted`, which a touch that lifts has had: one still here was taken away
            }
    }

    private func moved(_ x: Double, from x0: Double) {
        guard tap != nil else { changed(minute(x), x); return }
        let t = touch
        t.x = x
        if t.start == nil {
            t.start = x0; t.held = false
            t.wait = Task { @MainActor in
                try? await Task.sleep(nanoseconds: UInt64(Self.holdFor * 1e9))
                guard !Task.isCancelled, t.start != nil, !t.held else { return }
                t.held = true
                changed(minute(t.x), t.x)
            }
        }
        if !t.held && abs(x - (t.start ?? x)) > Self.slop { t.wait?.cancel(); t.held = true }
        if t.held { changed(minute(x), x) }
    }

    private func lifted(_ x: Double) {
        guard let tap else { return }
        let t = touch, held = t.held
        t.wait?.cancel(); t.wait = nil; t.start = nil; t.held = false
        if held { ended() } else { tap(minute(x)) }
    }

    private func taken() {
        let t = touch
        guard t.start != nil else { return }
        let held = t.held
        t.wait?.cancel(); t.wait = nil; t.start = nil; t.held = false
        if held { ended() }
    }
}

/// The two lines that say the minute under a finger: the stretch with its hours and length (the name gives way when it is too long), and a line of what else. They follow the finger at once.
struct TLReadoutLines: View {
    let r: TLReadout
    let second: String
    var centered = false                // under the date on the phone's 今天 page
    @Environment(\.tlBarWidth) private var width

    var body: some View {
        let edge: Alignment = centered ? .center : .leading
        VStack(alignment: centered ? .center : .leading, spacing: 1) {
            HStack(spacing: 7) {
                Circle().fill((r.hue ?? RGB(196, 204, 238)).color(r.hue == nil ? 0 : r.strong ? 1 : 0.5)).frame(width: 8, height: 8)
                    .overlay(Circle().stroke(ink.opacity(r.hue == nil ? 0.4 : 0), lineWidth: 1))
                HStack(spacing: 0) {
                    Text(r.name).lineLimit(1)
                    Text(r.tail).fixedSize().layoutPriority(1)
                }
                .font(.system(size: 15, weight: .semibold)).foregroundStyle(ink)
            }
            .frame(height: 19, alignment: edge)
            Text(second).font(.system(size: 13)).foregroundStyle(muted).lineLimit(1).frame(height: 15, alignment: edge)
        }
        .frame(width: width, height: TLLayout.lineH, alignment: centered ? .top : .topLeading)
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

/// The row of the day's numbers under the hours: a chip for each, pressing one brings that kind forward (and pressing it again lets it go). Sideways when there are more than fit; `whole`
/// (the phone's timeline), only those that fit the row, in order, none cut at its edge.
struct TLChips: View, Equatable {
    let chips: [TLChip]
    let health: [TLBody]                // what Health reported, after them
    let active: TLLens?
    var extra: [TLPlainChip] = []       // a page's own numbers, after the chips and before what Health reported
    var lead: [TLPlainChip] = []        // a page's own, before the chips
    var width = W                       // the row's width: the phone's, or the iPad's top band
    var whole = false
    let toggle: (TLLens) -> Void

    static func == (a: TLChips, b: TLChips) -> Bool {
        a.chips == b.chips && a.health == b.health && a.active == b.active && a.extra == b.extra && a.lead == b.lead && a.width == b.width && a.whole == b.whole
    }

    var body: some View {
        if whole {
            let n = fit
            row(Array(lead.prefix(n.lead)), Array(chips.prefix(n.chips)), Array(extra.prefix(n.extra)), Array(health.prefix(n.health)))
                .padding(.horizontal, 30)
                .frame(width: width, height: TLLayout.chipsHit, alignment: .leading)
        } else {
            ScrollView(.horizontal, showsIndicators: false) {
                row(lead, chips, extra, health)
                    .padding(.horizontal, 30)
                    .frame(height: TLLayout.chipsHit)
            }
            .frame(width: width, height: TLLayout.chipsHit)
            // a chip going off the side goes softly
            .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 24 / width), .init(color: .black, location: 1 - 24 / width), .init(color: .clear, location: 1)],
                                 startPoint: .leading, endPoint: .trailing))
        }
    }

    private func row(_ lead: [TLPlainChip], _ chips: [TLChip], _ extra: [TLPlainChip], _ health: [TLBody]) -> some View {
        HStack(spacing: 6) {
            ForEach(lead) { c in face(c.symbol, c.color, c.word, c.value, on: false) }
            ForEach(chips) { c in chip(c) }
            ForEach(extra) { c in face(c.symbol, c.color, c.word, c.value, on: false) }
            if !health.isEmpty { TLBodyValues(values: health).padding(.leading, chips.isEmpty ? 0 : 8) }        // after the numbers, quiet
        }
    }

    /// how many of each fit the row whole, in order: the faces 6 pt apart, then (8 pt on) Health's values 14 pt apart, while every face is in
    private var fit: (lead: Int, chips: Int, extra: Int, health: Int) {
        let faces = lead.map { Self.faceWidth($0.word, $0.value) } + chips.map { Self.faceWidth($0.word, $0.value) } + extra.map { Self.faceWidth($0.word, $0.value) }
        let room = width - 60, n = Self.fitting(faces, room: room, gap: 6)
        let used = n == 0 ? 0 : faces.prefix(n).reduce(0, +) + 6 * Double(n - 1) + 6 + (chips.isEmpty ? 0 : 8)
        let h = n < faces.count ? 0 : Self.fitting(health.map(Self.bodyWidth), room: room - used, gap: 14)
        return (min(n, lead.count), min(max(0, n - lead.count), chips.count), max(0, n - lead.count - chips.count), h)
    }

    /// how wide a chip's face is: its icon, its word and its value, 9 pt either side
    static func faceWidth(_ word: String, _ value: String) -> Double {
        12 + 5 + ceil(tlWidth(word, size: 11.5)) + 5 + ceil(tlWidth(value, size: 11.5, weight: .medium, figures: true)) + 18
    }
    /// how wide one of Health's values is
    static func bodyWidth(_ v: TLBody) -> Double { ceil(tlWidth(v.prefix, size: 11.5)) + ceil(tlWidth(v.value, size: 11.5, mono: true)) + ceil(tlWidth(v.suffix, size: 11.5)) }
    /// how many of `widths` fit `room` in order, `gap` between them: the first that does not, and every one after it, are left out
    static func fitting(_ widths: [Double], room: Double, gap: Double) -> Int {
        var used = -gap
        for (i, w) in widths.enumerated() {
            used += gap + w
            if used > room { return i }
        }
        return widths.count
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
