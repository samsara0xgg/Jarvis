import SwiftUI
import MapKit

// The pieces of the footprint's one-day screen (Sky/FootprintDayPage.swift places them). Each reads the held minute itself, so that a finger on the bar draws these small views and not the page.

private let ink = SetColor.ink
private let muted = SetColor.muted

/// the way back, the date, the today label (a label) or on another day the way back to it, and the page's name; laid out clear of her corner as a page of the timeline is
struct FootprintDayHead: View {
    let ui: StarUI
    let back: (() -> Void)?             // none on the phone, where the page is one of the bar at the foot

    var body: some View {
        let tl = ui.tl, today = tl.day == tl.today
        HStack(spacing: 2) {
            if let back {
                Button(action: back) {
                    Image(systemName: "chevron.left").font(.system(size: 17, weight: .semibold)).foregroundStyle(ink)
                        .frame(width: 44, height: 44).contentShape(Rectangle())
                }
                .buttonStyle(PressScale())
                .accessibilityLabel(Words.Footprint.back)
            }
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
                let t = held ?? last, r = bar.readout(at: t), head = model.map { FootprintDayContent.heldReadout($0, minute: t, r) } ?? r
                TLReadoutLines(r: head, second: model.map { FootprintDayContent.heldLine($0, minute: t, elsewhere: r.second) } ?? r.second)
                    .fadeThrough(on: holding, reduce: reduce).accessibilityHidden(!holding)
            }
        }
        .allowsHitTesting(false)
        .onChange(of: held) { _, t in if let t { last = t } }
    }
}

/// The line under her on the phone's map page (跨端UI标准 §2): the day and the page's name, and under it her sentence of the day on a small dark glass plate, so that it reads over the map; while the bar is
/// held the timeline's readout of that minute (where he was, how fast and his heart under it) takes the sentence's place, and they cross, blurred.
struct FootprintDayStrip: View {
    let ui: StarUI
    let summary: String
    let bar: DayTimelineModel?
    let model: FootprintModel?
    @State private var last = 0.0               // where his finger was last: what the readout shows while it fades out
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        let held = ui.footprint.minute, holding = held != nil
        VStack(spacing: 4) {
            Text(Words.PhoneMap.title(date: ui.dateText)).font(.system(size: Look.note)).foregroundStyle(Look.ink2).lineLimit(1)
            ZStack(alignment: .top) {
                if !summary.isEmpty {
                    Text(summary).font(.system(size: Look.body)).lineSpacing(3).foregroundStyle(Look.ink).multilineTextAlignment(.center).lineLimit(2)
                        .padding(.horizontal, 14).padding(.vertical, 7).plate()
                        .fadeThrough(on: !holding, reduce: reduce)
                }
                if let bar {
                    let t = held ?? last, r = bar.readout(at: t), head = model.map { FootprintDayContent.heldReadout($0, minute: t, r) } ?? r
                    TLReadoutLines(r: head, second: model.map { FootprintDayContent.heldLine($0, minute: t, elsewhere: r.second) } ?? r.second)
                        .environment(\.tlBarWidth, W - 2 * HomeTop.side - 28)
                        .padding(.horizontal, 14).padding(.vertical, 7).plate()
                        .fadeThrough(on: holding, reduce: reduce).accessibilityHidden(!holding)
                }
            }
        }
        .frame(width: W - 2 * HomeTop.side, alignment: .top)
        .allowsHitTesting(false)
        .onChange(of: held) { _, t in if let t { last = t } }
    }
}

private extension View {
    /// a small dark glass plate: the words over the map's colours stay readable
    func plate() -> some View {
        let shape = RoundedRectangle(cornerRadius: 14, style: .continuous)
        return background(Color.black.opacity(0.5), in: shape).glass(shape)
    }
}

/// The map under the line, fading into the sky at its top and its foot. The shape of the day is the content's; the dot of the held moment is the one thing that moves, and while it is held (by a finger or the replay)
/// the camera follows it.
struct FootprintDayMap: View {
    let ui: StarUI
    let content: FootprintDayContent
    let model: FootprintModel?
    let fitKey: String
    var ground = false                  // the phone: the map is the ground and runs to the top edge, under the strip and her; the iPad's sheet keeps it under its head

    var body: some View {
        let state = ui.footprint, moment = state.moment
        let dot = moment.map { m in model.flatMap { FootprintDayContent.dot($0, moment: m) } } ?? state.minute.flatMap { t in model.flatMap { FootprintDayContent.dot($0, minute: t) } }
        let h = ground ? FootprintDayPage.Y.groundH : FootprintDayPage.Y.mapH
        let clear = ground ? FootprintDayPage.Y.clear : 60          // from the top of the map to where the route may stand: under the strip and her sentence
        FootprintMapView(routes: content.routes, places: content.places, tokens: content.tokens, now: content.now, dot: dot, follow: moment?.follow,
                         fitPadding: UIEdgeInsets(top: clear, left: 24, bottom: 36, right: 110), fitKey: fitKey, locateTick: state.locateTick, legalInset: 44, touched: { state.player.touch() })          // MapKit's legal label stands above the fade
            .mask(LinearGradient(stops: [.init(color: ground ? .black : .clear, location: 0), .init(color: .black, location: ground ? 0 : 70 / h),
                                         .init(color: .black, location: 1 - 36 / h), .init(color: .clear, location: 1)],
                                 startPoint: .top, endPoint: .bottom))
    }
}

/// The ▶ button at the map's lower right, clear of its fade and of MapKit's legal label, and the caption of a beat of the replay over the map.
struct FootprintDayReplay: View {
    let ui: StarUI
    let model: FootprintModel
    let entries: [TLEntry]

    var body: some View {
        let state = ui.footprint
        ZStack(alignment: .topLeading) {
            if model.replay(.day) != nil {
                FootprintReplayButton(player: state.player, play: play).offset(x: W - 30 - 36, y: FootprintDayPage.Y.map + 264)
            }
            FootprintBeatCaption(beat: state.moment?.beat, model: model, day: true).frame(width: W).offset(y: FootprintDayPage.Y.map + 222)
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .onAppear {
            #if DEBUG
            FootprintHooks.play = play
            #endif
        }
    }

    private func play() {
        guard let replay = model.replay(.day) else { return }
        Self.connect(ui.footprint, model: model, entries: entries)
        ui.footprint.player.play(replay)
    }

    /// what the player's frames do on this page, played or under a finger: the moment held, and at the end the day's whole again
    static func connect(_ state: FootprintDayState, model: FootprintModel, entries: [TLEntry]) {
        state.player.emit = { f, r, playing in
            state.hold(playing ? FootprintMoment(f, height: r.height) : FootprintMoment(finger: f, model: model), model: model, entries: entries)
        }
        state.player.ended = { withAnimation(.easeOut(duration: 0.2)) { state.release() } }
    }
}

/// ▶ while it is at rest or paused, ⏸ while it plays: a 36 pt glass circle as the page's other buttons
struct FootprintReplayButton: View {
    let player: FootprintPlayer
    let play: () -> Void

    var body: some View {
        let playing = player.phase == .playing
        Button { Haptic.selection(); if playing { player.touch() } else { play() } } label: {
            Image(systemName: playing ? "pause.fill" : "play.fill").font(.system(size: 14, weight: .regular)).foregroundStyle(TLPaint.icon.color(0.8))
                .frame(width: 36, height: 36).glass(Circle())
        }
        .buttonStyle(PressScale())
        .accessibilityLabel(playing ? Words.Footprint.replayPause : Words.Footprint.replay)
    }
}

/// What a beat of the replay says, for about a second, in a capsule over the map: a long stay's place and length; a stop's on the day page the same when its place is known
/// ("Bankside · 6 分钟"), on the outing page (`day` false) "停 N 分".
struct FootprintBeatCaption: View {
    let beat: FootprintReplay.Beat?
    let model: FootprintModel
    var day = false

    private var text: String? {
        switch beat {
        case let .stay(id, minutes)?:
            let name = model.reply.stays.first { $0.id == id }.flatMap(FootprintDayContent.name) ?? Words.FootprintDay.unnamedPlace
            return Words.Footprint.replayStay(place: name, duration: tlDur(minutes))
        case let .stop(id, _, minutes)?:
            let name = day ? model.reply.outings.lazy.flatMap(\.stops).first { $0.id == id }.flatMap(FootprintDayContent.stopName) : nil
            return name.map { Words.Footprint.replayStay(place: $0, duration: tlDur(minutes)) } ?? Words.Footprint.stopFor(tlDur(minutes))
        case nil: return nil
        }
    }

    var body: some View {
        ZStack {
            if let text {
                Text(text).font(.system(size: 13, weight: .semibold)).foregroundStyle(TLPaint.ink.color).lineLimit(1).padding(.horizontal, 12).frame(height: 28)
                    .background(Capsule().fill(FootprintPaint.capsule.color))
                    .overlay(Capsule().stroke(Color.white.opacity(0.14), lineWidth: 0.5))
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.15), value: text)
        .allowsHitTesting(false)
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

/// The day's bar as home draws it, and while a finger holds it (or the replay plays) the same bar as its trips (Sky/FootprintTripBar.swift); a finger on it holds a moment for the line, the map and the list,
/// and under it are the same numbers and the two of this page.
struct FootprintDayBar: View {
    let ui: StarUI
    let built: TLBuilt
    let model: FootprintModel?
    let extra: [TLPlainChip]
    let lead: [TLPlainChip]
    let tripSymbol: String?
    let entries: [TLEntry]
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        let m = built.model, state = ui.footprint, held = state.holding
        let lens = state.lens.flatMap { l in m.chips.contains { $0.lens == l } ? l : nil }
        let trips = model.flatMap { $0.barLayout.hasSegments ? $0 : nil }
        ZStack(alignment: .topLeading) {
            ZStack(alignment: .topLeading) {
                TLBarView(scene: built.scene, scrub: held && trips != nil ? nil : state.minute, lens: lens != nil, lensColor: (lens ?? .trip).color, lensMarks: m.marks(of: lens ?? .trip), reduce: reduce)
                    .opacity(held && trips != nil ? 0 : 1)
                if let trips {
                    FootprintTripBar(art: trips.tripArt(span: m.span), k: held ? 1 : 0)
                    FootprintPlayhead(ui: ui).opacity(held ? 1 : 0)
                }
            }
            .animation(TLMotion.tripMorph(reduce: reduce), value: held)
            .frame(width: TLLayout.barW, height: TLLayout.barH, alignment: .topLeading)
            .contentShape(Rectangle().inset(by: -10))
            .modifier(TLBarScrub(span: m.span, changed: { t, x in scrubbed(t, x, m) }, ended: lifted))
            .offset(x: 30, y: FootprintDayPage.Y.bar)
            if !m.chips.isEmpty || !m.body.isEmpty || !extra.isEmpty || !lead.isEmpty {
                TLChips(chips: m.chips.map { c in var c = c; if c.lens == .trip { c.symbol = tripSymbol }; return c }, health: m.body, active: lens, extra: extra, lead: lead) { l in
                    Haptic.selection()
                    withAnimation(.easeOut(duration: 0.15)) { state.lens = state.lens == l ? nil : l }
                }
                .equatable()
                .offset(y: FootprintDayPage.Y.chips - (TLLayout.chipsHit - TLLayout.chipsH) / 2)
            }
        }
        .onAppear {
            #if DEBUG
            FootprintHooks.scrub = { x in if let x { scrubbed(TLBarScrub.minute(x: x, span: m.span), x, m) } else { lifted() } }
            #endif
        }
    }

    /// a finger on the bar at `x` (`t` is the minute at that x on the bar at rest): the moment of the trip layout there is held, the row of it is lit and the list goes to it, with a tick for each stretch of
    /// the bar it crosses; a day with no line is held in time as before
    private func scrubbed(_ t: Double, _ x: Double, _ bar: DayTimelineModel) {
        let state = ui.footprint
        state.player.touch()          // a finger on the bar pauses the replay
        guard let model, model.barLayout.hasSegments, let replay = model.replay(.day) else {
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
            return
        }
        FootprintDayReplay.connect(state, model: model, entries: entries)
        state.player.point(at: replay.seconds(atX: x), of: replay)          // the moment comes from the player, eased; ▶ goes on from where it stands
        let s = model.barLayout.stretch(atX: x)
        if state.lastStretch != s { state.lastStretch = s; Haptic.selection() }
    }

    /// the finger lifted: the bar turns back and the camera goes back to the day's whole, unless a replay stands paused, which keeps its playhead
    private func lifted() {
        let state = ui.footprint, rest = state.player.phase == .rest
        state.lastStretch = nil
        state.player.lift()
        if rest { withAnimation(.easeOut(duration: 0.2)) { state.release() } }
    }
}

/// The rows in the timeline's own look, each outing opening its page; a separate `TimelineState` lights the row under the held minute and keeps home's menu and open page out of it.
struct FootprintDayList: View {
    /// how much of a row shows while the bar is held at a minute before it
    static let later = 0.45
    private static var here: some View {
        LinearGradient(stops: [.init(color: ink.opacity(0), location: 0), .init(color: ink.opacity(0.5), location: 60 / TLLayout.barW),
                               .init(color: ink.opacity(0.5), location: 1 - 60 / TLLayout.barW), .init(color: ink.opacity(0), location: 1)], startPoint: .leading, endPoint: .trailing)
    }

    let ui: StarUI
    let engine: StarEngine
    let entries: [TLEntry]
    let open: (TLEntry) -> Void
    @State private var pos = ScrollPosition()
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        let tl = ui.footprint.list, held = ui.footprint.minute
        var y = 0.0
        let tops = entries.map { e in defer { y += e.height }; return y }
        let next = held.flatMap { t in entries.firstIndex { $0.t0 > t } }          // the first row after the held minute
        ScrollView(showsIndicators: false) {
            VStack(spacing: 0) {
                ForEach(Array(entries.enumerated()), id: \.element.id) { i, e in
                    TLRowView(e: e, dim: next.map { i >= $0 } == true ? Self.later : 1, top: tops[i], room: FootprintDayPage.Y.viewport, tl: tl, engine: engine, tap: { open(e) }).equatable()
                }
            }
            .overlay(alignment: .topLeading) {
                if let next, next > 0 {
                    Self.here.frame(height: 1).offset(y: tops[next] + entries[next].gap - 4)          // a hairline between the rows that have happened and the ones that have not
                        .animation(reduce ? nil : TLMotion.follow, value: next).transition(.opacity)
                }
            }
            .padding(.horizontal, 30)
        }
        .contentMargins(.bottom, TLLayout.listBottom + TLLayout.fade - HomeBar.top + 16, for: .scrollContent)          // the last row comes up clear of the bar at the foot
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
