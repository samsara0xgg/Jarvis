import SwiftUI
import WatchKit

/// The watch app: one vertical scroll (home, then the list), the screens it opens, and the talk laid over all of it.
/// Everything grows out of her and goes back into her: 0.3 s, no bounce. She is one view over all of it, which moves and resizes from the ring to the corner, to the middle of a
/// talk and to the corner of her answer.
struct WatchRoot: View {
    private static let grow = Animation.smooth(duration: 0.3)
    private static let cornerAt: CGFloat = 90          // points of scroll for her to travel from the middle to the corner

    enum Route: Hashable { case waiting(String), ride }
    private enum Phase { case idle, listening, thinking, answer, failed }

    private let model = WatchModel.shared
    @State private var path: [Route] = []
    @State private var told: Int?
    @State private var answered: Set<String> = []
    @State private var scroll: CGFloat = 0
    /// scrolled far enough that she has reached the corner: from here the system's own corner item carries her (it also moves the time out of her way)
    @State private var inList = false
    @State private var herHome: CGRect = .zero
    private let scrollToList: Bool
    /// a screen to push once home is up (the debug scenes): a stack that starts with a path is blank until it finds its destinations
    private let opening: Route?
    @State private var opened = false

    init(opening: Route? = nil, scrollToList: Bool = false) {
        self.opening = opening
        self.scrollToList = scrollToList
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

    var body: some View {
        ZStack {
            NavigationStack(path: $path) {
                home
                    .navigationDestination(for: Route.self) { route in
                        switch route {
                        case .waiting(let id): waitingPage(id)
                        case .ride: if let r = model.day?.ride { WatchRideView(ride: r) }
                        }
                    }
            }
            // one layer for the whole talk: it grows in once and goes once, and only its content changes from step to step (a transition on the switch itself would take the
            // layer out and back at every step, showing the home between)
            if phase != .idle {
                ZStack { Color.black.ignoresSafeArea(); talk }.transition(.opacity)          // a fade: she is what grows, not a square over the round ring
            }
            ZStack { her }.ignoresSafeArea()          // she is placed in screen coordinates, the ones her place is measured in (`.global`)
        }
        .animation(Self.grow, value: phase)
        .onChange(of: model.day) { old, new in
            answered = []
            path.removeAll { !Self.exists($0, in: new) }                 // answered on the phone or ended while its screen was open: back to the list, not a blank page
            if let old, let new {
                let had = Set(old.waiting.map(\.id))
                if new.waiting.contains(where: { !had.contains($0.id) }) { WatchHaptics.play(.waits) }
            }
        }
        .onChange(of: model.day?.ride?.alightNext) { old, new in if new == true, old != true { WatchHaptics.play(.alight) } }
        .onChange(of: phase) { _, new in if new == .listening { WatchHaptics.play(.listening) } }
        .task(id: model.day?.leaveAt) {
            guard let t = model.day?.leaveAt, t > .now else { return }
            try? await Task.sleep(for: .seconds(t.timeIntervalSinceNow))
            if !Task.isCancelled { WatchHaptics.play(.leave) }
        }
        .onOpenURL { if $0.scheme == "jarvis-watch", $0.host == "talk" { model.startTalk() } }
    }

    // MARK: home and the list

    private var home: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: 0) {
                    WatchHomePage(day: model.day, aod: aod, quiet: phase != .idle, told: $told, openRide: { path.append(.ride) }, herPlace: { herHome = $0 })
                        .frame(height: WKInterfaceDevice.current().screenBounds.height)
                    if let day = model.day {
                        WatchList(day: day, answered: answered, open: { path.append($0) }, answer: send)
                            .padding(.horizontal, 2).id("list")
                    }
                }
            }
            .onScrollGeometryChange(for: CGFloat.self) { max(0, $0.contentOffset.y + $0.contentInsets.top) } action: { _, y in scroll = y; inList = y >= Self.cornerAt }
            .onAppear {
                if let opening, !opened { opened = true; Task { try? await Task.sleep(for: .seconds(0.4)); path = [opening] } }
                if scrollToList { Task { try? await Task.sleep(for: .seconds(0.4)); withAnimation(Self.grow) { proxy.scrollTo("list", anchor: .top) } } }
            }
        }
        .ignoresSafeArea()
        .task(id: told) { if told != nil { try? await Task.sleep(for: .seconds(4)); if !Task.isCancelled { withAnimation(Self.grow) { told = nil } } } }
        .animation(Self.grow, value: told)
        .toolbar {
            if inList && phase == .idle { WatchCornerHer(face: .rest, primary: true) }
            else if phase != .idle { ToolbarItem(placement: .topBarTrailing) { Color.clear.frame(width: 28, height: 28) } }          // her answer puts her in this corner: the time keeps out of her way from the talk's start, so the bar does not change under the answer's move
        }
        .toolbar(inList || phase != .idle ? .visible : .hidden, for: .navigationBar)
    }

    /// She: the ball in the middle of the ring; as the list scrolls up she shrinks into the top-right corner, and grows back at the top. In a talk she goes to its middle, and with
    /// her answer to the top-right corner. At rest in the list or on a pushed screen the system's corner item carries her, and this one is not shown.
    private var her: some View {
        let screen = WKInterfaceDevice.current().screenBounds.size
        let corner = CGPoint(x: screen.width - 32.5, y: 35)          // where the system puts a 28 pt top-trailing item
        let middle = CGPoint(x: screen.width / 2, y: screen.height / 2 - WatchTalkView.herLift)
        let p = path.isEmpty ? min(1, scroll / Self.cornerAt) : 1
        let (at, d, face): (CGPoint, CGFloat, HerFace) = switch phase {
        case .idle: (CGPoint(x: herHome.midX + (corner.x - herHome.midX) * p, y: herHome.midY + (corner.y - herHome.midY) * p), 80 + (28 - 80) * p, aodFace)
        case .listening: (middle, 80, .listen)
        case .thinking: (middle, 80, .think)
        case .answer: (corner, 28, .talk)
        case .failed: (middle, 80, .rest)
        }
        let shown = phase != .idle || (!inList && path.isEmpty && herHome != .zero)
        let b = Button {
            switch phase {
            case .idle, .answer: model.startTalk()
            case .listening: model.endTalk()
            case .thinking: model.cancelTalk()
            case .failed: model.talk = .idle
            }
        } label: {
            HerSprite(face: face, size: d, paused: face == .doze).frame(width: max(d, 44), height: max(d, 44)).contentShape(Circle())
        }
        .buttonStyle(.plain)
        return Group { if phase == .idle { b.handGestureShortcut(.primaryAction) } else { b } }          // in a talk its own screen answers the double tap
            .position(at)
            .opacity(shown ? 1 : 0).allowsHitTesting(shown)
    }

    @Environment(\.isLuminanceReduced) private var aod
    private var aodFace: HerFace { aod ? .doze : .rest }

    // MARK: screens

    @ViewBuilder private func waitingPage(_ id: String) -> some View {
        let items = (model.day?.waiting ?? []).filter { !answered.contains($0.id) }
        if let i = items.firstIndex(where: { $0.id == id }) {
            WatchWaitingView(item: items[i], index: i + 1, count: items.count) { action in
                send(id, action)
                path.removeAll()
            }
        }
    }

    /// whether the screen of a route still has what it shows in the day the phone sent
    private static func exists(_ r: Route, in day: WatchDay?) -> Bool {
        switch r {
        case .waiting(let id): day?.waiting.contains { $0.id == id } ?? false
        case .ride: day?.ride != nil
        }
    }

    private func send(_ id: String, _ action: String) {
        model.answer(id, action)
        withAnimation(Self.grow) { _ = answered.insert(id) }
    }

    @ViewBuilder private var talk: some View {
        switch model.talk {
        case .idle: EmptyView()
        case .listening(let h), .thinking(let h):           // one view for both: his words stay where they are while she goes from listening to thinking
            let listening = phase == .listening
            WatchTalkView(heard: h, placeholder: listening ? Words.Watch.listening : Words.Watch.thinking) {
                if listening { model.endTalk() } else { model.cancelTalk() }
            }
        case .answer(let spoken, let written, let card):
            WatchAnswerView(spoken: spoken, written: written, card: card, answer: { id, action in
                model.answer(id, action)
                model.talk = .idle
            }, dismiss: { model.talk = .idle })
            .transition(.asymmetric(insertion: .opacity.animation(.easeOut(duration: 0.2).delay(0.25)), removal: .opacity))          // her words come once she has reached her corner, not under her on her way
        case .failed(let reason): WatchFailedView(reason: reason) { model.talk = .idle }
        }
    }
}
