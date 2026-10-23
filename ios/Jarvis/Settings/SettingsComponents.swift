// words: legacy
import SwiftUI

// MARK: - Night palette (the prototype's `--ink --muted --faint --line` and `--set-*`)

enum SetColor {
    private static func rgb(_ r: Double, _ g: Double, _ b: Double, _ a: Double = 1) -> Color {
        Color(red: r / 255, green: g / 255, blue: b / 255, opacity: a)
    }
    static let ink = Look.ink
    static let muted = Look.ink2
    static let faint = Look.ink3
    static let line = Look.line
    static let bg = rgb(14, 15, 30)
    static let cell = rgb(28, 30, 50)
    static let hi = Color.white.opacity(0.07)
    static let link = rgb(201, 214, 255)
    static let red = Look.red
    static let on = rgb(48, 209, 88)
    static let mint = rgb(139, 230, 194)
    static let glow = Look.glow
    static func color(_ r: Double, _ g: Double, _ b: Double, _ a: Double = 1) -> Color { rgb(r, g, b, a) }
}

enum SetMotion {
    /// The prototype's `--ease-out`, cubic-bezier(.16, 1, .3, 1).
    static func easeOut(_ duration: Double) -> Animation { .timingCurve(0.16, 1, 0.3, 1, duration: duration) }
}

/// A stroked polyline in a `box`-sized coordinate space (the prototype's small SVG icons).
struct Polyline: Shape {
    let points: [(Double, Double)]
    let box: CGSize

    func path(in rect: CGRect) -> Path {
        var p = Path()
        let pts = points.map { CGPoint(x: rect.minX + $0.0 / box.width * rect.width, y: rect.minY + $0.1 / box.height * rect.height) }
        p.move(to: pts[0])
        pts.dropFirst().forEach { p.addLine(to: $0) }
        return p
    }

    static let chevronRight = Polyline(points: [(1, 1), (7, 7), (1, 13)], box: CGSize(width: 8, height: 14))
    static let chevronLeft = Polyline(points: [(10, 2), (2, 10), (10, 18)], box: CGSize(width: 12, height: 20))
    static let check = Polyline(points: [(3.5, 9.5), (6.9, 12.9), (14.5, 4.9)], box: CGSize(width: 18, height: 18))
}

/// Row that darkens while pressed, like the prototype's `button.set-row:active`.
struct RowPressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(configuration.isPressed ? SetColor.hi : .clear)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

// MARK: - Inset grouped sections

struct SetSection<Content: View>: View {
    var header: String?
    var footer: String?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let header {
                Text(header).font(.footnote).foregroundStyle(SetColor.muted)
                    .padding(.horizontal, 32).padding(.bottom, 6)
            }
            SetGroup { content }
            if let footer {
                Text(footer).font(.footnote).foregroundStyle(SetColor.muted)
                    .padding(.horizontal, 32).padding(.top, 8)
            }
        }
        .padding(.top, 24)
    }
}

/// Rounded cell with hairlines between its rows.
struct SetGroup<Content: View>: View {
    @ViewBuilder var content: Content
    @Environment(\.displayScale) private var scale

    var body: some View {
        VStack(spacing: 0) {
            Group(subviews: content) { rows in
                ForEach(Array(rows.enumerated()), id: \.element.id) { i, row in
                    row.overlay(alignment: .top) {
                        if i > 0 {
                            Rectangle().fill(SetColor.line).frame(height: 1 / scale).padding(.leading, 16)
                        }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity)
        .background(SetColor.cell)
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .padding(.horizontal, 16)
    }
}

// MARK: - Rows

struct InfoRow: View {
    let label: String
    let detail: String

    var body: some View {
        HStack(spacing: 12) {
            Text(label).padding(.vertical, 11)
            Spacer(minLength: 0)
            Text(detail).font(.subheadline).foregroundStyle(SetColor.muted).lineLimit(1)
        }
        .font(.body)
        .padding(.horizontal, 16)
        .frame(minHeight: 44)
        .accessibilityElement(children: .combine)
    }
}

/// A row that goes somewhere: label, optional detail, chevron.
struct NavRow: View {
    let label: String
    var detail: String?
    var action: () -> Void = {}

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Text(label).padding(.vertical, 11)
                Spacer(minLength: 0)
                if let detail { Text(detail).font(.subheadline).foregroundStyle(SetColor.muted).lineLimit(1) }
                Polyline.chevronRight
                    .stroke(SetColor.faint, style: StrokeStyle(lineWidth: 2.2, lineCap: .round, lineJoin: .round))
                    .frame(width: 8, height: 14)
                    .padding(.leading, -4).padding(.trailing, -2)
            }
            .font(.body)
            .padding(.horizontal, 16)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(RowPressStyle())
    }
}

/// The whole row toggles, like the prototype; the switch itself is the native one (tinted), just not hit-testable.
struct SwitchRow: View {
    let title: String
    var detail: String?
    @Binding var isOn: Bool

    var body: some View {
        Button {
            withAnimation(.spring(duration: 0.4, bounce: 0.15)) { isOn.toggle() }
        } label: {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 0) {
                    Text(title).font(.body)
                    if let detail {
                        Text(isOn ? detail : "已关闭").font(.subheadline)
                            .foregroundStyle(isOn ? SetColor.muted : SetColor.faint)
                            .animation(.easeInOut(duration: 0.25), value: isOn)
                    }
                }
                .padding(.vertical, detail == nil ? 11 : 8)
                Spacer(minLength: 0)
                Toggle(title, isOn: $isOn).labelsHidden().tint(SetColor.on).allowsHitTesting(false)
            }
            .padding(.horizontal, 16)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(RowPressStyle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue(isOn ? "开" : "关")
        .accessibilityAddTraits(.isToggle)
    }
}

/// Label on the left, the chosen option on the right; tapping opens the system menu of `options` (value, shown text).
struct MenuRow: View {
    let title: String
    let options: [(value: String, text: String)]
    let selection: String
    let pick: (String) -> Void

    var body: some View {
        Menu {
            ForEach(options, id: \.value) { o in
                Button { pick(o.value) } label: {
                    if o.value == selection { Label(o.text, systemImage: "checkmark") } else { Text(o.text) }
                }
            }
        } label: {
            HStack(spacing: 12) {
                Text(title).padding(.vertical, 11)
                Spacer(minLength: 0)
                Text(options.first { $0.value == selection }?.text ?? selection).font(.subheadline).foregroundStyle(SetColor.muted).lineLimit(1)
                Image(systemName: "chevron.up.chevron.down").font(.system(size: 12, weight: .semibold)).foregroundStyle(SetColor.faint)
            }
            .font(.body)
            .foregroundStyle(SetColor.ink)
            .padding(.horizontal, 16)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(RowPressStyle())
    }
}

/// 64 high: title line, then a 44 high track row with the end labels or icons.
struct SliderRow<Lo: View, Hi: View>: View {
    let title: String
    @Binding var value: Double
    @ViewBuilder var lo: Lo
    @ViewBuilder var hi: Hi

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title).font(.body)
            HStack(spacing: 10) {
                lo
                Slider(value: $value, in: 0...100).tint(SetColor.link).accessibilityLabel(title)
                hi
            }
            .font(.subheadline)
            .foregroundStyle(SetColor.muted)
            .frame(height: 44)
        }
        .padding(.horizontal, 16)
        .padding(.top, 9)
    }
}

/// Segmented control, 210 x 32, with the prototype's light thumb (the native one cannot be recoloured per view).
struct SegmentedRow: View {
    let title: String
    let options: [String]
    @Binding var selection: Int

    private let width = 210.0, inset = 2.0

    var body: some View {
        let seg = (width - 2 * inset) / Double(options.count)
        HStack(spacing: 12) {
            Text(title).font(.body).padding(.vertical, 11)
            Spacer(minLength: 0)
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 7)
                    .fill(SetColor.color(238, 241, 255))
                    .shadow(color: .black.opacity(0.14), radius: 4, y: 3)
                    .frame(width: seg, height: 28)
                    .offset(x: inset + Double(selection) * seg)
                HStack(spacing: 0) {
                    ForEach(options.indices, id: \.self) { i in
                        Button { selection = i } label: {
                            Text(options[i]).font(.system(size: 14, weight: .medium)).lineLimit(1)
                                .foregroundStyle(i == selection ? SetColor.color(11, 13, 26) : SetColor.color(214, 220, 246, 0.78))
                                .frame(width: seg, height: 32)
                                .contentShape(Rectangle().inset(by: -6))
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(i == selection ? .isSelected : [])
                    }
                }
                .padding(.horizontal, inset)
            }
            .frame(width: width, height: 32)
            .background(SetColor.color(124, 132, 196, 0.2), in: RoundedRectangle(cornerRadius: 9))
            .animation(.spring(duration: 0.45, bounce: 0.12), value: selection)
            .accessibilityElement(children: .contain)
            .accessibilityLabel(title)
        }
        .padding(.horizontal, 16)
        .frame(minHeight: 44)
    }
}

/// 删除今天的记录: swaps its content in place (idle, ask, done, idle) and grows or shrinks to fit.
struct DeleteRow: View {
    let count: Int
    var onDelete: () -> Void

    private enum Phase { case idle, ask, done }
    @State private var phase = Phase.idle

    var body: some View {
        VStack(spacing: 0) {
            switch phase {
            case .idle:
                Button { go(.ask) } label: {
                    Text("删除今天的记录").font(.body).foregroundStyle(SetColor.red)
                        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                        .padding(.horizontal, 16)
                        .contentShape(Rectangle())
                }
                .buttonStyle(RowPressStyle())
                .transition(Self.fadeIn)
            case .ask:
                VStack(alignment: .leading, spacing: 14) {
                    Text("删掉今天手机报的 \(count) 条记录？").font(.body)
                    HStack(spacing: 10) {
                        AskButton(title: "取消", fill: .white.opacity(0.1), ink: SetColor.ink) { go(.idle) }
                        AskButton(title: "删除", fill: SetColor.red, ink: SetColor.color(29, 7, 9)) {
                            onDelete()
                            go(.done)
                        }
                    }
                }
                .padding(EdgeInsets(top: 14, leading: 16, bottom: 16, trailing: 16))
                .accessibilityElement(children: .contain)
                .accessibilityLabel("确认删除")
                .transition(Self.fadeIn)
            case .done:
                HStack(spacing: 8) {
                    Polyline.check
                        .stroke(SetColor.mint, style: StrokeStyle(lineWidth: 2.2, lineCap: .round, lineJoin: .round))
                        .frame(width: 18, height: 18)
                    Text("已删除").font(.body)
                }
                .foregroundStyle(SetColor.mint)
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .padding(.horizontal, 16)
                .transition(Self.fadeIn)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .clipped()
        .task(id: phase) {
            guard phase == .done else { return }
            try? await Task.sleep(for: .seconds(1.4))
            if !Task.isCancelled { go(.idle) }
        }
    }

    /// New content fades in over .3 s; the old content is gone at once.
    private static let fadeIn = AnyTransition.asymmetric(
        insertion: .opacity.animation(.easeInOut(duration: 0.3)), removal: .identity)

    private func go(_ next: Phase) {
        withAnimation(SetMotion.easeOut(0.4)) { phase = next }
    }
}

private struct AskButton: View {
    let title: String
    let fill: Color
    let ink: Color
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title).font(.body.weight(.semibold)).foregroundStyle(ink)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(fill, in: RoundedRectangle(cornerRadius: 12))
        }
        .buttonStyle(AskPressStyle())
    }
}

private struct AskPressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .brightness(configuration.isPressed ? -0.12 : 0)
            .animation(SetMotion.easeOut(0.2), value: configuration.isPressed)
    }
}
