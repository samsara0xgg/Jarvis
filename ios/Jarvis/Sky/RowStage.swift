import SwiftUI
import MapKit

// What a row of the day opens to, by what the row is (跨端UI标准 §11, ref/iPad-右边舞台-四种.jpg): a trip and a night are views of their own here, as wide as they are given, so the
// phone stacks them under the row and the iPad's stage can hold them; a stretch she is not sure of and something not yet happened are TLOpenView's (Sky/DayTimelineOpen.swift), which has
// their chips and buttons. What the brain has not sent stays out: nothing is guessed to fill a part.

private let prose = Color(red: 226 / 255, green: 230 / 255, blue: 250 / 255).opacity(0.86)

/// a part's small heading (路上, 这一晚, 身体, 她看到的…)
struct StageHeading: View {
    let text: String
    var body: some View { Text(text).font(.system(size: Look.small)).foregroundStyle(Look.ink3).padding(.top, 4) }
}

/// a light chip under a part: 问她, 在地图里看 ›
struct StageChip: View {
    let text: String
    var symbol: String?
    let action: () -> Void
    var body: some View {
        Button { Haptic.selection(); action() } label: {
            HStack(spacing: 4) {
                if let symbol { Image(systemName: symbol).font(.system(size: 11, weight: .semibold)) }
                Text(text).font(.system(size: 12.5, weight: .medium)).lineLimit(1).fixedSize()
            }
            .foregroundStyle(Look.ink).padding(.horizontal, 11).frame(height: 28)
            .background(Color.white.opacity(0.07), in: Capsule())
            .overlay(Capsule().strokeBorder(Look.ink.opacity(0.14), lineWidth: 1))
            .contentShape(Capsule().inset(by: -8))
        }
        .buttonStyle(PressScale())
    }
}

/// 跟平时比 (ADR 0411): a line a measure, its name, its value and where it stands against his usual level, as wide as it is given. A part that has no line is not drawn: the opened row
/// asks `BodyStore.ensure` and draws this only when `BodyStore.lines` has some.
struct BodyVsUsual: View {
    let lines: [BodyLine]
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            StageHeading(text: Words.PhoneRow.vsUsual)
            ForEach(lines) { l in
                HStack(spacing: 10) {
                    Text(l.label).font(.system(size: 13.5)).foregroundStyle(Look.ink2).lineLimit(1)
                    Spacer(minLength: 6)
                    Text(l.value).font(Look.figures(13.5, weight: .medium)).foregroundStyle(Look.ink).lineLimit(1)
                    Label(Self.word(l.mark), systemImage: Self.symbol(l.mark)).labelStyle(.titleAndIcon).font(.system(size: 11.5)).foregroundStyle(Look.ink3).lineLimit(1)
                        .frame(minWidth: 92, alignment: .trailing)
                }
                .frame(minHeight: 22)
                .accessibilityElement(children: .combine)
            }
        }
    }
    static func word(_ m: BodyLine.Mark) -> String { switch m { case .below: Words.PhoneRow.below; case .within: Words.PhoneRow.within; case .above: Words.PhoneRow.above } }
    static func symbol(_ m: BodyLine.Mark) -> String { switch m { case .below: "arrow.down"; case .within: "equal"; case .above: "arrow.up" } }
}

// MARK: - a trip

/// A trip opened: that outing of the footprint day as its page draws it (`FootprintOutingParts`): the map framed to it, the four numbers, the speed and heart-rate curves, where it stopped and
/// where he got off; then 问她 and 在地图里看 ›. With no footprint for the day (not read yet, or none), what the row says in full.
struct TripStage: View {
    let e: TLEntry
    let tl: TimelineState
    let engine: StarEngine

    var body: some View {
        let iso = Cal.cur.iso(tl.day), store = FootprintStore.shared
        VStack(alignment: .leading, spacing: 12) {
            if let reply = store.reply(iso), case let model = FootprintModel(reply: reply, day: tl.reply), let o = Self.outing(of: e, in: model) {
                trip(FootprintOutingParts(model: model, outing: o))
            } else {
                ForEach(Array(e.more.enumerated()), id: \.offset) { _, l in Text(l).font(.system(size: 13.5)).foregroundStyle(prose).fixedSize(horizontal: false, vertical: true) }
            }
        }
        .task { store.ensure(iso) }
    }

    /// the outing the row stands for: the one that shares the most of its time
    static func outing(of e: TLEntry, in model: FootprintModel) -> FootprintOuting? {
        let a = model.clock.ms(minute: e.t0), b = model.clock.ms(minute: e.t1 ?? e.t0)
        func shared(_ o: FootprintOuting) -> Int64 { min(b, o.end_ms ?? model.reply.now_ms) - max(a, o.start_ms) }
        return model.reply.outings.filter { shared($0) > 0 }.max { shared($0) < shared($1) }
    }

    private func trip(_ p: FootprintOutingParts) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            FootprintMapView(routes: [p.route], places: p.places, tokens: p.tokens, lineWidth: 4.2, fitPadding: UIEdgeInsets(top: 28, left: 28, bottom: 28, right: 28), fitKey: p.outing.id)
                .frame(height: 190).frame(maxWidth: .infinity)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            p.figures
            p.curveBlock(scrub: nil) { _ in }.frame(width: FootprintCurves.width, alignment: .leading)          // a finger on the curves is the trip's page's; its axis labels stand past the 380 pt
            StageHeading(text: Words.PhoneRow.onTheWay)
            VStack(alignment: .leading, spacing: 10) {
                ForEach(p.steps) { StepRow(step: $0, time: p.time($0.t0), model: p.model) }
            }
            HStack(spacing: 8) {
                if e.actions.contains(.ask) { StageChip(text: TLAction.ask.title, symbol: "mic") { tl.perform(.ask, on: e, engine: engine) } }
                StageChip(text: Words.PhoneRow.seeOnMap) { engine.showPage(.map); engine.ui.footprint.openOuting = p.outing.id }
            }
        }
    }
}

// MARK: - a night

/// A night opened: its stages as a bar (deep, core, REM, awake) and what they add up to; 这一晚, when he fell asleep, each time he woke and for how long, when he got up; 身体, what Health
/// reported for the day. Nothing is compared with his usual: the brain sends no usual yet.
struct NightStage: View {
    let e: TLEntry
    var health: [TLBody] = []           // what Health reported, the day's numbers row's
    let tl: TimelineState
    let engine: StarEngine
    /// what 跟平时比 says of a night: his body asleep, and how long
    static let measures = ["resting_heart_rate", "hrv", "breathing_rate", "blood_oxygen", "sleep"]

    var body: some View {
        let t1 = e.t1 ?? e.t0
        let woke = e.sleep.map { TLStage.blocks($0.stages, in: e.t0, t1).filter { $0.kind == .awake } } ?? []
        let window = tl.reply.map { r in BodyWindow(startMs: TLClock(r).ms(minute: e.t0), endMs: TLClock(r).ms(minute: t1)) }
        let usual = window.map { BodyStore.shared.lines($0, Self.measures) } ?? []
        return VStack(alignment: .leading, spacing: 10) {
            if let s = e.sleep {
                TLHypnogram(stages: s.stages, t0: e.t0, t1: t1)
                if let t = s.summary { Text(t).font(.system(size: 12.5)).foregroundStyle(Look.ink2).fixedSize(horizontal: false, vertical: true) }
            }
            StageHeading(text: Words.PhoneRow.theNight)
            line(e.t0, "moon.zzz", Words.PhoneRow.fellAsleep, nil)
            ForEach(Array(woke.enumerated()), id: \.offset) { _, w in line(w.t0, "eye", Words.PhoneRow.wokeOnce, tlDur(Int((w.t1 - w.t0).rounded()))) }
            if e.t1 != nil { line(t1, "sun.max", Words.PhoneRow.gotUp, nil) }
            if !usual.isEmpty {
                BodyVsUsual(lines: usual)          // his night against his usual level stands in for the day's numbers
            } else if !health.isEmpty {
                StageHeading(text: Words.PhoneRow.body)
                HStack(alignment: .top, spacing: 8) {
                    ForEach(health) { b in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(b.prefix.isEmpty ? b.suffix : b.prefix).font(.system(size: Look.small)).foregroundStyle(Look.ink3).lineLimit(1)
                            Text(b.value).font(Look.figures(17, weight: .semibold)).foregroundStyle(Look.ink).lineLimit(1).minimumScaleFactor(0.7)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            if e.actions.contains(.ask) { StageChip(text: TLAction.ask.title, symbol: "mic") { tl.perform(.ask, on: e, engine: engine) }.padding(.top, 2) }
        }
        .task(id: window) { if let window { BodyStore.shared.ensure(window) } }
    }

    private func line(_ t: Double, _ symbol: String, _ title: String, _ right: String?) -> some View {
        HStack(spacing: 10) {
            Text(clock(t)).font(Look.figures(11.5)).foregroundStyle(Look.ink2).frame(width: 40, alignment: .leading)
            Image(systemName: symbol).font(.system(size: 12)).foregroundStyle(Look.ink2).frame(width: 16)
            Text(title).font(.system(size: 13.5)).foregroundStyle(Look.ink)
            Spacer(minLength: 6)
            if let right { Text(right).font(Look.figures(11.5)).foregroundStyle(Look.ink2) }
        }
        .frame(minHeight: 24)
    }
}
