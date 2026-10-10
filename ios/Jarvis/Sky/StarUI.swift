import SwiftUI
import Observation

// What the SwiftUI layer shows. The engine writes a field only when its value changes, like the prototype's rowKey/text checks.

enum RightStyle { case look, mut, src }
enum Expand: Equatable {
    case draft(String), call, approve, hike, note(String)
    case card(WaitingCard)              // what the brain is waiting on, with its own buttons
}
struct RowModel: Identifiable, Equatable {
    var id: String
    var kd: Kd
    var title: String
    var meta: String
    var right: String
    var rightStyle: RightStyle
    var expand: Expand?
}
struct ChipModel: Identifiable, Equatable { var id: String { text }; var kd: Kd; var text: String }
struct DrawerGroup: Identifiable, Equatable { var id: String; var name: String; var rows: [RowModel] }
enum MicState { case mic, on, send }

/// A picture or file he sent with his words: the picture's small copy, or the file's name. Shown with his line so he can see what went.
struct SentItem: Identifiable, Equatable {
    let id = UUID()
    var image: UIImage?
    var title: String
}

/// What he sent with a turn, kept by the number of his row in the conversation so the drawer can show it under his line (until the app is closed).
@MainActor enum SentPics {
    static var byRow: [Int: [SentItem]] = [:]
    static var pending: [SentItem] = []
    nonisolated static func items(_ items: [AttachItem]) -> [SentItem] { items.map { SentItem(image: $0.kind == .photo ? $0.thumbnail : nil, title: $0.kind == .photo ? "" : $0.title) } }
}

/// One line of the conversation in the drawer: his or hers, with the time it was said.
struct ConvLine: Identifiable, Equatable {
    let id: Int                         // the row's number in the conversation
    let his: Bool
    let text: String
    let when: String

    /// the rows newest first as lines of the drawer; `today` is `YYYY-MM-DD`, and only a line of another day says its date
    static func lines(_ rows: [ConversationRow], today: String) -> [ConvLine] {
        rows.reversed().compactMap { r in
            let text = r.isHis ? r.text.trimmingCharacters(in: .whitespacesAndNewlines) : AnswerText.visible(r.text)
            if text.isEmpty { return nil }
            let day = String(r.ts.prefix(10)), clock = r.ts.count >= 16 ? String(r.ts.dropFirst(11).prefix(5)) : ""
            return ConvLine(id: r.seq, his: r.isHis, text: text, when: (day == today || day.isEmpty ? "" : String(day.dropFirst(5)) + " ") + clock)
        }
    }
}

/// One row of the talk on show under the dial: his words or hers, as they are said. `alphas` brightens her words one character at a time while they are being said (empty: all lit).
struct LiveTurn: Identifiable, Equatable {
    let id: Int
    var his: Bool
    var text: String
    var alphas: [Double] = []
    var pics: [SentItem] = []           // what he sent with the words
    var leadIn = false                  // a line before the answer ("我上网查查"): more follows
    var at = Date()
}

@Observable
final class StarUI {
    // the top row
    var dateText = "10月8日 周四"
    var topTime = ""                    // the time the dial points at, beside the date; on show while a talk is
    var level = 0                       // -1 近, 0 日, 1 周, 2 月
    var camE = 0.0                      // how far the near view has come in, 0...1; the grabber leaves the road as it does
    // the detail under the dial
    var view = "day"
    var flip = 0                        // bumps when the view changes: the detail slides in again
    var hm = "21:14"
    var tagText: String? = "现在"
    var dayText = ""
    var showBack = false
    var showSum = false
    var sumMain = ""
    var sumStat: String?
    var chips: [ChipModel] = []
    var rows: [RowModel] = []
    var emptyHint: String?
    var rowsCalm = true
    var detFast = false                 // the dial turns fast: the cards under it dim and hold still
    var rowsKey = 0                     // bumps when the list is rebuilt
    // the drawer of today's cards
    var drawerOpen = false
    var drawerGroups: [DrawerGroup] = []
    var history: [ConvLine] = []        // the conversation under them, when paired
    // talking
    var talking = false                 // a talk is on show: while it lasts and for a while after her last word
    var liveTurns: [LiveTurn] = []
    var typing = false
    var kbLift = 0.0
    var mic: MicState = .mic
    var chipsOn = false
    var settingsOpen = false
    var text = ""
    var talkChips: [String] = []
    var voiceNote: String?              // the voice's one status line: the model download, or what went wrong
    // what is attached to the words, and the panel that picks it (Attach/)
    let attach = AttachModel()
}
