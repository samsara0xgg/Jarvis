import SwiftUI
import CoreText

@main
struct JarvisApp: App {
    @UIApplicationDelegateAdaptor(ReportAppDelegate.self) private var reportDelegate
    @Environment(\.scenePhase) private var phase
    @State private var wasBackground = false

    init() {
        for n in ["InstrumentSerif-Regular", "IBMPlexMono-Regular", "IBMPlexMono-Medium"] {
            if let u = Bundle.main.url(forResource: n, withExtension: "ttf") { CTFontManagerRegisterFontsForURL(u as CFURL, .process, nil) }
        }
        #if DEBUG
        BrainLink.shared.pairFromLaunchArgument()
        #endif
    }
    var body: some Scene {
        WindowGroup { StarChartScreen().voiceEntry() }
            .onChange(of: phase) { _, p in
                // the brain's days are asked for again when the app comes back from the background (not on a timer)
                if p == .background { wasBackground = true }
                else if p == .active && wasBackground { wasBackground = false; BrainLink.shared.opened() }
            }
    }
}
