import SwiftUI
import Observation

// What the SwiftUI layer shows. The engine writes a field only when its value changes, like the prototype's rowKey/text checks.

enum RightStyle { case look, mut, src }
enum Expand: Equatable {
    case draft(String), call, approve, hike, note(String)
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

@Observable
final class StarUI {
    // the top row
    var dateText = "10月8日 周四"
    var level = 0                       // -1 近, 0 日, 1 周, 2 月
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
    // talking
    var talking = false
    var typing = false
    var kbLift = 0.0
    var capYou = ""
    var capHer = ""
    var capHerAlpha: [Double] = []
    var capOut = true
    var mic: MicState = .mic
    var chipsOn = false
    var settingsOpen = false
    var text = ""
    var talkChips: [String] = []
    // what is attached to the words, and the panel that picks it (Attach/)
    let attach = AttachModel()
}
