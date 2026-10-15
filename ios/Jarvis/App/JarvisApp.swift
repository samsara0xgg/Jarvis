import SwiftUI
import CoreText

@main
struct JarvisApp: App {
    @UIApplicationDelegateAdaptor(ReportAppDelegate.self) private var reportDelegate
    @Environment(\.scenePhase) private var phase
    @State private var wasBackground = false

    init() {
        StallLog.shared.start()
        BrainClient.trace = PairingTrace.note
        for n in ["InstrumentSerif-Regular", "IBMPlexMono-Regular", "IBMPlexMono-Medium"] {
            if let u = Bundle.main.url(forResource: n, withExtension: "ttf") { CTFontManagerRegisterFontsForURL(u as CFURL, .process, nil) }
        }
        #if DEBUG
        BrainLink.shared.pairFromLaunchArgument()
        #endif
    }
    var body: some Scene {
        WindowGroup { StarChartScreen().voiceEntry().task { Waiting.shared.start(); JarvisActivity.shared.start(); RideLiveActivity.shared.showDebug(); LifelogUploader.shared.kick(foreground: true) } }
            .onChange(of: BrainLink.shared.hasToken) { _, paired in
                // frames queued before pairing go out now, and the phone asks to be notified (once)
                if paired { Task { await Reporter.shared.flush() }; Task { await PushRegistry.shared.start() }; JarvisActivity.shared.start() }
                else { Task { await JarvisActivity.shared.end(); await RideLiveActivity.shared.end() } }
            }
            // a pairing (or a new one over the old) sends what the old token could not
            .onChange(of: BrainLink.shared.openTick) { _, _ in LifelogUploader.shared.kick(foreground: true) }
            .onChange(of: phase) { _, p in
                // the brain's days are asked for again when the app comes back from the background (not on a timer)
                StallLog.mark("phase \(p)")
                if p == .background { wasBackground = true }
                else if p == .active && wasBackground { wasBackground = false; BrainLink.shared.opened() }
                // what waits on him is asked for every few seconds, only while the app is on screen
                if p == .active { BrainLink.shared.reloadIfUnpaired(); Waiting.shared.start(); PushRegistry.shared.retryIfNeeded(); JarvisActivity.shared.start(); LifelogUploader.shared.kick(foreground: true) } else { Waiting.shared.stop() }
            }
    }
}
