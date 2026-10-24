// words: legacy
import SwiftUI

// One line of the day's list. On the phone (`inPlace`) it is the proto's: the time at the left in figures, the circle of its kind on the rail, the title (an amber question mark after it
// where she is not sure) with one grey line under it, and at the right how long; a small arrow at the far right only where the row opens where it stands. A place over a group of
// stretches is a header: its dot on the rail, its name and its hours. The rail runs through every circle, in the place's hue inside a place he keeps, solid above the warm dot of now and dashed
// below it; now is that dot and no row of its own. The heights are `TLMetrics` (46, with a line 60, a header 40, 12 between groups), so every row he can touch is a target of at least 44.
// Opened, a row on the phone unfolds what it has to say under itself (Sky/DayTimelineOpen.swift); on the iPad it opens on the stage beside the list, and the footprint page draws its
// own rows with the older columns below (`TLColumns`).

private let iconColor = Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.82)
private let tripRail = Color(red: 196 / 255, green: 208 / 255, blue: 1)
private let beadFill = Color(red: 17 / 255, green: 18 / 255, blue: 22 / 255)         // the dark of a circle on the rail: opaque, so that the rail passes behind it

/// where the phone's columns are, from the left of the row (design points of the proto)
private enum PhoneCol {
    static let side = TLLayout.Today.side, beadX = 75.3, bead = 29.0, text = 115.0
    static let railX = 89.3, railW = 1.5                // the rail's left edge and width: through the middle of the circles
    static let dotX = 85.3                              // a place's dot (9 pt) on it
    static let right = 49.0, arrow = 25.0               // the length ends 49 pt from the right edge, the arrow's middle 31
}

/// what the rail is between two rows of the phone's list
enum PhoneRail: Equatable {
    case none
    case past                       // what has happened, outside a place he keeps: a faint line
    case place(RGB)                 // between two rows of one place he keeps: its hue
    case ahead                      // after now: dashed

    /// the rail from `a` down to `b`; `ahead`: `b` is after the line for now
    static func between(_ a: TLEntry, _ b: TLEntry, ahead: Bool) -> PhoneRail {
        if ahead { return .ahead }
        if case .place(let hue) = b.rail, let g = b.group, g == a.id || g == a.group { return .place(hue) }
        return .past
    }
}

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
    var railUp = PhoneRail.none         // the phone's rail above its circle, and below it
    var railDown = PhoneRail.none
    @Environment(\.accessibilityReduceMotion) private var reduce
    @Environment(\.padFlight) private var flight          // set on the iPad only

    /// Two rows are the same when what they draw is, and where they stand (the closures come from the list and know where): a row that is the same is not drawn again, which is what keeps
    /// the list cheap while it is scrolled to the minute under a finger. What lights a row is read by the row's own `TLRowHeat`, and what it opened to reads what it shows itself.
    static func == (a: TLRowView, b: TLRowView) -> Bool {
        a.e == b.e && a.dim == b.dim && a.top == b.top && a.room == b.room && a.inPlace == b.inPlace && a.open == b.open && a.railUp == b.railUp && a.railDown == b.railDown
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
            lineOfRow
                .frame(height: e.base, alignment: .top)
                .overlay(alignment: .bottomLeading) {          // an outing's speed along it (the footprint page)
                    if !e.ribbon.isEmpty { TLRibbon(cells: e.ribbon).frame(width: TLColumns.words, height: TLRibbon.height).padding(.leading, TLColumns.text).padding(.bottom, 6) }
                }
                .background { TLRowHeat(id: e.id, tl: tl, inset: inPlace ? 12 : -8) }
                .contentShape(Rectangle())
                .modifier(TLRowTap(on: touchable, tap: tap, inset: inPlace ? 12 : -8))
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(touchable ? .isButton : [])
                .accessibilityHint(arrow ? (open ? Words.Timeline.collapse : Words.Timeline.expand) : "")
                .modifier(TLMenu(e: e, tl: tl, engine: engine, inPlace: inPlace, open: open, room: room))
            if open && inPlace {
                TLOpenView(e: e, tl: tl, engine: engine, inPlace: true, mapWidth: TLColumns.words - TLColumns.arrow)
                    .padding(.horizontal, 30)          // what it says keeps the columns of the 30 pt list it was laid out for
                    .onGeometryChange(for: Double.self) { Double($0.size.height) } action: { tl.openH = $0 }
                    .transition(reduce ? .opacity : TLUnfold.transition)
            }
        }
        .background(alignment: .topLeading) { if inPlace { phoneRails } else { rails } }
        .opacity(dim)
        .animation(.easeOut(duration: 0.15), value: dim)
    }

    // MARK: the line

    @ViewBuilder private var lineOfRow: some View { if inPlace { phoneLine } else { line } }

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
            Circle().fill((e.color.map { $0.color } ?? Look.ink2).opacity(e.kept ? 1 : 0.45)).frame(width: 8, height: 8)
                .padding(.leading, TLColumns.centre - 4)
            Text(e.title).font(.system(size: 15.5, weight: .semibold)).foregroundStyle(Look.ink).lineLimit(1)
                .padding(.leading, TLColumns.text - TLColumns.centre - 4)
            Spacer(minLength: 10)
            Text(e.right).font(tlFigures(11.5)).foregroundStyle(Look.ink2).lineLimit(1).fixedSize().layoutPriority(1)
                .padding(.trailing, arrowRoom)
        }
        .frame(height: e.base)
        .overlay(alignment: .trailing) { if arrow { chevron } }
    }

    private var nowLine: some View {
        HStack(spacing: 10) {
            Text(e.title).font(tlFigures(11.5)).foregroundStyle(Look.now).fixedSize()
            Rectangle().fill(Look.now.opacity(0.85)).frame(height: 1)
        }
        .frame(maxHeight: .infinity)
    }

    /// time, circle, title with its muted line (and the amber 等你 while it waits), and at the right how long, or the value of a point; what has not happened is at 0.62
    private var item: some View {
        HStack(alignment: .top, spacing: 0) {
            Text(e.time).font(Look.figures(11.5)).foregroundStyle(Look.ink2).lineLimit(1).frame(width: TLColumns.time, height: 18, alignment: .leading).padding(.top, titleTop)
            bead.padding(.leading, TLColumns.centre - TLColumns.bead / 2 - TLColumns.time).padding(.top, (e.base - TLColumns.bead) / 2)
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(e.title).font(.system(size: 15)).foregroundStyle(e.unknown ? Look.ink2 : Look.ink).lineLimit(1)
                    if e.unsure {
                        Text(Words.Timeline.unsure).font(.system(size: 15, weight: .semibold)).foregroundStyle(Look.waits).padding(.leading, -5).fixedSize()
                            .accessibilityLabel(Words.Timeline.unsureSaid)
                    }
                    if e.waiting { badge }
                }
                .frame(minHeight: 18, alignment: .leading)
                .animation(.easeOut(duration: 0.25), value: e.waiting)
                if let d = e.detail { Text(d).font(.system(size: 12)).foregroundStyle(Look.ink2).lineLimit(1).frame(minHeight: 15, alignment: .leading) }
            }
            .padding(.leading, TLColumns.text - TLColumns.centre - TLColumns.bead / 2).padding(.top, titleTop)
            Spacer(minLength: 8)
            Text(e.right).font(tlFigures(11.5)).foregroundStyle(e.going ? Look.now : e.strongRight ? Look.ink : Look.ink2).lineLimit(1).fixedSize().frame(height: 18).padding(.top, titleTop)
                .padding(.trailing, arrowRoom)
        }
        .overlay(alignment: .topTrailing) { if arrow { chevron.padding(.top, titleTop + 3) } }
        .opacity(e.later ? 0.62 : 1)
    }

    /// the arrow of a row that opens where it stands: down, and up while it is open
    private var chevron: some View {
        Image(systemName: "chevron.down").font(.system(size: 10, weight: .semibold)).foregroundStyle(Look.ink2)
            .frame(width: 12, height: 12)
            .rotationEffect(.degrees(open ? 180 : 0))
            .animation(reduce ? nil : .timingCurve(0.2, 0.8, 0.2, 1, duration: 0.3), value: open)
            .accessibilityHidden(true)
    }

    /// the amber 等你, which fades when what waited is answered
    private var badge: some View {
        Text("等你").font(.system(size: Look.small, weight: .semibold)).foregroundStyle(Look.waits).padding(.horizontal, 6).frame(height: 18)
            .background(Look.waits.opacity(0.16), in: Capsule()).fixedSize().transition(.opacity)
    }

    /// the circle (white 0.06, the kind's icon in it; dashed and outlined for what has not happened); a stop on the way is a small dot
    @ViewBuilder private var bead: some View {
        if let n = e.token {
            TLStopToken(number: n).frame(width: TLColumns.bead, height: TLColumns.bead)
        } else if e.rail == .gap {
            Color.clear.frame(width: TLColumns.bead, height: TLColumns.bead)
        } else if e.kind == .pause {
            Circle().fill(Look.ink.opacity(0.5)).frame(width: 7, height: 7).frame(width: TLColumns.bead, height: TLColumns.bead)
        } else {
            ZStack {
                if e.later { Circle().strokeBorder(Look.ink.opacity(0.34), style: StrokeStyle(lineWidth: 1, dash: [2.5, 2.5])) } else { Circle().fill(Color.white.opacity(0.06)) }
                TLSymbol(name: e.later ? e.symbol.replacingOccurrences(of: ".fill", with: "") : e.symbol, size: 14)
                    .foregroundStyle(e.kind == .segment ? e.color?.color ?? Look.ink2 : iconColor)          // a stretch's icon is its category's colour
            }
            .frame(width: TLColumns.bead, height: TLColumns.bead)
            .background { if let flight { Color.clear.onGeometryChange(for: CGRect.self) { $0.frame(in: .named("padHome")) } action: { flight.rects[e.id] = $0 } } }          // the iPad's stage flies this icon to its title
        }
    }

    // MARK: the phone's line

    @ViewBuilder private var phoneLine: some View {
        switch e.kind {
        case .now: phoneNow
        case .stay: phoneStay
        default: phoneItem
        }
    }

    /// now is no row: a warm dot on the rail (7.5 pt, a ring of the sky's dark round it and a glow), with the rail solid above it and dashed below
    private var phoneNow: some View {
        Color.clear.frame(maxWidth: .infinity).frame(height: e.base)
            .overlay(alignment: .leading) {
                Circle().fill(Look.now).frame(width: 7.5, height: 7.5)
                    .background(Circle().fill(Look.sky).padding(-3))
                    .shadow(color: Look.now.opacity(0.6), radius: 5)
                    .padding(.leading, PhoneCol.railX + PhoneCol.railW / 2 - 3.75)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(Words.PhoneList.now(e.time))
    }

    /// a place over its group of stretches: its dot (9 pt) on the rail, its name (16, semibold), and at the right the hours it was there, grey
    private var phoneStay: some View {
        HStack(spacing: 0) {
            Circle().fill((e.color.map { $0.color } ?? Look.ink2).opacity(e.kept ? 1 : 0.45)).frame(width: 9, height: 9)
                .padding(.leading, PhoneCol.dotX)
            Text(e.title).font(.system(size: 16, weight: .semibold)).foregroundStyle(Look.ink).lineLimit(1)
                .padding(.leading, PhoneCol.text - PhoneCol.dotX - 9)
            Spacer(minLength: 10)
            Text(hours).font(Look.figures(Look.note)).foregroundStyle(Look.ink3).lineLimit(1).fixedSize().layoutPriority(1)
        }
        .padding(.trailing, PhoneCol.right)
        .frame(height: e.base)
        .overlay(alignment: .trailing) { if arrow { chevron.padding(.trailing, PhoneCol.arrow) } }
    }

    /// the hours of a place (`18:50 起`, `15:19–18:15`), without the length the model adds after them
    private var hours: String { e.right.components(separatedBy: " · ").first ?? e.right }

    /// time, circle, title with its grey line (and the amber 等你 while it waits), and at the right how long; what has not happened is at 0.62
    private var phoneItem: some View {
        HStack(spacing: 0) {
            Text(e.time).font(Look.figures(Look.note)).foregroundStyle(Look.ink3).lineLimit(1)
                .frame(width: PhoneCol.beadX - PhoneCol.side, alignment: .leading)
            phoneBead.frame(width: PhoneCol.bead, height: PhoneCol.bead)
            VStack(alignment: .leading, spacing: 1) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(e.title).font(.system(size: Look.body, weight: .medium)).foregroundStyle(e.unknown ? Look.ink2 : Look.ink).lineLimit(1)
                    if e.unsure {
                        Text(Words.Timeline.unsure).font(.system(size: Look.body, weight: .semibold)).foregroundStyle(Look.waits).padding(.leading, -5).fixedSize()
                            .accessibilityLabel(Words.Timeline.unsureSaid)
                    }
                    if e.waiting { badge }
                }
                .animation(.easeOut(duration: 0.25), value: e.waiting)
                if let d = e.detail { Text(d).font(.system(size: Look.note)).foregroundStyle(Look.ink3).lineLimit(1) }
            }
            .padding(.leading, PhoneCol.text - PhoneCol.beadX - PhoneCol.bead).frame(maxWidth: .infinity, alignment: .leading)
            Text(e.right).font(Look.figures(Look.note, weight: e.going || e.strongRight ? .medium : .regular))
                .foregroundStyle(e.going ? Look.now : e.strongRight ? Look.ink : Look.ink2).lineLimit(1).fixedSize().padding(.leading, 8)
        }
        .padding(.leading, PhoneCol.side).padding(.trailing, PhoneCol.right)
        .frame(height: e.base)
        .overlay(alignment: .trailing) { if arrow { chevron.padding(.trailing, PhoneCol.arrow) } }
        .opacity(e.later ? 0.62 : 1)
    }

    /// the circle on the rail: dark and opaque, the kind's symbol in its colour (a stretch's category, else the faint one); dashed and outlined for what has not happened; a number ring for
    /// a stop on a trip, a small dot for a pause
    @ViewBuilder private var phoneBead: some View {
        if let n = e.token {
            Text("\(n)").font(.system(size: 12, weight: .semibold).monospacedDigit()).foregroundStyle(Look.ink)
                .frame(width: PhoneCol.bead, height: PhoneCol.bead)
                .background(Circle().fill(beadFill)).overlay(Circle().inset(by: 0.7).stroke(Look.ink.opacity(0.7), lineWidth: 1.4))
        } else if e.kind == .pause {
            Circle().fill(Look.ink.opacity(0.5)).frame(width: 7, height: 7)
        } else {
            ZStack {
                Circle().fill(beadFill)
                if e.later { Circle().strokeBorder(Look.ink.opacity(0.34), style: StrokeStyle(lineWidth: 1, dash: [2.5, 2.5])) }
                TLSymbol(name: e.later ? e.symbol.replacingOccurrences(of: ".fill", with: "") : e.symbol, size: 14)
                    .foregroundStyle(e.kind == .segment ? e.color?.color ?? Look.ink2 : iconColor)
            }
            .frame(width: PhoneCol.bead, height: PhoneCol.bead)
        }
    }

    /// the rail through the circles: from the top of the row (and the blank above it) to its middle, and from there to the foot, each as `PhoneRail` says
    private var phoneRails: some View {
        VStack(spacing: 0) {
            phoneRail(railUp).frame(height: e.gap + e.base / 2)
            phoneRail(railDown).frame(maxHeight: .infinity)
        }
        .frame(width: PhoneCol.railW)
        .padding(.leading, PhoneCol.railX)
    }

    @ViewBuilder private func phoneRail(_ r: PhoneRail) -> some View {
        switch r {
        case .none: Color.clear
        case .past: Rectangle().fill(Color.white.opacity(0.10))
        case .place(let hue): Rectangle().fill(hue.color(0.55))
        case .ahead: DashedRail().stroke(Color.white.opacity(0.17), style: StrokeStyle(lineWidth: PhoneCol.railW, dash: [4, 3]))
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
    var inset = -8.0                    // how far the wash reaches out past the row's sides (in, on the phone, where the row is as wide as the screen)

    func body(content: Content) -> some View {
        if on { Button(action: tap) { content }.buttonStyle(TLRowPress(inset: inset)) } else { content }
    }
}

private struct TLRowPress: ButtonStyle {
    let inset: Double

    func makeBody(configuration: Configuration) -> some View {
        let down = configuration.isPressed
        configuration.label
            .background {
                RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(down ? 0.07 : 0)).padding(.horizontal, inset)
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
                Text(e.time).font(Look.figures(11.5)).foregroundStyle(Look.ink2)
                TLSymbol(name: e.symbol, size: 14).foregroundStyle(e.kind == .segment ? e.color?.color ?? Look.ink2 : iconColor).frame(width: 28, height: 28).background(Color.white.opacity(0.06), in: Circle())
                VStack(alignment: .leading, spacing: 2) {
                    Text(e.title).font(.system(size: 15)).foregroundStyle(e.unknown ? Look.ink2 : Look.ink).lineLimit(3)
                    if let d = e.detail { Text(d).font(.system(size: 12)).foregroundStyle(Look.ink2).lineLimit(4) }
                }
                Spacer(minLength: 6)
                Text(e.right).font(tlFigures(11.5)).foregroundStyle(Look.ink2).lineLimit(1).fixedSize()
            }
            TLOpenView(e: e, tl: tl, engine: engine, interactive: false)
        }
        .padding(16)
        .frame(width: 344, alignment: .leading)
        .background(Color(red: 16 / 255, green: 18 / 255, blue: 30 / 255))
        .environment(\.colorScheme, .dark)
    }
}

/// What lights a row: the finger on the bar over it (`hot`), her answer's link to it (`linked`), and the way to what waits having just arrived at it (`flash`). It reads them itself, so that the minute moving across the bar
/// draws these small views and not the list and its rows.
private struct TLRowHeat: View {
    let id: String
    let tl: TimelineState
    let inset: Double

    var body: some View {
        let hot = tl.hot == id || tl.linked.contains(id), flashing = tl.flash == id
        ZStack {
            RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(hot ? 0.07 : 0)).animation(.easeOut(duration: 0.1), value: hot)
            RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(flashing ? 0.12 : 0)).animation(flashing ? nil : .easeOut(duration: 0.6), value: flashing)
        }
        .padding(.horizontal, inset)
    }
}
