import SwiftUI

// Her hold menu (a fan of tiles) is gone from the phone: a hold on her, or a touch that slides off her, opens 聊天 with the keyboard up (Sky/StarFlow.swift). What is left here is what the
// iPad's menu (Pad/PadHer.swift) wears, and the few names that files outside this one's area still call.

enum TLArc {
    static let tile = 52.0, corner = 14.0
    static let slop = 4.0                              // a finger this far off a tile is still on it
}

/// Kept only for the type of `TimelineState.arcHover` / `arcPicked` (Sky/DayTimeline.swift); goes with them
enum TLHerMenu {
    enum Kind { case type, tidy, today }
}

extension TimelineState {
    /// No-ops, kept for the callers outside this area (`letGo` in Sky/DayTimeline.swift, the gaze in Sky/DayTimelineGaze.swift, the debug scene 她菜单); delete with them
    static let arcPulse = 0.14, arcBack = 0.3          // the iPad's menu (Pad/PadHer.swift) times its tiles by these: how long the chosen tile pulses, and how long they are on screen after they start back
    func closeArc() {}
    func openArc(holding: Bool) {}
    var arcHoverAt: CGPoint? { nil }
}

/// One of her cards: the symbol on top, the word under it, on the chat's dark frosted ground. The iPad's menu (Pad/PadHer.swift) wears the same tiles.
struct TLHerTile: View {
    let title: String
    let symbol: String
    var enabled = true
    var lit = false
    var working = false                 // what it starts is under way: the symbol pulses
    private static let navy = Color(red: 8 / 255, green: 10 / 255, blue: 24 / 255)      // as the chat's tapback bar (Sky/StarTapback.swift)

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: TLArc.corner, style: .continuous)
        VStack(spacing: 4) {
            Image(systemName: symbol).font(.system(size: 18)).symbolEffect(.pulse, options: .repeating, isActive: working)
            Text(title).font(.system(size: 10, weight: .medium)).lineLimit(1).minimumScaleFactor(0.8)
        }
        .foregroundStyle(SetColor.ink.opacity(enabled ? 0.95 : 0.45))
        .frame(width: TLArc.tile, height: TLArc.tile)
        .background(shape.fill(Color.white.opacity(lit ? 0.14 : 0)))
        .background(shape.fill(.ultraThinMaterial)).background(shape.fill(Self.navy.opacity(0.8)))
        .overlay(shape.strokeBorder(Color.white.opacity(lit ? 0.4 : 0.14), lineWidth: 0.5))
    }
}
