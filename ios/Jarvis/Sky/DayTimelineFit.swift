import SwiftUI
import UIKit

// The columns of a row, and whether its own words are cut short at the right of them. A row with nothing more to say still opens when they are, to say them whole; one that fits does
// not, so that a tap is never on a row that has nothing to give.

/// the columns of a row, in the list's own points (380 wide): the time, the circle (28 across, its centre 60 from the left) with the rail through it, the words, and on the phone the
/// arrow of a row that opens where it stands at the far right (kept clear on every row, so that every length ends in one line)
enum TLColumns {
    static let time = 40.0, bead = 28.0, centre = 60.0, text = 84.0, arrow = 18.0
    /// what is left of the row's width for the words, before the length at the right
    static let words = TLLayout.barW - text
    /// the phone's 今天 list: the title column begins 115 pt from the screen's left and the length ends 49 pt from its right
    static let phoneWords = W - 49 - 115
}

/// A length, a count or a value with its words (`1 小时 40 分`, `还有 10 分钟`, `14 条`, `等你`): the system font with its figures all one width, so that a column of them lines up, and no
/// spacing added (Chinese set in a monospaced face, or spaced out, reads as a row of boxes). Clock times alone keep the monospaced face.
func tlFigures(_ size: Double, weight: Font.Weight = .medium) -> Font { .system(size: size, weight: weight).monospacedDigit() }

/// how wide a line is in the system font at a size, which is what the list's own text is set in (`mono`: in the monospaced face of a clock; `figures`: as `tlFigures` sets it)
func tlWidth(_ s: String, size: Double, weight: UIFont.Weight = .regular, mono: Bool = false, figures: Bool = false) -> Double {
    let font = mono ? UIFont.monospacedSystemFont(ofSize: size, weight: weight)
        : figures ? UIFont.monospacedDigitSystemFont(ofSize: size, weight: weight) : UIFont.systemFont(ofSize: size, weight: weight)
    return Double((s as NSString).size(withAttributes: [.font: font]).width)
}

extension TLEntry {
    /// its title (15) or its line (12) is wider than the phone's row leaves them: the words column (to where the arrow's room begins) less the length at the right (and the amber 等你 beside a title)
    var cutShort: Bool {
        switch kind {
        case .stay, .now, .pause, .segment: return false
        default:
            let room = TLColumns.phoneWords - 8 - tlWidth(right, size: 11.5, weight: .medium, figures: true)
            return tlWidth(title, size: 15) + (waiting ? 46 : 0) > room || (detail.map { tlWidth($0, size: 12) > room } ?? false)
        }
    }
}
