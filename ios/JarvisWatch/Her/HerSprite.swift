import SwiftUI

/// Her faces on the watch, as the Mac names them: rest 平时, listen 35 在听, think 30 在想, ask 等你, talk 39 在说, doze 睡.
enum HerFace: String, CaseIterable { case rest, listen, think, ask, talk, doze }

/// Her, drawn from frames rendered from the Mac's own code (ios/Tools/her_frames). `size` is the ball's diameter in points.
/// Placeholder until the frames land: a plain disc of her size.
struct HerSprite: View {
    var face: HerFace
    var size: CGFloat
    var paused = false
    var body: some View { Circle().fill(Color(white: 0.12)).frame(width: size, height: size) }
}
