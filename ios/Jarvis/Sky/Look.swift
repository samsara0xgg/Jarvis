import SwiftUI

// The phone's one table of colours and one type scale (跨端UI标准 v0, §7–8; the Mac's CSS holds the same values). Every colour the phone's pages draw text, lines and marks in comes
// from here; the colours of what he was doing are TLCategory's (Sky/DayTimelineDoing.swift), which are this table's too.

enum Look {
    static let ink = RGB(238, 240, 251).color(1)                       // #EEF0FB words
    static let ink2 = RGB(196, 204, 238).color(0.62)                   // secondary words
    static let ink3 = RGB(196, 204, 238).color(0.34)                   // fainter still
    static let line = RGB(157, 180, 255).color(0.12)                   // hairlines
    static let waits = RGB(255, 201, 143).color(1)                     // #FFC98F what waits on him, and nothing else
    static let now = RGB(255, 240, 220).color(1)                       // #FFF0DC now, and nothing else
    static let glow = RGB(157, 180, 255).color(1)                      // #9DB4FF her light
    static let red = RGB(255, 107, 107).color(1)                       // #FF6B6B an error, the recording dot
    static let card = RGB(26, 30, 56).color(1)                         // #1A1E38 cards, her menu, the typing field
    static let sky = RGB(9, 10, 20).color(1)                           // #090A14 under everything

    /// the places on the place line: home and school fixed, any other place a colour its id picks (Brain/PlaceTint.swift)
    static let home = RGB(240, 144, 122), school = RGB(90, 169, 230)

    // the five sizes: the stage's title, a big title and her answer, a row's title and body, grey words and the date, the hours, the tabs and badges
    static let stage = 22.0, title = 17.0, body = 15.0, note = 13.0, small = 11.0

    /// times and numbers that line up: the system font with monospaced digits (no Plex, no JetBrains on the phone)
    static func figures(_ size: Double, weight: Font.Weight = .regular) -> Font { .system(size: size, weight: weight).monospacedDigit() }
    static func figuresUI(_ size: Double, weight: UIFont.Weight = .regular) -> UIFont { .monospacedDigitSystemFont(ofSize: size, weight: weight) }
}
