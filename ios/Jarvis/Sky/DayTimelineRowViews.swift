import SwiftUI

// One line of the day's list: the time, the icon in its circle (dashed for what has not happened, a lit star on what mattered), the title with its one muted line, and at the right how long
// or the value. A place carries a rail in its hue through what happened there, a trip a dashed one through all of its run. The heights are `TLMetrics` (single 44, with a line 56, a place's
// header 44, 12 between groups), so nothing crowds and every row is a target of at least 44. Opened, a row says its own title and line whole (they wrap instead of being cut short); what
// else opening it shows is in Sky/DayTimelineOpen.swift.

private let ink = SetColor.ink
private let muted = SetColor.muted
private let amber = Color(red: 1, green: 201 / 255, blue: 143 / 255)               // what waits on him, and nothing else
private let warm = Color(red: 1, green: 240 / 255, blue: 220 / 255)               // now
private let iconColor = Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.82)
private let tripRail = Color(red: 196 / 255, green: 208 / 255, blue: 1)

extension TLAction {
    /// the words on its button and in the menu of a long press
    var title: String {
        switch self {
        case .ask: "问她"
        case .openTalk: "打开对话"
        case .rename: "改名字"
        case .acknowledge: "知道了"
        case .summarize: "让她总结这段"
        case .mark: "标为重要"
        case .unmark: "取消重要"
        }
    }
    var symbol: String {
        switch self {
        case .ask: "text.bubble"
        case .openTalk: "bubble.left.and.bubble.right"
        case .rename: "pencil"
        case .acknowledge: "checkmark.circle"
        case .summarize: "sparkles"
        case .mark: "star"
        case .unmark: "star.slash"
        }
    }
    /// what the menu of a long press offers, in this order: summing up a stretch of work sends a turn at once, so it is a button he sees and not a line of a menu
    static let menu: [TLAction] = [.ask, .openTalk, .rename, .acknowledge, .mark, .unmark]
}

struct TLRowView: View, Equatable {
    let e: TLEntry
    let open: Bool
    let dim: Double                     // 1, less while a lens is on and this is not of its kind
    let top: Double                     // where the row stands in the list and how tall the list is: what its closures were made with
    let room: Double
    let tl: TimelineState
    let engine: StarEngine
    let tap: () -> Void
    let opened: (Double) -> Void        // how tall the row is while it is open (its line, which wraps, and what it opened to), for the list to bring it into view
    @Environment(\.accessibilityReduceMotion) private var reduce

    /// Two rows are the same when what they draw is, and where they stand (the closures come from the list and know where): a row that is the same is not drawn again, which is what keeps
    /// the list cheap while it is scrolled to the minute under a finger. What lights a row is read by the row's own `TLRowHeat`.
    static func == (a: TLRowView, b: TLRowView) -> Bool {
        a.e == b.e && a.open == b.open && a.dim == b.dim && a.top == b.top && a.room == b.room
    }

    /// the circle's size: a place's header has a dot, a stop on the way a smaller one
    private var beadSize: Double { e.kind == .stay ? 8 : e.kind == .pause ? 7 : TLColumns.bead }
    /// where the title's line begins: the block of a row is centred in its height
    private var titleTop: Double { e.detail == nil ? (e.base - 18) / 2 : (e.base - 35) / 2 }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Color.clear.frame(height: e.gap)
            line
                .frame(minHeight: e.base, maxHeight: open ? .infinity : e.base, alignment: .top)        // opened, its words wrap and the line grows to them
                .background { TLRowHeat(id: e.id, tl: tl) }
                .contentShape(Rectangle())
                .onTapGesture(perform: tap)
                .accessibilityElement(children: .combine)
                .accessibilityAddTraits(e.opens ? .isButton : [])
                .modifier(TLMenu(e: e, tl: tl, engine: engine))
            if open {
                TLOpenView(e: e, tl: tl, engine: engine)
                    .padding(.leading, TLColumns.text).padding(.top, 2).padding(.bottom, 14)
                    .transition(.asymmetric(insertion: .opacity.animation(.easeOut(duration: 0.18).delay(0.06)), removal: .opacity.animation(.easeOut(duration: 0.1))))
            }
        }
        .background(alignment: .topLeading) { rails }
        .onGeometryChange(for: Double.self) { open ? Double($0.size.height) - e.gap : 0 } action: { if $0 > 0 { opened($0) } }
        .opacity(dim)
        .animation(.easeOut(duration: 0.15), value: dim)
        .onChange(of: open) { _, now in if !now, tl.renaming == e.id { tl.cancelRename() } }          // shut with its field open: the field goes with it
    }

    // MARK: the line

    @ViewBuilder private var line: some View {
        switch e.kind {
        case .now: nowLine
        case .stay: stay
        default: item
        }
    }

    /// the place he stayed in: its colour as a dot on the rail, its name, and at the right its hours and how long
    private var stay: some View {
        HStack(spacing: 0) {
            Circle().fill((e.color.map { $0.color } ?? muted).opacity(e.kept ? 1 : 0.45)).frame(width: 8, height: 8)
                .padding(.leading, TLColumns.centre - 4)
            Text(e.title).font(.system(size: 15.5, weight: .semibold)).foregroundStyle(ink).lineLimit(1)
                .padding(.leading, TLColumns.text - TLColumns.centre - 4)
            if e.lit { TLRowStar(hers: e.hers).frame(width: 13, height: 13).padding(.leading, 6).transition(.opacity) }
            Spacer(minLength: 10)
            Text(e.right).font(AT.mono(11.5)).foregroundStyle(muted).lineLimit(1).fixedSize().layoutPriority(1)
        }
        .animation(tlStarFade(reduce: reduce), value: e.lit)
        .frame(height: e.base)
    }

    private var nowLine: some View {
        HStack(spacing: 10) {
            Text(e.title).font(AT.mono(11)).foregroundStyle(warm).fixedSize()
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
                    Text(e.title).font(.system(size: 15)).foregroundStyle(ink).lineLimit(open ? 3 : 1)
                    if e.waiting { badge }
                }
                .frame(minHeight: 18, alignment: .leading)
                .animation(.easeOut(duration: 0.25), value: e.waiting)
                if let d = e.detail { Text(d).font(.system(size: 12)).foregroundStyle(muted).lineLimit(open ? 4 : 1).frame(minHeight: 15, alignment: .leading) }
            }
            .padding(.leading, TLColumns.text - TLColumns.centre - TLColumns.bead / 2).padding(.top, titleTop)
            Spacer(minLength: 8)
            Text(e.right).font(AT.mono(11.5)).foregroundStyle(e.strongRight ? ink : muted).lineLimit(1).fixedSize().frame(height: 18).padding(.top, titleTop)
        }
        .opacity(e.later ? 0.62 : 1)
    }

    /// the amber 等你, which fades when what waited is answered
    private var badge: some View {
        Text("等你").font(.system(size: 11, weight: .semibold)).foregroundStyle(amber).padding(.horizontal, 6).frame(height: 18)
            .background(amber.opacity(0.16), in: Capsule()).fixedSize().transition(.opacity)
    }

    /// the circle (white 0.06, the kind's icon in it; dashed and outlined for what has not happened; a lit star on its corner for what mattered); a stop on the way is a small dot
    @ViewBuilder private var bead: some View {
        if e.kind == .pause {
            Circle().fill(ink.opacity(0.5)).frame(width: 7, height: 7).frame(width: TLColumns.bead, height: TLColumns.bead)
        } else {
            ZStack {
                if e.later { Circle().strokeBorder(ink.opacity(0.34), style: StrokeStyle(lineWidth: 1, dash: [2.5, 2.5])) } else { Circle().fill(Color.white.opacity(0.06)) }
                Image(systemName: e.later ? e.symbol.replacingOccurrences(of: ".fill", with: "") : e.symbol).font(.system(size: 14)).foregroundStyle(iconColor)
            }
            .frame(width: TLColumns.bead, height: TLColumns.bead)
            .overlay(alignment: .topTrailing) { if e.lit { TLRowStar(hers: e.hers).frame(width: 12, height: 12).offset(x: 3, y: -3).transition(.opacity) } }
            .animation(tlStarFade(reduce: reduce), value: e.lit)
        }
    }

    // MARK: the rail

    /// A place's rail (2 pt in its hue at 0.35) runs from its dot through the rows that happened there, and a trip's (dashed) through all of a run; each piece stops short of the circle it
    /// passes and runs on through what the row opened to. A row with no rail in or out has none.
    @ViewBuilder private var rails: some View {
        if e.rail != .none, e.railIn || e.railOut {
            let air = 3.0, above = max(0, (e.base - beadSize) / 2 - air)
            VStack(spacing: 0) {
                Color.clear.frame(height: e.gap)
                if e.railIn { rail.frame(height: above) } else { Color.clear.frame(height: above) }
                Color.clear.frame(height: beadSize + 2 * air)
                if e.railOut { rail.frame(maxHeight: .infinity) } else { Spacer(minLength: 0) }
            }
            .frame(width: 2).padding(.leading, TLColumns.centre - 1)
        }
    }

    @ViewBuilder private var rail: some View {
        switch e.rail {
        case .place(let hue): Rectangle().fill(hue.color(0.35)).frame(width: 2)
        case .trip: DashedRail().stroke(tripRail.opacity(0.4), style: StrokeStyle(lineWidth: 1, dash: [2, 3])).frame(width: 2)
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

// MARK: - the menu of a long press

/// A long press on a row lifts the row opened as its preview and offers what the row can do, each only where it works: 问她, 打开对话, 改名字, 知道了. A row that offers none has no menu.
private struct TLMenu: ViewModifier {
    let e: TLEntry
    let tl: TimelineState
    let engine: StarEngine

    private var actions: [TLAction] { TLAction.menu.filter { e.actions.contains($0) } }

    func body(content: Content) -> some View {
        if actions.isEmpty {
            content
        } else {
            content.contextMenu {
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
                Image(systemName: e.symbol).font(.system(size: 14)).foregroundStyle(iconColor).frame(width: 28, height: 28).background(Color.white.opacity(0.06), in: Circle())
                VStack(alignment: .leading, spacing: 2) {
                    Text(e.title).font(.system(size: 15)).foregroundStyle(ink).lineLimit(3)
                    if let d = e.detail { Text(d).font(.system(size: 12)).foregroundStyle(muted).lineLimit(4) }
                }
                Spacer(minLength: 6)
                Text(e.right).font(AT.mono(11.5)).foregroundStyle(muted).lineLimit(1).fixedSize()
            }
            TLOpenView(e: e, tl: tl, engine: engine, interactive: false)
        }
        .padding(16)
        .frame(width: 344, alignment: .leading)
        .background(Color(red: 16 / 255, green: 18 / 255, blue: 30 / 255))
        .environment(\.colorScheme, .dark)
    }
}

/// What lights a row: the finger on the bar over it (`hot`), and the way to what waits having just arrived at it (`flash`). It reads them itself, so that the minute moving across the bar
/// draws these small views and not the list and its rows.
private struct TLRowHeat: View {
    let id: String
    let tl: TimelineState

    var body: some View {
        let hot = tl.hot == id, flashing = tl.flash == id
        ZStack {
            RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(hot ? 0.07 : 0)).animation(.easeOut(duration: 0.1), value: hot)
            RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(flashing ? 0.12 : 0)).animation(flashing ? nil : .easeOut(duration: 0.6), value: flashing)
        }
        .padding(.horizontal, -8)
    }
}
