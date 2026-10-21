import SwiftUI

/// Listening and thinking: she stays in the middle, his words come in under her; a tap ends the listening (or drops the question while she thinks).
struct WatchTalkView: View {
    var face: HerFace
    var heard: String
    var placeholder: String
    var tap: () -> Void

    var body: some View {
        Button(action: tap) {
            ZStack {
                HerSprite(face: face, size: 80).offset(y: -34)
                Group {
                    if heard.isEmpty { Text(placeholder).foregroundStyle(WatchTheme.muted) } else { Text(heard).foregroundStyle(WatchTheme.ink) }
                }
                .font(.system(size: 17, weight: .medium)).multilineTextAlignment(.center).lineLimit(3).minimumScaleFactor(0.8)
                .padding(.horizontal, 10)
                .frame(height: 72, alignment: .top)
                .offset(y: 56)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .handGestureShortcut(.primaryAction)
    }
}

/// Her answer: she is in the top-right corner with the talk face, her words under, scrollable; a card she put up has its buttons at the end.
struct WatchAnswerView: View {
    var spoken: String
    var written: String
    var card: WatchDay.Waiting?
    var answer: (_ id: String, _ action: String) -> Void
    var dismiss: () -> Void

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text(written.isEmpty ? spoken : written).font(.system(size: 17, weight: .medium)).foregroundStyle(WatchTheme.ink)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if let c = card {
                        HStack(spacing: 6) {
                            ForEach(Array(c.actions.prefix(2).enumerated()), id: \.offset) { i, a in
                                WatchPill(title: a.title, primary: i == 0, fill: WatchTheme.ink) { answer(c.id, a.id) }
                            }
                        }
                    } else {
                        WatchPill(title: Words.Watch.done, primary: false, hint: nil, action: dismiss)
                    }
                }
                .padding(.horizontal, 2)
            }
            .toolbar { WatchCornerHer(face: .talk) }
        }
    }
}

/// a talk that failed: her words for why, a tap puts it away
struct WatchFailedView: View {
    var reason: String
    var dismiss: () -> Void
    var body: some View {
        Button(action: dismiss) {
            VStack(spacing: 10) {
                HerSprite(face: .rest, size: 56)
                Text(reason).font(.system(size: 15)).foregroundStyle(WatchTheme.ink).multilineTextAlignment(.center).lineLimit(4)
            }
            .padding(.horizontal, 10)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .handGestureShortcut(.primaryAction)
    }
}
