import SwiftUI

// 理一下: a capsule in the header's top row, on today, that has the brain tidy the day (`POST /inherent/day/tidy`, ADR 0310). The brain answers when a model has written the doing lane afresh,
// which takes up to about a minute; meanwhile the list stays usable, the capsule says 在理…, and a band of light goes along the bar's pieces. When a new run is written the days are asked for
// again by the same way every change of the day is (`TLBrain.refetch`): the list grows what is new and collapses what went, as it does for anything else, and the bar fades to its new drawing
// (Sky/DayTimelineTop.swift keeps the old one over it until the next reading is in).

private let ink = SetColor.ink

/// what the button says
enum TLTidy: Equatable {
    case idle, running, nothing, failed

    var words: String {
        switch self {
        case .idle: "理一下"
        case .running: "在理…"
        case .nothing: "没有要改的"
        case .failed: "没理成，等会儿再试"
        }
    }

    /// what the button comes to once the brain has answered: a new run is written and the button is as it was; the others say so for a while (`TimelineState.tidyHolds`)
    static func after(_ answer: TidyAnswer) -> TLTidy {
        switch answer {
        case .written: .idle
        case .unchanged: .nothing
        case .failed: .failed
        }
    }
}

extension TimelineState {
    /// the button is there on today, with a brain to ask (a debug build's made-up day stands in for one)
    var tidyShown: Bool {
        guard day == today else { return false }
        #if DEBUG
        if fixture != nil { return true }
        #endif
        return BrainLink.shared.hasToken
    }

    /// A tap: the brain is asked once (a second tap while it works does nothing), and what it answers is the button's words for a while, or, when a run was written, the days asked for again.
    /// The answer is the state's and not the screen's: a day swiped to, or the timeline left, in the meantime changes nothing of it.
    func tidyDay() {
        guard tidy != .running else { return }
        tidyHold?.cancel(); tidyHold = nil
        tidied = nil
        Haptic.impact(.light)
        withAnimation(.easeOut(duration: 0.2)) { tidy = .running }
        Task { @MainActor [self] in
            let answer = await brain.tidy()
            if answer == .written, day == today { tidied = reply?.serial }
            let next = TLTidy.after(answer)
            withAnimation(.easeOut(duration: 0.2)) { tidy = next }
            switch answer {
            case .written:
                Haptic.notify(.success)
                brain.refetch()
            case .unchanged, .failed:
                if answer == .failed { Haptic.notify(.warning) }
                let seconds = next == .nothing ? tidyHolds.nothing : tidyHolds.failed
                tidyHold = Task { @MainActor [self] in
                    try? await Task.sleep(nanoseconds: UInt64(seconds * 1e9))
                    if !Task.isCancelled { withAnimation(.easeOut(duration: 0.2)) { tidy = .idle } }
                }
            }
        }
    }
}

/// the capsule: the look of 今天 beside it, with sparkles that pulse while the brain works (not under Reduce Motion: the words alone)
struct TLTidyButton: View {
    let tl: TimelineState
    @Environment(\.accessibilityReduceMotion) private var reduce

    static func hasIcon(_ s: TLTidy) -> Bool { s == .idle || s == .running }
    /// how wide the capsule is for what it says, which the top row takes from the weather's room
    static func width(_ s: TLTidy) -> Double { 18 + (hasIcon(s) ? 15 : 0) + tlWidth(s.words, size: 12, weight: .semibold) }

    var body: some View {
        let s = tl.tidy, running = s == .running
        Button { tl.tidyDay() } label: {
            ZStack {
                HStack(spacing: 4) {
                    if Self.hasIcon(s) {
                        Image(systemName: "sparkles").font(.system(size: 11, weight: .semibold))
                            .symbolEffect(.pulse, options: .repeating, isActive: running && !reduce)
                    }
                    Text(s.words).font(.system(size: 12, weight: .semibold)).lineLimit(1)
                }
                .id(s).transition(TLMotion.swapWords(reduce: reduce))
            }
            .fixedSize()
            .foregroundStyle(ink.opacity(0.9)).padding(.horizontal, 9).frame(height: 22)
            .background(Color.white.opacity(0.1), in: Capsule())
            .contentShape(Rectangle().inset(by: -9))
        }
        .buttonStyle(PressScale())
        .animation(.easeOut(duration: 0.2), value: s)
        .accessibilityLabel(running ? "正在整理" : "理一下这一天")
        .accessibilityValue(s == .nothing || s == .failed ? s.words : "")
    }
}

/// A soft band of light along the bar while the brain works, drawn only over the pieces (the track and the holes stay as they are). It is a fixed gradient that slides: a pass takes 1.6 s,
/// easing in and out, and the bar rests a moment before the next, the band going on past the end for the rest. It goes when the view does (a fade, in the bar).
struct TLTidySweep: View {
    let scene: TLBarScene
    @State private var going = false
    private let band = 130.0, period = 2.2, sweep = 1.6

    var body: some View {
        let rects = TLGeo.rects(scene.pieces, span: scene.span, width: TLLayout.barW, height: TLLayout.barH)
        let lit = zip(scene.pieces, rects).filter { TLPaint.look($0.0.fill) != nil }.map(\.1)
        LinearGradient(colors: [.clear, Color.white.opacity(0.4), .clear], startPoint: .leading, endPoint: .trailing)
            .frame(width: band, height: TLLayout.barH)
            .offset(x: going ? -band + (TLLayout.barW + band) * period / sweep : -band)
            .frame(width: TLLayout.barW, height: TLLayout.barH, alignment: .leading)
            .mask {
                Canvas { ctx, _ in for r in lit { ctx.fill(Path(r), with: .color(.black)) } }
                    .frame(width: TLLayout.barW, height: TLLayout.barH)
            }
            .onAppear { withAnimation(.easeInOut(duration: period).repeatForever(autoreverses: false)) { going = true } }
            .allowsHitTesting(false).accessibilityHidden(true)
    }
}
