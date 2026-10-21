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
    @ObservationIgnored var lastStretch: String?        // the stretch of the bar the finger was last in, for its tick

    func reset() { minute = nil; openOuting = nil; lens = nil; lastStretch = nil; list.hot = nil }
}

/// The overlay: the page slides in from the right over the sky and goes back the way it came, by the chevron or by a swipe to the right from the left edge.
struct FootprintDayLayer: View {
    let engine: StarEngine
    let ui: StarUI
    @State private var drag = 0.0               // how far a swipe back has the page to the right
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        ZStack(alignment: .topLeading) {
            if ui.footprintOpen {
                FootprintDayPage(engine: engine, ui: ui, back: { ui.footprintOpen = false })
                    .overlay(alignment: .leading) { edge }
                    .offset(x: drag).opacity(TLMotion.pageShows(drag / W))
                    .transition(reduce ? .opacity : .move(edge: .trailing).combined(with: .opacity))
            }
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .animation(TLMotion.detail(reduce: reduce), value: ui.footprintOpen)
        .opacity(ui.panelOpen ? 0 : 1).allowsHitTesting(!ui.panelOpen)          // the history takes the place of whatever is under it
        .animation(TLMotion.dayChange(reduce: reduce), value: ui.panelOpen)
        .onChange(of: ui.footprintOpen) { _, open in if !open { ui.footprint.reset(); drag = 0 } }
    }

    /// the strip at the left edge a swipe back starts on (anywhere else a sideways swipe is a change of day, and the map's pan is its own)
    private var edge: some View {
        Color.clear.frame(width: 22).frame(maxHeight: .infinity).contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 10, coordinateSpace: .named("design"))
                .onChanged { v in
                    guard !reduce, TimelineState.backSwipeStarts(dx: v.translation.width, dy: v.translation.height) else { return }
                    drag = max(0, v.translation.width - 10)
                }
                .onEnded { v in
                    let goes = TimelineState.backSwipe(dx: v.translation.width, dy: reduce ? v.translation.height : 0, predicted: v.predictedEndTranslation.width)
                    if reduce { if goes { ui.footprintOpen = false }; return }
                    guard drag > 0 else { return }
                    let to = goes ? W : 0
                    let speed = abs(to - drag) > 1 ? v.velocity.width / (to - drag) : 0          // the finger's own speed, in parts of the way left per second
                    withAnimation(TLMotion.release(speed: speed)) { drag = to } completion: {
                        var t = Transaction(); t.disablesAnimations = true
                        withTransaction(t) { if goes { ui.footprintOpen = false }; drag = 0 }
                    }
                })
    }
}

struct FootprintDayPage: View {
    let engine: StarEngine
    let ui: StarUI
    let back: () -> Void
    @AppStorage("timeline.barByPlace") private var byPlace = false          // the bar drawn as home draws it
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var tl: TimelineState { ui.tl }
    private var state: FootprintDayState { ui.footprint }

    /// where things stand, in the design space
    enum Y {
        static let head = 57.0, line = 108.0, map = 109.0, mapH = 371.0, legend = 484.0, bar = 520.0, chips = 560.0, list = 612.0
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
                FootprintDayMap(ui: ui, content: content, model: model, fitKey: reply == nil ? "" : iso)
                    .frame(width: W, height: Y.mapH).offset(y: Y.map)
                FootprintDayLegend().offset(x: 30, y: Y.legend)
                FootprintDayHead(ui: ui, back: back).offset(x: 60, y: Y.head)
                    .opacity(ui.tlHome ? 1 : 0).animation(TLMotion.dayChange(reduce: reduce), value: ui.tlHome)
                FootprintDayPicker(ui: ui).offset(x: W - 30 - 36, y: Y.head + 4)          // 36 pt, centred on the head's row
                    .opacity(ui.tlHome ? 1 : 0).animation(TLMotion.dayChange(reduce: reduce), value: ui.tlHome)
                Group {
                    FootprintDayLine(ui: ui, summary: content.summary, bar: built?.bar(places: byPlace), model: model).offset(x: 30, y: Y.line)
                    if let built {
                        FootprintDayBar(ui: ui, built: built, byPlace: byPlace, extra: content.chips, lead: content.place, tripSymbol: content.tripSymbol, entries: content.entries)
                    }
                    FootprintDayList(ui: ui, engine: engine, entries: content.entries) { e in open(e) }
                        .offset(y: Y.list)
                }
                .opacity(ui.tlHome ? 1 : 0).animation(TLMotion.dayChange(reduce: reduce), value: ui.tlHome)
                .id(tl.day).transition(TLMotion.page(slide: TLMotion.daySlide * Double(tl.slide), reduce: reduce))
            }
            .opacity(opened == nil ? 1 : 0).allowsHitTesting(opened == nil)
            if let model, let opened {
                FootprintOutingPage(model: model, outing: opened, back: { state.openOuting = nil })
                    .transition(reduce ? .opacity : .move(edge: .trailing).combined(with: .opacity))
            }
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .animation(TLMotion.detail(reduce: reduce), value: state.openOuting)
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
