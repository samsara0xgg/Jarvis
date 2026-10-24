// words: legacy
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

/// A picture or file he sent with his words, as the talk shows it. A picture is told by its attachment id (the brain's name for it, known once it is up or from the conversation's row);
/// `image` is the small copy this phone has in hand, drawn until the picture's own file is read.
struct SentItem: Identifiable, Equatable {
    let id: String
    var attachment: String?
    var isImage: Bool
    var title: String               // a file's name
    var gone = false                // the brain has swept it (ADR 0255)
    var image: UIImage?
}

enum SentPics {
    /// what he attached, as the live row of his line shows it
    static func items(_ items: [AttachItem]) -> [SentItem] {
        items.map { SentItem(id: UUID().uuidString, isImage: $0.kind == .photo, title: $0.kind == .photo ? "" : $0.title, image: $0.kind == .photo ? $0.thumbnail : nil) }
    }

    /// what the conversation's row of his says he sent
    static func items(_ list: [RowAttachment]) -> [SentItem] {
        list.map { SentItem(id: $0.id, attachment: $0.id, isImage: $0.isImage, title: $0.name, gone: $0.gone) }
    }
}

/// Where his typed message stands: with the brain, on its way (dimmed), or not sent (点重试).
enum SendState: Equatable { case sent, sending, failed }

/// One row of the talk on show under the dial: his words or hers, as they are said. `reveal` lights her words one at a time while they are being said (nil: all lit).
struct LiveTurn: Identifiable, Equatable {
    let id: Int
    var his: Bool
    var text: String
    var reveal: Reveal?
    var pics: [SentItem] = []           // what he sent with the words
    var leadIn = false                  // a line before the answer ("我上网查查"): more follows
    var growing = false                 // his words still coming in (the recognizer's partial)
    var heard = false                   // his words are what the phone's recognizer heard, not typed or sent by a button: the timeline's place for the talk shows them so that he can see whether she understood
    var card: WaitingCard?              // a card she put up, in the place of words
    var status: StatusLine?             // not words yet: she is typing, or working on a step (Brain/TurnStatus); her first words take this row
    var steps: StepsInfo?               // what she did to answer, for the small line under it
    var failed = false                  // the turn failed: "没答出来" in her place, and a button to send the same words again
    var send = SendState.sent           // his message: on its way, or "没发出去" with 点重试 under it
    var recordId: String?               // the conversation record of these words, once the brain has said which (ADR 0256): a row without one cannot be reacted to
    var places: [AnswerPlace] = []      // the places her answer names: a map under it
    var at = Date()
}

@Observable
final class StarUI {
    // the top row
    var dateText = "10月8日 周四"
    var dateLabel: String?             // an all-day event of that day, after the date
    var topTime = ""                    // the time the dial points at, beside the date; on show while a talk is
    var level = 0                       // -1 近, 0 日, 1 周, 2 月
    var camE = 0.0                      // how far the near view has come in, 0...1; the grabber leaves the road as it does
    // the detail under the dial
    var view = "day"
    var flip = 0                        // bumps when the view changes: the detail slides in again
    var hm = "21:14"
    var tagText: String? = "现在"
    var dayText = ""
    var dayLine: String?                // her sentence about the day shown, under dayText
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
    var panelRelease = 0.0              // a finger has just let go of the history's top edge at this speed (parts of the way left per second): the panel's spring goes on from it
    var pinned: [PinGroup] = []
    var chipOpen = false                // the pinned groups are open under the panel's chip
    var unfolded: Set<String> = []      // pinned groups of more than two that he opened
    var convDays: [TalkDay] = []        // the conversation of record, by day (Brain/TalkStream)
    var olderMore = false               // the brain has rows older than the first one listed
    var scrollTo: String?               // a row of the stream the panel is asked to show
    var tapback: TalkRow?               // the row whose reactions bar is up (Sky/StarTapback)
    var tapbackPicker = false           // the bar is asking for any emoji, with the system keyboard
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
    /// how far the composer is raised over its rest place: by the keyboard, and on the timeline's conversation at least over the bar at the foot (Sky/HomeBar.swift)
    var footLift: Double { max(kbLift, talkPage ? HomeBar.fieldLift : 0) }
    var mic: MicState = .mic
    var chipsOn = false
    var settingsOpen = false            // settings is open: on the timeline the third page of the bar at the foot, under the dial and on the iPad a sheet
    var tuning = false                  // the near-view camera panel is open (Sky/StarTune.swift)
    var text = ""
    var talkChips: [String] = []
    var voiceNote: String?              // the voice's one status line: the model download, or what went wrong
    // what is attached to the words, and the panel that picks it (Attach/)
    let attach = AttachModel()
    // the whole-day timeline, which can take the dial's place in the middle of the screen (Sky/DayTimeline.swift)
    let tl = TimelineState()
    var tlShow = HomeModule.shared.isTimeline       // the timeline is drawn: it is the middle of the screen, or the dial is still giving way to it
    var tlHome = HomeModule.shared.isTimeline       // she has come up to her place over the timeline (within 20 pt of it): her sentence and the bar are drawn only then, so she never glides over them
    var footprintOpen = false           // the footprint's one-day screen is over the timeline, which gives way to it (Sky/FootprintDayPage.swift)
    let footprint = FootprintDayState()
    var padHome = false                 // the iPad's own home is on screen, not the phone's: an iPad at regular width (Pad/PadHome.swift's `HomeChoice` sets it)
    /// the page of the home the glass bar at its foot is on (Sky/HomeBar.swift): the day, or over it the history, the footprint's page or settings (`StarEngine.showPage` changes it)
    var page: HomePage { settingsOpen ? .settings : panelOpen ? .talk : footprintOpen ? .map : .day }
    /// the history is up on the phone's timeline: the conversation's page, the whole screen under her (Sky/StarPanel.swift's `TalkPage`); under the dial it is a panel over it, and on
    /// the iPad's own home a sheet over it (Pad/PadHome.swift)
    var talkPage: Bool { panelOpen && HomeModule.shared.isTimeline && !padHome }
    var herCardFoot = 0.0               // how far down her card under her reaches, in design points; 0 while it is down (Sky/DayTimelineTalk.swift): what it covers of the day's top slides out
    var tlTalk = false                  // the timeline's place for a talk is on show: her sentence, the bar and the chips have given way to the latest round (Sky/DayTimelineTalk.swift)

    /// The composer is out: always under the dial. On the timeline the field is on 聊天 only (a hold on her, or 问她 on a row, goes there); the other pages run to the foot of the screen.
    var composerOut: Bool {
        !HomeModule.shared.isTimeline || panelOpen
    }
}
