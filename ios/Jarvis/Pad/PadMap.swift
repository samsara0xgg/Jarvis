import SwiftUI

// 地图 on the iPad (跨端UI标准 §5, §11; the 地图 panel of iPad-横着-新排法.jpg): the footprint page as it is, laid out at iPad size. In a wide window the phone's column at the left (the speed
// legend, the day's bar, its numbers, the list of places and outings) and the real map at the right, running to the top and right edges under her and the strip; narrower, the map across
// the top and the column under it in the middle. The strip: 日历 at the left, the day and her line of it under her, 回到我在哪 at the right. A tap on an outing's row shows that outing alone
// on the map, a second tap opens its page (the phone's: in a wide window its stops at the left and the map with the curves at the right). The pieces are the phone's
// (Sky/FootprintDay*.swift); this file only places them.

struct PadMap: View {
    let engine: StarEngine
    let width: Double
    let height: Double                  // the window's, to its foot under the bar
    let top: Double                     // the status bar's height
    let wide: Bool
    @State private var picked: String?            // the outing shown alone on the map
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var ui: StarUI { engine.ui }
    private var tl: TimelineState { ui.tl }
    private var state: FootprintDayState { ui.footprint }
    private typealias Y = FootprintDayPage.Y

    /// the phone's column: at the left edge, or in the middle under the map
    private var columnX: Double { wide ? 0 : ((width - W) / 2).rounded() }
    /// the map: at the right of the column to the top and right edges, or across the top
    private var map: CGRect { wide ? CGRect(x: W, y: 0, width: width - W, height: height) : CGRect(x: 0, y: 0, width: width, height: (height * 0.42).rounded()) }
    /// where the column's bar stands: under the strip's buttons, or under the map (the legend 36 pt over it, as on the phone); the rest of the column moves with it
    private var barY: Double { wide ? top + 110 : map.maxY + 40 }
    /// the top of the strip's round buttons, on her line
    private var buttonsTop: Double { top + PadHome.herY - HomeTop.button / 2 }

    var body: some View {
        let iso = Cal.cur.iso(tl.day), store = FootprintStore.shared
        let reply = store.reply(iso), day = tl.reply
        let model = reply.map { FootprintModel(reply: $0, day: day) }
        let content = model.map { FootprintDayContent.make($0, past: tl.day != tl.today) } ?? FootprintDayContent()
        let built = day.map { tl.built($0) }
        let opened = state.openOuting.flatMap { id in reply?.outings.first { $0.id == id } }
        let dy = barY - Y.bar, listY = barY + Y.list - Y.bar
        ZStack(alignment: .topLeading) {
            ZStack(alignment: .topLeading) {
                mapPane(content, model, alone: picked.flatMap { id in reply?.outings.first { $0.id == id } }, iso: iso)
                if let model { FootprintDayReplay(ui: ui, model: model, entries: content.entries, room: replayRoom).offset(x: map.minX) }
                ZStack(alignment: .topLeading) {
                    FootprintDayLegend().offset(x: 30, y: Y.legend + dy)
                        .opacity(state.holding ? 0 : 1).animation(TLMotion.tripMorph(reduce: reduce), value: state.holding)          // the held bar's capsule takes its row
                    if let built {
                        FootprintDayBar(ui: ui, built: built, model: model, extra: content.chips, lead: content.place, tripSymbol: content.tripSymbol, entries: content.entries).offset(y: dy)
                    }
                    // to the window's foot beside the bar; over the bar in one column, its fade ending 12 pt into the glass (TLLayout.listBottom)
                    FootprintDayList(ui: ui, engine: engine, entries: content.entries, height: height - listY - (wide ? 0 : PadHome.barH - 12)) { e in tapped(e) }.offset(y: listY)
                }
                .frame(width: W, height: height, alignment: .topLeading)
                .offset(x: columnX)
                .id(tl.day).transition(TLMotion.page(slide: TLMotion.daySlide * Double(tl.slide), reduce: reduce))
                strip(content.summary, bar: built?.model, model: model)
            }
            .opacity(opened == nil ? 1 : 0).allowsHitTesting(opened == nil)
            if let model, let opened {
                Group {
                    if wide {
                        FootprintOutingPage(model: model, outing: opened, split: (CGSize(width: width, height: height), buttonsTop), back: { state.openOuting = nil })
                    } else {
                        // ponytail: in one column the phone's outing page at its design size (440 x 956), centred; a taller window leaves sky under its stops
                        FootprintOutingPage(model: model, outing: opened, back: { state.openOuting = nil }).offset(x: columnX)
                    }
                }
                .transition(reduce ? .opacity : .move(edge: .trailing).combined(with: .opacity))
            }
        }
        .frame(width: width, height: height, alignment: .topLeading)
        .animation(TLMotion.detail(reduce: reduce), value: state.openOuting)
        .onChange(of: content.summary, initial: true) { _, text in state.summary = text }
        .onChange(of: iso) { _, _ in state.player.stop(); state.release(); picked = nil }          // another day: the replay and the outing shown are of the one before
        .onChange(of: state.holding) { _, held in if !held { state.list.hot = picked } }          // the bar let go: the outing shown is lit again
        .task(id: "\(iso) \(BrainLink.shared.openTick)") { store.ensure(iso) }          // on appear, on another day, and when the app has been opened since
        .task(id: model?.unnamedStops.map(\.id) ?? []) { FootprintStreets.shared.ask(model?.unnamedStops.map(\.coord) ?? []) }          // the streets of the stops nobody named
        .onDisappear { state.reset() }          // the page's state goes with it, as on the phone when another tab is picked
        #if DEBUG
        .task {          // `-padScene trip` (with `-scene 足迹`): the day's first outing shown alone, as a tap on its row does
            guard UserDefaults.standard.string(forKey: "padScene") == "trip" else { return }
            try? await Task.sleep(for: .seconds(2))
            if let id = FootprintStore.shared.reply(Cal.cur.iso(tl.day))?.outings.first?.id { picked = id; state.list.hot = id }
        }
        #endif
    }

    /// The phone's map at the right or across the top: the day's whole, or one outing alone as its page draws it (its route, where it left and arrived, its stops numbered)
    private func mapPane(_ content: FootprintDayContent, _ model: FootprintModel?, alone: FootprintOuting?, iso: String) -> some View {
        var shown = content
        if let model, let alone {
            let parts = FootprintOutingParts(model: model, outing: alone)
            shown.routes = [parts.route]; shown.places = parts.places; shown.tokens = parts.tokens; shown.now = nil
        }
        let fit = wide ? UIEdgeInsets(top: top + 150, left: 56, bottom: PadHome.barRoom + 24, right: 80) : UIEdgeInsets(top: top + 150, left: 24, bottom: 60, right: 80)
        let fade: [Gradient.Stop] = wide ? [.init(color: .clear, location: 0), .init(color: .black, location: 26 / map.width)]
                                         : [.init(color: .black, location: 0), .init(color: .black, location: 1 - 36 / map.height), .init(color: .clear, location: 1)]
        return FootprintDayMap(ui: ui, content: shown, model: model, fitKey: model == nil ? "" : alone?.id ?? iso, room: (fit, wide ? PadHome.barH + 8 : 44))
            .mask(LinearGradient(stops: fade, startPoint: wide ? .leading : .top, endPoint: wide ? .trailing : .bottom))
            .frame(width: map.width, height: map.height)
            .offset(x: map.minX)
            .animation(padMotion, value: alone?.id)
    }

    /// ▶ at the map's lower right, clear of the bar at the foot, or of its fade
    private var replayRoom: CGSize { CGSize(width: map.width, height: wide ? height - PadHome.barH - 16 : map.height - 56) }

    /// 日历 at the left and 回到我在哪 at the right on her line; under her the day and the page's name, and her line of the day on its plate (the phone's `FootprintDayStrip`), which give way to
    /// her card
    private func strip(_ summary: String, bar: DayTimelineModel?, model: FootprintModel?) -> some View {
        ZStack(alignment: .top) {
            HStack {
                FootprintDayPicker(ui: ui)
                Spacer()
                TopGlassButton(symbol: "location.fill", label: Words.PhoneMap.locate) {
                    Haptic.selection()
                    picked = nil; state.list.hot = nil
                    if tl.day != tl.today { tl.goToday() }
                    state.locateTick += 1
                }
            }
            .padding(.horizontal, 28)
            .padding(.top, buttonsTop)
            FootprintDayStrip(ui: ui, summary: summary, bar: bar, model: model)
                .padding(.top, top + 70)
                .opacity(ui.herCardOn ? 0 : 1).animation(.easeOut(duration: 0.2), value: ui.herCardOn)          // her card stands in its place
        }
        .frame(width: width)
    }

    /// A tap on an outing's row: the map shows it alone and the row stays lit; a tap on the outing shown opens its page.
    private func tapped(_ e: TLEntry) {
        guard e.tapOnly else { return }
        Haptic.selection()
        if picked == e.id {
            engine.releaseTalk()
            state.openOuting = e.id
        } else {
            picked = e.id
            state.list.hot = e.id
        }
    }
}
