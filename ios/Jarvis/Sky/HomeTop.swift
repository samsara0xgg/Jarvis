import SwiftUI

// The strip at the top of each page of the bar at the foot (跨端UI标准 §2): a round glass button at the left, the line under her in the middle, a round glass button at the right. She sits
// over its middle (Sky/DayTimelineSeat.swift) and takes no room of it. Every page draws its own strip with these pieces, at the same places, so the date stays put across tabs.
// Design points, as the chrome (Sky/StarChartScreen.swift).

enum HomeTop {
    static let button = 40.0, buttonY = 63.0, side = 20.0      // the round buttons: 40 pt, their tops 63 pt down, 20 pt in from each side
    static let lineY = 116.0                                    // the line under her (13 pt)
}

/// One round glass button of the strip: the system's Liquid Glass where there is one (grey, see-through, no tint), the dial's thin glass before iOS 26.
struct TopGlassButton: View {
    let symbol: String
    let label: String
    var on = true                       // a switch's state (她出不出声): off draws its symbol fainter
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 16, weight: .medium)).foregroundStyle(Look.ink.opacity(on ? 0.92 : 0.6))
                .contentTransition(.symbolEffect(.replace))
                .frame(width: HomeTop.button, height: HomeTop.button)
                .contentShape(Circle())
                .jarvisGlass(Circle())
        }
        .buttonStyle(GlassPress())
        .accessibilityLabel(label)
    }
}

/// press = a slight grow, release = a spring back (§10); the system's interactive glass adds its own light under the finger
struct GlassPress: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.scaleEffect(configuration.isPressed ? 1.08 : 1)
            .animation(configuration.isPressed ? .snappy(duration: 0.15) : .spring(response: 0.35, dampingFraction: 0.6), value: configuration.isPressed)
    }
}

extension View {
    /// the grey glass of the bar at the foot and every floating button: `.glassEffect(.regular.interactive())` with no tint on iOS 26, white 8% and a 0.6 pt rim, no blur, before it
    @ViewBuilder func jarvisGlass<S: Shape>(_ shape: S) -> some View {
        if #available(iOS 26.0, *) {
            glassEffect(.regular.interactive(), in: shape)
        } else {
            background(shape.fill(Color.white.opacity(0.08))).overlay(shape.stroke(Color.white.opacity(0.14), lineWidth: 0.6))
        }
    }
}

/// The strip: `left` and `right` are the buttons (or nothing), `middle` the line under her, centred.
struct TopStrip<Left: View, Middle: View, Right: View>: View {
    @ViewBuilder var left: Left
    @ViewBuilder var middle: Middle
    @ViewBuilder var right: Right

    var body: some View {
        ZStack(alignment: .topLeading) {
            left.offset(x: HomeTop.side, y: HomeTop.buttonY)
            right.offset(x: W - HomeTop.side - HomeTop.button, y: HomeTop.buttonY)
            middle.frame(width: W - 2 * HomeTop.side, alignment: .top).offset(x: HomeTop.side, y: HomeTop.lineY)
        }
        .frame(width: W, height: H, alignment: .topLeading)
    }
}
