import SwiftUI

/// Listening and thinking: she stays in the middle, his words come in under her; a tap ends the listening (or drops the question while she thinks).
struct WatchTalkView: View {
    /// how far over the screen's middle she sits in a talk (WatchRoot draws her); his words are under her
    static let herLift = 34.0
    var heard: String
    var placeholder: String
    var tap: () -> Void

    var body: some View {
        Button(action: tap) {
            Group {
                if heard.isEmpty { Text(placeholder).foregroundStyle(WatchTheme.muted) } else { Text(heard).foregroundStyle(WatchTheme.ink) }
            }
            .font(.system(size: 17, weight: .medium)).multilineTextAlignment(.center).lineLimit(3).minimumScaleFactor(0.8)
            .padding(.horizontal, 10)
            .frame(height: 72, alignment: .top)
            .offset(y: 56)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .handGestureShortcut(.primaryAction)
        .ignoresSafeArea()
    }
}

/// Her answer: she is in the top-right corner with the talk face (WatchRoot draws her), her words under, scrollable; a card she put up has its buttons at the end.
struct WatchAnswerView: View {
    var spoken: String
    var written: String
    var card: WatchDay.Waiting?
    var answer: (_ id: String, _ action: String) -> Void
    var dismiss: () -> Void

    var body: some View {
        // no stack of its own (a new one is a few frames of black before it draws): the home's bar keeps the time out of her corner (WatchRoot draws her there)
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
    }
}

/// a talk that failed: her words for why, a tap puts it away
struct WatchFailedView: View {
    var reason: String
    var dismiss: () -> Void
    var body: some View {
        Button(action: dismiss) {
            Text(reason).font(.system(size: 15)).foregroundStyle(WatchTheme.ink).multilineTextAlignment(.center).lineLimit(4)          // under her, where his words were (WatchRoot draws her)
                .padding(.horizontal, 10)
                .frame(height: 80, alignment: .top)
                .offset(y: 60)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .handGestureShortcut(.primaryAction)
        .ignoresSafeArea()
    }
}
