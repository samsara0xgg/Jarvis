import SwiftUI
import Observation

// The footprint's one-day screen: the day on the timeline's home, as a map of where he went, the day bar and its numbers, and the list of the places and outings. It is an overlay of the
// star chart's chrome (Sky/StarChartScreen.swift) in the 440 x 956 design space, over the sky, with the composer and her cards above it; the home timeline and the top row give way to it.
// A page shows the day `ui.tl.day` is on, so a swipe along the list changes it as it does on home. The pieces are in Sky/FootprintDayParts.swift, what they show is made in
// Sky/FootprintDayContent.swift.

/// What the page holds: the minute a finger has on its bar, the outing opened, the number pressed, and the state its rows are drawn with (their light and menu are not home's).
@Observable
final class FootprintDayState {
    var minute: Double?                 // under a finger on the bar
    var openOuting: String?             // the outing whose page is over this one
    var lens: TLLens?                   // the day's number he pressed: the bar brings that kind forward
    let list = TimelineState()          // the rows' own: which one is lit while the bar is held, where the list is asked to go
    var moment: FootprintMoment?        // under a finger on the bar or played by the replay: where the camera goes
    var holding = false                 // `moment` is set: the bar is in its trips
    let player = FootprintPlayer()      // the ▶ replay
    var summary: String?                // her line about the day ("今天出门 2 趟…现在在家。"), for the strip at the top to say under her
    var locateTick = 0                  // bumped by the strip's 回到我在哪: the map goes back to where he is
    @ObservationIgnored var lastStretch: String?        // the stretch of the bar the finger was last in, for its tick
    @ObservationIgnored var last = (x: 0.0, text: "")   // the playhead's place and capsule, kept while the bar turns back

    /// The moment a finger or the replay holds: the line, the map's dot and camera, the playhead and the list follow it.
    @MainActor func hold(_ m: FootprintMoment, model: FootprintModel, entries: [TLEntry]) {
        last = (m.x ?? last.x, FootprintPlayhead.text(m, model))
        let t = model.clock.minute(ms: m.ms)
        minute = t
        moment = m
        if !holding { holding = true }
        let id = FootprintDayContent.hot(at: t, in: entries)
        if id != list.hot {
            list.hot = id
            if let id, let i = entries.firstIndex(where: { $0.id == id }) {
                list.scroll(to: max(0, entries[..<i].reduce(0) { $0 + $1.height } - 12), animated: false, glide: true)
            }
        }
    }

    /// nothing is held any more: the bar turns back to time and the camera to the day's whole
    func release() { moment = nil; minute = nil; list.hot = nil; lastStretch = nil; if holding { holding = false } }

    func reset() {
        player.stop(); player.emit = nil; player.ended = nil
        release(); openOuting = nil; lens = nil
    }
}

/// The overlay: on the phone the page is the map's in the bar at the foot (Sky/HomeBar.swift), over the sky; it fades in when its tab is picked and out when another is.
struct FootprintDayLayer: View {
    let engine: StarEngine
    let ui: StarUI
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        ZStack(alignment: .topLeading) {
            if ui.footprintOpen {
                FootprintDayPage(engine: engine, ui: ui, back: nil).transition(.opacity)
            }
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .animation(TLMotion.detail(reduce: reduce), value: ui.footprintOpen)
        .opacity(ui.panelOpen ? 0 : 1).allowsHitTesting(!ui.panelOpen)          // the history takes the place of whatever is under it
        .animation(TLMotion.dayChange(reduce: reduce), value: ui.panelOpen)
        .onChange(of: ui.footprintOpen) { _, open in if !open { ui.footprint.reset() } }
    }
}

struct FootprintDayPage: View {
    let engine: StarEngine
    let ui: StarUI
    let back: (() -> Void)?             // the way out, where the page is one (the iPad's sheet); on the phone it is a page of the bar at the foot
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var tl: TimelineState { ui.tl }
    private var state: FootprintDayState { ui.footprint }
    private var phone: Bool { back == nil }          // the iPad's sheet has a way out and keeps its head and its map where they were

    /// where things stand, in the design space
    enum Y {
        static let head = 57.0, line = 108.0, map = 109.0, mapH = 371.0, legend = 484.0, bar = 520.0, chips = 560.0, list = 612.0
        static let groundH = map + mapH          // the phone's map runs from the top edge down to where the iPad's map ends
        static let clear = 190.0                 // and keeps the route this far from the top: under the strip and her sentence
        static var viewport: Double { TLLayout.listBottom - list }
    }

    var body: some View {
        let iso = Cal.cur.iso(tl.day), store = FootprintStore.shared
        let reply = store.reply(iso), day = tl.reply
        let model = reply.map { FootprintModel(reply: $0, day: day) }
        let content = model.map { FootprintDayContent.make($0, past: tl.day != tl.today) } ?? FootprintDayContent()
        let built = day.map { tl.built($0) }
        let opened = state.openOuting.flatMap { id in reply?.outings.first { $0.id == id } }
        ZStack(alignment: .topLeading) {
            Color.clear.contentShape(Rectangle())           // the sky under the page takes no touch that the page leaves
            ZStack(alignment: .topLeading) {
                scrim
                FootprintDayMap(ui: ui, content: content, model: model, fitKey: reply == nil ? "" : iso, ground: phone)
                    .frame(width: W, height: phone ? Y.groundH : Y.mapH).offset(y: phone ? 0 : Y.map)
                FootprintDayLegend().offset(x: 30, y: Y.legend)
                    .opacity(state.holding ? 0 : 1).animation(TLMotion.tripMorph(reduce: reduce), value: state.holding)          // the held bar's capsule takes its row
                if phone {
                    TopStrip {
                        FootprintDayPicker(ui: ui)
                    } middle: {
                        EmptyView()
                    } right: {
                        TopGlassButton(symbol: "location.fill", label: Words.PhoneMap.locate) { Haptic.selection(); if tl.day != tl.today { tl.goToday() }; state.locateTick += 1 }
                    }
                    .opacity(ui.tlHome ? 1 : 0).animation(TLMotion.dayChange(reduce: reduce), value: ui.tlHome)
                } else {
                    FootprintDayHead(ui: ui, back: back).offset(x: 60, y: Y.head)          // the iPad's sheet: its way out, then the date
                        .opacity(ui.tlHome ? 1 : 0).animation(TLMotion.dayChange(reduce: reduce), value: ui.tlHome)
                    FootprintDayPicker(ui: ui).offset(x: W - 30 - 40, y: Y.head + 2)          // 40 pt, centred on the head's row
                        .opacity(ui.tlHome ? 1 : 0).animation(TLMotion.dayChange(reduce: reduce), value: ui.tlHome)
                }
                Group {
                    if phone {
                        FootprintDayStrip(ui: ui, summary: content.summary, bar: built?.model, model: model).offset(x: HomeTop.side, y: HomeTop.lineY)
                            .opacity(ui.herCardOn ? 0 : 1).animation(.easeOut(duration: 0.2), value: ui.herCardOn)          // her card stands in its place
                    } else {
                        FootprintDayLine(ui: ui, summary: content.summary, bar: built?.model, model: model).offset(x: 30, y: Y.line)
                    }
                    if let model { FootprintDayReplay(ui: ui, model: model, entries: content.entries) }
                    if let built {
                        FootprintDayBar(ui: ui, built: built, model: model, extra: content.chips, lead: content.place, tripSymbol: content.tripSymbol, entries: content.entries)
                    }
                    FootprintDayList(ui: ui, engine: engine, entries: content.entries) { e in open(e) }
                        .offset(y: Y.list)
                }
                .opacity(ui.tlHome ? 1 : 0).animation(TLMotion.dayChange(reduce: reduce), value: ui.tlHome)
                .id(tl.day).transition(TLMotion.page(slide: TLMotion.daySlide * Double(tl.slide), reduce: reduce))
            }
            .opacity(opened == nil ? 1 : 0).allowsHitTesting(opened == nil)
            if let model, let opened {
                FootprintOutingPage(model: model, outing: opened, phone: phone, back: { state.openOuting = nil })
                    .transition(reduce ? .opacity : .move(edge: .trailing).combined(with: .opacity))
            }
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .animation(TLMotion.detail(reduce: reduce), value: state.openOuting)
        .onChange(of: content.summary, initial: true) { _, text in state.summary = text }          // for the strip's line under her
        .onChange(of: iso) { _, _ in state.player.stop(); state.release() }          // another day: the replay is of the one before
        .task(id: "\(iso) \(BrainLink.shared.openTick)") { store.ensure(iso) }          // on appear, on another day, and when the app has been opened since
        .task(id: model?.unnamedStops.map(\.id) ?? []) { FootprintStreets.shared.ask(model?.unnamedStops.map(\.coord) ?? []) }          // the streets of the stops nobody named
    }

    /// An outing's page opens from its row; a long stay's row has no page.
    private func open(_ e: TLEntry) {
        guard e.tapOnly else { return }
        Haptic.selection()
        engine.releaseTalk()
        state.openOuting = e.id
    }

    /// black under the bar and the list as home has it, so the rows sit on near-black and the sky keeps the head
    private var scrim: some View {
        LinearGradient(stops: [.init(color: .black.opacity(0), location: 0), .init(color: .black.opacity(0.8), location: 0.12), .init(color: .black.opacity(0.8), location: 0.78), .init(color: .black.opacity(0), location: 1)],
                       startPoint: .top, endPoint: .bottom)
            .frame(width: W, height: TLLayout.listBottom + TLLayout.fade - 500).offset(y: 500)
            .allowsHitTesting(false)
    }
}
