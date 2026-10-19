import XCTest
@testable import Jarvis

/// Naming a place the brain keeps none at (Sky/DayTimelineActions.swift): which stays offer it, what is sent (the name, the stay's own point and the kind of place), that the stay wears the place the brain
/// answers with, its name and its hue, and that a refusal says why under the field and keeps nothing. The brain is a stub that records what it was asked (TLStubBrain); the route itself is in
/// DayActionsTests.
@MainActor
final class DayTimelineNameTests: XCTestCase {
    private var cal: Cal { Cal.cur }
    private var base: Int64 { Int64(cal.origin.timeIntervalSince1970 * 1000) }
    private func at(_ minute: Double) -> Int64 { base + Int64(minute * 60_000) }
    /// the stay at the burger place: a point, and no place he keeps at it
    private let stay = "stay-5"

    private func state(_ stub: TLStubBrain) -> TimelineState {
        let tl = TimelineState()
        tl.day = tl.today
        tl.fixture = .init(replies: [0: TimelineFixture.today(cal)], now: TimelineFixture.now)
        tl.brain = stub.brain
        return tl
    }
    private func model(_ tl: TimelineState) throws -> DayTimelineModel { tl.model(try XCTUnwrap(tl.reply)) }
    private func entry(_ tl: TimelineState, _ id: String) throws -> TLEntry { try XCTUnwrap(try model(tl).entries.first { $0.id == id }, id) }
    private func until(_ what: String, _ ok: () -> Bool) async {
        for _ in 0..<300 where !ok() { try? await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertTrue(ok(), what)
    }
    private func kept(_ name: String, _ role: String = "other") -> KeptPlace { KeptPlace(id: "p-demo-new", name: name, role: role) }
    /// one stay as the brain serves it, with `more` after the common fields
    private func build(_ more: String, done: TLDone = TLDone()) throws -> TLEntry {
        let item = #"{"id": "s", "lane": "you", "kind": "stay", "state": "done", "start_ms": \#(at(600)), "end_ms": \#(at(700)), \#(more)}"#
        let day = #"{"date": "2026-10-08", "tz": "\#(Cal.cur.cal.timeZone.identifier)", "start_ms": \#(base), "end_ms": \#(base + 86_400_000), "now_ms": \#(at(TimelineFixture.now)), "missing": [], "items": [\#(item)]}"#
        let r = try JSONDecoder().decode(DayReply.self, from: Data(day.utf8))
        return try XCTUnwrap(DayTimelineModel.build(r, now: TimelineFixture.now, done: done).entries.first { $0.id == "s" })
    }

    // MARK: which stays offer it

    func testOnlyAStayAtAPointWithNoPlaceHeKeepsOffersAFirstName() throws {
        let tl = state(TLStubBrain())
        let bare = try entry(tl, stay), home = try entry(tl, "stay-1")
        XCTAssertEqual(bare.place?.canName, true)
        XCTAssertEqual(bare.actions, [.name, .ask, .mark, .sayWhat])
        XCTAssertEqual(home.place?.canName, false, "he keeps a place there")
        XCTAssertEqual(home.actions, [.rename, .ask, .mark, .sayWhat], "and renames it")
        // a stay the phone has no coordinates for cannot be kept at a point, whatever its words
        for e in DayTimelineModel.build(TimelineFixture.fragmented(cal), now: nil).entries where e.kind == .stay || e.kind == .pause { XCTAssertFalse(e.actions.contains(.name), e.id) }
        XCTAssertEqual(try build(#""place": "Starbucks, Douglas St""#).actions.contains(.name), false, "words and no point")
        XCTAssertEqual(try build(#""lat": 120, "lng": 10, "place": "X""#).actions.contains(.name), false, "a point that is not on the earth is no point")
        XCTAssertEqual(try build(#""lat": 48.45, "lng": -123.3"#).actions.contains(.name), true, "a point alone is enough")
        // a place he keeps whose id could not stand in a path is not renamed, and is not named again either
        let odd = try build(#""lat": 48.45, "lng": -123.3, "kept_place": {"id": "a/b", "name": "家", "role": "home"}"#)
        XCTAssertFalse(odd.actions.contains(.name) || odd.actions.contains(.rename))
    }

    func testTheMenuOfALongPressOffersToNameAndTheButtonSaysIt() throws {
        let e = try entry(state(TLStubBrain()), stay)
        XCTAssertEqual(TLAction.menu.filter { e.actions.contains($0) }, [.ask, .name, .mark, .sayWhat])
        XCTAssertEqual(TLAction.name.title, "起个名字")
        XCTAssertNotEqual(TLAction.name.symbol, TLAction.rename.symbol)
    }

    func testTheKindsOfPlaceAreTheBrainsThreeInOrder() {
        XCTAssertEqual(TLRole.allCases.map(\.rawValue), ["home", "school", "other"], "jarvis/state/places.py has no more")
        XCTAssertEqual(TLRole.allCases.map(\.word), ["家", "学校", "其他"])
    }

    // MARK: the field

    func testBeginningTheFieldOpensTheRowAndStartsAsOtherEveryTime() throws {
        let tl = state(TLStubBrain())
        let e = try entry(tl, stay)
        XCTAssertEqual(e.barName, "A&W", "what the field starts with: what the phone called it")
        tl.nameRole = .home
        tl.beginRename(e)
        XCTAssertEqual(tl.renaming, stay)
        XCTAssertEqual(tl.openRow, stay)
        XCTAssertEqual(tl.nameRole, .other)
        tl.cancelRename()
        tl.nameRole = .school
        tl.perform(.name, on: e, engine: StarEngine())          // the long-press menu's way
        XCTAssertEqual(tl.renaming, stay)
        XCTAssertEqual(tl.nameRole, .other)
    }

    func testNothingIsSentForAnEmptyOrTooLongName() async throws {
        let stub = TLStubBrain()
        let tl = state(stub)
        let e = try entry(tl, stay)
        tl.beginRename(e)
        tl.rename(e, to: "  \n ")
        XCTAssertEqual(tl.renameNote, "名字不能是空的")
        tl.rename(e, to: String(repeating: "字", count: 81))
        XCTAssertEqual(tl.renameNote, "名字最多 80 个字")
        try? await Task.sleep(nanoseconds: 60_000_000)
        XCTAssertTrue(stub.named.isEmpty)
        XCTAssertEqual(tl.renaming, stay, "the field stays")
        // 80 is the most the brain takes
        tl.rename(e, to: String(repeating: "字", count: 80))
        await until("the brain was asked") { stub.named.count == 1 }
    }

    // MARK: what is sent, and what the stay becomes

    func testNamingSendsTheNameTheStaysOwnPointAndTheKindOnceWhateverIsTappedMeanwhile() async throws {
        let stub = TLStubBrain(); stub.hold = true
        let tl = state(stub)
        let e = try entry(tl, stay)
        tl.beginRename(e)
        tl.nameRole = .school
        tl.rename(e, to: "  A&W   汉堡 ")
        await until("the brain was asked") { stub.named.count == 1 }
        XCTAssertEqual(stub.named.first?.name, "A&W 汉堡", "blanks collapsed as the brain does")
        XCTAssertEqual(stub.named.first?.lat, 48.4538)
        XCTAssertEqual(stub.named.first?.lng, -123.3268)
        XCTAssertEqual(stub.named.first?.role, "school")
        XCTAssertTrue(tl.renameBusy)
        XCTAssertEqual(tl.renaming, stay, "the field stays until the brain has answered")
        XCTAssertFalse(try entry(tl, stay).kept, "and nothing is kept before it has")
        tl.rename(e, to: "another")
        stub.release()
        await until("the field closed") { tl.renaming == nil }
        XCTAssertEqual(stub.named.count, 1, "one word at a time")
        XCTAssertFalse(tl.renameBusy)
    }

    func testTheStayWearsThePlaceTheBrainKeepsItsNameAndHueAndTheDayIsAskedForAgain() async throws {
        for (role, icon) in [(TLRole.home, "house.fill"), (.school, "graduationcap.fill"), (.other, "mappin")] {
            let stub = TLStubBrain()
            stub.nameAnswer = .done(kept("A&W 汉堡", role.rawValue), change: "change-1")
            let tl = state(stub)
            let e = try entry(tl, stay)
            XCTAssertFalse(e.kept)
            XCTAssertEqual(e.color, PlaceTint.lineNeutral)
            tl.beginRename(e)
            tl.nameRole = role
            tl.rename(e, to: "A&W 汉堡")
            await until("the field closed, \(role)") { tl.renaming == nil }
            XCTAssertNil(tl.renameNote)
            XCTAssertEqual(stub.refetches, 1, "the days come again, with the other stays at that point")
            let n = try entry(tl, stay)
            XCTAssertEqual(n.title, "A&W 汉堡", "\(role)")
            XCTAssertTrue(n.kept)
            XCTAssertEqual(n.color, PlaceTint.line(role: role.rawValue, id: "p-demo-new"), "its hue is its kind's")
            XCTAssertEqual(n.barName, "A&W 汉堡")
            XCTAssertEqual(n.barIcon, icon)
            XCTAssertEqual(n.place?.keptId, "p-demo-new")
            XCTAssertEqual(n.place?.keptName, "A&W 汉堡")
            XCTAssertEqual(n.more, ["A&W, Shelbourne St, Victoria", "常去的地方 · A&W 汉堡"])
            XCTAssertEqual(n.actions, [.rename, .ask, .mark, .sayWhat], "it is a place he keeps now: it is renamed")
            // and the bar shows it: the piece is his place, in its hue
            let piece = try XCTUnwrap(try model(tl).scene.pieces.first { $0.id == stay })
            XCTAssertEqual(piece.fill, .place(PlaceTint.line(role: role.rawValue, id: "p-demo-new"), kept: true), "\(role)")
            XCTAssertEqual(piece.name, "A&W 汉堡")
            XCTAssertEqual(piece.icon, icon)
        }
    }

    func testWhatTheBrainRefusesIsSaidUnderTheFieldAndNothingIsKept() async throws {
        let stub = TLStubBrain()
        stub.nameAnswer = .refused("这个名字或这个地方已经记过了")
        let tl = state(stub)
        let e = try entry(tl, stay)
        tl.beginRename(e)
        tl.rename(e, to: "家")
        await until("it says why") { tl.renameNote != nil }
        XCTAssertEqual(tl.renameNote, "没起成：这个名字或这个地方已经记过了")
        XCTAssertEqual(tl.renaming, stay, "the field stays, to try another name")
        XCTAssertFalse(try entry(tl, stay).kept)
        XCTAssertEqual(try entry(tl, stay).title, "A&W")
        XCTAssertTrue(tl.named.isEmpty)
        XCTAssertEqual(stub.refetches, 0)
        await until("not busy") { !tl.renameBusy }
        // another name, which the brain takes
        stub.nameAnswer = .done(kept("小店"), change: nil)
        tl.rename(e, to: "小店")
        await until("the field closed") { tl.renaming == nil }
        XCTAssertNil(tl.renameNote)
        XCTAssertEqual(try entry(tl, stay).title, "小店")
        XCTAssertEqual(stub.named.map(\.name), ["家", "小店"])
    }

    func testAnAnswerThePhoneCannotReadStillClosesTheFieldAndTheDayComesAgain() async throws {
        let stub = TLStubBrain()
        stub.nameAnswer = .done(nil, change: nil)
        let tl = state(stub)
        let e = try entry(tl, stay)
        tl.beginRename(e)
        tl.rename(e, to: "小店")
        await until("the field closed") { tl.renaming == nil }
        XCTAssertEqual(stub.refetches, 1)
        XCTAssertTrue(tl.named.isEmpty, "no place to wear: the day says it when it comes")
        XCTAssertFalse(try entry(tl, stay).kept)
    }

    func testAPlaceHeKeepsIsStillRenamedAndNeverKeptAgain() async throws {
        let stub = TLStubBrain()
        let tl = state(stub)
        let home = try entry(tl, "stay-1")
        tl.beginRename(home)
        tl.rename(home, to: "老家")
        await until("renamed") { tl.renamed["p-home"] != nil }
        XCTAssertEqual(tl.renamed["p-home"]?.name, "老家")
        XCTAssertTrue(stub.named.isEmpty, "that is the other route")
    }

    // MARK: the hold

    func testTheNameShowsUntilTheBrainsOwnAnswerIsTheDays() throws {
        let tl = state(TLStubBrain())
        tl.named[stay] = TLHeld(value: kept("小店"), until: Date().addingTimeInterval(60))
        XCTAssertEqual(try entry(tl, stay).title, "小店")
        tl.named[stay] = TLHeld(value: kept("小店"), until: Date().addingTimeInterval(-1))
        XCTAssertEqual(try entry(tl, stay).title, "A&W", "the hold is over: the day is what the brain says")
        XCTAssertEqual(TimelineState.renameHolds, 120)
    }

    func testAPlaceTheBrainSaysHeKeepsIsTheOneItSaysWhateverHeJustNamed() throws {
        let said = KeptPlace(id: "p-x", name: "X", role: "home")
        let e = try build(#""lat": 48.45, "lng": -123.3, "kept_place": {"id": "p-x", "name": "X", "role": "home"}"#, done: TLDone(named: ["s": kept("Y", "school")]))
        XCTAssertEqual(e.place?.keptId, said.id)
        XCTAssertEqual(e.title, "X")
        XCTAssertEqual(e.barIcon, "house.fill")
    }
}
