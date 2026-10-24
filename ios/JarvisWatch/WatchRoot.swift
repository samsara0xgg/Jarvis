import SwiftUI
import WatchKit

/// The watch app (跨端UI标准 §12): the screen is her. A talk keeps her in the middle, a little smaller, his words under her; a card lies over her and she peeks out
/// above it; the crown turns to the pages (今天要办的, 坐车), where she sits in the top-right corner. She is one view over all of it, which moves and resizes between
/// these places on one spring. 设置 is pushed from the end of 今天要办的, she still in the corner. Sizes are read off the boards, drawn at 208 × 248 (46 mm), and scale with the screen.
struct WatchRoot: View {
    /// a card rises and drops, and she moves, on this one spring
    static let spring = Animation.spring(duration: 0.45, bounce: 0.15)

    enum Page: Hashable { case home, today, ride }
    private enum Phase { case idle, listening, thinking, answer, failed }

    private let model = WatchModel.shared
    @State private var page: Page
    /// waiting items to lay over her as cards: what he opened from 今天要办的 first, then what came in
    @State private var queue: [String]
    @State private var answered: Set<String> = []
    /// the 该出门了 he put away (its `leaveAt`), so it does not come back
    @State private var leaveDismissed: Date?
    @State private var cardHeight: CGFloat = 0
    @State private var settingsOpen: Bool
    /// the pages open scrolled to their end (the debug scenes)
    private let atEnd: Bool
    @AppStorage(WatchSettings.taps) private var taps = true
    @Environment(\.isLuminanceReduced) private var aod

    init(page: Page = .home, card: String? = nil, settings: Bool = false, atEnd: Bool = false) {
        _page = State(initialValue: page)
        _queue = State(initialValue: card.map { [$0] } ?? [])
        _settingsOpen = State(initialValue: settings)
        self.atEnd = atEnd
    }

    private var phase: Phase {
        switch model.talk {
        case .idle: .idle
        case .listening: .listening
        case .thinking: .thinking
        case .answer: .answer
        case .failed: .failed
        }
    }

    private var answerCard: WatchCard? {
        if case .answer(let spoken, let written, let action) = model.talk { return .answer(spoken: spoken, written: written, action: action) }
        return nil
    }

    private var waits: Bool { model.day?.waiting.contains { !answered.contains($0.id) } ?? false }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 10)) { ctx in          // 该出门了 comes up and counts down by the clock
            let card = WatchCard.top(answer: answerCard, day: model.day, queue: queue, answered: answered, leaveDismissed: leaveDismissed, now: ctx.date)
            stage(card, now: ctx.date)
                .onChange(of: card != nil) { _, up in if up { withAnimation(Self.spring) { page = .home } } }          // a card lies over her in the middle
        }
        .onChange(of: model.day) { old, new in
            answered = []
            if let old, let new {
                let had = Set(old.waiting.map(\.id))
                let came = new.waiting.map(\.id).filter { !had.contains($0) }
                if !came.isEmpty {
                    if taps { WatchHaptics.play(.waits) }          // 提醒时轻敲
                    queue += came
                }
            }
            if (page == .ride && new?.ride == nil) || (page == .today && new == nil) { page = .home }          // ended while its page was open
        }
        .onChange(of: page) { _, new in if new == .home { settingsOpen = false } }          // back to her: 设置 goes with the page it came from
        .onChange(of: model.day?.ride?.alightNext) { old, new in if new == true, old != true { WatchHaptics.play(.alight) } }
        .onChange(of: phase) { _, new in
            if new == .listening { WatchHaptics.play(.listening) }
            if new != .idle { withAnimation(Self.spring) { page = .home } }
        }
        .task(id: model.day?.leaveAt) {
            guard let t = model.day?.leaveAt, t > .now else { return }
            try? await Task.sleep(for: .seconds(t.timeIntervalSinceNow))
            if !Task.isCancelled { WatchHaptics.play(.leave) }
        }
        .onOpenURL { if $0.scheme == "jarvis-watch", $0.host == "talk" { page = .home; model.startTalk() } }
    }

    private func stage(_ card: WatchCard?, now: Date) -> some View {
        let s = WKInterfaceDevice.current().screenBounds.size
        let k = s.width / 208, ky = s.height / 248
        return ZStack {
            pages
            if phase == .listening || phase == .thinking || phase == .failed { captions(ky: ky).transition(.opacity) }
            if phase == .idle, card == nil, page == .home, waits {
                Circle().fill(WatchTheme.amber).frame(width: 8 * k, height: 8 * k).shadow(color: WatchTheme.amber.opacity(0.7), radius: 4)
                    .opacity(aod ? 0.45 : 1)
                    .position(x: 22 * k, y: 18 * ky)
                    .frame(maxWidth: .infinity, maxHeight: .infinity).ignoresSafeArea().allowsHitTesting(false)
            }
            ZStack { her(card, s: s, k: k, ky: ky) }.frame(maxWidth: .infinity, maxHeight: .infinity).ignoresSafeArea()          // placed in screen points
            if let card {
                VStack {
                    Spacer(minLength: 0)
                    WatchCardView(card: card, now: now, maxHeight: s.height - 6 * k - 92 * ky,
                                  answer: { id, action in answer(card, id, action) }, dismiss: { dismiss(card) })
                        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { cardHeight = $0 }
                }
                .padding([.horizontal, .bottom], 6 * k)
                .ignoresSafeArea()
                .id(Self.key(card))
                .transition(.move(edge: .bottom))
            }
        }
        .animation(Self.spring, value: card.map(Self.key))
        .animation(Self.spring, value: phase)
        .animation(Self.spring, value: page)
        .animation(Self.spring, value: cardHeight)
    }

    // MARK: the pages, turned with the crown

    private var pages: some View {
        NavigationStack {
            TabView(selection: $page) {
                Group {
                    if model.day == nil {
                        Text(Words.Watch.noDay).font(.system(size: 13)).foregroundStyle(WatchTheme.muted)
                            .frame(maxHeight: .infinity, alignment: .bottom)
                    } else {
                        Color.clear
                    }
                }
                .tag(Page.home)
                if let day = model.day {
                    WatchTodayPage(day: day, answered: answered, open: openCard, answer: { id, action in send(id, action) },
                                   openSettings: { settingsOpen = true }, atEnd: atEnd).tag(Page.today)
                }
                if let r = model.day?.ride { WatchRideView(ride: r).tag(Page.ride) }
            }
            .tabViewStyle(.verticalPage)
            .navigationDestination(isPresented: $settingsOpen) { WatchSettingsPage(reachable: model.phoneReachable, atEnd: atEnd) }
        }
    }

    // MARK: her

    /// She: full screen in the middle at home; a little smaller and higher in a talk; peeking above a card; in the top-right corner on a page.
    private func her(_ card: WatchCard?, s: CGSize, k: CGFloat, ky: CGFloat) -> some View {
        let mid = s.width / 2
        let (at, d): (CGPoint, CGFloat) =
            if card != nil {
                (CGPoint(x: mid, y: s.height - 6 * k - (cardHeight > 0 ? cardHeight : 136 * ky) - 36 * k), 144 * k)          // her eyes just over the card's top edge
            } else if phase != .idle {
                (CGPoint(x: mid, y: 98 * ky), 141 * k)
            } else if page != .home {
                (CGPoint(x: s.width - 34 * k, y: 30 * ky), 42 * k)          // the corner the page's toolbar keeps free (the system then centres the time)
            } else {
                (CGPoint(x: mid, y: 132 * ky), 170 * k)
            }
        let b = Button { tapHer(card) } label: {
            HerSprite(face: face(card), size: d, paused: aod).frame(width: max(d, 44), height: max(d, 44)).contentShape(Circle())
        }
        .buttonStyle(.plain)
        return Group { if phase == .idle && card == nil && page == .home { b.handGestureShortcut(.primaryAction) } else { b } }          // otherwise a card's first button, or the talk's own screen, answers the double tap
            .opacity(aod ? 0.45 : 1)
            .position(at)
    }

    private func face(_ card: WatchCard?) -> HerFace {
        if aod { return .doze }
        switch phase {
        case .listening: return .listen
        case .thinking: return .think
        case .answer: return .talk
        case .failed: return .rest
        case .idle: break
        }
        if card != nil { return .ask }
        if page != .home { return .talk }
        return waits ? .ask : .rest
    }

    private func tapHer(_ card: WatchCard?) {
        switch phase {
        case .idle:
            if let card { dismiss(card) } else if page != .home { page = .home } else { model.startTalk() }
        case .listening: model.endTalk()
        case .thinking: model.cancelTalk()
        case .answer, .failed: model.talk = .idle
        }
    }

    // MARK: the talk and the cards

    /// his words under her as she hears them, dimmed while she thinks; or why the talk failed. A tap ends the listening, drops the question, or puts the failure away.
    @ViewBuilder private func captions(ky: CGFloat) -> some View {
        switch model.talk {
        case .listening(let h):
            WatchCaptions(text: h.isEmpty ? Words.Watch.listening : h, tint: h.isEmpty ? WatchTheme.muted : WatchTheme.ink, top: 186 * ky) { model.endTalk() }
        case .thinking(let h):
            WatchCaptions(text: h.isEmpty ? Words.Watch.thinking : h, tint: WatchTheme.faint, top: 186 * ky) { model.cancelTalk() }
        case .failed(let reason):
            WatchCaptions(text: reason, tint: WatchTheme.ink, top: 186 * ky) { model.talk = .idle }
        default: EmptyView()
        }
    }

    private static func key(_ c: WatchCard) -> String {
        switch c {
        case .answer: "answer"
        case .waiting(let w): "waiting." + w.id
        case .leave: "leave"
        }
    }

    /// a waiting item opened from 今天要办的: its card over her
    private func openCard(_ id: String) {
        queue.removeAll { $0 == id }
        queue.insert(id, at: 0)
    }

    private func send(_ id: String, _ action: String) {
        model.answer(id, action)
        withAnimation(Self.spring) {
            _ = answered.insert(id)
            queue.removeAll { $0 == id }
        }
    }

    private func answer(_ card: WatchCard, _ id: String, _ action: String) {
        send(id, action)
        if case .answer = card { model.talk = .idle }
    }

    private func dismiss(_ card: WatchCard) {
        switch card {
        case .answer: model.talk = .idle
        case .waiting(let w): queue.removeAll { $0 == w.id }
        case .leave(let t): leaveDismissed = t
        }
    }
}
