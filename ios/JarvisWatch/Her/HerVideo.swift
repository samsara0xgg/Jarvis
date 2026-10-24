import AVFoundation
import AVKit
import CoreMedia
import SwiftUI

/// Her faces as 60 fps HEVC loops (ios/Tools/her_frames/video.cjs, HerVideo/her-<face>.mp4), each exactly periodic: frame N-1 leads
/// into frame 0 as any frame into the next. The player decodes and keeps time; each display frame takes its current picture
/// (AVPlayerVideoOutput) and SwiftUI draws it, so there is no player view: SwiftUI's VideoPlayer takes the clock off the screen,
/// and SpriteKit's video node drew nothing in the watch simulator.
@MainActor final class HerLoop {
    static var all: [HerFace: HerLoop] = [:]
    static func of(_ f: HerFace) -> HerLoop { if let l = all[f] { return l }; let l = HerLoop(f); all[f] = l; return l }

    /// `-herVideoPlayer 1` (a hidden setting, also in Release): play in SwiftUI's VideoPlayer instead, drawn by the system, which
    /// takes the clock off the screen; kept to fall back to if pulling the pictures costs the watch more than it can spare.
    static let inPlayer = UserDefaults.standard.bool(forKey: "herVideoPlayer")

    let player: AVPlayer
    private(set) var last: CGImage?                       // the picture last taken, kept while no newer one is due (and while paused)
    private var lastTime = CMTime.invalid
    private static let repeats = 64
    private static let space = CGColorSpace(name: CGColorSpace.sRGB)!

    /// watchOS has no AVQueuePlayer or AVPlayerLooper. The loop is laid end to end `repeats` times in one composition, so the player
    /// runs through every wrap without a seek (each loop is its own closed GOP); only at the composition's end, ten minutes or more on,
    /// does it seek back to the start.
    private init(_ f: HerFace) {
        let asset = AVURLAsset(url: Bundle.main.url(forResource: "her-\(f.rawValue)", withExtension: "mp4")!), loops = AVMutableComposition()
        let one = CMTimeRange(start: .zero, duration: asset.duration)
        for _ in 0..<Self.repeats { try? loops.insertTimeRange(one, of: asset, at: loops.duration) }
        player = AVPlayer(playerItem: AVPlayerItem(asset: loops))
        player.isMuted = true
        player.actionAtItemEnd = .none
        // set before the item is read, as the header asks: BGRA in IOSurface-backed buffers, which a CGImage reads in place
        if !Self.inPlayer {
            let spec = AVVideoOutputSpecification(tagCollections: [.monoscopicForVideoOutput()])
            spec.defaultOutputSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA, kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any]()]
            player.videoOutput = AVPlayerVideoOutput(specification: spec)
        }
        NotificationCenter.default.addObserver(forName: AVPlayerItem.didPlayToEndTimeNotification, object: player.currentItem, queue: .main) { [player] _ in
            player.seek(to: .zero, toleranceBefore: .zero, toleranceAfter: .zero)
        }
    }

    /// The picture due now, or the last one when none is newer. The image reads the decoder's buffer in place (no copy): the buffer
    /// stays locked and held for as long as the image lives.
    func picture() -> CGImage? {
        guard let got = player.videoOutput?.taggedBuffers(forHostTime: CMClockGetTime(.hostTimeClock)), got.presentationTime != lastTime,
              case .pixelBuffer(let pb) = got.taggedBufferGroup.first?.buffer else { return last }
        lastTime = got.presentationTime
        CVPixelBufferLockBaseAddress(pb, .readOnly)
        let rows = CVPixelBufferGetBytesPerRow(pb), h = CVPixelBufferGetHeight(pb)
        guard let base = CVPixelBufferGetBaseAddress(pb),
              let data = CGDataProvider(dataInfo: Unmanaged.passRetained(pb).toOpaque(), data: base, size: rows * h, releaseData: { info, _, _ in
                  let pb = Unmanaged<CVPixelBuffer>.fromOpaque(info!).takeRetainedValue()
                  CVPixelBufferUnlockBaseAddress(pb, .readOnly)
              }) else { CVPixelBufferUnlockBaseAddress(pb, .readOnly); return last }
        last = CGImage(width: CVPixelBufferGetWidth(pb), height: h, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: rows, space: Self.space,
                       bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue),
                       provider: data, decode: nil, shouldInterpolate: false, intent: .defaultIntent) ?? last
        return last
    }
}

/// The shown face plays; on a change the new face fades in over the old, which stays whole under it until it is covered (no dip to black,
/// also while the new face's first picture is still being decoded), then the old one pauses. The video is opaque on black: drawn with
/// plusLighter, the black adds nothing to what lies under her.
struct HerVideoView: View {
    var face: HerFace
    @State private var from: HerFace?
    @State private var fadeStart: Date?
    private let fade = 0.2

    var body: some View {
        Group {
            if HerLoop.inPlayer { VideoPlayer(player: HerLoop.of(face).player).persistentSystemOverlays(.visible) } else {
                TimelineView(.animation) { tl in
                    let im = HerLoop.of(face).picture(), old = from.flatMap { HerLoop.of($0).last }
                    let k = fadeStart.map { min(1, tl.date.timeIntervalSince($0) / fade) } ?? 0
                    ZStack {
                        if let old, k < 1 { Image(decorative: old, scale: 2).resizable() }
                        if let im { Image(decorative: im, scale: 2).resizable().opacity(from == nil ? 1 : k) }
                    }
                    .onChange(of: im != nil) { if from != nil && fadeStart == nil { fadeStart = tl.date } }   // the new face's first picture is in
                }
            }
        }
        .blendMode(.plusLighter)
        .allowsHitTesting(false)                              // a tap goes to her button
        .onAppear { HerLoop.of(face).player.play() }
        .onChange(of: face) { old, new in
            HerLoop.of(new).player.play()
            if HerLoop.inPlayer { HerLoop.of(old).player.pause(); return }
            from = old; fadeStart = HerLoop.of(new).last == nil ? nil : .now
        }
        .task(id: fadeStart) {
            guard let start = fadeStart else { return }
            try? await Task.sleep(for: .seconds(max(0, fade - Date.now.timeIntervalSince(start))))
            if let from, from != face { HerLoop.of(from).player.pause() }
            from = nil; fadeStart = nil
        }
        .onDisappear { for l in HerLoop.all.values { l.player.pause() } }
    }
}
