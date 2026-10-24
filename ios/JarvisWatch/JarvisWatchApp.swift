import SwiftUI

@main
struct JarvisWatchApp: App {
    @Environment(\.scenePhase) private var phase

    init() {
        WatchLink.shared.start()
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
        .onChange(of: phase) { _, p in
            guard p == .active else { return }
            WatchCharge.schedule()   // not in init: watchOS aborts a refresh asked for before SwiftUI's delegate is there
            Task { await WatchCharge.send() }
        }
        .backgroundTask(.appRefresh(WatchCharge.refreshID)) { await WatchCharge.refresh() }
    }
}
