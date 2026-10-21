import SwiftUI
import WatchKit

/// The watch app: one vertical scroll (home, then the list), the screens it opens, and the talk laid over all of it.
/// Everything grows out of her and goes back into her: 0.3 s, no bounce.
struct WatchRoot: View {
    static let space = "watch-root"
    private static let grow = Animation.smooth(duration: 0.3)

    enum Route: Hashable { case waiting(String), ride }
    private enum Phase { case idle, listening, thinking, answer, failed }

    private let model = WatchModel.shared
    @State private var path: [Route]
    @State private var told: Int?
    @State private var answered: Set<String> = []
    @State private var scroll: CGFloat = 0
    @State private var herHome: CGRect = .zero
    @State private var size: CGSize = .zero
    private let scrollToList: Bool

    init(path: [Route] = [], scrollToList: Bool = false) {
        _path = State(initialValue: path)
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
            if phase != .idle {
                talk.background(Color.black.ignoresSafeArea()).transition(.scale(scale: 0.4).combined(with: .opacity))
            }
        }
        .animation(Self.grow, value: phase)
        .onChange(of: model.day) { old, new in
            answered = []
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
                    WatchHomePage(day: model.day, told: $told, openRide: { path.append(.ride) }, herPlace: { herHome = $0 })
                        .frame(height: WKInterfaceDevice.current().screenBounds.height)
                    if let day = model.day {
                        WatchList(day: day, answered: answered, open: { path.append($0) }, answer: send)
                            .padding(.horizontal, 2).id("list")
                    }
                }
            }
            .onScrollGeometryChange(for: CGFloat.self) { max(0, $0.contentOffset.y + $0.contentInsets.top) } action: { _, y in scroll = y }
            .onAppear { if scrollToList { Task { try? await Task.sleep(for: .seconds(0.4)); withAnimation(Self.grow) { proxy.scrollTo("list", anchor: .top) } } } }
        }
        .overlay { her }
        .ignoresSafeArea()
        .coordinateSpace(name: Self.space)
        .onGeometryChange(for: CGSize.self) { $0.size } action: { size = $0 }
        .task(id: told) { if told != nil { try? await Task.sleep(for: .seconds(4)); if !Task.isCancelled { withAnimation(Self.grow) { told = nil } } } }
        .animation(Self.grow, value: told)
        .toolbar(.hidden, for: .navigationBar)
    }

    /// She: the ball in the middle of the ring; as the list scrolls up she shrinks into the top-right corner, and grows back at the top.
    private var her: some View {
        let p = min(1, scroll / 90)
        let corner = CGPoint(x: size.width - 22, y: 14)
        let at = CGPoint(x: herHome.midX + (corner.x - herHome.midX) * p, y: herHome.midY + (corner.y - herHome.midY) * p)
        let d = 80 + (28 - 80) * p
        return Button { model.startTalk() } label: {
            HerSprite(face: aodFace, size: d, paused: aodFace == .doze).frame(width: max(d, 44), height: max(d, 44)).contentShape(Circle())
        }
        .buttonStyle(.plain)
        .handGestureShortcut(.primaryAction)
        .position(at)
        .opacity(herHome == .zero ? 0 : 1)
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

    private func send(_ id: String, _ action: String) {
        model.answer(id, action)
        withAnimation(Self.grow) { _ = answered.insert(id) }
    }

    @ViewBuilder private var talk: some View {
        switch model.talk {
        case .idle: EmptyView()
        case .listening(let h): WatchTalkView(face: .listen, heard: h, placeholder: Words.Watch.listening) { model.endTalk() }
        case .thinking(let h): WatchTalkView(face: .think, heard: h, placeholder: Words.Watch.thinking) { model.cancelTalk() }
        case .answer(let spoken, let written, let card):
            WatchAnswerView(spoken: spoken, written: written, card: card, answer: { id, action in
                model.answer(id, action)
                model.talk = .idle
            }, dismiss: { model.talk = .idle })
        case .failed(let reason): WatchFailedView(reason: reason) { model.talk = .idle }
        }
    }
}
