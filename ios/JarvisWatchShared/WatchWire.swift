import Foundation

// What the phone and the watch say to each other over WatchConnectivity (the watch reaches the brain only through the phone: docs/adr, the watch's route).
// The phone makes every word and every colour from the brain's day; the watch draws what it is given and makes nothing of its own.
// Compiled into the phone app, the watch app and the watch's complications.

/// The day as the watch draws it: the ring, what is now, what waits on him, what comes next, and the ride he is on. The phone sends a new one with
/// `updateApplicationContext` whenever the day, what waits or the ride changes, so the watch opens with it already there.
struct WatchDay: Codable, Equatable {
    /// the brain's day, yyyy-MM-dd
    var date: String
    /// the language of the words table on the phone ("zh" | "en"); the watch's own words follow it
    var language: String
    /// the minute of the day (from the day's midnight) when the phone made this, and when that was
    var now: Double
    var made: Date
    /// what he was doing until now, solid on the ring in the phone's category colours, in time order
    var blocks: [Block] = []
    /// what is planned after now: short ticks on the ring's faint future
    var ahead: [Tick] = []
    /// the next thing, under her on the home ring (`22:00 陶艺课` and `还有 10 分钟`)
    var headline: Headline?
    /// 现在: the stretch he is in
    var current: Row?
    /// 等你: what waits on him, each answerable from the wrist
    var waiting: [Waiting] = []
    /// 接下来: reminders, calendar, buses, in time order
    var next: [Row] = []
    /// the bus he is on
    var ride: Ride?
    /// when he should leave for the bus he means to take (the watch taps 该出门了 then)
    var leaveAt: Date?
    /// a stretch she is not sure of, which the wrist can say 对 to (the most recent one); optional, as is `slept`, so a day from an older phone decodes
    var unsure: Unsure?
    /// how long he slept the night that ended this day, in minutes (the phone's 睡 number); the wrist's 早 card says it
    var slept: Int?

    struct Block: Codable, Equatable {
        var t0: Double, t1: Double          // minutes from the day's midnight
        var rgb: UInt32                     // 0xRRGGBB, the phone's category colour
        var title: String                   // what the centre says when the block is tapped: `上课`
        var sub: String                     // and under it: `UVic · 15:20–16:40 · 1 小时 20 分`
    }
    struct Tick: Codable, Equatable {
        var t: Double
        var waits: Bool                     // amber: it waits on him
    }
    struct Headline: Codable, Equatable {
        var title: String                   // `22:00 陶艺课`
        var note: String                    // `还有 10 分钟`
        var waits: Bool                     // amber
    }
    /// one row in the phone timeline's anatomy: an icon in its circle, a title, a grey line, something at the right
    struct Row: Codable, Equatable, Identifiable {
        var id: String
        var symbol: String                  // SF Symbol
        var rgb: UInt32?                    // the icon's colour; none: the plain grey of the list
        var title: String
        var sub: String
        var right: String
        var planned = false                 // not happened yet: the circle is dashed
    }
    struct Waiting: Codable, Equatable, Identifiable {
        enum Kind: String, Codable { case reminder, card }
        var id: String
        var kind: Kind
        var symbol: String
        var title: String                   // `吃药`
        var sub: String                     // `21:30 响过了 · 饭后那次`
        var actions: [Action]               // the first is the primary one (double tap)
    }
    struct Action: Codable, Equatable {
        var id: String                      // what the phone answers with (a card's answer, or `ack` for a reminder)
        var title: String                   // `吃了`, `发吧`
    }
    struct Unsure: Codable, Equatable, Identifiable {
        var id: String                      // the stretch's span id, which the brain's answer takes
        var symbol: String
        var rgb: UInt32?
        var title: String                   // `在图书馆学习`
        var span: String                    // `15:20–16:30`
        var line: String                    // how she came to think so
        var yes: String                     // the words a 对 stands for
    }
    struct Ride: Codable, Equatable {
        var route: String                   // `12 路`
        var title: String                   // `还有 3 站`
        var sub: String                     // `去 UVic · 23:24 到`
        var stops: [String]                 // the stops of the ride, first to last (the watch names the first and the last)
        var at: Int                         // the index of the stop he is at or last passed
        var note: String?                   // `下一站下车，提前轻敲你`
        var alightNext: Bool                // the next stop is his
    }
}

/// From the watch to the phone.
enum WatchAsk: Codable, Equatable {
    case refresh                            // send the day again
    case answer(id: String, action: String) // a waiting item answered on the wrist
    case talkStart(id: UUID)                // he started talking; audio follows as data messages tagged with this talk
    case talkEnd(id: UUID)                  // he stopped (a tap, or the watch heard the pause)
    case talkCancel(id: UUID)
    case charge(percent: Int, state: String, at: Date)   // the watch's charge in whole percent, `charging`, `full` or `unplugged`, when it read it (ADR 0377)
}

/// From the phone to the watch.
enum WatchTell: Codable, Equatable {
    case heard(talk: UUID, text: String, final: Bool)       // his words as the phone hears them
    case thinking(talk: UUID)                                // the words went to the brain
    case reply(talk: UUID, spoken: String, written: String, card: WatchDay.Waiting?)   // her answer; a card she put up comes with it, its buttons under her words
    case failed(talk: UUID, reason: String)                  // in the words table's language
}

/// The last day the phone sent, kept in the App Group so the complications draw it too.
enum WatchDayStore {
    private static let key = "watch.day"
    private static var store: UserDefaults? { UserDefaults(suiteName: "group.com.yilunshi.jarvis") }
    static func load() -> WatchDay? { store?.data(forKey: key).flatMap { try? JSONDecoder().decode(WatchDay.self, from: $0) } }
    static func save(_ d: WatchDay) { if let data = try? JSONEncoder().encode(d) { store?.set(data, forKey: key) } }
}

enum WatchWire {
    /// the key of the one value of every WatchConnectivity message (a dictionary), and of the application context
    static let key = "m"
    /// the first byte of an audio data message: then the talk's 16 bytes, then 16 kHz mono Int16 little-endian samples
    static let audioTag: UInt8 = 0xA1
    static let sampleRate = 16_000.0

    static func encode<T: Encodable>(_ v: T) -> [String: Any] {
        guard let d = try? JSONEncoder().encode(v) else { return [:] }
        return [key: d]
    }
    static func decode<T: Decodable>(_ type: T.Type, _ message: [String: Any]) -> T? {
        guard let d = message[key] as? Data else { return nil }
        return try? JSONDecoder().decode(type, from: d)
    }

    static func audio(talk: UUID, samples: Data) -> Data {
        var d = Data([audioTag])
        withUnsafeBytes(of: talk.uuid) { d.append(contentsOf: $0) }
        d.append(samples)
        return d
    }
    /// the talk and the samples of an audio data message, or nil when it is not one
    static func audio(_ d: Data) -> (talk: UUID, samples: Data)? {
        guard d.count >= 17, d.first == audioTag else { return nil }
        let b = [UInt8](d[d.startIndex + 1 ..< d.startIndex + 17])
        let u = UUID(uuid: (b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]))
        return (u, d.subdata(in: d.startIndex + 17 ..< d.endIndex))
    }
}
