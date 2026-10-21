import SwiftUI
import MapKit

// The pieces of the footprint's one-day screen (Sky/FootprintDayPage.swift places them). Each reads the held minute itself, so that a finger on the bar draws these small views and not the page.

private let ink = SetColor.ink
private let muted = SetColor.muted

/// the way back, the date, the today label (a label) or on another day the way back to it, and the page's name; laid out clear of her corner as a page of the timeline is
struct FootprintDayHead: View {
    let ui: StarUI
    let back: () -> Void

    var body: some View {
        let tl = ui.tl, today = tl.day == tl.today
        HStack(spacing: 2) {
            Button(action: back) {
                Image(systemName: "chevron.left").font(.system(size: 17, weight: .semibold)).foregroundStyle(ink)
                    .frame(width: 44, height: 44).contentShape(Rectangle())
            }
            .buttonStyle(PressScale())
            .accessibilityLabel(Words.Footprint.back)
            HStack(spacing: 10) {
                Text(ui.dateText).font(.system(size: 17, weight: .semibold)).foregroundStyle(ink).lineLimit(1)
                if today {
                    Text(Words.Footprint.today).font(.system(size: 12, weight: .semibold)).foregroundStyle(ink.opacity(0.9)).padding(.horizontal, 9).frame(height: 22)
                        .background(Color.white.opacity(0.1), in: Capsule())
                } else {
                    Button { tl.goToday() } label: { TLBackLabel(text: Words.Footprint.backToToday).contentShape(Rectangle().inset(by: -9)) }
                        .buttonStyle(PressScale())
                }
                Text(Words.Footprint.name).font(.system(size: 12.5)).foregroundStyle(muted).lineLimit(1)
            }
            .fixedSize()
        }
        .frame(height: 44)
    }
}

/// The sentence of the day under the head, and in its place while the bar is held the timeline's readout of that minute with where he was, how fast and his heart under it; they cross, blurred.
struct FootprintDayLine: View {
    let ui: StarUI
    let summary: String
    let bar: DayTimelineModel?
    let model: FootprintModel?
    @State private var last = 0.0               // where his finger was last: what the readout shows while it fades out
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        let held = ui.footprint.minute, holding = held != nil
        ZStack(alignment: .topLeading) {
            Text(summary).font(.system(size: 15)).lineSpacing(3).foregroundStyle(ink.opacity(0.9)).lineLimit(2)
                .frame(width: TLLayout.barW, height: TLLayout.lineH, alignment: .topLeading)
                .fadeThrough(on: !holding, reduce: reduce)
            if let bar {
                let t = held ?? last, r = bar.readout(at: t)
                TLReadoutLines(r: r, second: model.map { FootprintDayContent.heldLine($0, minute: t, elsewhere: r.second) } ?? r.second)
                    .fadeThrough(on: holding, reduce: reduce).accessibilityHidden(!holding)
            }
        }
        .allowsHitTesting(false)
        .onChange(of: held) { _, t in if let t { last = t } }
    }
}

/// The map under the line, fading into the sky at its top and its foot. The shape of the day is the content's; the dot of the held minute is the one thing that moves, and while it is held the camera follows it.
struct FootprintDayMap: View {
    let ui: StarUI
    let content: FootprintDayContent
    let model: FootprintModel?
    let fitKey: String

    var body: some View {
        let dot = ui.footprint.minute.flatMap { t in model.flatMap { FootprintDayContent.dot($0, minute: t) } }
        FootprintMapView(routes: content.routes, places: content.places, tokens: content.tokens, now: content.now, dot: dot, follow: dot.flatMap { model?.follow(atMs: $0.ms) },
                         fitPadding: UIEdgeInsets(top: 60, left: 24, bottom: 36, right: 110), fitKey: fitKey, legalInset: 44)          // MapKit's legal label stands above the fade
            .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 70 / FootprintDayPage.Y.mapH),
                                         .init(color: .black, location: 1 - 36 / FootprintDayPage.Y.mapH), .init(color: .clear, location: 1)],
                                 startPoint: .top, endPoint: .bottom))
    }
}

/// slow, the ramp, fast: what the colour of a route says
struct FootprintDayLegend: View {
    var body: some View {
        HStack(spacing: 6) {
            Text(Words.Footprint.slow)
            LinearGradient(stops: FootprintPaint.ramp.map { .init(color: $0.rgb.color, location: $0.kmh / (FootprintPaint.ramp.last?.kmh ?? 1)) }, startPoint: .leading, endPoint: .trailing)
                .frame(width: 56, height: 4).clipShape(Capsule())
            Text(Words.Footprint.fast)
        }
        .font(.system(size: 12.5)).foregroundStyle(muted).lineLimit(1)
        .allowsHitTesting(false)
    }
}

/// The day's bar as home draws it, with a finger on it that holds a minute for the line, the map and the list, and under it the same numbers and the two of this page.
struct FootprintDayBar: View {
    let ui: StarUI
    let built: TLBuilt
    let byPlace: Bool
    let extra: [TLPlainChip]
    let entries: [TLEntry]
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        let m = built.model, bar = built.bar(places: byPlace), state = ui.footprint
        let lens = state.lens.flatMap { l in m.chips.contains { $0.lens == l } ? l : nil }
        ZStack(alignment: .topLeading) {
            TLBarView(scene: built.scene(places: byPlace), scrub: state.minute, lens: lens != nil, lensColor: (lens ?? .trip).color, lensMarks: m.marks(of: lens ?? .trip), reduce: reduce)
                .modifier(TLBarScrub(span: m.span, changed: { t, _ in held(t, bar) }, ended: lifted))
                .offset(x: 30, y: FootprintDayPage.Y.bar)
            if !m.chips.isEmpty || !m.body.isEmpty || !extra.isEmpty {
                TLChips(chips: m.chips, health: m.body, active: lens, extra: extra) { l in
                    Haptic.selection()
                    withAnimation(.easeOut(duration: 0.15)) { state.lens = state.lens == l ? nil : l }
                }
                .equatable()
                .offset(y: FootprintDayPage.Y.chips - (TLLayout.chipsHit - TLLayout.chipsH) / 2)
            }
        }
    }

    /// a finger on the bar at minute `t`: the row there is lit and the list goes to it, with a tick for each stretch of the bar it crosses
    private func held(_ t: Double, _ bar: DayTimelineModel) {
        let state = ui.footprint
        state.minute = t
        let id = FootprintDayContent.hot(at: t, in: entries)
        if id != state.list.hot {
            state.list.hot = id
            if let id, let i = entries.firstIndex(where: { $0.id == id }) {
                state.list.scroll(to: max(0, entries[..<i].reduce(0) { $0 + $1.height } - 12), animated: false, glide: true)
            }
        }
        let s = bar.stretch(at: t)
        if state.lastStretch != s { state.lastStretch = s; Haptic.selection() }
    }

    private func lifted() {
        let state = ui.footprint
        state.lastStretch = nil
        withAnimation(.easeOut(duration: 0.2)) { state.minute = nil; state.list.hot = nil }
    }
}

/// The rows in the timeline's own look, each outing opening its page; a separate `TimelineState` lights the row under the held minute and keeps home's menu and open page out of it.
struct FootprintDayList: View {
    let ui: StarUI
    let engine: StarEngine
    let entries: [TLEntry]
    let open: (TLEntry) -> Void
    @State private var pos = ScrollPosition()
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        let tl = ui.footprint.list
        var y = 0.0
        let tops = entries.map { e in defer { y += e.height }; return y }
        ScrollView(showsIndicators: false) {
            VStack(spacing: 0) {
                ForEach(Array(entries.enumerated()), id: \.element.id) { i, e in
                    TLRowView(e: e, dim: 1, top: tops[i], room: FootprintDayPage.Y.viewport, tl: tl, engine: engine, tap: { open(e) }).equatable()
                }
            }
            .padding(.horizontal, 30)
        }
        .contentMargins(.bottom, 40, for: .scrollContent)
        .scrollPosition($pos)
        .onChange(of: tl.request) { _, r in
            if r.glide && !reduce { withAnimation(TLMotion.follow) { pos.scrollTo(y: r.y) } } else { pos.scrollTo(y: r.y) }
        }
        .frame(width: W, height: FootprintDayPage.Y.viewport + TLLayout.fade, alignment: .top)
        .mask(alignment: .top) {
            VStack(spacing: 0) {
                LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: TLLayout.fadeTop)
                Color.black.frame(height: FootprintDayPage.Y.viewport - TLLayout.fadeTop)
                LinearGradient(colors: [.black, .clear], startPoint: .top, endPoint: .bottom).frame(height: TLLayout.fade)
            }
        }
        // a swipe along the list is a day before or after, as on home; up and down is the list's own
        .simultaneousGesture(DragGesture(minimumDistance: 24).onEnded { v in
            if let d = TimelineState.swipe(dx: v.translation.width, dy: v.translation.height, predicted: v.predictedEndTranslation.width) { ui.tl.swipe(d) }
        })
    }
}
