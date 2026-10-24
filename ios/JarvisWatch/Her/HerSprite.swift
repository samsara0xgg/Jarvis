import SwiftUI

/// Her faces on the watch, as the Mac names them: rest 平时, listen 35 在听, think 30 在想, ask 等你, talk 39 在说, doze 睡.
enum HerFace: String, CaseIterable { case rest, listen, think, ask, talk, doze }

/// Her, played as video rendered out of the Mac's own code (ios/Tools/her_frames, HerVideo.swift). `size` is the ball's diameter in points;
/// the picture carries a margin for her eyes and sparks, which overflows the `size` square without taking layout space.
/// `paused` is Always On, where video does not play: her asleep, one still frame.
struct HerSprite: View {
    var face: HerFace
    var size: CGFloat
    var paused = false
    private static let base: CGFloat = 170               // her biggest on the watch (the home sprite, 236 of 208 pt); BALL in render.cjs
    private static let margin: CGFloat = 272.0 / 170     // FRAME / BALL in render.cjs and video.cjs

    var body: some View {
        Group {
            if paused { Image("her-doze").resizable().interpolation(.high) } else { HerVideoView(face: face) }
        }
        .frame(width: Self.base * Self.margin, height: Self.base * Self.margin)
        .allowsHitTesting(false)
        // drawn at one size and scaled: while she moves and resizes, the scale is what animates, and the picture stays one picture
        .frame(width: Self.base, height: Self.base)
        .scaleEffect(size / Self.base)
        .frame(width: size, height: size)
    }
}
