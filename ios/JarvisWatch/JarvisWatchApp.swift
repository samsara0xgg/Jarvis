import SwiftUI

@main
struct JarvisWatchApp: App {
    @Environment(\.scenePhase) private var phase

    init() {
        WatchLink.shared.start()
        WatchCharge.schedule()
        Task { await WatchCharge.send() }
        #if DEBUG
        WatchScene.apply()
        #endif
    }

    var body: some Scene {
        WindowGroup {
            #if DEBUG
            WatchDebugHost()
            #else
            WatchRoot()
            #endif
        }
        .onChange(of: phase) { _, p in if p == .active { Task { await WatchCharge.send() } } }
        .backgroundTask(.appRefresh(WatchCharge.refreshID)) { await WatchCharge.refresh() }
    }
}
