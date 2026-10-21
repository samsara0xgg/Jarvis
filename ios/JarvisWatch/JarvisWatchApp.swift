import SwiftUI

@main
struct JarvisWatchApp: App {
    init() {
        WatchLink.shared.start()
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
