import SwiftUI

// 设置 on the iPad (跨端UI标准 §2, §5, §11; out-set.jpg): a page of the bar like the others, not a sheet. Under her the line of how the brain stands; in a wide window the groups of the
// phone's list at the left and the chosen one's rows at the right, a page it pushes (重新配对, the record sent to the brain) taking the right's place with its way back; narrower, the
// phone's whole list in one column at the window's width. The rows and the pushed pages are the phone's (Settings/SettingsPages.swift); this file only places them.

struct PadSettings: View {
    let engine: StarEngine
    let width: Double
    let height: Double                  // the window's, to its foot under the bar
    let top: Double                     // the status bar's height
    let wide: Bool
    @Bindable private var model = SettingsModel.shared
    @State private var group = SettingsGroup.talk          // the mock's
    @State private var page = SettingsPage.main
    @Environment(\.accessibilityReduceMotion) private var reduce

    private var ui: StarUI { engine.ui }
    /// where the groups and the rows begin under her line (the mock's 136 with a status bar of 24)
    private var listTop: Double { top + 112 }
    private static let columnW = 380.0

    var body: some View {
        ZStack(alignment: .topLeading) {
            if wide {
                groups.frame(width: Self.columnW).padding(.top, listTop)
                Rectangle().fill(SetColor.line).frame(width: 0.5, height: max(0, height - listTop - 96)).offset(x: Self.columnW + 12, y: listTop)
                pane.frame(width: width - Self.columnW - 8 - 24).offset(x: Self.columnW + 8)
            } else {
                pane.frame(width: min(width, 680)).frame(width: width)          // the phone's list at the window's width, kept to a readable measure
            }
            BrainStatusLine().frame(width: width).padding(.top, top + 72)
                .opacity(ui.herCardOn ? 0 : 1).animation(.easeOut(duration: 0.2), value: ui.herCardOn)          // her card stands in its place
                .allowsHitTesting(false)
        }
        .frame(width: width, height: height, alignment: .topLeading)
        .foregroundStyle(SetColor.ink)
        .tint(SetColor.link)
        #if DEBUG
        .onAppear { if UserDefaults.standard.string(forKey: "settingsPage") == "pair" { group = .brain; page = .pair } }          // `-settingsPage pair`, as the phone's
        #endif
    }

    /// the groups, the chosen one lit, and the version under them
    private var groups: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(SettingsGroup.here, id: \.self) { g in
                let on = g == group && wide
                Button { Haptic.selection(); go(.main); group = g } label: {
                    HStack(spacing: 12) {
                        Image(systemName: g.symbol).font(.system(size: 15)).foregroundStyle(SetColor.ink.opacity(0.85))
                            .frame(width: 30, height: 30).background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
                        Text(g.title).font(.system(size: 16)).foregroundStyle(SetColor.ink)
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, 14).frame(height: 50)
                    .background(RoundedRectangle(cornerRadius: 13).fill(Color.white.opacity(on ? 0.065 : 0))
                        .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.white.opacity(on ? 0.08 : 0), lineWidth: 0.6)))
                    .contentShape(Rectangle())
                }
                .buttonStyle(PressScale())
                .accessibilityAddTraits(on ? .isSelected : [])
            }
            Text(model.version).font(.footnote).foregroundStyle(SetColor.muted).padding(.horizontal, 14).padding(.top, 18)
        }
        .padding(.horizontal, 12)
    }

    /// the chosen group under its name, or a page it pushed under its way back and its title; in one column the whole list
    private var pane: some View {
        ZStack(alignment: .topLeading) {
            scroll(.main) {
                VStack(alignment: .leading, spacing: 0) {
                    if wide { Text(group.title).font(.system(size: 22, weight: .semibold)).padding(.horizontal, 16).accessibilityAddTraits(.isHeader) }
                    SettingsMainPage(model: model, isCurrent: page == .main, asPage: true, group: wide ? group : nil) { go($0) }
                        .padding(.top, wide ? -10 : 0)          // the first section's own 24 pt over it, less under the group's name
                }
                .id(group)
            }
            scroll(.pair) {
                VStack(alignment: .leading, spacing: 0) { back; SettingsPairPage(isCurrent: page == .pair) { go(.main) } }
            }
            scroll(.log) {
                VStack(alignment: .leading, spacing: 0) { back; SettingsLogPage(model: model) }
            }
        }
    }

    /// a pushed page's way back and its title, over it
    private var back: some View {
        HStack(spacing: 12) {
            TopGlassButton(symbol: "chevron.left", label: Words.PhoneFrame.backToSettings) { go(.main) }
            Text(page.title).font(.system(size: 22, weight: .semibold)).lineLimit(1).accessibilityAddTraits(.isHeader)
        }
        .padding(.horizontal, 16)
    }

    /// one page of the pane, scrolling under a narrow soft edge at its top; the pushed one comes in from the right as on the phone
    private func scroll<C: View>(_ p: SettingsPage, @ViewBuilder _ content: () -> C) -> some View {
        let current = page == p
        return ScrollView { content() }
            .scrollIndicators(.hidden)
            .contentMargins(.top, 14, for: .scrollContent)
            .contentMargins(.bottom, PadHome.barRoom, for: .scrollContent)
            .mask(alignment: .top) {
                VStack(spacing: 0) { LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: 14); Color.black }
            }
            .padding(.top, listTop - 14)
            .opacity(current ? 1 : 0)
            .offset(x: reduce || current || p == .main ? 0 : 40)
            .allowsHitTesting(current)
            .accessibilityHidden(!current)
    }

    private func go(_ next: SettingsPage) {
        guard next != page else { return }
        withAnimation(reduce ? .easeInOut(duration: 0.25) : padMotion) { page = next }
    }
}
