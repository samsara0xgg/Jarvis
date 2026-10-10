import SwiftUI

/// 设置: content for a standard iOS page sheet. Present it with `.sheet(isPresented:) { SettingsSheet() }`.
/// It draws its own header (grabber, title, 完成 or back) and pushes the two sub pages over the list.
struct SettingsSheet: View {
    @Bindable private var model = SettingsModel.shared
    @State private var page = SettingsPage.main
    @State private var scrolled: [SettingsPage: Bool] = [:]
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init() {
        #if DEBUG
        // `-settingsPage pair` opens on the pairing page, for screenshots
        let a = ProcessInfo.processInfo.arguments
        if let i = a.firstIndex(of: "-settingsPage"), i + 1 < a.count, a[i + 1] == "pair" { _page = State(initialValue: .pair) }
        #endif
    }

    private static let headerHeight = 56.0

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .top) {
                SetColor.bg.ignoresSafeArea()
                scrollPage(.main, width: geo.size.width) {
                    SettingsMainPage(model: model, isCurrent: page == .main) { go($0) }
                }
                scrollPage(.pair, width: geo.size.width) { SettingsPairPage(isCurrent: page == .pair) { go(.main) } }
                scrollPage(.log, width: geo.size.width) { SettingsLogPage(model: model) }
                header
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
    private func scrollPage<Content: View>(_ p: SettingsPage, width: Double, @ViewBuilder content: () -> Content) -> some View {
        let current = page == p
        return ScrollView { content() }
            .scrollIndicators(.hidden)
            .safeAreaInset(edge: .top, spacing: 0) { Color.clear.frame(height: Self.headerHeight) }
            .onScrollGeometryChange(for: Bool.self) { $0.contentOffset.y + $0.contentInsets.top > 2 } action: { _, isScrolled in
                scrolled[p] = isScrolled
            }
            .background(SetColor.bg)
            .shadow(color: .black.opacity(p == .main ? 0 : 0.35), radius: 16, x: -14)
            .offset(x: reduceMotion || current ? 0 : (p == .main ? -0.3 * width : width))
            .opacity(current ? 1 : (reduceMotion ? 0 : (p == .main ? 0.35 : 1)))
            .allowsHitTesting(current)
            .accessibilityHidden(!current)
    }

    // MARK: header

    private var header: some View {
        ZStack(alignment: .top) {
            // blur only once content scrolls under it; at rest the header is the page colour
            Rectangle().fill(.ultraThinMaterial).opacity(scrolled[page] == true ? 1 : 0)
            SetColor.color(14, 15, 30, 0.72)
            Capsule().fill(SetColor.color(235, 235, 245, 0.26)).frame(width: 36, height: 5).padding(.top, 6)
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
                    .shown(page == .main)
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
