import XCTest
@testable import InherentCard

final class SystemAudioDuckerTests: XCTestCase {
  func test_parseSnapshot() throws {
    XCTAssertEqual(
      try SystemAudioDucker.parseSnapshot("42,false"),
      SystemAudioDucker.Snapshot(outputVolume: 42, outputMuted: false)
    )
    XCTAssertEqual(
      try SystemAudioDucker.parseSnapshot("125,true"),
      SystemAudioDucker.Snapshot(outputVolume: 100, outputMuted: true)
    )
  }
}
