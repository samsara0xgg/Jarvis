import SwiftUI

// The day bar while a finger or the replay holds it (replay brief, section 1): the trip layout of `FootprintBarLayout` drawn over the time bar's own place. `FootprintTripArt` is what it draws, made once
// from the model: for each bead, run and notch where it lies in the trip layout and where the same stretch of time lies on the bar at rest, so that `FootprintTripBar` can move each piece from the
// one to the other while it turns (k from 0 to 1). The bar at rest is `TLBarView`, which stays as it is; this fades in over it as that fades out.

extension FootprintPaint {
    static let capsule = RGB(28, 31, 58)             // the capsule over the playhead
    static let bead = 24.0                          // a stay's disc on the held bar
}

final class FootprintTripArt {
    struct Bead { let x: ClosedRange<Double>, timeX: ClosedRange<Double>, fill: RGB, symbol: String?, glyph: String?, length: String }
    struct Run { let x: ClosedRange<Double>, timeX: ClosedRange<Double>, stops: [Gradient.Stop] }
    struct Notch { let x: ClosedRange<Double>, timeX: ClosedRange<Double>, number: Int }
    /// a word under the bar: its middle in the trip layout and on the bar at rest
    struct Word { let x: Double, timeX: Double, text: String, ring: Bool }

    private(set) var beads: [Bead] = [], runs: [Run] = [], notches: [Notch] = [], words: [Word] = []

    init(model: FootprintModel, span: ClosedRange<Double>) {
        let layout = model.barLayout, r = model.reply, dayEnd = min(r.end_ms, r.now_ms)
        func timeX(_ ms: Int64) -> Double { TLGeo.x(model.clock.minute(ms: ms), span, TLLayout.barW) }
        var taken = -Double.infinity            // the right edge of the last word, so that none stands on another
        func say(_ text: String, x: Double, timeX: Double, ring: Bool = false) {
            let w = TLBarLabel.measure(text)
            guard text != "", x - w / 2 >= taken + 3, x + w / 2 <= TLLayout.barW + 12 else { return }
            taken = x + w / 2
            words.append(Word(x: x, timeX: timeX, text: text, ring: ring))
        }
        for it in layout.items {
            switch it.kind {
            case let .bead(id):
                guard let s = r.stays.first(where: { $0.id == id }) else { continue }
                let kept = s.kept_place, name = FootprintDayContent.name(s)
                let symbol = ["home": "house.fill", "school": "graduationcap.fill"][kept?.role ?? ""] ?? (name == nil ? "mappin" : nil)
                let minutes = max(0, tlInt(Double(min(s.end_ms ?? dayEnd, dayEnd) - max(s.start_ms, r.start_ms)) / 60_000))
                let bead = Bead(x: it.x0...it.x1, timeX: timeX(it.ms0)...max(timeX(it.ms1), timeX(it.ms0)), fill: kept.map { PlaceTint.line(role: $0.role, id: $0.id) } ?? PlaceTint.lineNeutral,
                                symbol: symbol, glyph: symbol == nil ? name.flatMap { $0.first.map(String.init) } : nil, length: tlDur(minutes))
                beads.append(bead)
                say(bead.length, x: (it.x0 + it.x1) / 2, timeX: (bead.timeX.lowerBound + bead.timeX.upperBound) / 2)
            case let .segment(id):
                guard let track = it.track, let o = r.outings.first(where: { $0.id == id }) else { continue }
                for p in it.pieces {
                    let tx = timeX(p.ms0)...max(timeX(p.ms1), timeX(p.ms0))
                    switch p.kind {
                    case let .run(d0, d1): runs.append(Run(x: p.x0...p.x1, timeX: tx, stops: Self.stops(model, o, track, d0, d1, width: p.x1 - p.x0)))
                    case let .notch(_, number):
                        notches.append(Notch(x: p.x0...p.x1, timeX: tx, number: number))
                        say("\(number)", x: (p.x0 + p.x1) / 2, timeX: (tx.lowerBound + tx.upperBound) / 2, ring: true)
                    }
                }
                if it.x1 - it.x0 >= 48, let d = tlDistance(o.distance_m) { say(d, x: (it.x0 + it.x1) / 2, timeX: (timeX(it.ms0) + timeX(it.ms1)) / 2) }
            }
        }
    }

    /// the colour of the ramp along a run of the line, a stop every few points of the bar; where no fix says how fast (a gap, the join to a stay) the gap's colour
    private static func stops(_ m: FootprintModel, _ o: FootprintOuting, _ t: FootprintTrack, _ d0: Double, _ d1: Double, width: Double) -> [Gradient.Stop] {
        let n = Int(min(max((width / 3).rounded(.up), 2), 48))
        return (0...n).map { j in
            let f = Double(j) / Double(n), d = d0 + (d1 - d0) * f
            let recorded = t.fixes.map { $0.contains(d) } ?? false
            let rgb = recorded ? m.speedMps(atMs: t.ms(atDistance: d)).map { FootprintPaint.speed(mps: $0) } : nil
            return .init(color: rgb?.color ?? FootprintPaint.gap.color(FootprintPaint.gapAlpha * 0.6), location: f)
        }
    }
}

/// The trip layout at `k` of the way from the bar at rest (0) to the held bar (1): each piece between its place on the one and on the other, the whole fading in with k.
struct FootprintTripBar: View, Animatable {
    let art: FootprintTripArt
    var k: Double
    var animatableData: Double { get { k } set { k = newValue } }

    private let bh = TLLayout.barH
    private func lerp(_ a: Double, _ b: Double) -> Double { a + (b - a) * k }

    var body: some View {
        ZStack(alignment: .topLeading) {
            ForEach(Array(art.runs.enumerated()), id: \.offset) { _, r in
                let x0 = lerp(r.timeX.lowerBound, r.x.lowerBound), x1 = lerp(r.timeX.upperBound, r.x.upperBound)
                RoundedRectangle(cornerRadius: 4).fill(LinearGradient(stops: r.stops, startPoint: .leading, endPoint: .trailing))
                    .frame(width: max(x1 - x0, 1), height: bh).offset(x: x0)
            }
            ForEach(Array(art.notches.enumerated()), id: \.offset) { _, n in
                let mid = lerp((n.timeX.lowerBound + n.timeX.upperBound) / 2, (n.x.lowerBound + n.x.upperBound) / 2)
                Circle().fill(TLPaint.ink.color).frame(width: 6, height: 6).offset(x: mid - 3, y: bh / 2 - 3)
            }
            ForEach(Array(art.beads.enumerated()), id: \.offset) { _, b in
                let x0 = lerp(b.timeX.lowerBound, b.x.lowerBound), x1 = lerp(b.timeX.upperBound, b.x.upperBound), h = lerp(bh, FootprintPaint.bead)
                RoundedRectangle(cornerRadius: lerp(6, FootprintPaint.bead / 2)).fill(b.fill.color)
                    .frame(width: max(x1 - x0, 1), height: h).offset(x: x0, y: (bh - h) / 2)
                glyph(b).frame(width: FootprintPaint.bead, height: FootprintPaint.bead).offset(x: (x0 + x1) / 2 - FootprintPaint.bead / 2, y: (bh - FootprintPaint.bead) / 2).opacity(k)
            }
            ForEach(Array(art.words.enumerated()), id: \.offset) { _, w in
                if w.ring {
                    Text(w.text).font(.system(size: 9.5, weight: .medium).monospacedDigit()).foregroundStyle(TLPaint.ink.color)
                        .frame(width: 13, height: 13).background(Circle().fill(FootprintPaint.token.color)).overlay(Circle().strokeBorder(TLPaint.ink.color(0.8), lineWidth: 1))
                        .offset(x: lerp(w.timeX, w.x) - 6.5, y: bh + 5).opacity(k)
                } else {
                    Text(w.text).font(AT.mono(10)).foregroundStyle(SetColor.muted).lineLimit(1).frame(width: 80)
                        .offset(x: lerp(w.timeX, w.x) - 40, y: bh + 4).opacity(k)
                }
            }
        }
        .frame(width: TLLayout.barW, height: bh, alignment: .topLeading)
        .opacity(k)
        .allowsHitTesting(false)
    }

    @ViewBuilder private func glyph(_ b: FootprintTripArt.Bead) -> some View {
        if let s = b.symbol {
            Image(systemName: s).font(.system(size: 11, weight: .semibold)).foregroundStyle(TLPaint.darkInk.color(0.9))
        } else if let g = b.glyph {
            Text(g).font(.system(size: 12, weight: .semibold)).foregroundStyle(TLPaint.darkInk.color(0.9))
        }
    }
}

/// The playhead on the held bar: a dot on the bar with its glow, the hairline up to its capsule, and in it "time · speed". It follows the finger or the replay at once; with none held it stays where it was while the bar turns back.
struct FootprintPlayhead: View {
    let ui: StarUI

    var body: some View {
        let state = ui.footprint, moment = state.moment, x = moment?.x ?? state.last.x
        let bh = TLLayout.barH, bw = TLLayout.barW
        ZStack(alignment: .topLeading) {
            Rectangle().fill(TLPaint.ink.color(0.7)).frame(width: 1, height: bh / 2 + 8).offset(x: x - 0.5, y: -8)
            Circle().fill(TLPaint.ink.color(0.35)).frame(width: 18, height: 18).offset(x: x - 9, y: bh / 2 - 9)
            Circle().fill(TLPaint.ink.color).frame(width: 11, height: 11).offset(x: x - 5.5, y: bh / 2 - 5.5)
            Text(state.last.text).font(AT.mono(11)).foregroundStyle(TLPaint.ink.color).lineLimit(1).padding(.horizontal, 8).frame(height: 19)
                .background(Capsule().fill(FootprintPaint.capsule.color))
                .overlay(Capsule().stroke(Color.white.opacity(0.14), lineWidth: 0.5))
                .fixedSize().frame(width: 140).offset(x: clamp(x - 70, 0, bw - 140), y: -27)
        }
        .frame(width: bw, height: bh, alignment: .topLeading)
        .allowsHitTesting(false)
    }

    /// "13:31 · 54 km/h": the time and, while he moved, how fast
    @MainActor static func text(_ m: FootprintMoment, _ model: FootprintModel) -> String {
        let t = clock(model.clock.minute(ms: m.ms))
        return model.speedMps(atMs: m.ms).map { t + " \u{00B7} " + FootprintDayContent.speedText($0) } ?? t
    }
}
