import SwiftUI

/// Her faces on the watch, as the Mac names them: rest 平时, listen 35 在听, think 30 在想, ask 等你, talk 39 在说, doze 睡.
enum HerFace: String, CaseIterable { case rest, listen, think, ask, talk, doze }

/// Her, played from frames rendered out of the Mac's own code (ios/Tools/her_frames, table in HerFrames.swift).
/// `size` is the ball's diameter in points; the frames carry a margin for her eyes and sparks, which overflows
/// the `size` square without taking layout space. A frame is decoded when it is drawn and let go after (`image`).
struct HerSprite: View {
    var face: HerFace
    var size: CGFloat
    var paused = false
    @State private var from: HerFace?
    @State private var fadeStart = Date.distantPast
    private let fade = 0.2
    private static let base: CGFloat = 170               // her biggest on the watch (the home sprite, 236 of 208 pt); BALL in render.cjs

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
        return Image(uiImage: Self.image("her-\(f.rawValue)-\(String(format: "%02d", i))"))
            .resizable().interpolation(.high)
            .frame(width: Self.base * c.margin, height: Self.base * c.margin)
            .allowsHitTesting(false)
    }

    /// The frames are data assets, not image sets: the system keeps a named image decoded, and a 30 fps face is 96 frames of
    /// 544 px (110 MB). Only the last few are kept here, for redraws within one frame and the crossfade between two faces.
    private static let recent = { let c = NSCache<NSString, UIImage>(); c.countLimit = 4; return c }()
    private static func image(_ name: String) -> UIImage {
        if let im = recent.object(forKey: name as NSString) { return im }
        let im = NSDataAsset(name: name).flatMap { UIImage(data: $0.data, scale: 2) } ?? UIImage()
        recent.setObject(im, forKey: name as NSString)
        return im
    }
}
