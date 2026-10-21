// words: legacy
import SwiftUI
import UIKit

// A long press on a message of the talk brings up the reactions bar, as iMessage's tapbacks do (ADR 0256): the six standard ones and a + for any emoji, the message itself
// lifted under the bar, and 拷贝 under it. The bar is not placed at the row's own spot: that would need the row's frame, and a geometry callback per row is what froze long
// histories. The message is shown again under the bar instead.

private let ink = SetColor.ink
private let muted = SetColor.muted
private let navy = Color(red: 8 / 255, green: 10 / 255, blue: 24 / 255)

extension StarEngine {
    func openTapback(_ row: TalkRow) {
        wake()
        Haptic.impact(.medium)
        ui.tapbackPicker = false
        ui.tapback = row
    }
    func closeTapback() {
        ui.tapbackPicker = false
        ui.tapback = nil
    }
    /// His pick on `row`: the same one again takes it back, another replaces it. Shown at once; the brain's refusal takes it back with a note.
    func react(_ emoji: String, on row: TalkRow) {
        guard let id = row.recordId else { return }
        closeTapback()
        Haptic.impact(.light)
        Task { @MainActor in
            if !(await Reactions.shared.pick(emoji, on: id, shown: row.reactions.allen)) { trouble("表情没发出去") }
        }
    }
}

/// What a long press on a row of the talk does, and what VoiceOver offers on it instead: the reactions where the row can have them, and 拷贝 where it has words.
struct RowPress: ViewModifier {
    let row: TalkRow
    let engine: StarEngine

    func body(content: Content) -> some View {
        let copy = TalkStream.copyText(row), reactable = TalkStream.reactable(row)
        content
            .gesture(LongPressGesture(minimumDuration: 0.35).onEnded { _ in engine.openTapback(row) }, including: copy != nil || reactable ? .all : .subviews)
            .accessibilityActions {
                if reactable { ForEach(Tapback.standard, id: \.self) { e in Button("回应 \(e)") { engine.react(e, on: row) } } }
                if let t = copy { Button("拷贝") { UIPasteboard.general.string = t } }
            }
    }
}

/// The bar over the whole screen while `ui.tapback` is set.
struct TapbackOverlay: View {
    let engine: StarEngine
    let ui: StarUI

    var body: some View {
        if let row = ui.tapback { TapbackCard(engine: engine, ui: ui, row: row).transition(.opacity) }
    }
}

private struct TapbackCard: View {
    let engine: StarEngine
    let ui: StarUI
    let row: TalkRow
    @State private var shown = false
    @Environment(\.accessibilityReduceMotion) private var reduce

    var body: some View {
        let side: HorizontalAlignment = row.his ? .trailing : .leading
        let copy = TalkStream.copyText(row), reactable = TalkStream.reactable(row), picking = ui.tapbackPicker
        ZStack(alignment: .topLeading) {
            Color.black.opacity(0.62).frame(width: W * 3, height: H * 3).offset(x: -W, y: -H)
                .contentShape(Rectangle()).onTapGesture { engine.closeTapback() }
                .accessibilityHidden(true)
            VStack(alignment: side, spacing: 10) {
                if reactable { bar }
                // room inside the clip for the badges, which stand over the row's corner; taken back outside it so the row is as wide as it is in the stream
                CapHeight(cap: picking ? 160 : 320) { TalkRowView(row: row).equatable().padding(.horizontal, 14).padding(.top, 14) }
                    .clipped().padding(.horizontal, -14).allowsHitTesting(false)
                    .accessibilityHidden(true)
                if let t = copy, !picking { menu(t) }
            }
            .padding(.horizontal, 32).frame(width: W, alignment: row.his ? .trailing : .leading)
            .offset(y: 110)
            .scaleEffect(shown || reduce ? 1 : 0.94, anchor: row.his ? .topTrailing : .topLeading)
            .opacity(shown ? 1 : 0)
            EmojiEntry(active: ui.tapbackPicker, pick: { engine.react($0, on: row) }, ended: { ui.tapbackPicker = false })
                .frame(width: 1, height: 1).accessibilityHidden(true)
        }
        .frame(width: W, height: H, alignment: .topLeading)
        .accessibilityAddTraits(.isModal)
        .accessibilityAction(.escape) { engine.closeTapback() }
        .onAppear { withAnimation(reduce ? .easeOut(duration: 0.15) : .spring(response: 0.3, dampingFraction: 0.78)) { shown = true } }
    }

    /// the six tapbacks and a +; the one he has on this message is lit
    private var bar: some View {
        HStack(spacing: 2) {
            ForEach(Tapback.standard, id: \.self) { e in
                Button { engine.react(e, on: row) } label: {
                    Text(e).font(.system(size: 26)).frame(width: 44, height: 44)
                        .background(Circle().fill(Color.white.opacity(row.reactions.allen == e ? 0.22 : 0)))
                        .contentShape(Circle())
                }
                .buttonStyle(PressScale()).accessibilityLabel(row.reactions.allen == e ? "取消 \(e)" : "回应 \(e)")
            }
            Button { ui.tapbackPicker.toggle() } label: {
                Image(systemName: "plus").font(.system(size: 18, weight: .semibold)).foregroundStyle(ink.opacity(0.86)).frame(width: 44, height: 44)
                    .background(Circle().fill(Color.white.opacity(ui.tapbackPicker ? 0.22 : 0.1))).contentShape(Circle())
            }
            .buttonStyle(PressScale()).accessibilityLabel("其他表情")
        }
        .padding(.horizontal, 8).padding(.vertical, 6)
        .background(Capsule().fill(.ultraThinMaterial)).background(Capsule().fill(navy.opacity(0.8)))
        .overlay(Capsule().stroke(Color.white.opacity(0.14), lineWidth: 0.5))
    }

    private func menu(_ text: String) -> some View {
        Button {
            UIPasteboard.general.string = text
            Haptic.selection()
            engine.closeTapback()
        } label: {
            HStack(spacing: 10) {
                Text("拷贝").font(.system(size: 16))
                Spacer(minLength: 16)
                Image(systemName: "doc.on.doc").font(.system(size: 15))
            }
            .foregroundStyle(ink.opacity(0.92)).padding(.horizontal, 16).frame(width: 190, height: 46).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .background(RoundedRectangle(cornerRadius: 14).fill(.ultraThinMaterial)).background(RoundedRectangle(cornerRadius: 14).fill(navy.opacity(0.8)))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.14), lineWidth: 0.5))
    }
}

/// Its one child at its own height, up to `cap` (a frame with only a maximum would take all of it)
private struct CapHeight: Layout {
    let cap: Double
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let s = subviews[0].sizeThatFits(ProposedViewSize(width: proposal.width, height: nil))
        return CGSize(width: s.width, height: min(s.height, cap))
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        subviews[0].place(at: bounds.origin, anchor: .topLeading, proposal: ProposedViewSize(width: bounds.width, height: nil))
    }
}

/// The system's emoji keyboard, for the +: a field nobody sees takes the keyboard, and the first emoji typed is the pick.
private struct EmojiEntry: UIViewRepresentable {
    let active: Bool
    let pick: (String) -> Void
    let ended: () -> Void

    final class Field: UITextField {
        override var textInputContextIdentifier: String? { "jarvis.emoji" }       // a non-nil identifier is what lets the field choose its own keyboard
        override var textInputMode: UITextInputMode? { UITextInputMode.activeInputModes.first { $0.primaryLanguage == "emoji" } ?? super.textInputMode }
    }

    final class Coordinator: NSObject, UITextFieldDelegate {
        var parent: EmojiEntry
        init(_ p: EmojiEntry) { parent = p }
        @objc func changed(_ f: UITextField) {
            let got = Tapback.firstEmoji(in: f.text ?? "")
            f.text = ""
            if let got { parent.pick(got) }
        }
        func textFieldDidEndEditing(_ f: UITextField) { if parent.active { parent.ended() } }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeUIView(context: Context) -> Field {
        let f = Field()
        f.delegate = context.coordinator
        f.addTarget(context.coordinator, action: #selector(Coordinator.changed(_:)), for: .editingChanged)
        f.autocorrectionType = .no; f.spellCheckingType = .no; f.autocapitalizationType = .none
        f.alpha = 0                         // not isHidden: a hidden view cannot take the keyboard
        return f
    }
    func updateUIView(_ f: Field, context: Context) {
        context.coordinator.parent = self
        if active && !f.isFirstResponder { DispatchQueue.main.async { f.becomeFirstResponder() } }
        else if !active && f.isFirstResponder { f.resignFirstResponder() }
    }
}

#if DEBUG
extension StarEngine {
    /// `-scene reactions`: reactions on both sides of the conversation (his on hers, hers on his, both on one, a long answer, the eyes that arrived as a frame after the read),
    /// as the brain's rows would carry them. `-scene reactionsBar` also brings the bar up on her answer, which he has already reacted to; `-scene reactionsBarHis` on his line.
    /// A pick goes to a stand-in that answers yes after a moment. `-scene reactionsLive` is the reaction-only answer: his line, the typing dots, then her frame, and no text row ever.
    func runReactionsScene(_ name: String) {
        setLevel(0)
        let cal = Cal.cur.cal, now = Date()
        func ts(_ minutesAgo: Double) -> String {
            let c = now.addingTimeInterval(-minutesAgo * 60)
            return TalkStream.dayId(c, cal) + "T" + TalkStream.clock(c, cal) + ":00"
        }
        func row(_ n: Int, his: Bool, _ minutes: Double, _ text: String, _ by: [String: String] = [:]) -> ConversationRow {
            ConversationRow(seq: n, source: his ? "allen" : "jarvis", ts: ts(minutes), text: text, id: "r\(n)", reactions: RowReactions(allen: by["allen"], jarvis: by["jarvis"]))
        }
        let heart = Tapback.standard[0], up = Tapback.standard[1], haha = Tapback.standard[3], bang = Tapback.standard[4]
        let doc = "<voice>好，列在下面了。</voice><document>## 本周安排\n- 周五 10:00 周会\n- 周六 09:00 爬山，我会提前看天气\n- 周日 21:00 整理这一周的记忆</document>"
        let rows = [row(1, his: true, 30, "明天几点出门？", ["jarvis": heart]),
                    row(2, his: false, 30, "<voice>九点二十。坐 32 路，路上三十二分钟，可能下雨，带伞。</voice>", ["allen": up]),
                    row(3, his: true, 20, "那后天呢", ["jarvis": bang, "allen": haha]),
                    row(4, his: false, 20, "<voice>后天上午十点你约了牙医，九点钟出门。</voice>", ["allen": heart]),
                    row(5, his: true, 8, "帮我整理一下这周的安排"),
                    row(6, his: false, 8, doc, ["allen": heart]),
                    row(7, his: true, 2, "好的，谢谢")]
        MainActor.assumeIsolated {
            Reactions.shared.send = { _, _ in try? await Task.sleep(nanoseconds: 300_000_000); return true }
            Conversation.shared.debugSet(rows)
            if name != "reactionsLive" { Reactions.shared.apply(ReactionFrame(target: "r7", by: "jarvis", emoji: Tapback.eyes)) }       // a frame, after the read
        }
        openPanel(quiet: true)
        if name == "reactionsLive" {
            // his line is in the talk; the typing dots; then her reaction frame ends the wait quietly: no "她还在想", no failure
            beginTalk(base: 7); lingerHold = true
            let his = addTurn(his: true, "晚上我到家了")
            showStatus()
            if let i = ui.liveTurns.firstIndex(where: { $0.id == his }) { ui.liveTurns[i].recordId = "live1" }
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                MainActor.assumeIsolated { Reactions.shared.apply(ReactionFrame(target: "live1", by: "jarvis", emoji: heart)) }
                self.dropStatus()
            }
            return
        }
        if name == "reactionsBar" || name == "reactionsBarHis" {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
                let days = TalkStream.compose(self.ui.convDays, live: [], overlay: MainActor.assumeIsolated { Reactions.shared.overlay }, cal: cal, now: Date())
                let id = name == "reactionsBar" ? "r4" : "r3"
                if let r = days.flatMap(\.rows).first(where: { $0.recordId == id }) { self.openTapback(r) }
                if UserDefaults.standard.bool(forKey: "tapbackPicker") { self.ui.tapbackPicker = true }
            }
        }
    }
}
#endif
