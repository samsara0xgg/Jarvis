import XCTest

/// The pages for review, on made-up days: each shot is written to $SHOTS/<name>.png.
final class PageShots: XCTestCase {
    private let dir = ProcessInfo.processInfo.environment["SHOTS"] ?? NSTemporaryDirectory()

    private func shot(_ name: String) {
        try? XCUIScreen.main.screenshot().pngRepresentation.write(to: URL(fileURLWithPath: dir).appendingPathComponent(name + ".png"))
    }
    private func open(_ scene: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-scene", scene, "-words.language", "zh"]
        app.launch()
        sleep(5)
        return app
    }
    private func tab(_ app: XCUIApplication, _ i: Int) { app.tabBars.firstMatch.buttons.element(boundBy: i).tap(); sleep(2) }

    func testToday() {
        let app = open("时间线:按在干嘛")
        shot("1-today-top")
        let w = app.windows.firstMatch
        w.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).press(forDuration: 0.05, thenDragTo: w.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.85)))
        sleep(2)
        shot("2-today-scrolled")
        tab(app, 3)
        shot("6-settings")
    }

    func testChat() {
        let app = open("时间线:聊天")
        let w = app.windows.firstMatch
        w.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.45)).press(forDuration: 0.05, thenDragTo: w.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.55)))
        sleep(2)
        shot("3-chat-under-her")
    }

    func testChatTrail() {
        _ = open("时间线:聊天:跟")
        sleep(2)
        shot("3b-chat-topic-followed")
    }

    func testChatTopics() {
        let app = open("时间线:聊天:话题")
        app.buttons["搜我们说过的和她整理的话题"].tap()
        sleep(2)
        shot("3c-chat-search-topics")
        app.buttons.containing(NSPredicate(format: "label CONTAINS '这周的安排'")).firstMatch.tap()
        sleep(1)
        shot("3d-chat-topic-recap")
        app.buttons["接着聊"].tap()
        sleep(1)
        shot("3e-chat-going-on")
    }

    func testCard() {
        let app = open("时间线:来回")
        shot("7-card-dialog-today")
        app.windows.firstMatch.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.75)).tap()
        sleep(2)
        shot("8-card-folded")
    }

    func testCardMap() {
        _ = open("时间线:来回:地图")
        shot("9-card-map")
    }

    func testCardLong() {
        _ = open("时间线:长答")
        sleep(2)
        shot("10-card-long-answer")
    }

    func testCardTidy() {
        _ = open("时间线:理一下")
        shot("11-card-tidying")
    }

    func testMap() {
        _ = open("足迹")
        shot("4-map-day")
    }

    func testTrip() {
        _ = open("足迹:开:0")
        sleep(2)
        shot("5-map-trip")
    }
}
