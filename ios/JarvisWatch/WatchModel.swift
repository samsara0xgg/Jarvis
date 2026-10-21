import Foundation
import Observation
import WidgetKit

/// Everything the watch shows: the day the phone sent and the talk going on. The link to the phone (WatchLink) writes it; the screens read it and
/// call the closures to act, which the link sets.
@Observable @MainActor
final class WatchModel {
    static let shared = WatchModel()

    enum Talk: Equatable {
        case idle
        case listening(heard: String)                       // his words so far
        case thinking(heard: String)
        case answer(spoken: String, written: String, card: WatchDay.Waiting?)
        case failed(String)
    }

    private(set) var day: WatchDay? = WatchDayStore.load()
    var talk: Talk = .idle
    var phoneReachable = false

    // set by the link to the phone
    @ObservationIgnored var startTalk: () -> Void = {}
    @ObservationIgnored var endTalk: () -> Void = {}
    @ObservationIgnored var cancelTalk: () -> Void = {}
    @ObservationIgnored var answer: (_ id: String, _ action: String) -> Void = { _, _ in }

    /// a new day from the phone: the words follow its language, and the complications draw it
    func adopt(_ d: WatchDay) {
        guard d != day else { return }
        day = d
        if let l = Words.Lang(rawValue: d.language) { Words.shared.set(l) }
        WatchDayStore.save(d)
        WidgetCenter.shared.reloadAllTimelines()
    }

    /// what the phone says of the talk
    func tell(_ t: WatchTell) {
        switch t {
        case .heard(_, let text, _): talk = .listening(heard: text)
        case .thinking: if case .listening(let h) = talk { talk = .thinking(heard: h) } else { talk = .thinking(heard: "") }
        case .reply(_, let spoken, let written, let card): talk = .answer(spoken: spoken, written: written, card: card)
        case .failed(_, let reason): talk = .failed(reason)
        }
    }
}
