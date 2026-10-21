import SwiftUI

@main
struct JarvisWatchApp: App {
    init() {
        // WatchLink.shared.start() — the phone link, added by the link's branch
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
    }
}
