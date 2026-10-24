import SwiftUI

// 聊天 on the iPad in a wide window (跨端UI标准 §5, §11; 聊天-搜索和话题.jpg): at the left a column that stays open, 「搜我们说过的…」 and her topics (the phone's `ChatSearchCard`
// as a column), at the right the one conversation (the phone's page, `TalkPanel`, laid out in the column), running up under her to the status bar's narrow blur. The round button at
// the top left folds the column away, the one at the top right is 她出不出声. A topic's 前情, 看原话 and the ‹ › bar over the field are the phone's own, and so is the field
// (`ChatComposer`), 8 pt over the bar and 8 pt over the keys while he types.
// Narrower (portrait, Split View) the conversation takes the window's width, and the round button at the top left drops the phone's card of search and topics over it instead.
// Laid out at the mock's places (13" landscape, design/ipad-src/ipad.html), from the top of the window.

struct PadChat: View {
    let engine: StarEngine
    let width: Double
    let height: Double                  // the window's, to its foot under the bar
    let top: Double                     // the status bar's height
    let wide: Bool                      // wide enough for the column of topics beside the talk
    @Binding var folded: Bool
    @Binding var focused: Bool          // the field has the keyboard (the home's, which the engine's beginEditing / endEditing set)
    let keyboard: Double                // how far the keyboard reaches up from the foot
    @State private var past = ChatSearchModel()
    @State private var searching = false          // one column: the card of search and topics is down from the left button
    @AppStorage(VoicePrefs.key) private var spoken = true      // 她出不出声

    private var ui: StarUI { engine.ui }
    private static let columnW = 380.0, side = 28.0
    /// the conversation's own width, the column's room beside the left one (the mock's 404 on): folded, the same width in the middle, so no row wraps again
    private var talkW: Double { wide ? width - Self.columnW - 24 : width }
    /// where the field's foot is: 8 pt over the bar, and over the keys while they are up (the bar goes while he types)
    static func fieldFoot(height: Double, keyboard: Double) -> Double { keyboard > 0 ? height - keyboard - StarUI.keyGap : height - PadHome.barH - 8 }
    /// The phone's field with what it attaches, its foot at `foot`; what is attached grows it upward, and the panel that picks it opens over it as on the phone
    static func field(engine: StarEngine, focused: Binding<Bool>, x: Double, width: Double, foot: Double) -> some View {
        let ui = engine.ui
        return ZStack(alignment: .topLeading) {
            ChatComposer(engine: engine, ui: ui, focused: focused, width: width).offset(x: x, y: foot - ChatComposer.fieldBox - ui.attach.rise)
            AttachOverlay(model: ui.attach, kbLift: 0).offset(x: x - TalkPage.side, y: foot - (H - StarUI.restGap))          // laid out over the phone's field: moved onto this one
        }
    }
    /// the stream ends over the field: 34 pt over it at rest, as on the phone's page, 4 pt while he types
    private var streamFoot: Double { Self.fieldFoot(height: height, keyboard: keyboard) - ChatComposer.fieldBox - ui.attach.rise - (ui.typing ? 4 : 34) }

    var body: some View {
        ZStack(alignment: .topLeading) {
            conversation
            if wide && !folded {
                ChatSearchCard(model: past, engine: engine, close: {}, column: true)
                    .frame(width: Self.columnW - 28).frame(maxHeight: .infinity, alignment: .top)
                    .padding(.leading, 14).padding(.top, top + 70).padding(.bottom, PadHome.barRoom)
                    .transition(.move(edge: .leading).combined(with: .opacity))
                Rectangle().fill(SetColor.line).frame(width: 0.5, height: height - 100 - 96).offset(x: Self.columnW + 12, y: 100)
                    .transition(.opacity)
            }
            edge
            if !wide { search }
            buttons
            Self.field(engine: engine, focused: $focused, x: talkX + 20 + TalkPage.side, width: talkW - 40 - 2 * TalkPage.side, foot: Self.fieldFoot(height: height, keyboard: keyboard))
        }
        .frame(width: width, height: height, alignment: .topLeading)
        .animation(padMotion, value: folded)
        .task(id: ui.page == .talk) { if ui.page == .talk { await past.refreshTopics() } }          // the page stays made under the others: her topics as they are now each time it comes up
        .onChange(of: ui.page) { _, p in if p != .talk { setSearch(false) } }
    }

    /// the phone's page in the right column, 40 pt in from each side as the mock's messages; its rows as wide as the mock's (62% of the room)
    private var conversation: some View {
        let w = talkW - 40
        return TalkPanel(engine: engine, ui: ui, pull: .constant(nil), page: true,
                         column: .init(size: CGSize(width: w, height: height), inset: top + 84, line: top + 72, bottom: streamFoot))
            .environment(\.talkCardWidth, ((w - 2 * TalkPage.side) * 0.62).rounded())
            .frame(width: w, height: height, alignment: .topLeading)
            .coordinateSpace(name: "design")
            .offset(x: talkX + 20)
    }
    /// where the conversation's room begins: beside the column, or in the middle when it is folded
    private var talkX: Double { folded || !wide ? (width - talkW) / 2 : width - talkW }

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

    /// one column: the phone's card of search and topics, dropped from the left button, over a layer that takes the tap that closes it
    @ViewBuilder private var search: some View {
        if searching {
            Color.clear.frame(width: width, height: height).contentShape(Rectangle()).onTapGesture { setSearch(false) }
            ChatSearchCard(model: past, engine: engine, close: { setSearch(false) })
                .offset(x: Self.side - 12, y: top + PadHome.herY + HomeTop.button / 2 + 7)
                .transition(.scale(scale: 0.96, anchor: UnitPoint(x: 0.06, y: 0)).combined(with: .opacity))
        }
    }

    private func setSearch(_ on: Bool) {
        guard searching != on else { return }
        withAnimation(.easeOut(duration: 0.3)) { searching = on }
        if !on { past.reset() }
    }

    /// the column's fold at the left, 她出不出声 at the right, on her line
    private var buttons: some View {
        HStack {
            if wide {
                TopGlassButton(symbol: "sidebar.left", label: folded ? Words.Pad.unfoldTopics : Words.Pad.foldTopics, on: !folded) { Haptic.selection(); folded.toggle() }
            } else {
                TopGlassButton(symbol: "magnifyingglass", label: Words.PhoneChat.history) { Haptic.selection(); setSearch(!searching) }
            }
            Spacer()
            TopGlassButton(symbol: spoken ? "speaker.wave.2" : "speaker.slash", label: spoken ? Words.PhoneChat.spokenOn : Words.PhoneChat.spokenOff, on: spoken) {
                Haptic.selection(); if !spoken { Sound.shared.play(.speakerOn, gain: SoundFeedback.gain) }; VoicePrefs.spoken = !spoken      // the Mac's speaker-on as her voice comes back
            }
        }
        .padding(.horizontal, Self.side)
        .frame(width: width)
        .padding(.top, top + PadHome.herY - HomeTop.button / 2)
    }
}
