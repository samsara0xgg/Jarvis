// words: legacy
import SwiftUI

// The talk panel under the dial. Its bottom edge is fixed above the composer; its top edge has two places: the reading zone (星盘为主, the talk on show over the sky)
// and under the top row (对话为主, the whole conversation over a dimmed dial). Both heights show the same rows in the same styles, so opening only reveals more above
// the line he is reading: that line does not move. On the timeline the conversation is instead a page of its own, the whole screen under her (`TalkPage`).

private let ink = SetColor.ink
private let muted = SetColor.muted
private let amber = Color(red: 1, green: 217 / 255, blue: 176 / 255)
private let navy = Color(red: 8 / 255, green: 10 / 255, blue: 24 / 255)
/// the conversation page's cards and bubbles: hers the night's blue lifted off the black under the page, his the blue of the field's focus ring (Sky/StarChartScreen.swift) deepened
/// until white reads on it
private let card = Color(red: 26 / 255, green: 30 / 255, blue: 56 / 255)
private let mine = Color(red: 74 / 255, green: 94 / 255, blue: 200 / 255)

/// The conversation as a page of its own on the phone's timeline, a page of the bar at the foot (Sky/HomeBar.swift; yilun 2026-10-21: the whole screen, as a chat app's): she heads it
/// from the middle of the top (Sky/DayTimelineSeat.swift) and the talk runs under her down to the field over the bar, her words on dark cards at the left, his in filled bubbles at the
/// right, the times between them in the middle. The sky goes dark under her as it does under the day's list; what waits on him and what she is doing is a chip under her.
enum TalkPage {
    /// the stream's top: under her as big as she is while she listens, with a gap; its first `topFade` pt fade, so the rows go out before they reach her
    static let top: Double = TLSeat.talk.y + TLSeat.talkRadius * TLSeat.listening + 6, topFade = 30.0
    /// in from each side, as the field
    static let side = 20.0
    /// her cards end at least this far short of the other side, and his bubbles begin this far in: who said it shows in where it stands
    static let across = 56.0
    static var cardWidth: Double { W - 2 * side - across }
    static let corner = 20.0
    /// the chip under her: its top
    static var chipY: Double { top + 2 }
    /// how far under the stream's top the dark under her is full: a long way, so that no edge shows across the sky of an empty page
    static let darkBy = 90.0
    /// The black over the sky as (y, opacity): none round her, `strength` from `darkBy` under her, and none again at the foot under the field and the bar (as under the day's
    /// list, TLLayout.scrim), both ramps eased in `steps` steps so that no edge shows.
    static func scrim(strength: Double = 0.8, steps: Int = 12) -> [(y: Double, alpha: Double)] {
        func eased(_ t: Double) -> Double { t * t * (3 - 2 * t) }
        let from = top - 6, full = top + darkBy, end = HomeBar.top + 12, out = end - 48
        let down = (0...steps).map { i -> (y: Double, alpha: Double) in let t = Double(i) / Double(steps); return (from + t * (full - from), strength * eased(t)) }
        let up = (0...steps).map { i -> (y: Double, alpha: Double) in let t = Double(i) / Double(steps); return (out + t * (end - out), strength * (1 - eased(t))) }
        return down + up
    }
}
/// the panel's top edge moves with this; with Reduce Motion nothing moves, things fade in 0.2 s instead. Let go of by a finger it goes on at the finger's own speed (`velocity`: parts of the
/// way left per second, as `TLMotion.release`), a spring that does not bounce; else it starts from rest.
func panelSpring(_ reduce: Bool, velocity: Double = 0) -> Animation {
    reduce ? .easeOut(duration: 0.2) : velocity != 0 ? .interpolatingSpring(duration: 0.38, bounce: 0, initialVelocity: velocity) : .spring(response: 0.42, dampingFraction: 0.9)
}

// MARK: rows

/// one row of the talk: his words in a bubble on the right, hers plain on the left (on the conversation's page on a card)
struct TalkRowView: View, Equatable {
    let row: TalkRow
    var stepsOpen = false
    var cards = false                   // as the conversation's page lays it out (`TalkPage`): her words on a dark card, his in a filled bubble
    var dropAbout: () -> Void = {}
    var toggleSteps: () -> Void = {}
    var retry: () -> Void = {}
    @Environment(\.accessibilityReduceMotion) private var reduce
    static func == (a: Self, b: Self) -> Bool { a.row == b.row && a.stepsOpen == b.stepsOpen && a.cards == b.cards }
    /// her words end this far short of the row's edge, with or without a reaction on them, so a badge on their corner never covers a character and no line wraps differently for it
    private static let badgeRoom = 20.0

    var body: some View {
        if let a = row.about { AboutChip(about: a, drop: dropAbout) } else if let c = row.card { cardRow(c) } else if row.his { bubble } else { hers }
    }

    /// Her typing line, then her words in the same place (the line fades into them), and under them what she did to get there.
    private var hers: some View {
        VStack(alignment: .leading, spacing: cards ? 6 : 4) {
            if cards { words.modifier(HerCard(reactions: row.reactions)) } else { words }
            if !row.places.isEmpty, row.status == nil, !row.failed { PlaceMapCard(places: row.places).padding(.trailing, cards ? TalkPage.across : Self.badgeRoom).transition(.opacity) }
            if let info = row.steps, row.status == nil { StepsLine(info: info, open: stepsOpen, toggle: toggleSteps).padding(.leading, cards ? 6 : 0) }
        }
        .animation(.easeOut(duration: 0.2), value: row.status == nil)
    }

    /// her side of the row: her turn failed, the typing line, or her words
    @ViewBuilder private var words: some View {
        if row.failed { failedRow } else if let s = row.status { StatusRow(line: s, hug: cards).transition(.opacity) } else { her.transition(.opacity) }
    }

    /// her turn failed: it says so in her place, with a button that sends the same words again
    private var failedRow: some View {
        HStack(spacing: 12) {
            Text(row.text).font(.system(size: 16)).foregroundStyle(muted)
            Button(action: retry) {
                Text("重试").font(.system(size: 14, weight: .semibold)).foregroundStyle(amber)
                    .padding(.horizontal, 14).frame(minHeight: 32).background(Color.white.opacity(0.08), in: Capsule()).contentShape(Capsule())
            }.buttonStyle(.plain)
            if !cards { Spacer(minLength: 0) }
        }
    }

    /// a card she put up while she talks: her question over its fields and buttons, in the glass box the pinned cards open into (on the conversation's page, her cards' own)
    private func cardRow(_ c: WaitingCard) -> some View {
        let shape = RoundedRectangle(cornerRadius: cards ? TalkPage.corner : 14, style: .continuous)
        return VStack(alignment: .leading, spacing: 12) {
            Text(c.title).font(.system(size: 16)).foregroundStyle(ink.opacity(0.92)).lineLimit(3)
            WaitingCardView(card: c)
        }
        .padding(.horizontal, cards ? 16 : 14).padding(.vertical, 12).frame(maxWidth: .infinity, alignment: .leading)
        .background(cards ? card : Color.white.opacity(0.05), in: shape)
        .overlay(shape.stroke(Color.white.opacity(cards ? 0.07 : 0.08), lineWidth: 0.5))
    }

    private var bubble: some View {
        let shape = RoundedRectangle(cornerRadius: cards ? TalkPage.corner : 18, style: .continuous)
        return VStack(alignment: .trailing, spacing: 8) {
            if !row.pics.isEmpty { SentPicturesView(items: row.pics) }
            if !row.text.isEmpty {
                // round his words from the right as they come in (the engine lets them in a character at a time)
                HisText(text: row.text, live: row.current, tone: cards ? .white : ink.opacity(0.86)).font(.system(size: cards ? 17 : 16))
                    .padding(.horizontal, cards ? 16 : 14).padding(.vertical, cards ? 11 : 10)
                    .background(shape.fill(cards ? mine : Color.white.opacity(0.08)))
                    .overlay(shape.stroke(Color.white.opacity(cards ? 0 : 0.06), lineWidth: 0.5))
                    .overlay(alignment: .topLeading) { ReactionBadges(reactions: row.reactions, his: true) }
                    .frame(maxWidth: cards ? TalkPage.cardWidth : 300, alignment: .trailing)
                    .opacity(row.send == .sending ? 0.55 : 1)
            }
            if row.send == .failed { notSent }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
    }

    /// his message did not go out: it says so under the bubble, and the button sends that same message again (under the id it first had)
    private var notSent: some View {
        Button(action: retry) {
            HStack(spacing: 5) {
                Image(systemName: "exclamationmark.circle.fill")
                Text("没发出去，点重试")
            }
            .font(.system(size: 13)).foregroundStyle(amber).frame(minHeight: 32).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("没发出去，点重试")
    }

    /// her words: written ones shown at once and laid out as they are, said ones lit as she says them. On a card the card wears the reactions; else they are on the words' corner.
    @ViewBuilder private var her: some View {
        let blocks = AnswerBlocks.parse(row.text)
        if blocks.contains(where: { if case .paragraph = $0 { false } else { true } }) {
            if cards { AnswerView(blocks: blocks, size: 17, lineSpacing: 4) }
            else { AnswerView(blocks: blocks).padding(.trailing, Self.badgeRoom).overlay(alignment: .topTrailing) { ReactionBadges(reactions: row.reactions, his: false) } }
        } else if cards { plain } else {
            plain.padding(.trailing, Self.badgeRoom)
                .overlay(alignment: .topTrailing) { ReactionBadges(reactions: row.reactions, his: false) }       // on the words' own width, not the row's
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var plain: some View {
        RevealText(text: styledWords(row.text, base: row.leadIn ? muted : ink.opacity(0.92)), reveal: row.reveal)
            .font(.system(size: row.leadIn ? 15 : cards ? 17 : 18)).lineSpacing(row.leadIn ? 3 : cards ? 4 : 5)
            .tint(amber)
    }
}

/// Her side of a row on the conversation's page: on a dark card as wide as her words up to `TalkPage.cardWidth`, the reactions on its corner.
private struct HerCard: ViewModifier {
    let reactions: RowReactions

    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: TalkPage.corner, style: .continuous)
        content.padding(.horizontal, 16).padding(.vertical, 12)
            .background(card, in: shape)
            .overlay(shape.strokeBorder(Color.white.opacity(0.07), lineWidth: 0.5))
            .overlay(alignment: .topTrailing) { ReactionBadges(reactions: reactions, his: false) }
            .frame(maxWidth: TalkPage.cardWidth, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The reactions on a message, small round bubbles on its top corner on the side toward the other speaker (his on her words, hers on his), as iMessage puts them.
/// They are an overlay: they take no room, so a row's height is the same with and without them.
struct ReactionBadges: View {
    let reactions: RowReactions
    let his: Bool                                   // the message is his

    var body: some View {
        // the other speaker's first, so it is the one at the corner
        let list: [(by: String, emoji: String)] = (his ? ["jarvis", "allen"] : ["allen", "jarvis"]).compactMap { by in reactions[by].map { (by, $0) } }
        if !list.isEmpty {
            HStack(spacing: -7) {
                ForEach(his ? list : Array(list.reversed()), id: \.by) { r in
                    Text(r.emoji).font(.system(size: 14)).frame(minWidth: 26, minHeight: 26)
                        .background(Circle().fill(navy).overlay(Circle().fill(Color.white.opacity(r.by == "allen" ? 0.16 : 0.1))))
                        .overlay(Circle().stroke(Color.white.opacity(0.18), lineWidth: 0.5))
                        .accessibilityElement(children: .ignore).accessibilityLabel("\(r.by == "allen" ? "你" : "她")回了 \(r.emoji)")
                }
            }
            .offset(x: his ? -12 : 6, y: -13).allowsHitTesting(false)
        }
    }
}

/// Her words as styled text: links in amber and underlined, tapped they open in the browser.
func styledWords(_ s: String, base: Color) -> AttributedString {
    var a = AnswerText.linked(s)
    for run in Array(a.runs) {
        a[run.range].foregroundColor = run.link != nil ? amber : base
        if run.link != nil { a[run.range].underlineStyle = .single }
    }
    return a
}
func herText(_ s: String, base: Color) -> Text { Text(styledWords(s, base: base)) }

/// What he has open, over the first row of the talk: the star's mark, its title and time, and a × to take it off. While it shows, every turn tells the brain what "it" is (ADR 0214).
struct AboutChip: View {
    let about: About
    let drop: () -> Void

    var body: some View {
        HStack(spacing: 0) {
            HStack(spacing: 8) {
                MarkShape(kd: about.mark).frame(width: 16, height: 16)
                Text(about.title).font(.system(size: 14)).foregroundStyle(ink.opacity(0.86)).lineLimit(1)
                Text(about.when(Cal.cur.cal, Date())).font(AT.mono(12)).foregroundStyle(muted).lineLimit(1).fixedSize()
            }
            .padding(.leading, 14)
            Button(action: drop) {
                Image(systemName: "xmark").font(.system(size: 11, weight: .semibold)).foregroundStyle(muted).frame(width: 36, height: 36).contentShape(Rectangle())
            }
            .buttonStyle(.plain).accessibilityLabel("不再说这一项")
        }
        .frame(height: 36).glass(Capsule())
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// His words. While he is still speaking the recognizer hands them over in chunks about a second apart: the part already shown stays as it is
/// and only what changed after it comes in over 0.16 s, so the bubble does not jump. One short animation per chunk, nothing between chunks.
struct HisText: View {
    let text: String
    let live: Bool
    var tone = SetColor.ink.opacity(0.86)               // white on the conversation page's filled bubble
    @State private var stable = Int.max                 // characters at the front that need no blending
    @State private var chunk = 0
    @Environment(\.accessibilityReduceMotion) private var reduce

    /// how many characters at the front of `new` are the same in `old`, backed off to the start of a word when it cuts one (letters only; Chinese cuts anywhere)
    static func common(_ old: String, _ new: String) -> Int {
        let o = Array(old), n = Array(new)
        var i = 0
        while i < o.count && i < n.count && o[i] == n[i] { i += 1 }
        func letter(_ c: Character) -> Bool { c.isASCII && (c.isLetter || c.isNumber) }
        while i > 0 && i < n.count && letter(n[i - 1]) && letter(n[i]) { i -= 1 }
        return i
    }

    var body: some View {
        Text("").keyframeAnimator(initialValue: 1.0, trigger: chunk) { _, a in render(a) } keyframes: { _ in
            MoveKeyframe(0.0)
            CubicKeyframe(1.0, duration: 0.16)
        }
        .onChange(of: text) { old, new in
            guard live, !reduce else { stable = Int.max; return }
            stable = Self.common(old, new)
            chunk += 1
        }
    }

    /// the tail starts at 0.25 of its brightness and rises to 1
    private func render(_ a: Double) -> Text {
        let chars = Array(text), cut = min(stable, chars.count)
        var head = AttributedString(String(chars[..<cut])); head.foregroundColor = tone
        var tail = AttributedString(String(chars[cut...])); tail.foregroundColor = tone.opacity(0.25 + 0.75 * a)
        return Text(head + tail)
    }
}

/// A written answer: paragraphs, headings, lists with a hanging indent, code and tables in mono boxes (a table scrolls sideways), inline marks as markdown.
struct AnswerView: View {
    let blocks: [AnswerBlock]
    var size = 18.0                     // the size of her words, which the rest follows (the timeline's place for the talk reads at 15, with a line spacing of 3)
    var lineSpacing = 5.0

    /// code and tables keep their size down to 11.5, however small her words are
    private var monoSize: Double { max(11.5, size - 5) }

    var body: some View {
        VStack(alignment: .leading, spacing: size * 2 / 3) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, b in block(b) }
        }
        .tint(amber)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func block(_ b: AnswerBlock) -> some View {
        switch b {
        case .paragraph(let s): herText(s, base: ink.opacity(0.92)).font(.system(size: size)).lineSpacing(lineSpacing)
        case .heading(let s): herText(s, base: ink).font(.system(size: size - 1, weight: .semibold)).padding(.top, 4)
        case .list(let items):
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, it in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(it.marker).font(it.marker == "•" ? .system(size: size) : AT.mono(size - 3)).foregroundStyle(muted).frame(minWidth: 22, alignment: .trailing)
                        herText(it.text, base: ink.opacity(0.92)).font(.system(size: size)).lineSpacing(lineSpacing)
                    }
                    .padding(.leading, 20 * Double(min(it.level, 3)))
                }
            }
        case .quote(let s):
            herText(s, base: muted).font(.system(size: size - 1)).lineSpacing(lineSpacing)
                .padding(.leading, 14).overlay(alignment: .leading) { Capsule().fill(Color.white.opacity(0.22)).frame(width: 3) }
        case .rule: Color.white.opacity(0.14).frame(height: 0.5)
        case .code(let s): mono(s)
        case .table(let rows): table(rows)
        }
    }

    /// a table: mono cells in columns, the header over a line, in a box that scrolls sideways
    private func table(_ rows: [[String]]) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 6) {
                ForEach(Array(rows.enumerated()), id: \.offset) { r, cells in
                    GridRow {
                        ForEach(Array(cells.enumerated()), id: \.offset) { _, c in
                            herText(c, base: r == 0 ? ink : ink.opacity(0.8)).font(AT.mono(monoSize)).fixedSize()
                        }
                    }
                    if r == 0 { Color.white.opacity(0.14).frame(height: 0.5) }
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 10)
        }
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(0.06)))
    }

    /// code keeps its lines and columns: a box that scrolls sideways
    private func mono(_ s: String) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Text(s).font(AT.mono(monoSize)).foregroundStyle(ink.opacity(0.88)).fixedSize().padding(.horizontal, 12).padding(.vertical, 10)
        }
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(0.06)))
    }
}

// MARK: dragging the panel's top edge

/// A finger on the grabber or the header: the top edge follows it (`pull`, design y), and lets go open or shut by how far and how fast it went.
func panelDrag(_ engine: StarEngine, _ pull: Binding<Double?>, opens: Bool, tap: Bool) -> some Gesture {
    let home = StarEngine.panelHome, top = StarEngine.panelOpen
    return DragGesture(minimumDistance: 0, coordinateSpace: .named("design"))
        .onChanged { v in
            let dy = v.location.y - v.startLocation.y
            if abs(dy) > 7 { pull.wrappedValue = clamp((opens ? home : top) + dy, top, home) }
        }
        .onEnded { v in
            let dy = v.location.y - v.startLocation.y, vel = v.velocity.height, held = pull.wrappedValue, moved = held != nil
            let goes = opens ? (dy < -60 || vel < -500) : (dy > 80 || vel > 500)
            if let held {          // it goes on from where it is held at the speed of the finger: how much of the way left it covers in a second
                let to = goes == opens ? top : home, way = to - held
                engine.ui.panelRelease = abs(way) > 1 ? vel / way : 0
                Task { @MainActor in try? await Task.sleep(nanoseconds: 600_000_000); engine.ui.panelRelease = 0 }
            }
            pull.wrappedValue = nil
            if moved { if goes { opens ? engine.openPanel() : engine.closePanel() } }
            else if tap { opens ? engine.openPanel() : engine.closePanel() }
        }
}

/// Where the stream is scrolled and where the row that was first listed stands, so that the row he is looking at stays where it is when older rows come in above it.
final class StreamProbe {
    var offset = 0.0
    var y: Double?                      // the top, in the stream's own space, of the first row listed (while a page is asked for and just after: the row that was first)
    var anchor: String?                 // that row
    var rest = true                     // the list is not being moved
    var touching = false                // a finger is on it
    var asked = false                   // a page is being asked for
    var scrolled = false                // the page came and the stream was sent to the anchor: from here its place is corrected
    var pinY = 0.0, pinUntil = 0.0      // where the anchor is kept, and until when: the lazy stack's heights firm up as the rows are laid out
    var following = false               // a scroll to the bottom is on its way
    var pinning = false                 // a scroll that puts the anchor back is on its way
    var away = 0.0                      // how far the stream is from its bottom
}

/// where the panel's top edge is: held by a finger, or at one of its two places
func panelTop(_ ui: StarUI, _ pull: Double?) -> Double { pull ?? (ui.panelOpen ? StarEngine.panelOpen : StarEngine.panelHome) }
/// 0 at the reading zone, 1 fully open
func panelOpenness(_ top: Double) -> Double { clamp((StarEngine.panelHome - top) / (StarEngine.panelHome - StarEngine.panelOpen), 0, 1) }

// MARK: the panel

struct TalkPanel: View {
    let engine: StarEngine
    @Bindable var ui: StarUI
    @Binding var pull: Double?
    var page = false                                    // the phone's timeline: the conversation is a page of its own (`TalkPage`), not a panel
    @State private var pinContent = 0.0
    @State private var pos = ScrollPosition()
    @State private var probe = StreamProbe()
    @State private var follow = true                    // the stream keeps to its bottom (he has not scrolled up on purpose)
    @State private var byHand = false                   // a finger is on the stream, or it is still gliding from one
    @Environment(\.accessibilityReduceMotion) private var reduce

    /// grabber and chip are the panel's whole top part (spec-2c: no header)
    private static let grabH = 20.0, chipH = 32.0

    /// while he types the stream ends 8 pt over the composer (which is 8 pt over the keys); at rest, and while the voice's status line is up, it leaves that line its place
    private var bottom: Double { (ui.typing && ui.voiceNote == nil ? H - StarUI.restGap - 48 - StarUI.keyGap : 836) - ui.attach.rise - ui.footLift }
    private var chip: String? { PanelRules.chip(waits: ui.pinned.first { $0.id == "等你的" }?.rows.count ?? 0, works: ui.pinned.first { $0.id == "她在做" }?.rows.count ?? 0) }

    var body: some View {
        if page { pageBody } else { panelBody }
    }

    /// The conversation's page (`TalkPage`): the black under it, the stream from under her down to the field, and under her the chip with what it opens. It comes and goes with the
    /// bar's tab, a fade, as the other pages do.
    private var pageBody: some View {
        let bottom = self.bottom, chip = self.chip
        return ZStack(alignment: .topLeading) {
            LinearGradient(stops: TalkPage.scrim().map { .init(color: .black.opacity($0.alpha), location: $0.y / H) }, startPoint: .top, endPoint: .bottom)
                .frame(width: W, height: H).allowsHitTesting(false)
            stream(top: TalkPage.top, bottom: bottom, fade: ui.typing ? 8 : 24, open: 1)
            if let chip { pageChip(chip, bottom: bottom) }
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .opacity(ui.panelOpen ? 1 : 0).allowsHitTesting(ui.panelOpen)
        .animation(.easeOut(duration: ui.panelOpen ? 0.25 : 0.2), value: ui.panelOpen)
        .onChange(of: ui.panelOpen) { _, on in if !on { ui.chipOpen = false } else { follow = true } }
    }

    /// What waits on him and what she is doing, on a chip under her (amber when anything waits); a tap opens the pinned groups on a card under it, over the stream, and again folds them.
    private func pageChip(_ text: String, bottom: Double) -> some View {
        let waits = ui.pinned.contains { $0.id == "等你的" }, open = ui.chipOpen
        let shape = RoundedRectangle(cornerRadius: TalkPage.corner, style: .continuous)
        return VStack(spacing: 8) {
            Button { ui.chipOpen.toggle() } label: {
                HStack(spacing: 6) {
                    Text(text).font(.system(size: 13, weight: .semibold))
                    Image(systemName: "chevron.down").font(.system(size: 10, weight: .semibold)).rotationEffect(.degrees(open ? 180 : 0))
                }
                .foregroundStyle(waits ? amber : muted)
                .padding(.horizontal, 14).frame(height: 30)
                .background(card, in: Capsule()).overlay(Capsule().strokeBorder(Color.white.opacity(0.08), lineWidth: 0.5))
                .contentShape(Capsule())
            }
            .buttonStyle(PressScale())
            pinnedList(open ? min(pinContent, 0.5 * (bottom - TalkPage.chipY)) : 0, inset: 14)
                .padding(.vertical, open ? 6 : 0)
                .background(card, in: shape).overlay(shape.strokeBorder(Color.white.opacity(0.08), lineWidth: 0.5))
                .padding(.horizontal, TalkPage.side)
                .opacity(open ? 1 : 0).allowsHitTesting(open)
        }
        .animation(.easeOut(duration: 0.2), value: open)
        .frame(width: W)
        .offset(y: TalkPage.chipY)
    }

    private var panelBody: some View {
        let top = panelTop(ui, pull), p = panelOpenness(top), held = pull != nil
        let bottom = self.bottom, chip = self.chip
        let chipH = chip == nil ? 0 : Self.chipH
        let pinH = chip != nil && ui.chipOpen ? min(pinContent, 0.4 * (bottom - StarEngine.panelOpen)) : 0
        // The stream's frame goes straight to where the panel is heading: a scroll view resized every frame of the spring loses its rows for a few frames. The older rows above it fade in with the panel.
        let topTo = held ? top : ui.panelOpen ? StarEngine.panelOpen : StarEngine.panelHome
        let streamTo = topTo + (Self.grabH + chipH + pinH) * panelOpenness(topTo)
        let shown = (ui.talking && !HomeModule.shared.isTimeline) || ui.panelOpen || held          // on the timeline a talk is in her place over the day (Sky/DayTimelineTalk.swift): the panel is the history, there when it is opened
        return ZStack(alignment: .topLeading) {
            background.opacity(p).animation(held ? nil : panelSpring(reduce, velocity: ui.panelRelease), value: top).frame(width: W, height: H - top + 60).offset(y: top)
            topPart(p: p, held: held, chip: chip, pinH: pinH).frame(width: W, height: Self.grabH + chipH + pinH, alignment: .top).offset(y: top)
            stream(top: streamTo, bottom: bottom, fade: ui.typing ? 8 : 24, open: held ? p : ui.panelOpen ? 1 : 0).animation(nil, value: streamTo)
            measure
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .animation(held || reduce ? nil : panelSpring(false, velocity: ui.panelRelease), value: top)
        .opacity(shown ? 1 : 0).allowsHitTesting(shown)
        .animation(held ? nil : .easeOut(duration: ui.talking || ui.panelOpen ? 0.25 : 0.35), value: shown)          // under a finger it is there at once: it does not fade in as the finger pulls
        .onChange(of: ui.panelOpen) { _, on in if !on { ui.chipOpen = false } else { follow = true } }
    }

    private var background: some View {
        UnevenRoundedRectangle(topLeadingRadius: 28, topTrailingRadius: 28).fill(.ultraThinMaterial)
            .overlay(UnevenRoundedRectangle(topLeadingRadius: 28, topTrailingRadius: 28).fill(navy.opacity(0.72)))
            .overlay(alignment: .top) { UnevenRoundedRectangle(topLeadingRadius: 28, topTrailingRadius: 28).stroke(Color.white.opacity(0.1), lineWidth: 0.5) }
            .shadow(color: .black.opacity(0.4), radius: 25, y: -9)
            .contentShape(Rectangle())
    }

    // MARK: grabber, chip, pinned

    private func topPart(p: Double, held: Bool, chip: String?, pinH: Double) -> some View {
        VStack(spacing: 0) {
            Capsule().fill(Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.28)).frame(width: 36, height: 5)
                .frame(maxWidth: .infinity).frame(height: Self.grabH).contentShape(Rectangle())
                .gesture(panelDrag(engine, $pull, opens: false, tap: true))
            if let chip { chipButton(chip) }
            pinnedList(pinH)
        }
        .opacity(held ? p : ui.panelOpen ? 1 : 0)
        .animation(.easeOut(duration: 0.2).delay(ui.panelOpen ? 0.1 : 0), value: ui.panelOpen)
        .allowsHitTesting(ui.panelOpen)
    }

    /// `等你 2 件 · 她在做 1 ›`: amber when anything waits; a tap opens the pinned groups under it, and again folds them
    private func chipButton(_ text: String) -> some View {
        let waits = ui.pinned.contains { $0.id == "等你的" }
        return Button { ui.chipOpen.toggle() } label: {
            HStack(spacing: 6) {
                Text(text).font(.system(size: 13, weight: .semibold))
                Image(systemName: "chevron.right").font(.system(size: 11, weight: .semibold)).rotationEffect(.degrees(ui.chipOpen ? 90 : 0))
                Spacer(minLength: 0)
            }
            .foregroundStyle(waits ? amber : muted)
            .padding(.horizontal, 32).frame(height: Self.chipH).contentShape(Rectangle())
        }.buttonStyle(.plain)
    }

    private func pinnedList(_ height: Double, inset: Double = 30) -> some View {
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(ui.pinned) { g in
                    let folds = g.rows.count > 2, folded = folds && !ui.unfolded.contains(g.id)
                    Button { if folds { if folded { ui.unfolded.insert(g.id) } else { ui.unfolded.remove(g.id) } } } label: {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(g.name).font(.system(size: 13, weight: .semibold)).tracking(0.5).foregroundStyle(g.id == "等你的" ? amber : muted)
                            Text("\(g.rows.count)").font(AT.mono(11.5)).foregroundStyle(SetColor.faint)
                            Spacer(minLength: 0)
                        }.padding(.top, 8).padding(.bottom, 6).contentShape(Rectangle())
                    }.buttonStyle(.plain)
                    ForEach(folded ? Array(g.rows.prefix(1)) : g.rows) { r in RowView(row: r, engine: engine) }
                    if folded {
                        Button { ui.unfolded.insert(g.id) } label: {
                            Text("还有 \(g.rows.count - 1) 件 ›").font(.system(size: 13)).foregroundStyle(muted).frame(maxWidth: .infinity, alignment: .leading).frame(height: 36)
                        }.buttonStyle(.plain)
                    }
                }
            }
            .padding(.horizontal, inset)
            .onGeometryChange(for: Double.self) { Double($0.size.height) } action: { pinContent = $0 }
        }
        .frame(height: height)
    }

    // MARK: the stream

    private func askOlder() {
        guard probe.rest, probe.offset < 40, ui.olderMore, ui.panelOpen, !probe.asked, nowMs() > probe.pinUntil, nowMs() - engine.panelSince > 700,
              let id = ui.convDays.first?.rows.first?.id, let y = probe.y else { return }
        probe.asked = true; probe.scrolled = false; probe.anchor = id; probe.pinY = y
        engine.loadOlder { if !$0 { probe.asked = false } }
    }

    /// A row's top moved. The first row listed is followed; once a page is asked for, the row that was first, which is kept where he saw it while the page's rows are laid out above it.
    private func moved(_ id: String, to y: Double) {
        guard probe.asked || nowMs() < probe.pinUntil else {
            if id == ui.convDays.first?.rows.first?.id { probe.y = y }
            return
        }
        guard id == probe.anchor else { return }
        probe.y = y
        // on the next turn of the run loop, one at a time: a scroll from inside a geometry action makes the lazy stack lay out again, which calls the action again
        if probe.scrolled && !probe.touching && abs(y - probe.pinY) > 1 && !probe.pinning {
            probe.pinning = true
            DispatchQueue.main.async {
                probe.pinning = false
                if let y = probe.y, !probe.touching, abs(y - probe.pinY) > 1 { pos.scrollTo(y: probe.offset + y - probe.pinY) }
            }
        }
    }

    /// The page came in above the anchor: the stream goes to it (a lazy stack lays the rows out on the way, so the place is right), then `moved` sets it back where it was.
    private func arrived(_ proxy: ScrollViewProxy) {
        guard probe.asked, let id = probe.anchor else { return }
        probe.asked = false; probe.pinUntil = nowMs() + 1200
        DispatchQueue.main.async { proxy.scrollTo(id, anchor: .top); probe.scrolled = true }
    }

    /// `fade` is the strip under the last row where rows scrolled out of view melt away; the last row itself rests above it
    private func stream(top: Double, bottom: Double, fade: Double, open: Double) -> some View {
        let days = TalkStream.compose(ui.convDays, live: ui.liveTurns, about: ui.about, overlay: Reactions.shared.overlay, cal: Cal.cur.cal, now: Date())
        let _ = StallLog.count("compose")
        // under the dial the previous round shows dimmer over the newest one; with the panel open every row shows in full.
        // Those rounds are in the last two days: read only them, not the whole history on every frame of her talk.
        let tail = days.suffix(2).flatMap(\.rows)
        let shown = Dictionary(zip(tail.map(\.id), TalkStream.reading(tail)), uniquingKeysWith: { a, _ in a })
        return ScrollViewReader { proxy in
            ScrollView(showsIndicators: false) {
                // on the conversation's page the days and times stand in the middle between the rows, as a chat has them, the day before the first time of it ("今天 09:19"),
                // and scroll with them
                LazyVStack(alignment: .leading, spacing: 14, pinnedViews: ui.typing || page ? [] : [.sectionHeaders]) {
                    ForEach(days) { day in
                        Section {
                            ForEach(day.rows) { r in
                                VStack(alignment: .leading, spacing: page ? 8 : 6) {
                                    if let s = r.stamp { Text(page && r.id == day.rows.first?.id ? day.title + " " + s : s).font(page ? .system(size: 12, weight: .medium).monospacedDigit() : AT.mono(11)).foregroundStyle(muted).frame(maxWidth: .infinity, alignment: page ? .center : r.his ? .trailing : .leading) }
                                    TalkRowView(row: r, stepsOpen: ui.stepsOpen.contains(r.id), cards: page, dropAbout: { engine.dropAbout() }, toggleSteps: { engine.toggleSteps(r.id) }, retry: { engine.sendAgain(row: r.id) }).equatable()
                                }
                                .opacity((shown[r.id] ?? 0) + (1 - (shown[r.id] ?? 0)) * open)
                                .id(r.id)
                                .onGeometryChange(for: Double.self) { $0.frame(in: .named("stream")).minY } action: { moved(r.id, to: $0) }
                                .transition(reduce ? .opacity : .opacity.combined(with: .offset(y: 6)))
                                .modifier(RowPress(row: r, engine: engine))
                            }
                        } header: {
                            // no day headers while he types: the keyboard leaves no room for them (on the conversation's page the day is with the first time of it)
                            if !ui.typing && !page {
                                Text(day.title).font(.system(size: 13, weight: .semibold)).foregroundStyle(muted)
                                    .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 6).padding(.horizontal, 32)
                                    .background(navy).padding(.horizontal, -32)
                                    .opacity(open)
                            }
                        }
                    }
                }
                .padding(.horizontal, page ? TalkPage.side : 32)
                .animation(.easeOut(duration: 0.25), value: ui.panelOpen)
            }
            .contentMargins(.top, page ? TalkPage.topFade : 0, for: .scrollContent)          // the oldest row can rest clear of the fade under her
            .contentMargins(.bottom, fade, for: .scrollContent)
            .id(page ? false : ui.chipOpen)         // a lazy stack resized under it loses its place now and then: opening or folding the chip starts the list again, at the bottom (on the page the chip's card is over the stream, which keeps its size)
            // It starts at the bottom, and rows fewer than its height sit at the bottom. Not anchored to the bottom as sizes change: that kept the lazy stack
            // laying out forever when the keyboard went down and the panel shut with a talk on show (the app froze, reproduced every time in the
            // simulator; 2026-10-11). Keeping to the bottom as sizes change is `follow`'s.
            .defaultScrollAnchor(.bottom, for: .initialOffset)
            .defaultScrollAnchor(.bottom, for: .alignment)
            .scrollPosition($pos)
            .coordinateSpace(name: "stream")
            // At the top of a panel that has been open a moment, at rest, with older rows at the brain: ask for the page before. Not while the finger or its glide is still moving the list: a scroll made then would be undone.
            .onScrollGeometryChange(for: Double.self) { $0.contentOffset.y + $0.contentInsets.top } action: { _, y in probe.offset = y; askOlder() }
            .onScrollPhaseChange { _, phase in probe.rest = phase == .idle; probe.touching = phase == .interacting; askOlder() }
            .onChange(of: ui.convDays.first?.rows.first?.id) { _, _ in arrived(proxy) }
            .simultaneousGesture(DragGesture(minimumDistance: 0).onChanged { _ in engine.holdLinger(true) }.onEnded { _ in engine.holdLinger(false) })
            .onChange(of: ui.scrollTo) { _, id in
                guard let id else { return }
                follow = false
                proxy.scrollTo(id, anchor: UnitPoint(x: 0.5, y: 0.1)); ui.scrollTo = nil       // a little under the top, so the day's header does not cover the time above the row
            }
            // Kept to the bottom while he has not scrolled up: opening, the keyboard, a new row and a growing answer all change the size of the rows or of the window,
            // and then the distance to the bottom grows without him (PanelRules.follows). Past 80 pt of his own scrolling the ↓ button stands in for the jump.
            .onScrollPhaseChange { _, phase in
                let was = byHand
                byHand = phase == .interacting || phase == .decelerating
                // his finger let go within 80 pt of the bottom: it eases back down. Only after his own scroll, and on the next turn of the run loop:
                // a scroll's own end is a phase change too, and a scroll made here at once can end at once
                if was && !byHand && follow && probe.away > 0.5 {
                    DispatchQueue.main.async { if follow && !byHand && probe.away > 0.5 { withAnimation(.easeOut(duration: 0.25)) { pos.scrollTo(edge: .bottom) } } }
                }
            }
            .onScrollGeometryChange(for: Double.self) { g in (max(0, g.contentSize.height + g.contentInsets.bottom - g.visibleRect.maxY) * 2).rounded() / 2 } action: { _, away in
                follow = PanelRules.follows(was: follow, byHand: byHand, away: away)
                probe.away = away
                // on the next turn of the run loop, at most one at a time: a scroll from inside this action made a long history's lazy stack
                // estimate its height again, which called the action again, and the main thread never got out (the app froze, then was killed)
                // never under his finger or its glide: pulled back to the bottom while within 80 pt of it, he could not scroll up at all
                if follow && away > 0.5 && !byHand && !probe.following {
                    probe.following = true
                    DispatchQueue.main.async { probe.following = false; StallLog.count("follow"); pos.scrollTo(edge: .bottom) }
                }
            }
            .onChange(of: ui.panelOpen) { _, on in if on { pos.scrollTo(edge: .bottom) } }
            // his line going out and her reply coming in each take him to the bottom and back to following, wherever he had scrolled, the panel open or shut.
            // Rows leaving (the talk handed over to the conversation, a failed send) are not new rows: the place stays.
            .onChange(of: ui.liveTurns.count) { old, new in
                guard new > old else { return }
                follow = true
                if !probe.following {
                    probe.following = true
                    DispatchQueue.main.async { probe.following = false; pos.scrollTo(edge: .bottom) }
                }
            }
            // the way back to the newest: at the right, and on the conversation's page in the middle over the field, dark as her cards
            .overlay(alignment: page ? .bottom : .bottomTrailing) {
                let show = ui.panelOpen && !follow
                Button { follow = true; withAnimation(.easeOut(duration: 0.25)) { pos.scrollTo(edge: .bottom) } } label: {
                    Image(systemName: page ? "chevron.down" : "arrow.down").font(.system(size: page ? 15 : 14, weight: .semibold)).foregroundStyle(ink)
                        .frame(width: page ? 36 : 32, height: page ? 36 : 32).background(Circle().fill(page ? card : Color.white.opacity(0.14))).overlay(Circle().stroke(Color.white.opacity(0.12), lineWidth: 0.5))
                }
                .buttonStyle(PressScale()).accessibilityLabel("回到最新")
                .padding(.trailing, page ? 0 : 14).padding(.bottom, fade + 8)
                .opacity(show ? 1 : 0).allowsHitTesting(show).animation(.easeOut(duration: 0.2), value: show)
            }
        }
        .mask(VStack(spacing: 0) {
            LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: page ? TalkPage.topFade : 24 * (1 - open))
            Color.black
            LinearGradient(colors: [.black, .clear], startPoint: .top, endPoint: .bottom).frame(height: fade)
        })
        .frame(width: W, height: max(0, bottom + fade - top))
        .clipped()
        .offset(y: top)
    }

    /// Until the panel has opened by itself once, a copy of the talk's rows (unseen) tells whether they are taller than the place under the dial.
    @ViewBuilder private var measure: some View {
        if ui.talking && !ui.panelOpen && !engine.autoOpenSpent && !HomeModule.shared.isTimeline {
            VStack(alignment: .leading, spacing: 14) { if let a = ui.about { AboutChip(about: a, drop: {}) }; ForEach(ui.liveTurns) { TalkRowView(row: TalkStream.row($0)).equatable() } }
                .padding(.horizontal, 32).frame(width: W, alignment: .topLeading).fixedSize(horizontal: false, vertical: true)
                .onGeometryChange(for: Double.self) { Double($0.size.height) } action: { engine.measureLive($0) }
                .hidden().allowsHitTesting(false)
        }
    }
}
