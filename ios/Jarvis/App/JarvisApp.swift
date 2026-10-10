import SwiftUI
import CoreText

@main
struct JarvisApp: App {
    init() {
        for n in ["InstrumentSerif-Regular", "IBMPlexMono-Regular", "IBMPlexMono-Medium"] {
            if let u = Bundle.main.url(forResource: n, withExtension: "ttf") { CTFontManagerRegisterFontsForURL(u as CFURL, .process, nil) }
        }
    }
    var body: some Scene {
        WindowGroup { StarChartScreen() }
    }
}
