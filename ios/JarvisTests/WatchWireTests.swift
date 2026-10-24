import XCTest
@testable import Jarvis

/// What the phone and the watch say to each other (ios/JarvisWatchShared/WatchWire.swift): each message goes through one dictionary and back, and a data message is audio only when it says so.
final class WatchWireTests: XCTestCase {
    private let talk = UUID()

    private var card: WatchDay.Waiting {
        .init(id: "notice-n1", kind: .card, symbol: "bell.fill", title: "吃药", sub: "21:30", actions: [.init(id: "a0", title: "知道了")])
    }

    func testEveryAskRoundTrips() {
        let asks: [WatchAsk] = [.refresh, .answer(id: "reminder-1", action: "ack"), .talkStart(id: talk), .talkEnd(id: talk), .talkCancel(id: talk),
                              .charge(percent: 87, state: "charging", at: Date(timeIntervalSince1970: 1_790_000_123.456))]
        for a in asks { XCTAssertEqual(WatchWire.decode(WatchAsk.self, WatchWire.encode(a)), a) }
    }

    func testEveryTellRoundTrips() {
        let tells: [WatchTell] = [.heard(talk: talk, text: "明天几点", final: false), .heard(talk: talk, text: "明天几点", final: true), .thinking(talk: talk),
                                  .reply(talk: talk, spoken: "八点", written: "", card: nil), .reply(talk: talk, spoken: "要吗", written: "详细", card: card),
                                  .failed(talk: talk, reason: "连不上大脑")]
        for t in tells { XCTAssertEqual(WatchWire.decode(WatchTell.self, WatchWire.encode(t)), t) }
    }

    func testADayRoundTrips() {
        var d = WatchDay(date: "2026-10-08", language: "zh", now: 1310, made: Date(timeIntervalSince1970: 1_790_000_000))
        d.blocks = [.init(t0: 55, t1: 545, rgb: 0x6678CD, title: "睡觉", sub: "09:05 · 8 时")]
        d.ahead = [.init(t: 1320, waits: true)]
        d.headline = .init(title: "22:00 陶艺课", note: "还有 10 分", waits: true)
        d.current = .init(id: "doing-1175", symbol: "laptopcomputer", rgb: 0x5B8CFF, title: "做 Jarvis", sub: "家", right: "2 时")
        d.waiting = [card]
        d.leaveAt = Date(timeIntervalSince1970: 1_790_000_600)
        d.ride = .init(route: "27 路", title: "还有 3 站", sub: "到 UVic", stops: ["A", "B"], at: 0, note: nil, alightNext: false)
        d.unsure = .init(id: "span-1055", symbol: "book.fill", rgb: 0x9CE078, title: "写作业", span: "17:35–18:15", line: "还在学校", yes: "写作业")
        d.slept = 400
        XCTAssertEqual(WatchWire.decode(WatchDay.self, WatchWire.encode(d)), d)
    }

    func testADayFromAnOlderPhoneDecodesWithoutTheNewFields() throws {
        let old = #"{"date": "2026-10-08", "language": "zh", "now": 1310, "made": 0, "blocks": [], "ahead": [], "waiting": [], "next": []}"#
        let d = try XCTUnwrap(WatchWire.decode(WatchDay.self, ["m": Data(old.utf8)]))
        XCTAssertEqual(d.date, "2026-10-08")
        XCTAssertNil(d.unsure)
        XCTAssertNil(d.slept)
    }

    func testWhatIsNotOneOfOursIsNotDecoded() {
        XCTAssertNil(WatchWire.decode(WatchAsk.self, [:]))
        XCTAssertNil(WatchWire.decode(WatchAsk.self, ["m": "refresh"]))
        XCTAssertNil(WatchWire.decode(WatchTell.self, ["m": Data("{}".utf8)]))
        XCTAssertNil(WatchWire.decode(WatchTell.self, WatchWire.encode(WatchAsk.refresh)))
    }

    func testAudioRoundTrips() throws {
        let samples = Data((0..<2400).flatMap { i -> [UInt8] in let v = Int16(truncatingIfNeeded: i * 7 - 8000).littleEndian; return withUnsafeBytes(of: v) { Array($0) } })
        let message = WatchWire.audio(talk: talk, samples: samples)
        XCTAssertEqual(message.count, 17 + samples.count)
        XCTAssertEqual(message.first, 0xA1)
        let got = try XCTUnwrap(WatchWire.audio(message))
        XCTAssertEqual(got.talk, talk)
        XCTAssertEqual(got.samples, samples)
        // a message that begins in the middle of a larger Data (a slice) is read the same
        let slice = (Data([9, 9]) + message).dropFirst(2)
        XCTAssertEqual(WatchWire.audio(slice)?.samples, samples)
    }

    func testADataMessageThatIsNotAudioIsRejected() {
        XCTAssertNil(WatchWire.audio(Data()))
        XCTAssertNil(WatchWire.audio(Data("{\"m\":1}".utf8)))
        XCTAssertNil(WatchWire.audio(WatchWire.audio(talk: talk, samples: Data([1, 2]))[1...]), "the tag is the first byte")
        XCTAssertNil(WatchWire.audio(Data([0xA1] + [UInt8](repeating: 0, count: 15))), "a tag and a talk cut short")
        var wrong = WatchWire.audio(talk: talk, samples: Data([1, 2])); wrong[0] = 0xA2
        XCTAssertNil(WatchWire.audio(wrong))
    }
}
