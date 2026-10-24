import SwiftUI

// 聊天 on the iPad in a wide window (跨端UI标准 §5, §11; 聊天-搜索和话题.jpg): at the left a column that stays open, 「搜我们说过的…」 and her topics (the phone's `ChatSearchCard`
// as a column), at the right the one conversation (the phone's page, `TalkPanel`, laid out in the column), running up under her to the status bar's narrow blur. The round button at
// the top left folds the column away, the one at the top right is 她出不出声. A topic's 前情, 看原话 and the ‹ › bar over the field are the phone's own.
// Laid out at the mock's places (13" landscape, design/ipad-src/ipad.html), from the top of the window.

struct PadChat: View {
    let engine: StarEngine
    let width: Double
    let height: Double                  // the window's, to its foot under the bar
    let top: Double                     // the status bar's height
    @Binding var folded: Bool
    @State private var past = ChatSearchModel()
    @AppStorage(VoicePrefs.key) private var spoken = true      // 她出不出声

    private var ui: StarUI { engine.ui }
    private static let columnW = 380.0, side = 28.0
    /// the conversation's own width, the column's room beside the left one (the mock's 404 on): folded, the same width in the middle, so no row wraps again
    private var talkW: Double { width - Self.columnW - 24 }
    /// ponytail: the field's place, kept clear until the phone's composer is a view of its own to host here: as on the phone, 52 pt tall and 8 pt over the bar
    private var fieldTop: Double { height - PadHome.barH - 8 - 52 }

    var body: some View {
        ZStack(alignment: .topLeading) {
            conversation
            if !folded {
                ChatSearchCard(model: past, engine: engine, close: {}, column: true)
                    .frame(width: Self.columnW - 28).frame(maxHeight: .infinity, alignment: .top)
                    .padding(.leading, 14).padding(.top, top + 70).padding(.bottom, PadHome.barRoom)
                    .transition(.move(edge: .leading).combined(with: .opacity))
                Rectangle().fill(SetColor.line).frame(width: 0.5, height: height - 100 - 96).offset(x: Self.columnW + 12, y: 100)
                    .transition(.opacity)
            }
            edge
            buttons
        }
        .frame(width: width, height: height, alignment: .topLeading)
        .animation(padMotion, value: folded)
    }

    /// the phone's page in the right column, 40 pt in from each side as the mock's messages; its rows as wide as the mock's (62% of the room)
    private var conversation: some View {
        let w = talkW - 40, x = folded ? (width - talkW) / 2 : width - talkW
        return TalkPanel(engine: engine, ui: ui, pull: .constant(nil), page: true,
                         column: .init(size: CGSize(width: w, height: height), inset: top + 84, line: top + 72, bottom: fieldTop - 34))
            .environment(\.talkCardWidth, ((w - 2 * TalkPage.side) * 0.62).rounded())
            .frame(width: w, height: height, alignment: .topLeading)
            .coordinateSpace(name: "design")
            .offset(x: x + 20)
    }

    /// the one soft edge of the page: a narrow blur under the status bar, so the clock reads over the messages that went by under it (the phone's `statusEdge`, the mock's `.topblur`)
    private var edge: some View {
        ZStack {
            Rectangle().fill(.thinMaterial)
            LinearGradient(colors: [Look.sky.opacity(0.85), Look.sky.opacity(0)], startPoint: .top, endPoint: .bottom)
        }
        .frame(width: width, height: top + 16)
        .mask(LinearGradient(stops: [.init(color: .black, location: 0), .init(color: .black, location: 0.6), .init(color: .clear, location: 1)], startPoint: .top, endPoint: .bottom))
        .allowsHitTesting(false)
    }

    /// the column's fold at the left, 她出不出声 at the right, on her line
    private var buttons: some View {
        HStack {
            TopGlassButton(symbol: "sidebar.left", label: folded ? Words.Pad.unfoldTopics : Words.Pad.foldTopics, on: !folded) { Haptic.selection(); folded.toggle() }
            Spacer()
            TopGlassButton(symbol: spoken ? "speaker.wave.2" : "speaker.slash", label: spoken ? Words.PhoneChat.spokenOn : Words.PhoneChat.spokenOff, on: spoken) {
                Haptic.selection(); VoicePrefs.spoken = !spoken
            }
        }
        .padding(.horizontal, Self.side)
        .frame(width: width)
        .padding(.top, top + PadHome.herY - HomeTop.button / 2)
    }
}
