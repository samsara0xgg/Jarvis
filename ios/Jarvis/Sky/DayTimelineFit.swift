import UIKit

// The columns of a row, and whether its own words are cut short at the right of them. A row with nothing more to say still opens when they are, to say them whole; one that fits does
// not, so that a tap is never on a row that has nothing to give.

/// the columns of a row, in the list's own points (380 wide): the time, the circle (28 across, its centre 60 from the left) with the rail through it, and the words
enum TLColumns {
    static let time = 40.0, bead = 28.0, centre = 60.0, text = 84.0
    /// what is left of the row's width for the words, before the length at the right
    static let words = TLLayout.barW - text
}

/// how wide a line is in the system font at a size, which is what the list's own text is set in
func tlWidth(_ s: String, size: Double, weight: UIFont.Weight = .regular, mono: Bool = false) -> Double {
    let font = mono ? UIFont.monospacedSystemFont(ofSize: size, weight: weight) : UIFont.systemFont(ofSize: size, weight: weight)
    return Double((s as NSString).size(withAttributes: [.font: font]).width)
}

extension TLEntry {
    /// its title (15) or its line (12) is wider than the row leaves them: the words column less the length at the right (and the amber 等你 beside a title)
    var cutShort: Bool {
        switch kind {
        case .stay, .now, .pause: return false
        default:
            let room = TLColumns.words - 8 - tlWidth(right, size: 11.5, weight: .medium, mono: true)
            return tlWidth(title, size: 15) + (waiting ? 46 : 0) > room || (detail.map { tlWidth($0, size: 12) > room } ?? false)
        }
    }
}
