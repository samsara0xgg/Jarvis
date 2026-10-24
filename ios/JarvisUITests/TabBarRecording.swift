import XCTest

/// Drives the bar at the foot for a screen recording: taps, a slow press-and-drag along it, a flick, and a tap on a row just over the bar (it must reach the row, not the bar).
final class TabBarRecording: XCTestCase {
    func testTapAndDragTheBar() {
        let app = XCUIApplication()
        app.launchArguments = ["-scene", "时间线:按在干嘛", "-words.language", "zh"]
        app.launch()
        sleep(4)
        let w = app.windows.firstMatch
        func at(_ x: Double, _ y: Double) -> XCUICoordinate { w.coordinate(withNormalizedOffset: CGVector(dx: x, dy: y)) }
        let y = 0.900
        at(0.38, y).tap(); sleep(2)          // 聊天
        at(0.62, y).tap(); sleep(2)          // 地图
        at(0.85, y).tap(); sleep(2)          // 设置
        at(0.15, y).tap(); sleep(2)          // 今天
        at(0.15, y).press(forDuration: 0.6, thenDragTo: at(0.85, y), withVelocity: 120, thenHoldForDuration: 0.6); sleep(2)          // press and drag slowly to 设置
        at(0.85, y).press(forDuration: 0.3, thenDragTo: at(0.40, y), withVelocity: 1500, thenHoldForDuration: 0); sleep(2)          // a flick back
        at(0.15, y).tap(); sleep(2)
        at(0.5, 0.80).tap(); sleep(2)        // a row just over the bar
    }
}
