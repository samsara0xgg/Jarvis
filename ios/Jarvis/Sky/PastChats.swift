import SwiftUI
import Observation

// 以前的聊天: the list under the chat page's left button, a card that drops from it (the prototype's `.hist`). The brain has no list of past chats yet, so for now it is the days of the
// conversation the phone holds, newest first, and a search over everything said (`GET /inherent/memory/search`); a day or a hit goes to its place in the stream, and one older than
// what is held opens that day's records in the card (`GET /inherent/memory/day/{day}/records?around=`). `ChatsPage` is the shape the brain has agreed to serve; nothing asks for it yet.

// MARK: what the brain says

/// One line of what was said, from a search or from a day's records: `who` is "user" for his words, anything else is hers.
struct PastLine: Equatable, Identifiable {
    let id: String
    let ts: String
    let who: String
    let text: String
    var his: Bool { who == "user" }
}

/// The hits of one day of a search, oldest first.
struct PastHitDay: Equatable, Identifiable {
    let day: String                     // `YYYY-MM-DD`
    let hits: [PastLine]
    var id: String { day }
}

/// One chat of the conversation as the brain has agreed to cut it (`GET /inherent/conversation/chats`): `id` is `chat-<first_seq>`, `his` and `hers` the first words of each side.
struct ChatSummary: Equatable, Identifiable {
    let id: String
    let startMs: Int64
    let endMs: Int64
    let firstSeq: Int
    let lastSeq: Int
    let his: String
    let hers: String
    let title: String
}

struct ChatsPage: Equatable {
    var chats: [ChatSummary]
    var hasMore: Bool

    static func parse(_ data: Data) -> ChatsPage? {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let list = o["chats"] as? [[String: Any]] else { return nil }
        let chats = list.compactMap { c -> ChatSummary? in
            guard let id = c["id"] as? String, let s = (c["start_ms"] as? NSNumber)?.int64Value, let e = (c["end_ms"] as? NSNumber)?.int64Value,
                  let a = c["first_seq"] as? Int, let b = c["last_seq"] as? Int else { return nil }
            return ChatSummary(id: id, startMs: s, endMs: e, firstSeq: a, lastSeq: b, his: (c["his"] as? String) ?? "", hers: (c["hers"] as? String) ?? "", title: (c["title"] as? String) ?? "")
        }
        return ChatsPage(chats: chats, hasMore: o["has_more"] as? Bool ?? false)
    }
}

enum PastParse {
    private static func line(_ r: [String: Any]) -> PastLine? {
        guard let id = r["id"] as? String, let ts = r["ts"] as? String, let who = r["who"] as? String, let text = r["text"] as? String else { return nil }
        return PastLine(id: id, ts: ts, who: who, text: text)
    }

    /// the days of an answer of `GET /inherent/memory/search` (`{days: [{day, hits: [{id, ts, who, text}]}]}`), newest first as the brain sends them, or nil when it is not one
    static func search(_ data: Data) -> [PastHitDay]? {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let days = o["days"] as? [[String: Any]] else { return nil }
        return days.compactMap { d in
            guard let day = d["day"] as? String, let hits = d["hits"] as? [[String: Any]] else { return nil }
            let lines = hits.compactMap(line)
            return lines.isEmpty ? nil : PastHitDay(day: day, hits: lines)
        }
    }

    /// the records of an answer of `GET /inherent/memory/day/{day}/records` (`{records: [{id, ts, who, text}]}`), oldest first, or nil when it is not one
    static func records(_ data: Data) -> [PastLine]? {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let rows = o["records"] as? [[String: Any]] else { return nil }
        return rows.compactMap(line)
    }
}

extension BrainClient {
    /// `GET /inherent/memory/search?q=&who=`: every record with any of the words, grouped by day.
    static func memorySearch(_ q: String, who: String = "all", bases: [URL], token: String) async throws -> [PastHitDay] {
        let r = try await send("GET", "/inherent/memory/search", query: [URLQueryItem(name: "q", value: q), URLQueryItem(name: "who", value: who)], bases: bases, token: token, unauthorized: .revoked)
        guard let days = PastParse.search(r.data) else { throw BrainError.garbled }
        return days
    }

    /// `GET /inherent/memory/day/{day}/records?around=`: one day's conversation, oldest first; with `around` (a record id) the page starts a little before that record.
    static func dayRecords(_ day: String, around: String?, bases: [URL], token: String) async throws -> [PastLine] {
        let query = around.map { [URLQueryItem(name: "around", value: $0)] } ?? []
        let r = try await send("GET", "/inherent/memory/day/\(day)/records", query: query, bases: bases, token: token, unauthorized: .revoked)
        guard let lines = PastParse.records(r.data) else { throw BrainError.garbled }
        return lines
    }

    /// `GET /inherent/conversation/chats?before_ms=&limit=`: the conversation cut into chats, newest first. The brain does not serve it yet, so nothing calls this: the list is made
    /// from the conversation the phone holds until it does (`PastChats.days`).
    static func chats(beforeMs: Int64?, limit: Int, bases: [URL], token: String) async throws -> ChatsPage {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let beforeMs { query.append(URLQueryItem(name: "before_ms", value: String(beforeMs))) }
        let r = try await send("GET", "/inherent/conversation/chats", query: query, bases: bases, token: token, unauthorized: .revoked)
        guard let page = ChatsPage.parse(r.data) else { throw BrainError.garbled }
        return page
    }
}

// MARK: what the phone holds

/// One day of the conversation the phone holds: when it was, what was said first and how much.
struct PastDay: Equatable, Identifiable {
    let id: String                      // `YYYY-MM-DD`
    let title: String                   // 今天, 昨天, then the date and weekday
    let count: Int                      // rows of the conversation that day
    let first: String                   // his first words of it
    let firstSeq: Int                   // where it begins in the stream
}

enum PastChats {
    /// The days of `rows` (the conversation as held, oldest first), newest first.
    static func days(_ rows: [ConversationRow], cal: Calendar, now: Date) -> [PastDay] {
        var order: [String] = [], byDay: [String: [ConversationRow]] = [:], date: [String: Date] = [:]
        for r in rows {
            guard let at = TalkStream.time(r.ts, cal) else { continue }
            let id = TalkStream.dayId(at, cal)
            if byDay[id] == nil { order.append(id); date[id] = at }
            byDay[id, default: []].append(r)
        }
        return order.reversed().compactMap { id in
            guard let list = byDay[id], let first = list.first, let at = date[id] else { return nil }
            let his = list.first { $0.isHis }.map { TalkTopic.title($0.text) } ?? ""
            return PastDay(id: id, title: TalkStream.title(at, cal, now), count: list.count, first: his, firstSeq: first.seq)
        }
    }

    /// the title of a brain day `YYYY-MM-DD`: 今天, 昨天, the date and weekday
    static func title(day: String, cal: Calendar, now: Date) -> String {
        let p = day.split(separator: "-").compactMap { Int($0) }
        guard p.count == 3, let at = cal.date(from: DateComponents(year: p[0], month: p[1], day: p[2], hour: 12)) else { return day }
        return TalkStream.title(at, cal, now)
    }

    /// the clock of a record's time, in this phone's zone (the brain writes it with or without a zone suffix)
    static func clock(_ ts: String, cal: Calendar) -> String {
        if let d = ISO8601DateFormatter().date(from: ts) ?? ISO8601DateFormatter.withFraction.date(from: ts) { return TalkStream.clock(d, cal) }
        return TalkStream.time(ts, cal).map { TalkStream.clock($0, cal) } ?? ""
    }
}

private extension ISO8601DateFormatter {
    static let withFraction: ISO8601DateFormatter = { let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f }()
}

// MARK: the search

/// What the card is showing: the days, a search's hits, or one day's records.
@MainActor @Observable
final class PastChatsModel {
    enum Found: Equatable { case idle, searching, hits([PastHitDay]), failed }
    enum Opened: Equatable { case none, loading(day: String), lines(day: String, [PastLine], around: String?), failed }

    var query = ""
    private(set) var found = Found.idle
    private(set) var opened = Opened.none
    /// where the brain is asked: a test hands in its own
    var search: (String) async -> [PastHitDay]? = { q in try? await BrainLink.shared.call { try await BrainClient.memorySearch(q, bases: $0, token: $1) } }
    var records: (String, String?) async -> [PastLine]? = { day, around in try? await BrainLink.shared.call { try await BrainClient.dayRecords(day, around: around, bases: $0, token: $1) } }
    private var task: Task<Void, Never>?

    var trimmed: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    /// the field's words changed (the card calls it)
    func queryChanged() {
        task?.cancel()
        let q = trimmed
        guard !q.isEmpty else { found = .idle; return }
        found = .searching
        task = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 350_000_000)          // typing goes on: one ask when it stops
            guard !Task.isCancelled, let self else { return }
            let got = await self.search(q)
            guard !Task.isCancelled else { return }
            self.found = got.map { .hits($0) } ?? .failed
        }
    }

    /// a day's records, the page starting a little before `around`
    func open(day: String, around: String?) {
        opened = .loading(day: day)
        task?.cancel()
        task = Task { [weak self] in
            guard let self else { return }
            let got = await self.records(day, around)
            guard !Task.isCancelled else { return }
            self.opened = got.map { .lines(day: day, $0, around: around) } ?? .failed
        }
    }

    func closeDay() { opened = .none }
    func reset() { task?.cancel(); query = ""; found = .idle; opened = .none }
}

extension StarEngine {
    /// The stream goes to the row `seq`: it lists from there (a page of rows older than the newest ones listed), and scrolls to it once they are laid out.
    func showChatSeq(_ seq: Int) {
        wake()
        streamFrom = min(streamFrom ?? .max, seq)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 350_000_000)
            ui.scrollTo = "c\(seq)"
        }
    }
}

// MARK: the card

struct PastChatsCard: View {
    @Bindable var model: PastChatsModel
    let engine: StarEngine
    let close: () -> Void
    @FocusState private var focused: Bool

    static let width = 320.0, listMax = 380.0

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 20, style: .continuous)
        VStack(alignment: .leading, spacing: 8) {
            if case .none = model.opened { field } else { dayHeader }
            ScrollView(showsIndicators: false) { content }.frame(maxHeight: Self.listMax).fixedSize(horizontal: false, vertical: true)
        }
        .padding(10)
        .frame(width: Self.width)
        .background(Look.card, in: shape)
        .overlay(shape.strokeBorder(Look.glow.opacity(0.2), lineWidth: 0.6))
        .shadow(color: .black.opacity(0.5), radius: 25, y: 18)
        .onChange(of: model.query) { model.queryChanged() }
        .onAppear { if BrainLink.shared.hasToken { Task { await Conversation.shared.refresh() } } }
    }

    private var field: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass").font(.system(size: 13, weight: .medium)).foregroundStyle(Look.ink3)
            TextField("", text: $model.query, prompt: Text(Words.PhoneChat.searchPrompt).foregroundColor(Look.ink3))
                .font(.system(size: Look.body)).foregroundStyle(Look.ink).tint(Look.glow).focused($focused).submitLabel(.search).autocorrectionDisabled()
            if !model.query.isEmpty {
                Button { model.query = "" } label: { Image(systemName: "xmark.circle.fill").font(.system(size: 15)).foregroundStyle(Look.ink3).frame(width: 28, height: 28).contentShape(Rectangle()) }
                    .buttonStyle(.plain).accessibilityLabel(Words.PhoneChat.searchClear)
            }
        }
        .padding(.horizontal, 12).frame(height: 36)
        .background(Color.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }

    private var dayHeader: some View {
        let day: String? = { switch model.opened { case .loading(let d): d; case .lines(let d, _, _): d; default: nil } }()
        return HStack(spacing: 6) {
            Button { model.closeDay() } label: {
                Image(systemName: "chevron.left").font(.system(size: 14, weight: .semibold)).foregroundStyle(Look.ink).frame(width: 36, height: 36).contentShape(Rectangle())
            }
            .buttonStyle(.plain).accessibilityLabel(Words.PhoneChat.back)
            Text(day.map { PastChats.title(day: $0, cal: Cal.cur.cal, now: Date()) } ?? "").font(.system(size: Look.body, weight: .medium)).foregroundStyle(Look.ink)
            Spacer(minLength: 0)
        }
    }

    @ViewBuilder private var content: some View {
        switch model.opened {
        case .loading: note(Words.PhoneChat.dayLoading)
        case .failed: note(Words.PhoneChat.dayFailed)
        case .lines(_, let lines, let around): dayLines(lines, around: around)
        case .none:
            if model.trimmed.isEmpty { days } else { hits }
        }
    }

    private func note(_ text: String) -> some View {
        Text(text).font(.system(size: Look.note)).foregroundStyle(Look.ink3).frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 10).padding(.vertical, 12)
    }

    // the days of the conversation held
    @ViewBuilder private var days: some View {
        let list = PastChats.days(Conversation.shared.rows, cal: Cal.cur.cal, now: Date())
        if list.isEmpty { note(Words.PhoneChat.empty) } else {
            LazyVStack(alignment: .leading, spacing: 0) {
                ForEach(list) { d in
                    Button { engine.showChatSeq(d.firstSeq); close() } label: {
                        VStack(alignment: .leading, spacing: 0) {
                            Text(d.title).font(.system(size: Look.body, weight: .medium)).foregroundStyle(Look.ink)
                            Text([Words.PhoneChat.sentences(d.count), d.first].filter { !$0.isEmpty }.joined(separator: " · ")).font(.system(size: Look.note)).foregroundStyle(Look.ink3).lineLimit(1)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 10).padding(.vertical, 9).contentShape(Rectangle())
                    }
                    .buttonStyle(RowTap())
                }
                if Conversation.shared.hasMore { note(Words.PhoneChat.olderInSearch) }
            }
        }
    }

    // a search's hits, by day
    @ViewBuilder private var hits: some View {
        switch model.found {
        case .idle, .searching: note(Words.PhoneChat.searching)
        case .failed: note(Words.PhoneChat.searchFailed)
        case .hits(let list):
            if list.isEmpty { note(Words.PhoneChat.noHits) } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(list) { d in
                        Text(PastChats.title(day: d.day, cal: Cal.cur.cal, now: Date())).font(.system(size: Look.note, weight: .semibold)).foregroundStyle(Look.ink2)
                            .padding(.horizontal, 10).padding(.top, 8).padding(.bottom, 2)
                        ForEach(d.hits) { h in
                            Button { go(h, day: d.day) } label: {
                                (Text((h.his ? Words.PhoneChat.his : Words.PhoneChat.hers) + "  ").foregroundColor(Look.ink3) + Text(h.text).foregroundColor(Look.ink))
                                    .font(.system(size: Look.note)).lineLimit(2).multilineTextAlignment(.leading)
                                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 10).padding(.vertical, 7).contentShape(Rectangle())
                            }
                            .buttonStyle(RowTap())
                        }
                    }
                }
            }
        }
    }

    /// a hit: its row in the stream when the phone holds the conversation that far back, else that day's records in the card
    private func go(_ h: PastLine, day: String) {
        if let row = Conversation.shared.rows.first(where: { $0.id == h.id }) { engine.showChatSeq(row.seq); close() } else { focused = false; model.open(day: day, around: h.id) }
    }

    // one day's records
    private func dayLines(_ lines: [PastLine], around: String?) -> some View {
        ScrollViewReader { proxy in
            LazyVStack(alignment: .leading, spacing: 2) {
                if lines.isEmpty { note(Words.PhoneChat.dayEmpty) }
                ForEach(lines) { l in
                    VStack(alignment: .leading, spacing: 1) {
                        Text((l.his ? Words.PhoneChat.his : Words.PhoneChat.hers) + "  " + PastChats.clock(l.ts, cal: Cal.cur.cal)).font(Look.figures(Look.small)).foregroundStyle(Look.ink3)
                        Text(l.text).font(.system(size: Look.note)).foregroundStyle(Look.ink).lineLimit(8)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 10).padding(.vertical, 6)
                    .background(l.id == around ? Color.white.opacity(0.08) : .clear, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .id(l.id)
                }
            }
            .onAppear { if let around { proxy.scrollTo(around, anchor: .center) } }
        }
    }
}

/// a row of the card: a faint light under the finger
private struct RowTap: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.background(configuration.isPressed ? Color.white.opacity(0.06) : .clear, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}
