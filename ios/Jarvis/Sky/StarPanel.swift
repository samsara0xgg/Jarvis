import SwiftUI

// The talk panel under the dial. Its bottom edge is fixed above the composer; its top edge has two places: the reading zone (星盘为主, the talk on show over the sky)
// and under the top row (对话为主, the whole conversation over a dimmed dial). Both heights show the same rows in the same styles, so opening only reveals more above
// the line he is reading: that line does not move.

private let ink = SetColor.ink
private let muted = SetColor.muted
private let amber = Color(red: 1, green: 217 / 255, blue: 176 / 255)
private let navy = Color(red: 8 / 255, green: 10 / 255, blue: 24 / 255)
/// the panel's top edge moves with this; with Reduce Motion nothing moves, things fade in 0.2 s instead
func panelSpring(_ reduce: Bool) -> Animation { reduce ? .easeOut(duration: 0.2) : .spring(response: 0.42, dampingFraction: 0.9) }

// MARK: rows

/// one row of the talk: his words in a bubble on the right, hers plain on the left
struct TalkRowView: View, Equatable {
    let row: TalkRow
    @Environment(\.accessibilityReduceMotion) private var reduce
    static func == (a: Self, b: Self) -> Bool { a.row == b.row }

    var body: some View {
        if row.his { bubble } else { her }
    }

    private var bubble: some View {
        VStack(alignment: .trailing, spacing: 8) {
            if !row.pics.isEmpty { SentItemsRow(items: row.pics) }
            if !row.text.isEmpty {
                // while his words still come in the bubble holds its width, so the part shown does not slide left with every chunk; it closes round them when he is done
                HisText(text: row.text, live: row.current).font(.system(size: 16))
                    .frame(maxWidth: row.growing ? .infinity : nil, alignment: .leading)
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .background(RoundedRectangle(cornerRadius: 18).fill(Color.white.opacity(0.08)))
                    .overlay(RoundedRectangle(cornerRadius: 18).stroke(Color.white.opacity(0.06), lineWidth: 0.5))
                    .frame(maxWidth: 300, alignment: .trailing)
                    .animation(reduce ? nil : .easeOut(duration: 0.2), value: row.growing)
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
    }

    @ViewBuilder private var her: some View {
        let blocks = AnswerBlocks.parse(row.text)
        if blocks.contains(where: { if case .paragraph = $0 { false } else { true } }) { AnswerView(blocks: blocks) }       // written: shown at once, laid out as it is
        else { plain }
    }

    private var plain: some View {
        words.font(.system(size: row.leadIn ? 15 : 18)).lineSpacing(row.leadIn ? 3 : 5)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// her words with each character at its own brightness while she says them: runs of one brightness are one piece of text
    private var words: Text {
        let base = row.leadIn ? muted : ink.opacity(0.92)
        guard row.alphas.count == row.text.count, row.alphas.contains(where: { $0 < 1 }) else { return Text(row.text).foregroundStyle(base) }
        var out = AttributedString()
        let chars = Array(row.text)
        var i = 0
        while i < chars.count {
            var j = i
            while j < chars.count && row.alphas[j] == row.alphas[i] { j += 1 }
            var run = AttributedString(String(chars[i..<j]))
            run.foregroundColor = Color.white.opacity(row.alphas[i])
            out += run
            i = j
        }
        return Text(out)
    }
}

/// His words. While he is still speaking the recognizer hands them over in chunks about a second apart: the part already shown stays as it is
/// and only what changed after it comes in over 0.16 s, so the bubble does not jump. One short animation per chunk, nothing between chunks.
struct HisText: View {
    let text: String
    let live: Bool
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
        var head = AttributedString(String(chars[..<cut])); head.foregroundColor = ink.opacity(0.86)
        var tail = AttributedString(String(chars[cut...])); tail.foregroundColor = ink.opacity(0.86 * (0.25 + 0.75 * a))
        return Text(head + tail)
    }
}

/// A written answer: paragraphs, headings, lists with a hanging indent, code and tables in mono boxes (a table scrolls sideways), inline marks as markdown.
struct AnswerView: View {
    let blocks: [AnswerBlock]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, b in block(b) }
        }
        .tint(AT.pale)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func inline(_ s: String) -> AttributedString {
        (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(s)
    }

    @ViewBuilder private func block(_ b: AnswerBlock) -> some View {
        switch b {
        case .paragraph(let s): Text(inline(s)).font(.system(size: 18)).lineSpacing(5).foregroundStyle(ink.opacity(0.92))
        case .heading(let s): Text(inline(s)).font(.system(size: 17, weight: .semibold)).foregroundStyle(ink).padding(.top, 4)
        case .list(let items):
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, it in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(it.marker).font(it.marker == "•" ? .system(size: 18) : AT.mono(15)).foregroundStyle(muted).frame(minWidth: 22, alignment: .trailing)
                        Text(inline(it.text)).font(.system(size: 18)).lineSpacing(5).foregroundStyle(ink.opacity(0.92))
                    }
                }
            }
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
                            Text(c).font(AT.mono(13)).foregroundStyle(r == 0 ? ink : ink.opacity(0.8)).fixedSize()
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
            Text(s).font(AT.mono(13)).foregroundStyle(ink.opacity(0.88)).fixedSize().padding(.horizontal, 12).padding(.vertical, 10)
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
            let dy = v.location.y - v.startLocation.y, vel = v.velocity.height, moved = pull.wrappedValue != nil
            pull.wrappedValue = nil
            if moved { if opens ? (dy < -60 || vel < -500) : (dy > 80 || vel > 500) { opens ? engine.openPanel() : engine.closePanel() } }
            else if tap { opens ? engine.openPanel() : engine.closePanel() }
        }
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
    @State private var pinContent = 0.0
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        let top = panelTop(ui, pull), p = panelOpenness(top), held = pull != nil
        let bottom = 836 - ui.attach.rise - ui.kbLift
        let pinH = ui.typing ? 0 : min(pinContent, 0.4 * (bottom - StarEngine.panelOpen))      // while he types the keyboard leaves little room: what waits is under the header again after he sends
        // The stream's frame goes straight to where the panel is heading: a scroll view resized every frame of the spring loses its rows for a few frames. The older rows above it fade in with the panel.
        let topTo = held ? top : ui.panelOpen ? StarEngine.panelOpen : StarEngine.panelHome
        let streamTo = topTo + (90 + pinH) * panelOpenness(topTo)
        let shown = ui.talking || ui.panelOpen || held
        ZStack(alignment: .topLeading) {
            background.opacity(p).animation(held ? nil : panelSpring(reduce), value: top).frame(width: W, height: H - top + 60).offset(y: top)
            topPart(p: p, held: held, pinH: pinH).frame(width: W, height: 90 + pinH, alignment: .top).offset(y: top)
            stream(top: streamTo, bottom: bottom, open: held ? p : ui.panelOpen ? 1 : 0).animation(nil, value: streamTo)
            measure
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .animation(held || reduce ? nil : panelSpring(false), value: top)
        .opacity(shown ? 1 : 0).allowsHitTesting(shown)
        .animation(.easeOut(duration: ui.talking || ui.panelOpen ? 0.25 : 0.35), value: shown)
    }

    private var background: some View {
        UnevenRoundedRectangle(topLeadingRadius: 28, topTrailingRadius: 28).fill(.ultraThinMaterial)
            .overlay(UnevenRoundedRectangle(topLeadingRadius: 28, topTrailingRadius: 28).fill(navy.opacity(0.72)))
            .overlay(alignment: .top) { UnevenRoundedRectangle(topLeadingRadius: 28, topTrailingRadius: 28).stroke(Color.white.opacity(0.1), lineWidth: 0.5) }
            .shadow(color: .black.opacity(0.4), radius: 25, y: -9)
            .contentShape(Rectangle())
    }

    // MARK: grabber, header, pinned

    private func topPart(p: Double, held: Bool, pinH: Double) -> some View {
        VStack(spacing: 0) {
            Capsule().fill(Color(red: 214 / 255, green: 220 / 255, blue: 246 / 255).opacity(0.28)).frame(width: 36, height: 5)
                .frame(maxWidth: .infinity).frame(height: 38).contentShape(Rectangle())
                .gesture(panelDrag(engine, $pull, opens: false, tap: true))
            header.frame(height: 52).contentShape(Rectangle()).gesture(panelDrag(engine, $pull, opens: false, tap: true))
            pinnedList(pinH)
        }
        .opacity(held ? p : ui.panelOpen ? 1 : 0)
        .animation(.easeOut(duration: 0.2).delay(ui.panelOpen ? 0.1 : 0), value: ui.panelOpen)
        .allowsHitTesting(ui.panelOpen)
    }

    /// the day's little ring (where he is in the day), the time, and the next thing or what waits
    private var header: some View {
        HStack(spacing: 12) {
            Image(decorative: engine.miniSprite(0, label: false).img, scale: engine.sprites.px).frame(width: 40, height: 46).padding(.leading, 28).padding(.trailing, -4)
            VStack(alignment: .leading, spacing: 2) {
                TimelineView(.everyMinute) { _ in Text(clock(engine.nowT())).font(AT.mono(15)).foregroundStyle(ink) }
                Text(ui.headLine).font(.system(size: 13)).foregroundStyle(ui.headWaits ? amber : muted).lineLimit(1)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.down").font(.system(size: 14, weight: .semibold)).foregroundStyle(muted).padding(.trailing, 30)
        }
    }

    private func pinnedList(_ height: Double) -> some View {
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
            .padding(.horizontal, 30)
            .onGeometryChange(for: Double.self) { Double($0.size.height) } action: { pinContent = $0 }
        }
        .frame(height: height)
    }

    // MARK: the stream

    private func stream(top: Double, bottom: Double, open: Double) -> some View {
        let days = TalkStream.compose(ui.convDays, live: ui.liveTurns, cal: Cal.cur.cal, now: Date())
        return ScrollViewReader { proxy in
            ScrollView(showsIndicators: false) {
                LazyVStack(alignment: .leading, spacing: 14, pinnedViews: [.sectionHeaders]) {
                    ForEach(days) { day in
                        Section {
                            ForEach(day.rows) { r in
                                VStack(alignment: .leading, spacing: 6) {
                                    if let s = r.stamp { Text(s).font(AT.mono(11)).foregroundStyle(muted).frame(maxWidth: .infinity, alignment: r.his ? .trailing : .leading) }
                                    TalkRowView(row: r).equatable()
                                }
                                .opacity(r.current ? 1 : open)
                                .id(r.id)
                                .transition(reduce ? .opacity : .opacity.combined(with: .offset(y: 6)))
                                .contextMenu { Button("拷贝", systemImage: "doc.on.doc") { UIPasteboard.general.string = r.text } }
                            }
                        } header: {
                            Text(day.title).font(.system(size: 13, weight: .semibold)).foregroundStyle(muted)
                                .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 6).padding(.horizontal, 32)
                                .background(navy.opacity(0.96)).padding(.horizontal, -32)
                                .opacity(open)
                        }
                    }
                }
                .padding(.horizontal, 32)
                .animation(.easeOut(duration: 0.25), value: ui.panelOpen)
            }
            .defaultScrollAnchor(.bottom)
            .simultaneousGesture(DragGesture(minimumDistance: 0).onChanged { _ in engine.holdLinger(true) }.onEnded { _ in engine.holdLinger(false) })
            .onChange(of: ui.scrollTo) { _, id in
                guard let id else { return }
                proxy.scrollTo(id, anchor: UnitPoint(x: 0.5, y: 0.1)); ui.scrollTo = nil       // a little under the top, so the day's header does not cover the time above the row
            }
            // a lazy stack's height is an estimate until its rows are seen: opening lands on the real bottom
            .onChange(of: ui.panelOpen) { _, on in
                guard on, let last = days.last?.rows.last?.id else { return }
                DispatchQueue.main.async { proxy.scrollTo(last, anchor: .bottom) }
            }
        }
        .mask(VStack(spacing: 0) {
            LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: 24 * (1 - open))
            Color.black
        })
        .frame(width: W, height: max(0, bottom - top))
        .offset(y: top)
    }

    /// Until the panel has opened by itself once, a copy of the talk's rows (unseen) tells whether they are taller than the place under the dial.
    @ViewBuilder private var measure: some View {
        if ui.talking && !ui.panelOpen && !engine.autoOpenSpent {
            VStack(alignment: .leading, spacing: 14) { ForEach(ui.liveTurns) { TalkRowView(row: TalkStream.row($0)).equatable() } }
                .padding(.horizontal, 32).frame(width: W, alignment: .topLeading).fixedSize(horizontal: false, vertical: true)
                .onGeometryChange(for: Double.self) { Double($0.size.height) } action: { engine.measureLive($0) }
                .hidden().allowsHitTesting(false)
        }
    }
}
