import SwiftUI

// The home's pages in one glass bar at the foot of the screen (yilun, 2026-10-21: the map and settings leave the top row, whose right end is hers): the day, the map of where he went,
// and settings. A tap on a tab changes the page, and so does a finger slid along the bar, as the system's own tab bar does: the light follows the finger with a tick at each tab it
// reaches, and the page is the one it lets go on; a tap on the day while it is on show goes back to now. The bar floats over the page, which runs on under it, and gives way while the field
// he types in is out (with the history, or what he is sending), a place is being named or the near view tuned. Under the dial there is no bar: settings is a sheet from its button
// there. Design points, as the chrome (Sky/StarChartScreen.swift).

/// the home's pages, in the bar's order
enum HomePage: CaseIterable {
    case day, map, settings
}

enum HomeBar {
    static let h = 62.0, side = 30.0, foot = 21.0          // how tall it is, in from each side, and up from the foot of the screen
    static let inset = 4.0                                   // the light round the tab it is on, inside the bar
    static var top: Double { H - foot - h }
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

struct HomeTabBar: View {
    let ui: StarUI
    @State private var finger: Double?          // a finger on the bar, from its left edge
    @State private var under: HomePage?         // the tab that finger is on, for the tick when it reaches another
    @Environment(\.accessibilityReduceMotion) private var reduce

    /// on the timeline only, and not while the field is out, a place is being named or the near view tuned
    private var shows: Bool { HomeModule.shared.isTimeline && !ui.composerOut && ui.tl.renaming == nil && !ui.tuning }

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
            Image(systemName: symbol(p)).font(.system(size: 19, weight: .medium)).symbolVariant(lit ? .fill : .none)
                .frame(height: 24)
                .overlay(alignment: .topTrailing) {
                    if recording { Circle().fill(Color(red: 1, green: 92 / 255, blue: 92 / 255)).frame(width: 6, height: 6).offset(x: 4, y: 1) }
                }
            Text(title(p)).font(.system(size: 10.5, weight: .semibold)).lineLimit(1)
        }
        .foregroundStyle(lit ? SetColor.ink : SetColor.muted)
        .animation(.easeOut(duration: 0.15), value: lit)
        .frame(width: HomeBar.slot, height: HomeBar.h - 2 * HomeBar.inset)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(recording ? Words.Home.pageSettingsRecording : title(p))
        .accessibilityAddTraits(p == ui.page ? [.isButton, .isSelected] : [.isButton])
        .accessibilityAction { go(p) }
    }

    private func symbol(_ p: HomePage) -> String {
        switch p {
        case .day: "calendar.day.timeline.left"
        case .map: "map"
        case .settings: "gearshape"
        }
    }

    private func title(_ p: HomePage) -> String {
        switch p {
        case .day: Words.Home.pageDay
        case .map: Words.Home.pageMap
        case .settings: Words.Home.pageSettings
        }
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

    private func go(_ p: HomePage) {
        if p == .day && ui.page == .day { ui.tl.goToday(); return }          // the day's tab on the day: back to now, as a tab bar goes back to the top
        ui.page = p
    }
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
