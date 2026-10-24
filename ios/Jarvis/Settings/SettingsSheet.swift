// words: legacy
import SwiftUI

/// 设置: content for a standard iOS page sheet. Present it with `.sheet(isPresented:) { SettingsSheet() }`.
/// It draws its own header (grabber, title, 完成 or back) and pushes the two sub pages over the list. `asPage`: it is the phone home's settings page, one of the bar at the foot
/// (Sky/HomeBar.swift): no grabber, no 完成, no header. Her sky is bare above the list, which starts under the line under her (how the brain stands; on a sub page its title, with the way back
/// at the left) and scrolls under a narrow soft edge.
struct SettingsSheet: View {
    private let asPage: Bool
    private let tuneNear: () -> Void
    private let cardUp: Bool                    // her card is up under her (Sky/DayTimelineTalk.swift): the line under her gives way to it
    @Bindable private var model = SettingsModel.shared
    @State private var page = SettingsPage.main
    @State private var scrolled: [SettingsPage: Bool] = [:]
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(asPage: Bool = false, cardUp: Bool = false, tuneNear: @escaping () -> Void = {}) {
        self.asPage = asPage
        self.cardUp = cardUp
        self.tuneNear = tuneNear
        #if DEBUG
        // `-settingsPage pair` opens on the pairing page, for screenshots
        let a = ProcessInfo.processInfo.arguments
        if let i = a.firstIndex(of: "-settingsPage"), i + 1 < a.count, a[i + 1] == "pair" { _page = State(initialValue: .pair) }
        #endif
    }

    private static let headerHeight = 56.0
    /// as a page: where the list starts in the chrome's design points, and how tall its soft top edge is
    private static let pageTop = 146.0, edge = 14.0

    var body: some View {
        GeometryReader { geo in
            let fit = StarChartScreen.fit(geo.size)
            ZStack(alignment: .top) {
                if !asPage { SetColor.bg.ignoresSafeArea() }
                scrollPage(.main, size: geo.size, fit: fit) {
                    SettingsMainPage(model: model, isCurrent: page == .main, asPage: asPage, tuneNear: tuneNear) { go($0) }
                }
                scrollPage(.pair, size: geo.size, fit: fit) { SettingsPairPage(isCurrent: page == .pair) { go(.main) } }
                scrollPage(.log, size: geo.size, fit: fit) { SettingsLogPage(model: model) }
                if asPage { foot; strip.modifier(InDesign(fit: fit)).frame(width: geo.size.width, height: geo.size.height, alignment: .topLeading) } else { header }
            }
        }
        .foregroundStyle(SetColor.ink)
        .tint(SetColor.link)
        .presentationBackground(SetColor.bg)
        .presentationCornerRadius(14)
        .presentationDragIndicator(.hidden)
        .preferredColorScheme(.dark)
    }

    private func go(_ next: SettingsPage) {
        withAnimation(reduceMotion ? .easeInOut(duration: 0.25) : SetMotion.easeOut(0.5)) { page = next }
    }

    // MARK: pages

    /// One scrolling page. The list slides back 30% and dims while a sub page is pushed over it.
    private func scrollPage<Content: View>(_ p: SettingsPage, size: CGSize, fit: DesignFit, @ViewBuilder content: () -> Content) -> some View {
        let current = page == p, width = size.width
        return ScrollView { content() }
            .scrollIndicators(.hidden)
            .contentMargins(.bottom, asPage ? size.height - (fit.oy + HomeBar.top * fit.k) + 8 : 0, for: .scrollContent)          // as a page, its end comes up clear of the bar at the foot
            .safeAreaInset(edge: .top, spacing: 0) { Color.clear.frame(height: asPage ? 0 : Self.headerHeight) }
            .onScrollGeometryChange(for: Bool.self) { $0.contentOffset.y + $0.contentInsets.top > 2 } action: { _, isScrolled in
                scrolled[p] = isScrolled
            }
            .background(asPage ? Look.sky : SetColor.bg)
            .mask(alignment: .top) {          // as a page, a narrow soft edge at the top of the list
                VStack(spacing: 0) {
                    LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: asPage ? Self.edge * fit.k : 0)
                    Color.black
                }
            }
            .padding(.top, asPage ? fit.oy + Self.pageTop * fit.k : 0)          // above it the sky is bare: her, and a touch on her, are not covered
            .shadow(color: .black.opacity(p == .main ? 0 : 0.35), radius: 16, x: -14)
            .offset(x: reduceMotion || current ? 0 : (p == .main ? -0.3 * width : width))
            .opacity(current ? 1 : (reduceMotion ? 0 : (p == .main ? 0.35 : 1)))
            .allowsHitTesting(current)
            .accessibilityHidden(!current)
    }

    /// As a page, the list fades into the page colour as it goes into the bar at the foot, as the home's list does (TLLayout.listBottom): no line shows round the glass's ends or under it.
    private var foot: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 0)
            LinearGradient(colors: [Look.sky.opacity(0), Look.sky], startPoint: .top, endPoint: .bottom).frame(height: TLLayout.fade)
            Look.sky.frame(height: H - TLLayout.listBottom - TLLayout.fade)
        }
        .ignoresSafeArea(edges: .bottom)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }

    // MARK: the strip (as a page)

    /// The strip of the page (Sky/HomeTop.swift): no buttons on the list; on a sub page the way back at the left and its title in the line under her
    private var strip: some View {
        TopStrip {
            if page != .main { TopGlassButton(symbol: "chevron.left", label: Words.PhoneFrame.backToSettings) { go(.main) } }
        } middle: {
            ZStack {
                BrainStatusLine().opacity(page == .main && !cardUp ? 1 : 0).animation(.easeOut(duration: 0.2), value: cardUp)
                Text(page.title).font(.system(size: Look.note, weight: .semibold)).foregroundStyle(Look.ink2).lineLimit(1)
                    .opacity(page == .main ? 0 : 1).accessibilityAddTraits(.isHeader).accessibilityHidden(page == .main)
            }
            .animation(.easeOut(duration: 0.2), value: page)
        } right: {
            EmptyView()
        }
    }

    // MARK: header

    private var header: some View {
        ZStack(alignment: .top) {
            // blur only once content scrolls under it; at rest the header is the page colour (as a page, up under the status bar too)
            Rectangle().fill(.ultraThinMaterial).opacity(scrolled[page] == true ? 1 : 0).ignoresSafeArea(edges: .top)
            SetColor.color(14, 15, 30, 0.72).ignoresSafeArea(edges: .top)
            if !asPage { Capsule().fill(SetColor.color(235, 235, 245, 0.26)).frame(width: 36, height: 5).padding(.top, 6) }
            ZStack {
                ForEach([SettingsPage.main, .pair, .log], id: \.self) { p in
                    Text(p.title).font(.headline).lineLimit(1)
                        .padding(.horizontal, 80)
                        .opacity(page == p ? 1 : 0)
                        .animation(page == p ? .easeInOut(duration: 0.3).delay(0.1) : .easeInOut(duration: 0.15), value: page)
                        .accessibilityAddTraits(.isHeader)
                        .accessibilityHidden(page != p)
                }
                HStack(spacing: 0) {
                    Button { go(.main) } label: {
                        HStack(spacing: 4) {
                            Polyline.chevronLeft
                                .stroke(style: StrokeStyle(lineWidth: 2.6, lineCap: .round, lineJoin: .round))
                                .frame(width: 12, height: 20)
                            Text("设置")
                        }
                        .padding(.leading, 8).padding(.trailing, 16)
                        .frame(height: 44)
                        .contentShape(Rectangle())
                    }
                    .shown(page != .main)
                    .accessibilityLabel("返回设置")
                    Spacer(minLength: 0)
                    Button { dismiss() } label: {
                        Text("完成").fontWeight(.semibold).padding(.horizontal, 16).frame(height: 44).contentShape(Rectangle())
                    }
                    .shown(page == .main && !asPage)
                }
                .font(.body)
                .buttonStyle(NavButtonStyle())
            }
            .frame(height: 44)
            .padding(.top, 12)
        }
        .frame(height: Self.headerHeight)
        .overlay(alignment: .bottom) { Rectangle().fill(SetColor.line).frame(height: 0.5).opacity(scrolled[page] == true ? 1 : 0) }
        .animation(.easeInOut(duration: 0.2), value: scrolled[page])
    }
}

private struct NavButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(configuration.isPressed ? SetColor.link.opacity(0.45) : SetColor.link)
            .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}

private extension View {
    /// Fades a header control in (.3 s after a .1 s wait) or out (.15 s), and takes it out of reach while hidden.
    func shown(_ on: Bool) -> some View {
        opacity(on ? 1 : 0)
            .animation(on ? .easeInOut(duration: 0.3).delay(0.1) : .easeInOut(duration: 0.15), value: on)
            .allowsHitTesting(on)
            .accessibilityHidden(!on)
    }
}
