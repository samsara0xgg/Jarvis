import SwiftUI
import Observation

// The pinned top of the phone's 今天 page under the date (跨端UI标准 §3): the place line under the bar, the sideways row of the day's numbers, and what the words under the date make of the
// room below them. Sky/DayTimelineTop.swift lays them out; the bar and its stars are Sky/DayTimelineBar.swift's.

private let ink = Look.ink

// MARK: - the words under the date

/// How far the words under the date push what is below them (the bar, the place line, the chips, and with them the list): her words while she talks, and what is said of the tidying. The
/// talk's own height is measured by its view (Sky/DayTimelineTalk.swift) and the tidy's is one line; nothing is pushed while a finger is on the bar (the readout stands in the date's
/// place), so the bar never moves under a finger.
@MainActor @Observable
final class TLCaption {
    static let shared = TLCaption()
    var talkH = 0.0                 // how tall her place for a talk is now (his heard words, hers, what waits): 0 when there is none
    var tidyH = 0.0                 // and that of the line that says the tidying: one line of 15 pt while it is said
    static let tidyLine = 21.0, gap = 8.0

    /// how far down everything under the date stands, in design points: the room the words take and 8 pt of air; 0 with none
    var shift: Double { talkH > 0 ? talkH + Self.gap : tidyH > 0 ? tidyH + Self.gap : 0 }
}

// MARK: - the place line

/// The line under the bar along the day: a place he keeps in its colour with its name under it where the name fits, a trip as a dotted line, nothing where he was somewhere he does not keep
/// or nothing is known. It uses the bar's own axis, so a place stands under the stretch of the bar it is.
struct TLPlaceLine: View {
    let places: [TLPlaceBit]
    let axis: TLAxis
    private static let nameH = 14.0

    var body: some View {
        ZStack(alignment: .topLeading) {
            ForEach(places) { p in
                let x0 = axis.x(p.t0) + 1, x1 = axis.x(p.t1) - 1
                switch p.kind {
                case .place(let hue, let name):
                    RoundedRectangle(cornerRadius: 2).fill(hue.color(0.9)).frame(width: max(x1 - x0, 2), height: 4).offset(x: x0)
                    if x1 - x0 >= ceil(tlWidth(name, size: 12, weight: .medium)) + 4 {
                        Text(name).font(.system(size: 12, weight: .medium)).foregroundStyle(hue.color).lineLimit(1).fixedSize()
                            .frame(width: x1 - x0, height: Self.nameH).offset(x: x0, y: 9)
                    }
                case .trip:
                    Path { $0.move(to: CGPoint(x: x0, y: 2)); $0.addLine(to: CGPoint(x: max(x1, x0 + 1), y: 2)) }
                        .stroke(Color(red: 235 / 255, green: 235 / 255, blue: 245 / 255).opacity(0.30), style: StrokeStyle(lineWidth: 2, lineCap: .round, dash: [0.1, 4]))
                }
            }
        }
        .frame(width: axis.width, height: 24, alignment: .topLeading)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

// MARK: - the day's numbers

/// A quiet number after the chips (`8,240 步`, `静息 58`, `HRV 42`): no fill, nothing to press
private struct TLQuietValue: View {
    let v: TLBody

    var body: some View {
        HStack(spacing: 0) {
            Text(v.prefix).font(.system(size: 12.5)).foregroundStyle(Look.ink3)
            Text(v.value).font(Look.figures(12)).foregroundStyle(Look.ink2)
            Text(v.suffix).font(.system(size: 12.5)).foregroundStyle(Look.ink3)
        }
        .lineLimit(1).fixedSize().padding(.horizontal, 5).frame(height: TLLayout.Today.chipsH)
        .accessibilityElement(children: .combine)
    }
}

/// The phone's row of the day's numbers: one row of capsules (睡 · 路上 · 运动 · Mac · 通话 · 和她 · 花 · 照片, then what Health reported, quiet) that slides sideways and fades out at its right
/// edge. Pressing a capsule brings that kind forward (`toggle`), and pressing it again lets it go. The iPad's row is `TLChips`.
struct TLPhoneChips: View, Equatable {
    let chips: [TLChip]
    let health: [TLBody]
    let active: TLLens?
    let toggle: (TLLens) -> Void

    static func == (a: TLPhoneChips, b: TLPhoneChips) -> Bool { a.chips == b.chips && a.health == b.health && a.active == b.active }

    /// what a chip says: a length in `2 小时 15 分`, else its own words
    static func value(_ c: TLChip) -> String { c.minutes.map { tlDur($0) } ?? c.value }

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 7) {
                ForEach(chips) { c in chip(c) }
                ForEach(health) { v in TLQuietValue(v: v) }
            }
            .padding(.leading, TLLayout.Today.side).padding(.trailing, 24)
            .frame(height: 32)
        }
        .frame(width: W, height: 32)
        .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 14 / W), .init(color: .black, location: 1 - 48 / W), .init(color: .clear, location: 1)],
                             startPoint: .leading, endPoint: .trailing))
        .accessibilityElement(children: .contain).accessibilityLabel(Words.PhoneToday.chipsRow)
    }

    private func chip(_ c: TLChip) -> some View {
        let on = active == c.lens, value = Self.value(c)
        return Button { toggle(c.lens) } label: {
            HStack(spacing: 0) {
                Circle().fill(c.lens.color.color).frame(width: 6, height: 6).padding(.trailing, 6)
                Text(c.word).font(.system(size: 13)).foregroundStyle(on ? ink : Look.ink2)
                Text(value).font(tlFigures(13)).foregroundStyle(ink).padding(.leading, 4)
            }
            .lineLimit(1).fixedSize()
            .padding(.horizontal, 11).frame(height: TLLayout.Today.chipsH)
            .background(Capsule().fill(Color.white.opacity(on ? 0.15 : 0.065)))
            .overlay(Capsule().stroke(Look.ink.opacity(on ? 0.5 : 0), lineWidth: 1))
            .animation(.easeOut(duration: 0.2), value: on)
        }
        .buttonStyle(PressScale())
        .accessibilityLabel(c.word + " " + value)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}
