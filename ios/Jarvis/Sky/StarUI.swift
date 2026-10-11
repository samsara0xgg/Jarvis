import SwiftUI
import Observation

// What the SwiftUI layer shows. The engine writes a field only when its value changes, like the prototype's rowKey/text checks.

enum RightStyle { case look, mut, src }
enum Expand: Equatable {
    case draft(String), call, approve, hike, note(String)
    case card(WaitingCard)              // what the brain is waiting on, with its own buttons
    case talk(his: String, hers: String, start: Int64)      // a talk: its first exchange (empty while unknown) and when it began
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
struct PinGroup: Identifiable, Equatable { var id: String; var name: String; var rows: [RowModel] }
enum MicState { case mic, on, send }

/// A picture or file he sent with his words: the picture's small copy, or the file's name. Shown with his line so he can see what went.
struct SentItem: Identifiable, Equatable {
    let id = UUID()
    var image: UIImage?
    var title: String
}

/// What he sent with a turn, kept by the number of his row in the conversation so the panel can show it with his line (until the app is closed).
@MainActor enum SentPics {
    static var byRow: [Int: [SentItem]] = [:]
    static var pending: [SentItem] = []
    nonisolated static func items(_ items: [AttachItem]) -> [SentItem] { items.map { SentItem(image: $0.kind == .photo ? $0.thumbnail : nil, title: $0.kind == .photo ? "" : $0.title) } }
}

/// One row of the talk on show under the dial: his words or hers, as they are said. `reveal` lights her words one at a time while they are being said (nil: all lit).
struct LiveTurn: Identifiable, Equatable {
    let id: Int
    var his: Bool
    var text: String
    var reveal: Reveal?
    var pics: [SentItem] = []           // what he sent with the words
    var leadIn = false                  // a line before the answer ("我上网查查"): more follows
    var growing = false                 // his words still coming in (the recognizer's partial)
    var card: WaitingCard?              // a card she put up, in the place of words
    var status: StatusLine?             // not words yet: she is typing, or working on a step (Brain/TurnStatus); her first words take this row
    var steps: StepsInfo?               // what she did to answer, for the small line under it
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
    // the talk panel's second height: the whole conversation, with what waits on him and what she is doing pinned over it
    var panelOpen = false
    var pinned: [PinGroup] = []
    var chipOpen = false                // the pinned groups are open under the panel's chip
    var unfolded: Set<String> = []      // pinned groups of more than two that he opened
    var convDays: [TalkDay] = []        // the conversation of record, by day (Brain/TalkStream)
    var olderMore = false               // the brain has rows older than the first one listed
    var scrollTo: String?               // a row of the stream the panel is asked to show
    // talking
    var talking = false                 // a talk is on show: while it lasts and for a while after her last word
    var liveTurns: [LiveTurn] = []
    var stepsOpen: Set<String> = []     // answers (row ids of the stream) whose steps he opened
    var about: About?                   // the item he has open, as the chip over the talk's first row says it; every turn carries it while it shows
    var typing = false
    var kbLift = 0.0                    // how far the composer is raised over its rest place (restGap from the bottom): the keyboard's height less restGap - keyGap
    static let restGap = 34.0, keyGap = 8.0       // the composer's bottom from the screen's bottom at rest (the home indicator), and from the keyboard's top
    /// The keyboard's frame runs to the bottom of the screen, so its height already holds the home-indicator inset that restGap is: lifting the composer by the whole height
    /// would count that inset twice (a 34 pt gap over the keys). It is lifted by the height less the difference of the two gaps.
    static func lift(keyboardHeight h: Double) -> Double { h > 0 ? max(0, h - (restGap - keyGap)) : 0 }
    var mic: MicState = .mic
    var chipsOn = false
    var settingsOpen = false
    var tuning = false                  // the near-view camera panel is open (Sky/StarTune.swift)
    var text = ""
    var talkChips: [String] = []
    var voiceNote: String?              // the voice's one status line: the model download, or what went wrong
    // what is attached to the words, and the panel that picks it (Attach/)
    let attach = AttachModel()
}
