import SwiftUI

/// Her faces on the watch, as the Mac names them: rest 平时, listen 35 在听, think 30 在想, ask 等你, talk 39 在说, doze 睡.
enum HerFace: String, CaseIterable { case rest, listen, think, ask, talk, doze }

/// Her, played from frames rendered out of the Mac's own code (ios/Tools/her_frames, table in HerFrames.swift).
/// `size` is the ball's diameter in points; the frames carry a margin for her eyes and sparks, which overflows
/// the `size` square without taking layout space. Only the current face's frames are loaded.
struct HerSprite: View {
    var face: HerFace
    var size: CGFloat
    var paused = false
    @State private var from: HerFace?
    @State private var fadeStart = Date.distantPast
    private let fade = 0.2
    private static let base: CGFloat = 80                // her biggest on the watch

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / (from == nil ? face.clip.fps : 30), paused: paused)) { tl in
            let k = min(1, tl.date.timeIntervalSince(fadeStart) / fade)
            ZStack {
                if let from, k < 1 { frame(from, tl.date).opacity(1 - k) }
                frame(face, tl.date).opacity(from == nil ? 1 : k)
            }
            .transaction { $0.animation = nil }          // a new frame or face is never animated (an animated image change is a crossfade left behind where she was)
        }
        // drawn at one size and scaled: while she moves and resizes, the scale is what animates, and the frames stay one picture
        .frame(width: Self.base, height: Self.base)
        .scaleEffect(size / Self.base)
        .frame(width: size, height: size)
        .onChange(of: face) { old, _ in from = old; fadeStart = .now }
        .task(id: face) { try? await Task.sleep(for: .seconds(fade)); from = nil }
    }

    private func frame(_ f: HerFace, _ date: Date) -> some View {
        // the remainder is taken in Double: a real watch is arm64_32, where Int is 32-bit and the frames since 2001 (about 6.5e9 at 8 fps) do not fit
        let c = f.clip, i = Int((date.timeIntervalSinceReferenceDate * c.fps).truncatingRemainder(dividingBy: Double(c.frames)))
        return Image("her-\(f.rawValue)-\(String(format: "%02d", i))")
            .resizable().interpolation(.high)
            .frame(width: Self.base * c.margin, height: Self.base * c.margin)
            .allowsHitTesting(false)
    }
}
