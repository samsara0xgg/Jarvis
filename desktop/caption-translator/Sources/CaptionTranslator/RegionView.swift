import SwiftUI

struct RegionView: View {
    let image: CGImage
    let initialRegion: CGRect
    let confirm: (CGRect) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var selection: CGRect?

    private var chosen: CGRect { selection ?? initialRegion }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("框选字幕区域").font(.title2.weight(.semibold))
            Text("在预览里拖出一个框，只包含英文字幕。窗口移动后仍会跟随捕获。")
                .foregroundStyle(.secondary)
            GeometryReader { geometry in
                let ratio = CGFloat(image.width) / CGFloat(image.height)
                let width = min(geometry.size.width, geometry.size.height * ratio)
                let height = width / ratio
                let offset = CGPoint(x: (geometry.size.width - width) / 2, y: (geometry.size.height - height) / 2)
                ZStack(alignment: .topLeading) {
                    Color.black.opacity(0.05)
                    ZStack(alignment: .topLeading) {
                        Image(decorative: image, scale: 1).resizable().frame(width: width, height: height)
                        Path { path in
                            path.addRect(CGRect(x: 0, y: 0, width: width, height: height))
                            path.addRect(CGRect(x: chosen.minX * width, y: chosen.minY * height,
                                                width: chosen.width * width, height: chosen.height * height))
                        }.fill(Color.black.opacity(0.48), style: FillStyle(eoFill: true))
                        Rectangle().stroke(.white, lineWidth: 2)
                            .background(Color.accentColor.opacity(0.08))
                            .frame(width: chosen.width * width, height: chosen.height * height)
                            .offset(x: chosen.minX * width, y: chosen.minY * height)
                    }
                    .frame(width: width, height: height)
                    .contentShape(Rectangle())
                    .gesture(DragGesture(minimumDistance: 2).onChanged { drag in
                        let x1 = min(max(drag.startLocation.x / width, 0), 1)
                        let y1 = min(max(drag.startLocation.y / height, 0), 1)
                        let x2 = min(max(drag.location.x / width, 0), 1)
                        let y2 = min(max(drag.location.y / height, 0), 1)
                        selection = CGRect(x: min(x1, x2), y: min(y1, y2), width: abs(x2 - x1), height: abs(y2 - y1))
                    })
                    .offset(x: offset.x, y: offset.y)
                    .accessibilityLabel("窗口预览，拖动框选字幕；也可使用整个窗口")
                }
            }.frame(minHeight: 250)
            HStack {
                Button("使用整个窗口") { selection = CGRect(x: 0, y: 0, width: 1, height: 1) }
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("开始翻译") { confirm(chosen) }
                    .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    .disabled(chosen.width < 0.03 || chosen.height < 0.03)
            }
        }.padding(24).frame(width: 760, height: 560)
    }
}
