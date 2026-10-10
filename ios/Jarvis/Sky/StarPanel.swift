import SwiftUI

// The rows of the talk under the dial: his words in a bubble on the right, hers plain on the left. One style at every height of the panel,
// so what he is reading does not change size or place when more is shown above it.

private let ink = SetColor.ink
private let muted = SetColor.muted

struct TalkRowView: View {
    let turn: LiveTurn

    var body: some View {
        if turn.his { bubble } else { her }
    }

    private var bubble: some View {
        VStack(alignment: .trailing, spacing: 8) {
            if !turn.pics.isEmpty { SentItemsRow(items: turn.pics) }
            if !turn.text.isEmpty {
                Text(turn.text).font(.system(size: 16)).foregroundStyle(ink.opacity(0.86))
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .background(RoundedRectangle(cornerRadius: 18).fill(Color.white.opacity(0.08)))
                    .overlay(RoundedRectangle(cornerRadius: 18).stroke(Color.white.opacity(0.06), lineWidth: 0.5))
                    .frame(maxWidth: 300, alignment: .trailing)
            }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
    }

    private var her: some View {
        words.font(.system(size: turn.leadIn ? 15 : 18)).lineSpacing(turn.leadIn ? 3 : 5)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// her words with each character at its own brightness while she says them: runs of one brightness are one piece of text
    private var words: Text {
        let base = turn.leadIn ? muted : ink.opacity(0.92)
        guard turn.alphas.count == turn.text.count, turn.alphas.contains(where: { $0 < 1 }) else { return Text(turn.text).foregroundStyle(base) }
        var out = AttributedString()
        let chars = Array(turn.text)
        var i = 0
        while i < chars.count {
            var j = i
            while j < chars.count && turn.alphas[j] == turn.alphas[i] { j += 1 }
            var run = AttributedString(String(chars[i..<j]))
            run.foregroundColor = Color.white.opacity(turn.alphas[i])
            out += run
            i = j
        }
        return Text(out)
    }
}

/// The talk on show under the dial: the turns so far, the newest at the bottom, following the words as they come until he scrolls away.
struct LiveTalk: View {
    let ui: StarUI
    let touch: (Bool) -> Void

    var body: some View {
        ScrollView(showsIndicators: false) {
            VStack(alignment: .leading, spacing: 14) {
                ForEach(ui.liveTurns) { t in
                    TalkRowView(turn: t).transition(.opacity.combined(with: .offset(y: 6)))
                }
            }
            .padding(.horizontal, 32)
        }
        .defaultScrollAnchor(.bottom)
        .simultaneousGesture(DragGesture(minimumDistance: 0).onChanged { _ in touch(true) }.onEnded { _ in touch(false) })
        .mask(VStack(spacing: 0) {
            LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom).frame(height: 24)
            Color.black
        })
        .frame(width: W, height: 296)
        .opacity(ui.talking ? 1 : 0)
        .allowsHitTesting(ui.talking)
        .animation(.easeOut(duration: ui.talking ? 0.25 : 0.35), value: ui.talking)
        .offset(y: 540)
    }
}
