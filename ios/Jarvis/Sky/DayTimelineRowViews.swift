// words: legacy
import SwiftUI

// One line of the day's list: the time, the icon in its circle (dashed for what has not happened), the title (a question mark after it where she is not sure) with its one muted line, and
// at the right how long (warm while it goes on) or the value; on the phone a small arrow at the far right says the row opens where it stands. A place over a group of stretches is a header:
// its dot on the rail, its name and its hours. A place carries a rail in its hue through its group, a trip a dashed one that reaches out through the blanks either side of it. The heights are
// `TLMetrics` (46, with a line 60, a header 40, 12 between groups), so every row he can touch is a target of at least 44. Opened, a row on the phone unfolds what it has to say under itself
// (Sky/DayTimelineOpen.swift); on the iPad it opens on the stage beside the list.

private let ink = SetColor.ink
private let muted = SetColor.muted
private let amber = Color(red: 1, green: 201 / 255, blue: 143 / 255)               // what waits on him, and the question mark of what she is not sure of
private let warm = Color(red: 1, green: 240 / 255, blue: 220 / 255)               // now, and how long what goes on has gone
private let iconColor = Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.82)
private let tripRail = Color(red: 196 / 255, green: 208 / 255, blue: 1)

extension TLAction {
    /// the words on its button and in the menu of a long press
    var title: String {
        switch self {
        case .ask: "问她"
        case .rename: "改名字"
        case .name: "起个名字"
        case .acknowledge: "知道了"
        case .confirm: "是我的"
        case .deny: "不是"
        case .sayWhat: "这段在干嘛"
        }
    }
    var symbol: String {
        switch self {
        case .ask: "text.bubble"
        case .rename: "pencil"
        case .name: "mappin.and.ellipse"
        case .acknowledge: "checkmark.circle"
        case .confirm: "checkmark.circle"
        case .deny: "xmark.circle"
        case .sayWhat: "pencil.line"
        }
    }
    /// what the menu of a long press offers, in this order: asking her first, on every row there is something to put to her about, and 这段在干嘛 last
    static let menu: [TLAction] = [.ask, .rename, .name, .acknowledge, .confirm, .deny, .sayWhat]
}

struct TLRowView: View, Equatable {
    let e: TLEntry
    let dim: Double                     // 1, less while a lens is on and this is not of its kind
    let top: Double                     // where the row stands in the list and how tall the list is: what its closure was made with
    let room: Double
    let tl: TimelineState
    let engine: StarEngine
    let tap: () -> Void
    var inPlace = false                 // the phone's list: the row opens under itself, and wears the arrow that says so
    var open = false                    // and it is open
    @Environment(\.accessibilityReduceMotion) private var reduce
    @Environment(\.padFlight) private var flight          // set on the iPad only

    /// Two rows are the same when what they draw is, and where they stand (the closures come from the list and know where): a row that is the same is not drawn again, which is what keeps
    /// the list cheap while it is scrolled to the minute under a finger. What lights a row is read by the row's own `TLRowHeat`, and what it opened to reads what it shows itself.
    static func == (a: TLRowView, b: TLRowView) -> Bool {
        a.e == b.e && a.dim == b.dim && a.top == b.top && a.room == b.room && a.inPlace == b.inPlace && a.open == b.open
    }

    /// the circle's size: a place's header has a dot, a stop on the way a smaller one
    private var beadSize: Double { e.kind == .stay ? 8 : e.token != nil ? TLStopToken.size : e.kind == .pause ? 7 : TLColumns.bead }
    /// where the title's line begins: the block of a row is centred in its height
    private var titleTop: Double { e.hasLine ? (e.base - 35) / 2 : (e.base - 18) / 2 }
    /// a tap on it does something: on the phone, open it where it stands or frame its block; on the iPad, open it on the stage
    private var touchable: Bool { inPlace ? e.touchable : e.opens }
    /// it opens where it stands, which the arrow at its right says
    private var arrow: Bool { inPlace && e.expands }
    /// the column at the right that the arrow keeps clear on the phone
    private var arrowRoom: Double { inPlace ? TLColumns.arrow : 0 }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Color.clear.frame(height: e.gap)
            line
                .frame(height: e.base, alignment: .top)
                .overlay(alignment: .bottomLeading) {          // an outing's speed along it (the footprint page)
                    if !e.ribbon.isEmpty { TLRibbon(cells: e.ribbon).frame(width: TLColumns.words, height: TLRibbon.height).padding(.leading, TLColumns.text).padding(.bottom, 6) }
                }
                .background { TLRowHeat(id: e.id, tl: tl, stage: flight != nil) }
                .contentShape(Rectangle())
                .modifier(TLRowTap(on: touchable, tap: tap))
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(touchable ? .isButton : [])
                .accessibilityHint(arrow ? (open ? Words.Timeline.collapse : Words.Timeline.expand) : "")
                .modifier(TLMenu(e: e, tl: tl, engine: engine, inPlace: inPlace, open: open, room: room))
            if open && inPlace {
                TLOpenView(e: e, tl: tl, engine: engine, inPlace: true, mapWidth: TLColumns.words - TLColumns.arrow)
                    .onGeometryChange(for: Double.self) { Double($0.size.height) } action: { tl.openH = $0 }
                    .transition(reduce ? .opacity : TLUnfold.transition)
            }
        }
        .background(alignment: .topLeading) { rails }
        .opacity(dim)
        .animation(.easeOut(duration: 0.15), value: dim)
    }

    // MARK: the line

    @ViewBuilder private var line: some View {
        switch e.kind {
        case .now: nowLine
        case .stay: stay
        default: item
        }
    }

    /// The place he stayed in, or a place over a group of stretches: its colour as a dot on the rail, its name, and at the right its hours and how long.
    private var stay: some View {
        HStack(spacing: 0) {
            Circle().fill((e.color.map { $0.color } ?? muted).opacity(e.kept ? 1 : 0.45)).frame(width: 8, height: 8)
                .padding(.leading, TLColumns.centre - 4)
            Text(e.title).font(.system(size: 15.5, weight: .semibold)).foregroundStyle(ink).lineLimit(1)
                .padding(.leading, TLColumns.text - TLColumns.centre - 4)
            Spacer(minLength: 10)
            Text(e.right).font(tlFigures(11.5)).foregroundStyle(muted).lineLimit(1).fixedSize().layoutPriority(1)
                .padding(.trailing, arrowRoom)
        }
        .frame(height: e.base)
        .overlay(alignment: .trailing) { if arrow { chevron } }
    }

    private var nowLine: some View {
        HStack(spacing: 10) {
            Text(e.title).font(tlFigures(11.5)).foregroundStyle(warm).fixedSize()
            Rectangle().fill(warm.opacity(0.85)).frame(height: 1)
        }
        .frame(maxHeight: .infinity)
    }

    /// time, circle, title with its muted line (and the amber 等你 while it waits), and at the right how long, or the value of a point; what has not happened is at 0.62
    private var item: some View {
        HStack(alignment: .top, spacing: 0) {
            Text(e.time).font(AT.mono(11.5)).foregroundStyle(muted).lineLimit(1).frame(width: TLColumns.time, height: 18, alignment: .leading).padding(.top, titleTop)
            bead.padding(.leading, TLColumns.centre - TLColumns.bead / 2 - TLColumns.time).padding(.top, (e.base - TLColumns.bead) / 2)
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(e.title).font(.system(size: 15)).foregroundStyle(e.unknown ? muted : ink).lineLimit(1)
                    if e.unsure {
                        Text(Words.Timeline.unsure).font(.system(size: 15, weight: .semibold)).foregroundStyle(amber).padding(.leading, -5).fixedSize()
                            .accessibilityLabel(Words.Timeline.unsureSaid)
                    }
                    if e.waiting { badge }
                }
                .frame(minHeight: 18, alignment: .leading)
                .animation(.easeOut(duration: 0.25), value: e.waiting)
                if let d = e.detail { Text(d).font(.system(size: 12)).foregroundStyle(muted).lineLimit(1).frame(minHeight: 15, alignment: .leading) }
            }
            .padding(.leading, TLColumns.text - TLColumns.centre - TLColumns.bead / 2).padding(.top, titleTop)
            Spacer(minLength: 8)
            Text(e.right).font(tlFigures(11.5)).foregroundStyle(e.going ? warm : e.strongRight ? ink : muted).lineLimit(1).fixedSize().frame(height: 18).padding(.top, titleTop)
                .padding(.trailing, arrowRoom)
        }
        .overlay(alignment: .topTrailing) { if arrow { chevron.padding(.top, titleTop + 3) } }
        .opacity(e.later ? 0.62 : 1)
    }

    /// the arrow of a row that opens where it stands: down, and up while it is open
    private var chevron: some View {
        Image(systemName: "chevron.down").font(.system(size: 10, weight: .semibold)).foregroundStyle(RGB(196, 204, 238).color(0.6))
            .frame(width: 12, height: 12)
            .rotationEffect(.degrees(open ? 180 : 0))
            .animation(reduce ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.3), value: open)
            .accessibilityHidden(true)
    }

    /// the amber 等你, which fades when what waited is answered
    private var badge: some View {
        Text("等你").font(.system(size: 11, weight: .semibold)).foregroundStyle(amber).padding(.horizontal, 6).frame(height: 18)
            .background(amber.opacity(0.16), in: Capsule()).fixedSize().transition(.opacity)
    }

    /// the circle (white 0.06, the kind's icon in it; dashed and outlined for what has not happened); a stop on the way is a small dot
    @ViewBuilder private var bead: some View {
        if let n = e.token {
            TLStopToken(number: n).frame(width: TLColumns.bead, height: TLColumns.bead)
        } else if e.rail == .gap {
            Color.clear.frame(width: TLColumns.bead, height: TLColumns.bead)
        } else if e.kind == .pause {
            Circle().fill(ink.opacity(0.5)).frame(width: 7, height: 7).frame(width: TLColumns.bead, height: TLColumns.bead)
        } else {
            ZStack {
                if e.later { Circle().strokeBorder(ink.opacity(0.34), style: StrokeStyle(lineWidth: 1, dash: [2.5, 2.5])) } else { Circle().fill(Color.white.opacity(0.06)) }
                Image(systemName: e.later ? e.symbol.replacingOccurrences(of: ".fill", with: "") : e.symbol).font(.system(size: 14))
                    .foregroundStyle(e.kind == .segment ? e.color?.color ?? muted : iconColor)          // a stretch's icon is its category's colour
            }
            .frame(width: TLColumns.bead, height: TLColumns.bead)
            .background { if let flight { Color.clear.onGeometryChange(for: CGRect.self) { $0.frame(in: .named("padHome")) } action: { flight.rects[e.id] = $0 } } }          // the iPad's stage flies this icon to its title
        }
    }

    // MARK: the rail

    /// A place's rail (2 pt in its hue at 0.35) runs from its dot through the rows of its group, through what an open one says too, and a trip's (dashed) through the trip and out through the
    /// blanks either side of it (`gapRail`); each piece stops short of the circle it passes. A row with no rail in or out has none.
    @ViewBuilder private var rails: some View {
        if e.rail == .gap {          // a stretch with no fixes: dotted through the whole row
            VStack(spacing: 0) {
                Color.clear.frame(height: e.gap)
                rail(e.rail).frame(maxHeight: .infinity)
            }
            .frame(width: 2).padding(.leading, TLColumns.centre - 1)
        } else if e.gapRail != .none || (e.rail != .none && (e.railIn || e.railOut)) {
            let air = 3.0, above = max(0, (e.base - beadSize) / 2 - air), through = e.rail != .none
            VStack(spacing: 0) {
                if e.gapRail != .none { rail(e.gapRail).frame(height: e.gap) } else { Color.clear.frame(height: e.gap) }
                if through && e.railIn { rail(e.rail).frame(height: above) } else { Color.clear.frame(height: above) }
                Color.clear.frame(height: beadSize + 2 * air)
                if through && e.railOut { rail(e.rail).frame(maxHeight: .infinity) } else { Spacer(minLength: 0) }
            }
            .frame(width: 2).padding(.leading, TLColumns.centre - 1)
        }
    }

    @ViewBuilder private func rail(_ r: TLRail) -> some View {
        switch r {
        case .place(let hue): Rectangle().fill(hue.color(0.35)).frame(width: 2)
        case .trip: DashedRail().stroke(tripRail.opacity(0.4), style: StrokeStyle(lineWidth: 1, dash: [2, 3])).frame(width: 2)
        case .gap: DashedRail().stroke(FootprintPaint.gap.color(FootprintPaint.gapAlpha), style: StrokeStyle(lineWidth: 2, lineCap: .round, dash: [0.1, 5])).frame(width: 2)
        case .none: Color.clear
        }
    }
}

private struct DashedRail: Shape {
    func path(in r: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: r.midX, y: r.minY)); p.addLine(to: CGPoint(x: r.midX, y: r.maxY))
        return p
    }
}

// MARK: - the tap on a row

/// A row a tap does something on is a button, so that the row answers the touch itself: a wash of white (0.07, as the finger on the bar lights it) comes on quickly at touch-down and goes
/// more slowly on lift. The long press that lifts the menu (`TLMenu`) goes on over it. A row a tap does nothing on has no tap.
private struct TLRowTap: ViewModifier {
    let on: Bool
    let tap: () -> Void

    func body(content: Content) -> some View {
        if on { Button(action: tap) { content }.buttonStyle(TLRowPress()) } else { content }
    }
}

private struct TLRowPress: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        let down = configuration.isPressed
        configuration.label
            .background {
                RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(down ? 0.07 : 0)).padding(.horizontal, -8)
                    .animation(down ? .snappy(duration: 0.12) : .smooth(duration: 0.25), value: down)
            }
            .onChange(of: down) { _, on in if on { Haptic.prepare() } }
    }
}

// MARK: - the menu of a long press

/// A long press on a row lifts the row opened as its preview and offers 打开 (where a tap would open it: SwiftUI has no tap on the preview that commits to it), then what the row can do,
/// each only where it works: 问她 (on every row, a place's header too, that there is something to put to her about, whether it opens or not), 改名字, 起个名字, 知道了, 是我的, 不是, and on
/// a stretch 这段在干嘛 last. A row that opens to nothing and offers none has no menu.
private struct TLMenu: ViewModifier {
    let e: TLEntry
    let tl: TimelineState
    let engine: StarEngine
    let inPlace: Bool
    let open: Bool
    let room: Double

    private var actions: [TLAction] { TLAction.menu.filter { e.actions.contains($0) } }
    /// what 打开 does: on the phone the row opens where it stands (not when it is open already), on the iPad on the stage
    private var opens: Bool { inPlace ? e.expands && !open : e.opens }

    func body(content: Content) -> some View {
        if e.tapOnly || (actions.isEmpty && !opens) {
            content
        } else {
            content.contextMenu {
                if opens {
                    Button {
                        Haptic.selection()
                        if inPlace, let r = tl.reply { tl.unfold(e, in: tl.model(r), room: room) } else { tl.openRow = e.id }
                    } label: { Label("打开", systemImage: "arrow.up.left.and.arrow.down.right") }
                }
                ForEach(actions, id: \.self) { a in
                    Button { tl.perform(a, on: e, engine: engine) } label: { Label(a.title, systemImage: a.symbol) }
                }
            } preview: {
                TLPreview(e: e, tl: tl, engine: engine)
            }
        }
    }
}

/// the row as the menu lifts it: its line, and what it opens to
private struct TLPreview: View {
    let e: TLEntry
    let tl: TimelineState
    let engine: StarEngine

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Text(e.time).font(AT.mono(11.5)).foregroundStyle(muted)
                Image(systemName: e.symbol).font(.system(size: 14)).foregroundStyle(e.kind == .segment ? e.color?.color ?? muted : iconColor).frame(width: 28, height: 28).background(Color.white.opacity(0.06), in: Circle())
                VStack(alignment: .leading, spacing: 2) {
                    Text(e.title).font(.system(size: 15)).foregroundStyle(e.unknown ? muted : ink).lineLimit(3)
                    if let d = e.detail { Text(d).font(.system(size: 12)).foregroundStyle(muted).lineLimit(4) }
                }
                Spacer(minLength: 6)
                Text(e.right).font(tlFigures(11.5)).foregroundStyle(muted).lineLimit(1).fixedSize()
            }
            TLOpenView(e: e, tl: tl, engine: engine, interactive: false)
        }
        .padding(16)
        .frame(width: 344, alignment: .leading)
        .background(Color(red: 16 / 255, green: 18 / 255, blue: 30 / 255))
        .environment(\.colorScheme, .dark)
    }
}

/// What lights a row: the finger on the bar over it (`hot`), the way to what waits having just arrived at it (`flash`), and on the iPad its being the row on the stage. It reads them
/// itself, so that the minute moving across the bar draws these small views and not the list and its rows.
private struct TLRowHeat: View {
    let id: String
    let tl: TimelineState
    var stage = false                   // the list beside the iPad's stage

    var body: some View {
        let hot = tl.hot == id, flashing = tl.flash == id, staged = stage && tl.openRow == id
        ZStack {
            RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(staged ? 0.05 : 0)).animation(padMotion, value: staged)
            RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(hot ? 0.07 : 0)).animation(.easeOut(duration: 0.1), value: hot)
            RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(flashing ? 0.12 : 0)).animation(flashing ? nil : .easeOut(duration: 0.6), value: flashing)
        }
        .padding(.horizontal, -8)
    }
}
