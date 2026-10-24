import SwiftUI

// The chips under her answer that jump to a row of the 今天 page or a trip of the 地图 (跨端UI标准 §5). The brain sends them as `links` on an answer's row (GET /inherent/conversation and
// /phone/ws): at most three of `{kind: "row" | "outing", id, date: "YYYY-MM-DD", start_ms, end_ms | null, title}`. A row of the day has its id of `GET /inherent/day`, a trip
// `outing-<ms>`. An answer with no `links` has no chip: nothing is made up here.

struct AnswerLink: Equatable, Identifiable {
    enum Kind: String { case row, outing }
    let kind: Kind
    let target: String                  // the row's or the outing's id; may be empty, then the row that covers `startMs` that day
    let date: String
    let startMs: Int64
    let endMs: Int64?
    let title: String
    var id: String { kind.rawValue + "|" + target + "|" + String(startMs) }

    /// the most a row carries
    static let most = 3

    /// the links of a row (`links` of its JSON), or none: one that is not this shape is left out
    static func parse(_ raw: Any?) -> [AnswerLink] {
        guard let list = raw as? [[String: Any]] else { return [] }
        let all = list.compactMap { o -> AnswerLink? in
            guard let k = (o["kind"] as? String).flatMap(Kind.init(rawValue:)), let date = o["date"] as? String, date.count == 10,
                  let start = (o["start_ms"] as? NSNumber)?.int64Value else { return nil }
            return AnswerLink(kind: k, target: (o["id"] as? String) ?? "", date: date, startMs: start, endMs: (o["end_ms"] as? NSNumber)?.int64Value, title: (o["title"] as? String) ?? "")
        }
        return Array(all.prefix(most))
    }
}

/// The chips under her words, grey glass as the stage's (`StageChip`): the row's or the trip's title; a tap jumps there.
struct AnswerLinkChips: View {
    let links: [AnswerLink]
    let open: (AnswerLink) -> Void

    var body: some View {
        WrapLayout(spacing: 6) {
            ForEach(links) { l in
                StageChip(text: l.title.isEmpty ? (l.kind == .row ? Words.PhoneChat.jumpRow : Words.PhoneChat.jumpOuting) : l.title,
                          symbol: l.kind == .row ? "calendar.day.timeline.left" : "map") { open(l) }
            }
        }
    }
}

/// the day number (of `Cal.cur`) of a brain day `YYYY-MM-DD`
func chatDayNumber(iso: String) -> Int? {
    let p = iso.split(separator: "-").compactMap { Int($0) }
    guard p.count == 3, let at = Cal.cur.cal.date(from: DateComponents(year: p[0], month: p[1], day: p[2], hour: 12)) else { return nil }
    return floorDay(Cal.cur.t(at))
}

extension StarEngine {
    /// A tap on a chip under her answer: the 今天 page at that day with the row open, or the map at that day with the trip's page over it. The day is asked for by the pages as they
    /// are for any other day; the row or the trip is shown once it is there (a day that does not come leaves the page on the day).
    func openLink(_ l: AnswerLink) {
        guard let d = chatDayNumber(iso: l.date), d <= ui.tl.today else { return }
        wake()
        showPage(l.kind == .row ? .day : .map)
        ui.tl.setDay(d)
        Task { @MainActor in
            for _ in 0..<40 {                                                   // up to 4 s for the day to arrive
                try? await Task.sleep(nanoseconds: 100_000_000)
                guard ui.tl.day == d else { continue }
                if l.kind == .outing { try? await Task.sleep(nanoseconds: 400_000_000); ui.footprint.openOuting = l.target; return }
                if let r = ui.tl.reply { ui.tl.reveal(l, in: r); return }
            }
        }
    }
}

extension TimelineState {
    /// the row of a link: its id, else the row that covers the link's time; none when the day has neither
    func entry(_ l: AnswerLink, in r: DayReply) -> TLEntry? {
        let m = model(r), minute = TLClock(r).minute(ms: l.startMs)
        return m.entries.first(where: { $0.id == l.target }) ?? m.entries.first(where: { $0.t0 <= minute && minute <= ($0.t1 ?? $0.t0 + 1) })
    }

    /// the row of a link lit and opened where it stands
    func reveal(_ l: AnswerLink, in r: DayReply) {
        guard let e = entry(l, in: r) else { return }
        linked.insert(e.id)
        if openRow != e.id { tapRow(e, in: model(r), room: TLLayout.listBottom - TLLayout.Today.listTop) }
    }

    /// Her answer came with links while the day is on show: the rows of the day on show they name stay lit until the talk goes or the page leaves the day.
    func light(_ links: [AnswerLink]) {
        guard let r = reply else { return }
        let iso = Cal.cur.iso(day)
        linked.formUnion(links.filter { $0.date == iso }.compactMap { entry($0, in: r)?.id })
    }
}
