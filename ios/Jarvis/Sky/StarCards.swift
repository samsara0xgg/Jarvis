import SwiftUI

// What opens when a card the brain waits on is looked at (Brain/Waiting): its words, the fields of an ask card, and the buttons.

/// the card is opened in the short strip under the dial, where the composer is close below: its words get fewer lines than in the drawer
private struct CardCompact: EnvironmentKey { static let defaultValue = false }
extension EnvironmentValues {
    var cardCompact: Bool {
        get { self[CardCompact.self] }
        set { self[CardCompact.self] = newValue }
    }
}

struct WaitingCardView: View {
    let card: WaitingCard
    @Environment(\.cardCompact) private var compact
    private var waiting: Waiting { Waiting.shared }

    var body: some View {
        let busy = waiting.sending.contains(card.id)
        // one field to pick from: the pick is the answer
        let oneTap = card.kind == .clarification && card.fields.count == 1 && !card.fields[0].choices.isEmpty
        VStack(alignment: .leading, spacing: 12) {
            if !card.body.isEmpty { box { Text(card.body).font(.system(size: 14)).lineSpacing(14 * 0.5 - 3).lineLimit(compact ? (card.hint.isEmpty ? 4 : 3) : 12) } }
            if !card.hint.isEmpty { Text("一直允许：" + card.hint).font(AT.mono(11)).foregroundStyle(SetColor.muted).lineLimit(compact ? 1 : 3) }
            ForEach(card.fields, id: \.label) { f in field(f, oneTap: oneTap) }
            if let fail = waiting.failures[card.id] { Text(fail).font(.system(size: 13)).foregroundStyle(SetColor.red) }
            HStack(spacing: 10) {
                ForEach(Array(card.actions.enumerated()), id: \.offset) { i, a in
                    if !(oneTap && a.answer == .fill) { button(a, main: i == card.actions.count - 1) }
                }
            }
        }
        .opacity(busy ? 0.55 : 1).disabled(busy)
    }

    private func button(_ a: CardAction, main: Bool) -> some View {
        Button { waiting.answer(card, a.answer, filled: waiting.inputs[card.id] ?? [:]) } label: {
            Text(a.label).font(.system(size: 15.5, weight: .semibold)).foregroundStyle(main ? AT.navy : SetColor.ink)
                .frame(maxWidth: .infinity).frame(height: 44).background(main ? AT.pale : Color.white.opacity(0.1), in: Capsule())
        }.buttonStyle(PressScale())
    }

    private func box<C: View>(@ViewBuilder _ content: () -> C) -> some View {
        content().foregroundStyle(Color(red: 226 / 255, green: 230 / 255, blue: 250 / 255).opacity(0.84))
            .padding(.horizontal, 14).padding(.vertical, 12).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.08), lineWidth: 0.5))
    }

    private func value(_ label: String) -> Binding<String> {
        Binding(get: { waiting.inputs[card.id]?[label] ?? "" }, set: { waiting.inputs[card.id, default: [:]][label] = $0 })
    }

    @ViewBuilder private func field(_ f: CardField, oneTap: Bool) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(f.label).font(AT.mono(11.5)).tracking(0.5).foregroundStyle(SetColor.muted)
            if f.choices.isEmpty {
                TextField("", text: value(f.label), axis: .vertical).lineLimit(1...4)
                    .font(.system(size: 15)).foregroundStyle(SetColor.ink).tint(AT.pale)
                    .padding(.horizontal, 14).padding(.vertical, 11)
                    .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))
            } else {
                WrapLayout(spacing: 8) {
                    ForEach(f.choices, id: \.self) { c in
                        let on = waiting.inputs[card.id]?[f.label] == c
                        Button {
                            waiting.inputs[card.id, default: [:]][f.label] = c
                            if oneTap { waiting.answer(card, .fill, filled: [f.label: c]) }
                        } label: {
                            Text(c).font(.system(size: 14.5, weight: .medium)).foregroundStyle(on ? AT.navy : SetColor.ink).lineLimit(1).fixedSize()
                                .padding(.horizontal, 14).frame(height: 36).background(on ? AT.pale : Color.white.opacity(0.1), in: Capsule())
                        }.buttonStyle(PressScale())
                    }
                }
            }
        }
    }
}
