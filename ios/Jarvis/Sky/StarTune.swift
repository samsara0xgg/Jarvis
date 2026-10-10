import SwiftUI
import UIKit

/// A plain tool for finding the near view's best angle: eight sliders on the camera, live. Drawn in the 440 × 956 design space over the reading zone.
/// Every change reaches the engine's NearParams (setCam reads it each frame), is saved, and wakes the stage so a resting dial redraws.
struct NearTune: View {
    let engine: StarEngine
    let done: () -> Void
    @State private var p: NearParams
    @State private var copied = false

    init(engine: StarEngine, done: @escaping () -> Void) {
        self.engine = engine; self.done = done
        _p = State(initialValue: engine.NEARP)
    }

    private static func preset(_ edit: (inout NearParams) -> Void) -> NearParams { var n = NearParams(); edit(&n); return n }
    private var presets: [(String, NearParams)] {
        [("1c 现在", NearParams()),
         ("原来", Self.preset { $0.X = 215 }),
         ("高视角", Self.preset { $0.gr = 162; $0.gaMin = 60; $0.gh = 90; $0.tr = 142; $0.taMin = 150; $0.foc = 310; $0.X = 220; $0.Y = 280 }),
         ("贴地", Self.preset { $0.gh = 28; $0.foc = 220 })]
    }

    var body: some View {
        VStack(spacing: 7) {
            HStack(spacing: 8) {
                ForEach(presets, id: \.0) { name, v in button(name) { p = v } }
            }
            row("高度", $p.gh, 10...200, "pt")
            row("镜头在现在之前", $p.gaMin, 0...180, "分")
            row("看向现在之后", $p.taMin, 30...360, "分")
            row("镜头离圆心", $p.gr, 60...240, "pt")
            row("看向的半径", $p.tr, 60...240, "pt")
            row("焦距", $p.foc, 120...600, "pt")
            row("左右", $p.X, 100...400, "pt")
            row("上下", $p.Y, 150...500, "pt")
            HStack(spacing: 8) {
                button("恢复默认") { p = NearParams() }
                button(copied ? "已复制" : "复制数值") {
                    UIPasteboard.general.string = String(format: "NearParams gr %g ga %.3f gh %g tr %g ta %.3f foc %g X %g Y %g", p.gr, p.ga, p.gh, p.tr, p.ta, p.foc, p.X, p.Y)
                    copied = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { copied = false }
                }
                button("完成", done)
            }
        }
        .padding(.horizontal, 20).padding(.vertical, 12)
        .frame(width: W, height: 310)
        .background(Color.black.opacity(0.8))
        .foregroundStyle(.white)
        .offset(y: 556)
        .onChange(of: p) { _, n in engine.NEARP = n; n.save(); engine.wake() }
    }

    private func row(_ label: String, _ v: Binding<Double>, _ r: ClosedRange<Double>, _ unit: String) -> some View {
        HStack(spacing: 8) {
            Text(label).font(.system(size: 13)).frame(width: 104, alignment: .leading)
            Slider(value: v, in: r, step: 1)
            Text(String(format: "%.0f", v.wrappedValue) + " " + unit).font(.system(size: 13).monospacedDigit()).frame(width: 52, alignment: .trailing)
        }.frame(height: 24)
    }

    private func button(_ title: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.system(size: 14, weight: .medium)).frame(maxWidth: .infinity).frame(height: 32)
                .background(Color.white.opacity(0.14), in: Capsule())
        }.buttonStyle(.plain)
    }
}
