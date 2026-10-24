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
