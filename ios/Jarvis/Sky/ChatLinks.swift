import SwiftUI

// The cards under her answer that jump to a row of the 今天 page or a trip of the 地图 (跨端UI标准 §5). The brain sends them as `links` on an answer's row (GET /inherent/conversation and
// /phone/ws): at most three of `{kind: "row" | "outing", id, date: "YYYY-MM-DD", start_ms, end_ms | null, title}`. A row of the day has its id of `GET /inherent/day`, a trip
// `outing-<ms>`. An answer with no `links` has no card: nothing is made up here.

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

/// The links the conversation's rows brought, by the row's record id. The conversation's reader (`ConversationRow.parse`) hands each row's `links` to `note`; a row drawn asks for them.
final class AnswerLinkStore: @unchecked Sendable {
    static let shared = AnswerLinkStore()
    private var held: [String: [AnswerLink]] = [:]
    private let lock = NSLock()           // the reader runs wherever an answer is parsed; the rows read on the main actor

    func note(id: String, raw: Any?) {
        guard !id.isEmpty else { return }
        let links = AnswerLink.parse(raw)
        lock.withLock { held[id] = links.isEmpty ? nil : links }
    }
    func links(for id: String?) -> [AnswerLink] { lock.withLock { id.flatMap { held[$0] } ?? [] } }
}

/// The cards: a small round icon, the title, and when it was; a tap jumps there.
struct AnswerLinkCards: View {
    let links: [AnswerLink]
    let open: (AnswerLink) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(links) { l in
                Button { open(l) } label: {
                    HStack(spacing: 10) {
                        Image(systemName: l.kind == .row ? "calendar.day.timeline.left" : "map").font(.system(size: 13, weight: .medium)).foregroundStyle(Look.ink)
                            .frame(width: 28, height: 28).background(Circle().fill(Color.white.opacity(0.1)))
                        VStack(alignment: .leading, spacing: 1) {
                            Text(l.title.isEmpty ? (l.kind == .row ? Words.PhoneChat.jumpRow : Words.PhoneChat.jumpOuting) : l.title)
                                .font(.system(size: Look.body, weight: .medium)).foregroundStyle(Look.ink).lineLimit(1)
                            Text(Self.when(l)).font(Look.figures(Look.note)).foregroundStyle(Look.ink3).lineLimit(1)
                        }
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right").font(.system(size: 11, weight: .semibold)).foregroundStyle(Look.ink3)
                    }
                    .padding(.leading, 8).padding(.trailing, 12).padding(.vertical, 8)
                    .background(Color.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(Color.white.opacity(0.12), lineWidth: 0.6))
                    .contentShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                }
                .buttonStyle(PressScale())
            }
        }
    }

    /// 「今天 14:50–15:19」, 「10月21日 周三 09:00」
    static func when(_ l: AnswerLink) -> String {
        let cal = Cal.cur.cal, start = Date(timeIntervalSince1970: Double(l.startMs) / 1000)
        let span = [TalkStream.clock(start, cal), l.endMs.map { TalkStream.clock(Date(timeIntervalSince1970: Double($0) / 1000), cal) }].compactMap { $0 }.joined(separator: "–")
        return TalkStream.title(start, cal, Date()) + " " + span
    }
}

/// the day number (of `Cal.cur`) of a brain day `YYYY-MM-DD`
func chatDayNumber(iso: String) -> Int? {
    let p = iso.split(separator: "-").compactMap { Int($0) }
    guard p.count == 3, let at = Cal.cur.cal.date(from: DateComponents(year: p[0], month: p[1], day: p[2], hour: 12)) else { return nil }
    return floorDay(Cal.cur.t(at))
}

extension StarEngine {
    /// A tap on a card under her answer: the 今天 page at that day with the row open, or the map at that day with the trip's page over it. The day is asked for by the pages as they
    /// are for any other day; the row or the trip is shown once it is there (a day that does not come leaves the page on the day).
    func openLink(_ l: AnswerLink) {
        guard let d = chatDayNumber(iso: l.date), d <= ui.tl.today else { return }
        wake(); Haptic.selection()
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
    /// the row of a link opened where it stands: its id, else the row that covers the link's time; nothing when the day has none
    func reveal(_ l: AnswerLink, in r: DayReply) {
        let m = model(r), minute = TLClock(r).minute(ms: l.startMs)
        guard let e = m.entries.first(where: { $0.id == l.target }) ?? m.entries.first(where: { $0.t0 <= minute && minute <= ($0.t1 ?? $0.t0 + 1) }), openRow != e.id else { return }
        tapRow(e, in: m, room: TLLayout.listBottom - TLLayout.Today.listTop)
    }
}
