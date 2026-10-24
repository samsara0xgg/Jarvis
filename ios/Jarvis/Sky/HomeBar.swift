import SwiftUI

// The home's pages in one glass bar at the foot of the screen (yilun, 2026-10-21: every page goes there, and the top row's right end is hers): the day, the conversation with her
// (the history, which comes up over the day), the map of where he went, and settings. A tap on a tab changes the page, and so does a finger slid along the bar, as the system's own
// tab bar does: the light follows the finger with a tick at each tab it reaches, and the page is the one it lets go on; a tap on the day while it is on show goes back to now. The bar
// floats over the page, which runs on under it. On the conversation it stays, with the field he types in over it, and gives way only to the keyboard and what he picks to send;
// on the other pages it gives way while the field is out. It goes too while a place is being named or the near view tuned. Under the dial there is no bar: settings is a sheet from
// its button there. Design points, as the chrome (Sky/StarChartScreen.swift). On iOS 26 the bar is the system's own (`HomeStockTabs`; allen 2026-10-23: the lens of the
// Muse video he sent is the stock one), and `HomeTabBar` is the one drawn before iOS 26.

/// the home's pages, in the bar's order
enum HomePage: CaseIterable {
    case day, talk, map, settings

    /// the outline symbol, chosen or not (the footprint build's bar: no .fill)
    var symbol: String {
        switch self {
        case .day: "calendar.day.timeline.left"
        case .talk: "text.bubble"
        case .map: "map"
        case .settings: "gearshape"
        }
    }

    @MainActor var title: String {
        switch self {
        case .day: Words.Home.pageDay
        case .talk: Words.Home.pageTalk
        case .map: Words.Home.pageMap
        case .settings: Words.Home.pageSettings
        }
    }
}

enum HomeBar {
    static let side = 30.0                                   // in from each side
    /// how tall it is, and how far up from the foot of the screen: the drawn bar's 62 and 21 design points; on iOS 26 where a UITabBarController puts the system's own glass (62 pt
    /// tall, 21 pt over the screen's foot and in from its sides: measured on iPhone 17 Pro, iOS 26.4), in design points
    static var h: Double { stock ? 62 / scale : 62 }
    static var foot: Double { stock ? 21 / scale : 21 }
    private static var stock: Bool { if #available(iOS 26.0, *) { true } else { false } }
    private static var scale: Double { StarChartScreen.fit(UIScreen.main.bounds.size).k }
    static let inset = 4.0                                   // the light round the tab it is on, inside the bar
    static var top: Double { H - foot - h }
    /// how far the field rests over its place on the conversation's page: 8 pt over the bar
    static var fieldLift: Double { h + foot + 8 - StarUI.restGap }
    static var width: Double { W - 2 * side }
    /// how wide a tab is
    static var slot: Double { (width - 2 * inset) / Double(HomePage.allCases.count) }

    /// the tab under a finger `x` pt from the bar's left edge (past either end, the end's)
    static func page(at x: Double) -> HomePage {
        let all = HomePage.allCases
        return all[min(max(Int(((x - inset) / slot).rounded(.down)), 0), all.count - 1)]
    }

    /// where the light's left edge is, from the bar's: under its tab, or centred under a finger on the bar and kept inside it
    static func light(on page: HomePage, finger: Double?) -> Double {
        let all = HomePage.allCases, last = inset + Double(all.count - 1) * slot
        guard let x = finger else { return inset + Double(all.firstIndex(of: page) ?? 0) * slot }
        return min(max(x - slot / 2, inset), last)
    }
}

extension StarEngine {
    /// The home's page `p`: the history comes up for the conversation and goes for any other page, which the map's and settings' flags name. Quietly: a tab is no pull of the panel.
    func showPage(_ p: HomePage) {
        if p == .talk { openPanel(quiet: true) } else { closePanel(quiet: true) }
        ui.footprintOpen = p == .map
        ui.settingsOpen = p == .settings
    }

    /// A tab chosen: its page, and the day's tab on the day goes back to now, as a tab bar goes back to the top.
    func goPage(_ p: HomePage) {
        if p == .day && ui.page == .day { ui.tl.goToday(); return }
        showPage(p)
    }
}

extension StarUI {
    /// The bar at the foot is out: on the timeline only, not while a place is being named or the near view tuned; on the conversation not under the keyboard or what he picks to send.
    var barShows: Bool {
        guard HomeModule.shared.isTimeline, tl.renaming == nil, !tuning else { return false }
        return panelOpen ? !typing && !attach.isOpen : !composerOut
    }
}

struct HomeTabBar: View {
    let engine: StarEngine
    @State private var finger: Double?          // a finger on the bar, from its left edge
    @State private var under: HomePage?         // the tab that finger is on, for the tick when it reaches another
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var ui: StarUI { engine.ui }

    private var shows: Bool { ui.barShows }

    var body: some View {
        let lit = finger.map { HomeBar.page(at: $0) } ?? ui.page
        ZStack(alignment: .topLeading) {
            Capsule().fill(Color.white.opacity(finger == nil ? 0.1 : 0.16))
                .frame(width: HomeBar.slot, height: HomeBar.h - 2 * HomeBar.inset)
                .scaleEffect(finger == nil || reduce ? 1 : 1.08)
                .offset(x: HomeBar.light(on: ui.page, finger: finger), y: HomeBar.inset)
                .animation(follow, value: finger)
                .animation(reduce ? nil : Animation.spring(response: 0.35, dampingFraction: 0.78), value: ui.page)
            HStack(spacing: 0) {
                ForEach(HomePage.allCases, id: \.self) { p in tab(p, lit: p == lit) }
            }
            .padding(HomeBar.inset)
        }
        .frame(width: HomeBar.width, height: HomeBar.h, alignment: .topLeading)
        .modifier(BarGlass())
        .contentShape(Capsule())
        .gesture(slide)
        .accessibilityElement(children: .contain)
        .opacity(shows ? 1 : 0).offset(y: shows || reduce ? 0 : 16).allowsHitTesting(shows).accessibilityHidden(!shows)
        .animation(.easeOut(duration: shows ? 0.25 : 0.2), value: shows)
        .offset(x: HomeBar.side, y: HomeBar.top)
        .frame(width: W, height: H, alignment: .topLeading)
    }

    /// the light on its way: after the finger closely while it is down, and to its tab on a spring when it lets go
    private var follow: Animation? {
        if reduce { return nil }
        return finger == nil ? Animation.spring(response: 0.35, dampingFraction: 0.78) : Animation.interactiveSpring(response: 0.18, dampingFraction: 0.86)
    }

    private func tab(_ p: HomePage, lit: Bool) -> some View {
        let recording = p == .settings && LifelogRecorder.shared.recording          // the day is being recorded; its switch is in settings
        return VStack(spacing: 3) {
            Image(systemName: p.symbol).font(.system(size: 19, weight: .medium))
                .frame(height: 24)
                .overlay(alignment: .topTrailing) {
                    if recording { Circle().fill(Color(red: 1, green: 92 / 255, blue: 92 / 255)).frame(width: 6, height: 6).offset(x: 4, y: 1) }
                }
            Text(p.title).font(.system(size: 10.5, weight: .semibold)).lineLimit(1)
        }
        .foregroundStyle(lit ? SetColor.ink : SetColor.muted)
        .animation(.easeOut(duration: 0.15), value: lit)
        .frame(width: HomeBar.slot, height: HomeBar.h - 2 * HomeBar.inset)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(recording ? Words.Home.pageSettingsRecording : p.title)
        .accessibilityAddTraits(p == ui.page ? [.isButton, .isSelected] : [.isButton])
        .accessibilityAction { go(p) }
    }

    /// down on the bar the light comes to the finger and follows it; let go, the page is the tab it is on
    private var slide: some Gesture {
        DragGesture(minimumDistance: 0)
            .onChanged { v in
                let at = HomeBar.page(at: v.location.x)
                if let u = under, u != at { Haptic.selection() }
                under = at
                finger = v.location.x
            }
            .onEnded { v in
                go(HomeBar.page(at: v.location.x))
                finger = nil
                under = nil
            }
    }

    private func go(_ p: HomePage) { engine.goPage(p) }
}

/// the bar's glass: the system's Liquid Glass where there is one (iOS 26), else a frosted material with a hairline
private struct BarGlass: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 26.0, *) {
            content.glassEffect(.regular.interactive(), in: Capsule())
        } else {
            content.background(.ultraThinMaterial, in: Capsule())
                .overlay(Capsule().strokeBorder(Color.white.opacity(0.12), lineWidth: 0.5))
        }
    }
}

/// The system's own tab bar (iOS 26 Liquid Glass: press and drag lifts the chosen cell, it follows the finger and magnifies what is under it, and springs into the nearest tab), as the same four
/// pages: a bare UITabBar, not a tab controller, so nothing of it lies over the page but the bar itself. The pages stay the engine's flags (`StarEngine.goPage`).
struct HomeStockTabs: UIViewRepresentable {
    let engine: StarEngine

    func makeCoordinator() -> Coordinator { Coordinator(engine: engine) }

    func makeUIView(context: Context) -> UITabBar {
        let bar = UITabBar()
        bar.items = HomePage.allCases.enumerated().map { i, p in
            let img = UIImage(systemName: p.symbol)          // the outline symbol, chosen or not (no .fill: the footprint build's bar)
            return UITabBarItem(title: p.title, image: img, selectedImage: img).with { $0.tag = i }
        }
        bar.tintColor = .white
        bar.unselectedItemTintColor = UIColor.white.withAlphaComponent(0.6)
        bar.delegate = context.coordinator
        return bar
    }

    func updateUIView(_ bar: UITabBar, context: Context) {
        let i = HomePage.allCases.firstIndex(of: engine.ui.page) ?? 0
        if bar.selectedItem?.tag != i { bar.selectedItem = bar.items?[i] }
        let recording = LifelogRecorder.shared.recording          // the day is being recorded: a small red dot on settings
        bar.items?.last?.badgeValue = recording ? "" : nil
        bar.items?.last?.badgeColor = UIColor(Look.red)
    }

    final class Coordinator: NSObject, UITabBarDelegate {
        let engine: StarEngine
        init(engine: StarEngine) { self.engine = engine }
        func tabBar(_ tabBar: UITabBar, didSelect item: UITabBarItem) {
            MainActor.assumeIsolated { engine.goPage(HomePage.allCases[item.tag]) }
        }
    }
}

private extension UITabBarItem {
    func with(_ f: (UITabBarItem) -> Void) -> UITabBarItem { f(self); return self }
}
