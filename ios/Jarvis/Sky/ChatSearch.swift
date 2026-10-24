import SwiftUI
import Observation

// The way into what was said, under the chat page's left button (跨端UI标准 §5, ref/聊天-搜索和话题.jpg): a card that drops from it with 「搜我们说过的…」 on top (`GET /inherent/memory/search`)
// and, under it, the topics she keeps (`GET /inherent/topics`: a name, one line of where it stands, how many places, when last; the open ones, then the done ones dimmed). A hit goes to
// its place in the one conversation, those turns light up, and a small bar over the field steps through the places (`ChatTrail`). A topic opens her 前情 of it first, with 接着聊 (what
// he sends next carries the topic's id, `TopicFollow`, and a bar over the field says 「接着陶艺课 ✕」) and 看原话 (its places, as a hit's). Only she opens topics. There is no list of days.
// The brain builds the topics at night and does not serve them, nor take a topic on a send, yet: until it does the card is the search field alone.

// MARK: what the brain says

/// One line of what was said, from a search: `who` is "user" for his words, anything else is hers.
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

/// One topic she keeps (`GET /inherent/topics`, agreed with the brain 2026-10-24): its places oldest first, one a sentence.
struct ChatTopic: Equatable, Identifiable {
    struct Place: Equatable { let recordId: String; let seq: Int }
    let id: String
    let name: String
    var done = false                    // `state` "done": under the open ones, dimmed
    let line: String                    // where it stands now, in one line
    var recap = ""                      // her 前情: where it stands, what is decided, what is still open
    let count: Int
    let lastMs: Int64
    let places: [Place]
}

enum PastParse {
    /// the days of an answer of `GET /inherent/memory/search` (`{days: [{day, hits: [{id, ts, who, text}]}]}`), newest first as the brain sends them, or nil when it is not one
    static func search(_ data: Data) -> [PastHitDay]? {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let days = o["days"] as? [[String: Any]] else { return nil }
        return days.compactMap { d in
            guard let day = d["day"] as? String, let hits = d["hits"] as? [[String: Any]] else { return nil }
            let lines = hits.compactMap { r -> PastLine? in
                guard let id = r["id"] as? String, let ts = r["ts"] as? String, let who = r["who"] as? String, let text = r["text"] as? String else { return nil }
                return PastLine(id: id, ts: ts, who: who, text: text)
            }
            return lines.isEmpty ? nil : PastHitDay(day: day, hits: lines)
        }
    }

    /// the topics of an answer of `GET /inherent/topics` (`{topics: [{id, name, state, line, recap, count, last_ms, positions: [{record_id, seq, ts_ms, by}]}]}`), in the brain's order
    /// (the open ones newest first, then the done ones); a topic with no place is left out
    static func topics(_ data: Data) -> [ChatTopic]? {
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let list = o["topics"] as? [[String: Any]] else { return nil }
        return list.compactMap { t in
            guard let id = t["id"] as? String, let name = t["name"] as? String, let ps = t["positions"] as? [[String: Any]] else { return nil }
            let places = ps.compactMap { p -> ChatTopic.Place? in
                guard let r = p["record_id"] as? String, let s = p["seq"] as? Int else { return nil }
                return ChatTopic.Place(recordId: r, seq: s)
            }
            guard !places.isEmpty else { return nil }
            return ChatTopic(id: id, name: name, done: t["state"] as? String == "done", line: t["line"] as? String ?? "", recap: t["recap"] as? String ?? "", count: t["count"] as? Int ?? places.count,
                             lastMs: (t["last_ms"] as? NSNumber)?.int64Value ?? 0, places: places)
        }
    }

    /// the hits of a search as one list, oldest first: the places a bar steps through
    static func inOrder(_ days: [PastHitDay]) -> [PastLine] { days.reversed().flatMap(\.hits) }
}

extension BrainClient {
    /// `GET /inherent/memory/search?q=&who=`: every record with any of the words, grouped by day.
    static func memorySearch(_ q: String, who: String = "all", bases: [URL], token: String) async throws -> [PastHitDay] {
        let r = try await send("GET", "/inherent/memory/search", query: [URLQueryItem(name: "q", value: q), URLQueryItem(name: "who", value: who)], bases: bases, token: token, unauthorized: .revoked)
        guard let days = PastParse.search(r.data) else { throw BrainError.garbled }
        return days
    }

    /// `GET /inherent/topics`: the topics she keeps, newest first. Not served yet: it fails, and the card has no topics.
    static func topics(bases: [URL], token: String) async throws -> [ChatTopic] {
        let r = try await send("GET", "/inherent/topics", query: [], bases: bases, token: token, unauthorized: .revoked)
        guard let list = PastParse.topics(r.data) else { throw BrainError.garbled }
        return list
    }
}

// MARK: the trail

/// The places a hit or a topic goes through, oldest first, and the one shown: the bar over the field (「陶艺课 · 第 4 处，共 4 处 ‹ › ✕」) steps through them.
struct ChatTrail: Equatable {
    struct Place: Equatable { let recordId: String; var seq: Int? = nil }
    let name: String
    let places: [Place]
    var at: Int
}

/// The topic he goes on with (接着聊): what he sends carries its id (`topic` on the say and on POST /inherent/submit) until he takes it off or the brain refuses it.
/// The frames and the body read it here, so no send path passes it along.
enum TopicFollow {
    nonisolated(unsafe) static var id: String?
}

extension StarEngine {
    /// 接着聊 a topic, or stop (nil)
    func follow(_ t: ChatTopic?) { ui.following = t; TopicFollow.id = t?.id }

    /// The stream goes to the row `seq`: it lists from there (a page of rows older than the newest ones listed), and scrolls to it once they are laid out.
    func showChatSeq(_ seq: Int) {
        wake()
        streamFrom = min(streamFrom ?? .max, seq)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 350_000_000)
            ui.scrollTo = "c\(seq)"
        }
    }

    /// Go to place `i` of the trail: its turn and the answer after it light up and the stream scrolls to them. A place older than the rows held is reached by paging back.
    func goTrail(_ trail: ChatTrail, to i: Int) {
        guard trail.places.indices.contains(i) else { return }
        var t = trail
        t.at = i
        ui.trail = t
        let place = t.places[i]
        Task { @MainActor in
            // ponytail: pages back 200 rows at a time, ten at most; `GET /inherent/conversation?around=` opens there in one ask once the brain serves it
            for _ in 0..<10 {
                let rows = Conversation.shared.rows
                if rows.contains(where: { $0.id == place.recordId }) { break }
                if let s = place.seq, let first = rows.first?.seq, s >= first { break }
                guard await Conversation.shared.loadOlder() else { break }
            }
            let rows = Conversation.shared.rows
            guard ui.trail == t, let k = rows.firstIndex(where: { $0.id == place.recordId }) else { ui.lit = []; return }
            let next = rows.indices.contains(k + 1) && rows[k].isHis && !rows[k + 1].isHis ? [rows[k + 1].seq] : []
            ui.lit = Set(([rows[k].seq] + next).map { "c\($0)" })
            showChatSeq(rows[k].seq)
        }
    }

    func endTrail() { ui.trail = nil; ui.lit = [] }
}

// MARK: the card

/// What the card shows: the topics, or a search's hits.
@MainActor @Observable
final class ChatSearchModel {
    enum Found: Equatable { case idle, searching, hits([PastHitDay]), failed }

    var query = ""
    private(set) var found = Found.idle
    private(set) var topics: [ChatTopic] = []
    /// where the brain is asked: a test hands in its own
    var search: (String) async -> [PastHitDay]? = { q in try? await BrainLink.shared.call { try await BrainClient.memorySearch(q, bases: $0, token: $1) } }
    var loadTopics: () async -> [ChatTopic]? = {
        #if DEBUG
        if let t = ChatSearchModel.sceneTopics { return t }
        #endif
        return try? await BrainLink.shared.call { try await BrainClient.topics(bases: $0, token: $1) }
    }
    #if DEBUG
    /// `-scene 时间线:聊天:话题`: made-up topics for the card's look, as if the brain served them
    nonisolated(unsafe) static var sceneTopics: [ChatTopic]?
    #endif
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

    /// the card opened: her topics as the brain has them now (none while it does not serve them)
    func refreshTopics() async { topics = await loadTopics() ?? [] }

    func reset() { task?.cancel(); query = ""; found = .idle }
}

struct ChatSearchCard: View {
    @Bindable var model: ChatSearchModel
    let engine: StarEngine
    let close: () -> Void
    @FocusState private var focused: Bool
    @State private var opened: ChatTopic?       // a topic tapped: her 前情 of it, in the card's place

    static let width = 320.0, listMax = 380.0

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 20, style: .continuous)
        VStack(alignment: .leading, spacing: 8) {
            if let t = opened { recap(t) } else { list }
        }
        .padding(10)
        .frame(width: Self.width)
        .background(Look.card, in: shape)
        .overlay(shape.strokeBorder(Look.glow.opacity(0.2), lineWidth: 0.6))
        .shadow(color: .black.opacity(0.5), radius: 25, y: 18)
        .animation(.easeOut(duration: 0.2), value: opened)
        .onChange(of: model.query) { model.queryChanged() }
        .task { await model.refreshTopics() }
    }

    /// The topic's 前情 (its one line when she wrote none), and the two ways on: 接着聊, 看原话
    private func recap(_ t: ChatTopic) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Button { opened = nil } label: {
                    Image(systemName: "chevron.left").font(.system(size: 14, weight: .semibold)).foregroundStyle(Look.ink).frame(width: 32, height: 32).contentShape(Rectangle())
                }
                .buttonStyle(.plain).accessibilityLabel(Words.PhoneChat.back)
                Text(t.name).font(.system(size: Look.body, weight: .semibold)).foregroundStyle(Look.ink).lineLimit(1).accessibilityAddTraits(.isHeader)
                Spacer(minLength: 0)
            }
            ScrollView(showsIndicators: false) {
                Text(t.recap.isEmpty ? t.line : t.recap).font(.system(size: Look.note)).foregroundStyle(Look.ink2).lineSpacing(3).frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 240).fixedSize(horizontal: false, vertical: true).padding(.horizontal, 6)
            HStack(spacing: 8) {
                recapButton(Words.PhoneChat.goOnTopic, strong: true) { engine.endTrail(); engine.follow(t); close() }
                recapButton(Words.PhoneChat.seeWords, strong: false) {
                    go(ChatTrail(name: t.name, places: t.places.map { .init(recordId: $0.recordId, seq: $0.seq) }, at: t.places.count - 1))
                }
            }
        }
    }

    private func recapButton(_ title: String, strong: Bool, _ action: @escaping () -> Void) -> some View {
        Button { Haptic.selection(); action() } label: {
            Text(title).font(.system(size: Look.note, weight: .semibold)).foregroundStyle(strong ? Look.sky : Look.ink)
                .frame(maxWidth: .infinity, minHeight: 36).background(strong ? Look.ink : Color.white.opacity(0.08), in: Capsule()).contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder private var list: some View {
            field
            if !model.trimmed.isEmpty || !model.topics.isEmpty {          // no topics yet: the card is the field alone
                ScrollView(showsIndicators: false) { if model.trimmed.isEmpty { topics } else { hits } }.frame(maxHeight: Self.listMax).fixedSize(horizontal: false, vertical: true)
            }
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

    private func note(_ text: String) -> some View {
        Text(text).font(.system(size: Look.note)).foregroundStyle(Look.ink3).frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 10).padding(.vertical, 12)
    }

    /// when, as the list says it: 今天, 昨天, then the date
    private func when(_ at: Date) -> String { TalkStream.title(at, Cal.cur.cal, Date()) }

    // her topics, newest first
    private var topics: some View {
        LazyVStack(alignment: .leading, spacing: 0) {
            Text(Words.PhoneChat.topics).font(.system(size: Look.small, weight: .semibold)).foregroundStyle(Look.ink3).padding(.horizontal, 10).padding(.top, 4).padding(.bottom, 2)
            ForEach(model.topics) { t in
                Button { focused = false; opened = t } label: {
                    VStack(alignment: .leading, spacing: 1) {
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text(t.name).font(.system(size: Look.body, weight: .medium)).foregroundStyle(Look.ink).lineLimit(1)
                            Text("\(t.count)").font(Look.figures(Look.small)).foregroundStyle(Look.ink3)
                            Spacer(minLength: 4)
                            Text(when(Date(timeIntervalSince1970: Double(t.lastMs) / 1000))).font(.system(size: Look.small)).foregroundStyle(Look.ink3)
                        }
                        if !t.line.isEmpty { Text(t.line).font(.system(size: Look.note)).foregroundStyle(Look.ink3).lineLimit(1) }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 10).padding(.vertical, 9).contentShape(Rectangle())
                    .opacity(t.done ? 0.5 : 1)
                }
                .buttonStyle(RowTap())
            }
        }
    }

    // a search's hits, newest first, in one list
    @ViewBuilder private var hits: some View {
        switch model.found {
        case .idle, .searching: note(Words.PhoneChat.searching)
        case .failed: note(Words.PhoneChat.searchFailed)
        case .hits(let days):
            let order = PastParse.inOrder(days)
            if order.isEmpty { note(Words.PhoneChat.noHits) } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(order.enumerated().reversed()), id: \.element.id) { i, h in
                        Button { go(ChatTrail(name: "「\(model.trimmed)」", places: order.map { .init(recordId: $0.id) }, at: i)) } label: {
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                (Text((h.his ? Words.PhoneChat.his : Words.PhoneChat.hers) + "  ").foregroundColor(Look.ink3) + Text(h.text).foregroundColor(Look.ink))
                                    .font(.system(size: Look.note)).lineLimit(2).multilineTextAlignment(.leading)
                                Spacer(minLength: 4)
                                Text(TalkStream.time(h.ts, Cal.cur.cal).map(when) ?? "").font(.system(size: Look.small)).foregroundStyle(Look.ink3)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 10).padding(.vertical, 7).contentShape(Rectangle())
                        }
                        .buttonStyle(RowTap())
                    }
                }
            }
        }
    }

    private func go(_ t: ChatTrail) { focused = false; engine.follow(nil); engine.goTrail(t, to: t.at); close() }
}

/// The bar over the field while a hit or a topic is followed: its name, which place of how many, ‹ › to step and ✕ to stop.
struct ChatTrailBar: View {
    let trail: ChatTrail
    let engine: StarEngine

    var body: some View {
        HStack(spacing: 4) {
            Text(trail.name).font(.system(size: Look.note, weight: .semibold)).foregroundStyle(Look.ink).lineLimit(1)
            Text(Words.PhoneChat.place(trail.at + 1, of: trail.places.count)).font(.system(size: Look.small)).foregroundStyle(Look.ink3).lineLimit(1).padding(.trailing, 4)
            step("chevron.left", Words.PhoneChat.earlierPlace, on: trail.at > 0) { engine.goTrail(trail, to: trail.at - 1) }
            step("chevron.right", Words.PhoneChat.laterPlace, on: trail.at < trail.places.count - 1) { engine.goTrail(trail, to: trail.at + 1) }
            step("xmark", Words.PhoneChat.endTrail, on: true) { engine.endTrail() }
        }
        .padding(.leading, 14).padding(.trailing, 4).frame(height: 36)
        .jarvisGlass(Capsule())
    }

    private func step(_ symbol: String, _ label: String, on: Bool, _ action: @escaping () -> Void) -> some View {
        Button { Haptic.selection(); action() } label: {
            Image(systemName: symbol).font(.system(size: 11, weight: .semibold)).foregroundStyle(Look.ink.opacity(on ? 0.9 : 0.3))
                .frame(width: 28, height: 28).background(Color.white.opacity(0.08), in: Circle()).contentShape(Circle())
        }
        .buttonStyle(.plain).disabled(!on).accessibilityLabel(label)
    }
}

/// The bar over the field while he goes on with a topic: 「接着陶艺课」 and ✕ to stop
struct TopicFollowBar: View {
    let topic: ChatTopic
    let engine: StarEngine

    var body: some View {
        HStack(spacing: 6) {
            Text(Words.PhoneChat.goingOn(topic.name)).font(.system(size: Look.note, weight: .semibold)).foregroundStyle(Look.ink).lineLimit(1)
            Button { Haptic.selection(); engine.follow(nil) } label: {
                Image(systemName: "xmark").font(.system(size: 11, weight: .semibold)).foregroundStyle(Look.ink.opacity(0.9))
                    .frame(width: 28, height: 28).background(Color.white.opacity(0.08), in: Circle()).contentShape(Circle())
            }
            .buttonStyle(.plain).accessibilityLabel(Words.PhoneChat.stopTopic)
        }
        .padding(.leading, 14).padding(.trailing, 4).frame(height: 36)
        .jarvisGlass(Capsule())
    }
}

/// a row of the card: a faint light under the finger
private struct RowTap: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.background(configuration.isPressed ? Color.white.opacity(0.06) : .clear, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}
