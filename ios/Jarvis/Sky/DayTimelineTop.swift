import SwiftUI

// Her sentence, the overview bar under it and the day's numbers under that: the part of the timeline that follows his finger. It is a view of its own because the minute under a finger
// changes on every frame and whatever reads that minute is drawn again on every frame: kept here, that is this and nothing else of the screen, not the model of the day and not the list.

private let ink = SetColor.ink
private let muted = SetColor.muted

struct TLTopView: View {
    let ui: StarUI
    let model: DayTimelineModel?
    let scene: TLBarScene                       // the model's bar, made once with the model and not on every frame of a finger
    @Environment(\.accessibilityReduceMotion) private var reduce
    @GestureState private var touching = false                                       // a finger is on the bar
    @State private var lastScrub = 0.0                                               // where his finger was last: what the readout shows while it fades out
    @State private var lastStretch: String?
    @State private var lastLens = TLLens.trip                                        // the lens that was on last: what fades out when it goes

    private var tl: TimelineState { ui.tl }

    var body: some View {
        let holding = tl.scrub != nil
        return ZStack(alignment: .topLeading) {
            // her sentence, and in its place while a finger is on the bar the readout of the minute under it: a fade-through, not a cross-fade (the two are never both on show)
            if let line = model?.line {
                Text(line).font(.system(size: 15)).lineSpacing(3).foregroundStyle(ink.opacity(0.9)).lineLimit(2)
                    .frame(width: TLLayout.barW, height: TLLayout.lineH, alignment: .topLeading)
                    .fadeThrough(on: !holding, reduce: reduce)
                    .id(line).transition(.opacity)
                    .offset(x: 30, y: TLLayout.lineY)
            }
            if let m = model {
                readout(m.readout(at: tl.scrub ?? lastScrub)).fadeThrough(on: holding, reduce: reduce)
                    .offset(x: 30, y: TLLayout.lineY)
                    .allowsHitTesting(false).accessibilityHidden(!holding)
            }
            if let m = model {
                let lens = tl.activeLens(in: m)
                TLBarView(scene: scene, bracket: bracket(m), scrub: tl.scrub, lens: lens != nil, lensColor: (lens ?? lastLens).color, lensMarks: m.marks(of: lens ?? lastLens),
                          reveal: tl.reveal, reduce: reduce, breathe: ui.tlHeaderShown)
                    .animation(holding ? .easeOut(duration: 0.08) : .easeInOut(duration: 0.15), value: holding)       // the hairline and its time come and go with the finger
                    .gesture(scrubGesture(m))
                    .accessibilityElement().accessibilityLabel("一天的概览")
                    .accessibilityHint("拖动可以翻到那个时间，上下滑动一段一段地看")
                    .accessibilityValue(tl.scrub.map { spoken(m.readout(at: $0)) } ?? "")
                    .accessibilityAdjustableAction { nudge($0, m) }
                    .id(tl.day).transition(barTransition)
                    .offset(x: 30, y: TLLayout.barY)
                if !m.chips.isEmpty || !m.body.isEmpty {
                    TLChips(chips: m.chips, health: m.body, active: lens) { l in
                        Haptic.selection()
                        withAnimation(.easeOut(duration: 0.15)) { tl.lens = tl.lens == l ? nil : l }
                    }
                    .equatable()
                    .id(tl.day).transition(barTransition)
                    .offset(y: TLLayout.chipsY - (TLLayout.chipsHit - TLLayout.chipsH) / 2)
                }
            }
        }
        .onDisappear { lastStretch = nil; tl.follow(nil, in: nil) }          // a finger that was on the bar when this went away is not on it when it comes back
        .onChange(of: tl.lens) { _, l in if let l { lastLens = l } }
        .onChange(of: tl.scrub) { _, t in if let t { lastScrub = t } }
        .onChange(of: touching) { _, on in
            guard !on else { return }
            lastStretch = nil
            withAnimation(.easeOut(duration: 0.2)) { tl.follow(nil, in: nil) }
        }
        .animation(TLMotion.dayChange(reduce: reduce), value: model?.line)
        // only once she is in her corner, and gone as soon as she leaves it: she glides across where they stand
        .opacity(ui.tlHome ? 1 : 0).allowsHitTesting(ui.tlHome)
        .animation(.easeOut(duration: ui.tlHome ? (reduce ? TLMotion.reducedFade : 0.3) : 0.06), value: ui.tlHome)
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
        let d = TLMotion.barSlide * Double(tl.slide)
        return .asymmetric(insertion: .opacity.combined(with: .offset(x: -d)), removal: .opacity.combined(with: .offset(x: d)))
    }
}

extension StarUI {
    /// the top of the timeline (her sentence, the bar, the day's numbers) is on show: she is in her corner and no panel covers it. Whatever moves in it (the warm line for now breathes at
    /// 30 frames a second) is still while it is not.
    var tlHeaderShown: Bool { tlHome && !panelOpen }
}

// MARK: - the day's numbers

/// The row of the day's numbers under the hours: a chip for each, pressing one brings that kind forward (and pressing it again lets it go). Sideways when there are more than fit.
struct TLChips: View, Equatable {
    let chips: [TLChip]
    let health: [TLBody]                // what Health reported, after them
    let active: TLLens?
    let toggle: (TLLens) -> Void

    static func == (a: TLChips, b: TLChips) -> Bool { a.chips == b.chips && a.health == b.health && a.active == b.active }

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(chips) { c in chip(c) }
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

extension TimelineState {
    /// the lens that is on, if this day has that kind at all
    func activeLens(in m: DayTimelineModel) -> TLLens? {
        lens.flatMap { l in m.chips.contains { $0.lens == l } ? l : nil }
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
