import Foundation

// The phone's day as the watch draws it (ios/JarvisWatchShared/WatchWire.swift `WatchDay`). Plain values in, plain values out: the phone's own model of the day
// (Sky/DayTimelineModel.swift, its bar's pieces and colours) and what waits (Brain/Waiting.swift) become the ring, the stretch now, what waits and what comes next, in the phone's own words.

/// the id of the action that says a reminder that rang is known, and the two that say what he thinks of an event his mail made
enum WatchActionID {
    static let ack = "ack", confirm = "confirm", deny = "deny"
    /// a card's button is its place in the card's list
    static func card(_ index: Int) -> String { "a\(index)" }
    static func cardIndex(_ id: String) -> Int? { id.hasPrefix("a") ? Int(id.dropFirst()) : nil }
}

@MainActor
enum WatchDayMaker {
    /// the bus he is on, as the phone's Live Activity has it
    struct Riding { var route: String; var board: String?; var state: RideAttributes.ContentState }

    static func make(_ r: DayReply, minute: Double?, made: Date, cards: [WaitingCard] = [], riding: Riding? = nil,
                     acked: Set<String> = [], done: TLDone = TLDone()) -> WatchDay {
        let model = DayTimelineModel.build(r, now: minute, acked: acked, done: done)
        let entries = model.entries
        let byId = Dictionary(entries.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        let scene = model.scene

        // the ring: what he did, in the bar's colours (a colour at the bar's own strength over the ground it is drawn on); what is planned, as ticks.
        // The bar starts in the morning; the ring is the whole day, so a stretch cut at the bar's start (the night) starts when it did.
        let blocks = scene.pieces.compactMap { p -> WatchDay.Block? in
            guard let look = TLPaint.look(p.fill), let e = byId[p.id] else { return nil }
            let t0 = p.t0 <= scene.span.lowerBound ? max(0, min(p.t0, e.t0)) : p.t0
            return WatchDay.Block(t0: t0, t1: p.t1, rgb: hex(look.rgb, over: TLPaint.ground, alpha: look.alpha), title: e.title, sub: sub(e))
        }
        let ahead = scene.ahead.map { WatchDay.Tick(t: $0.t0, waits: model.waiting.contains($0.id)) }

        // what waits on him: the day's rows that do, then the cards (a reminder's card is its row)
        var waiting: [WatchDay.Waiting] = []
        for id in model.waiting {
            guard let e = byId[id], let w = row(e) else { continue }
            waiting.append(w)
        }
        let rows = Set(model.waiting)
        waiting += cards.filter { !($0.kind == .notice && rows.contains($0.brainId)) }.compactMap(card)

        // what comes next, in time order
        let planned = entries.filter(\.later).sorted { ($0.t0, $0.id) < ($1.t0, $1.id) }
        let waitingIds = Set(waiting.map(\.id))
        let next = planned.filter { !waitingIds.contains($0.id) }.map(planRow)

        var day = WatchDay(date: r.date, language: Words.shared.language.rawValue, now: minute ?? 0, made: made)
        day.blocks = blocks
        day.ahead = ahead
        day.current = minute.flatMap { current(model, byId, at: $0) }
        day.waiting = waiting
        day.next = next
        if let first = planned.first {
            day.headline = .init(title: first.time + " " + first.title, note: first.soon ? first.right : "", waits: first.waiting)
        } else if let w = waiting.first {
            day.headline = .init(title: w.title, note: Words.Relay.waits, waits: true)
        }
        day.leaveAt = planned.lazy.filter { $0.kind == .departure }.compactMap { e in r.items.first { $0.id == e.id }?.leave_at_ms }
            .map { Date(timeIntervalSince1970: Double($0) / 1000) }.first { $0 > made }
        if let riding {
            day.ride = ride(riding, now: made)
            day.headline = .init(title: day.ride?.route ?? "", note: day.ride?.title ?? "", waits: riding.state.isArriving)
        }
        return day
    }

    // MARK: pieces

    /// the stretch he is in: the bar's piece at now, as its row says it
    private static func current(_ m: DayTimelineModel, _ byId: [String: TLEntry], at now: Double) -> WatchDay.Row? {
        guard let s = m.seg(at: now) ?? m.seg(at: now.nextDown), let e = byId[s.id] else { return nil }
        return WatchDay.Row(id: e.id, symbol: symbol(e), rgb: e.kind == .segment ? e.color.map(hex) : nil, title: e.title, sub: e.detail ?? "", right: e.right)
    }

    /// something planned, as the list says it (its dashed circle: the outline of its symbol); the time stands at the right unless the row has a value of its own
    private static func planRow(_ e: TLEntry) -> WatchDay.Row {
        let own = e.soon || e.strongRight
        let line = [own ? e.time : nil, e.detail].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
        return WatchDay.Row(id: e.id, symbol: symbol(e), title: e.title, sub: line, right: own ? e.right : e.time, planned: true)
    }

    /// a row of the day that waits on him, with what he can say to it from the wrist; none when he can say nothing
    private static func row(_ e: TLEntry) -> WatchDay.Waiting? {
        let actions: [WatchDay.Action]
        switch e.kind {
        case .reminder: actions = [.init(id: WatchActionID.ack, title: TLAction.acknowledge.title)]
        case .event where e.commitment != nil: actions = [.init(id: WatchActionID.confirm, title: TLAction.confirm.title), .init(id: WatchActionID.deny, title: TLAction.deny.title)]
        default: return nil
        }
        return WatchDay.Waiting(id: e.id, kind: .reminder, symbol: e.symbol, title: e.title, sub: [e.time, e.detail].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " "), actions: actions)
    }

    /// a card, with the buttons that need nothing typed and nothing opened on the phone; none left: the card is not for the wrist
    static func card(_ c: WaitingCard) -> WatchDay.Waiting? {
        let actions = c.actions.enumerated().compactMap { i, a -> WatchDay.Action? in
            a.answer == .fill || a.answer == .openLogin ? nil : WatchDay.Action(id: WatchActionID.card(i), title: a.label)
        }
        guard !actions.isEmpty else { return nil }
        let symbol: String
        switch c.kind {
        case .confirmation: symbol = "checkmark.circle"
        case .clarification: symbol = "questionmark.bubble"
        case .notice: symbol = "bell.fill"
        case .plugin: symbol = "puzzlepiece.extension"
        case .claude: symbol = "terminal.fill"
        case .ride: symbol = "bus.fill"
        }
        return WatchDay.Waiting(id: c.id, kind: .card, symbol: symbol, title: c.title, sub: c.meta, actions: actions)
    }

    /// the bus: its words are the Live Activity's
    private static func ride(_ r: Riding, now: Date) -> WatchDay.Ride {
        let s = r.state
        let title = s.isEnded ? "" : s.isArriving ? Words.Relay.getOff : s.stopsLeft.map(Words.Relay.stopsLeft) ?? s.nextStop.map(Words.Relay.nextStop) ?? Words.Relay.onTheWay
        let sub = [s.alightStop.map(Words.Relay.toStop), s.minutesText].compactMap { $0 }.joined(separator: " · ")
        let stops = [r.board, s.alightStop].compactMap { $0 }
        return WatchDay.Ride(route: Words.Relay.route(s.route ?? r.route), title: title, sub: sub, stops: stops, at: 0,
                             note: s.trustNote.isEmpty ? nil : s.trustNote, alightNext: s.isArriving)
    }

    /// a stay's header has a dot and no symbol; the watch gets the symbol the bar wears for it. What has not happened is drawn as an outline.
    private static func symbol(_ e: TLEntry) -> String {
        let s = e.symbol.isEmpty ? e.barIcon ?? "mappin" : e.symbol
        return e.later ? s.replacingOccurrences(of: ".fill", with: "") : s
    }

    /// the line under a piece of the ring when it is tapped: where, then its hours and how long (the readout's words)
    private static func sub(_ e: TLEntry) -> String {
        let tail = e.head.map { $0.hasPrefix(e.title) ? String($0.dropFirst(e.title.count)) : " · " + $0 } ?? ""
        let rest = tail.hasPrefix(" · ") ? String(tail.dropFirst(3)) : tail
        return [e.kind == .segment ? e.detail : nil, rest.isEmpty ? nil : rest].compactMap { $0 }.joined(separator: " · ")
    }

    private static func hex(_ c: RGB) -> UInt32 { UInt32(c.r.rounded()) << 16 | UInt32(c.g.rounded()) << 8 | UInt32(c.b.rounded()) }
    private static func hex(_ c: RGB, over g: RGB, alpha a: Double) -> UInt32 {
        hex(RGB(c.r * a + g.r * (1 - a), c.g * a + g.g * (1 - a), c.b * a + g.b * (1 - a)))
    }
}
