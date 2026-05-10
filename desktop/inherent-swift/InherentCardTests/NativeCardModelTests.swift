import AppKit
import XCTest
@testable import InherentCard

@MainActor
final class NativeCardModelTests: XCTestCase {
  func test_streamingTurnPushesHistoryOnDone() async throws {
    let model = NativeCardModel()

    model.siriOpen(payload: ["q": "今天天气怎么样", "streaming": true])
    try await settle()
    XCTAssertTrue(model.isSubmitted)
    XCTAssertEqual(model.stateLabel, "thinking")

    model.siriAppend(payload: ["token": "Victoria 当前 14°C"])
    try await settle(milliseconds: 700)
    XCTAssertEqual(model.phase, .streaming)
    XCTAssertTrue(model.answerText.contains("Victoria"))

    model.siriDone(payload: ["fadeMs": 60000])
    try await settle(milliseconds: 80)
    XCTAssertEqual(model.phase, .done)
    XCTAssertEqual(model.history.count, 1)
    XCTAssertEqual(model.history.first?.question, "今天天气怎么样")
    XCTAssertEqual(model.history.first?.answer, "Victoria 当前 14°C")
    XCTAssertTrue(model.isHistoryShown)
  }

  func test_resetReturnsToIdleInputShell() async throws {
    let model = NativeCardModel()

    model.siriOpen(payload: ["q": "todo", "content": "Todos:\n- 吃药"])
    try await settle(milliseconds: 80)
    XCTAssertTrue(model.isSubmitted)
    XCTAssertFalse(model.answerText.isEmpty)

    model.siriReset()
    try await settle(milliseconds: 80)
    XCTAssertEqual(model.phase, .idle)
    XCTAssertFalse(model.isSubmitted)
    XCTAssertEqual(model.inputText, "")
    XCTAssertEqual(model.answerText, "")
    XCTAssertEqual(model.stateLabel, "")
  }

  func test_externalVoiceAcceptedUsesTranscriptAsSubmittedQuestion() async throws {
    let model = NativeCardModel()

    model.voiceState(payload: ["phase": "accepted", "text": "客厅几度"])
    try await settle(milliseconds: 80)

    XCTAssertEqual(model.phase, .submitting)
    XCTAssertTrue(model.isSubmitted)
    XCTAssertEqual(model.questionText, "客厅几度")
    XCTAssertEqual(model.stateLabel, "thinking")
    XCTAssertEqual(model.stateVariant, .thinking)
  }

  func test_followupInputCanRestorePreviousAnswerWhenSubmittedEmpty() async throws {
    let model = NativeCardModel()

    model.siriOpen(payload: ["q": "first", "content": "# Answer"])
    try await settle(milliseconds: 80)
    model.siriDone(payload: ["fadeMs": 60000])
    try await settle(milliseconds: 80)

    model.handleGlobalEnterDown()
    model.handleEnterUp()
    try await settle(milliseconds: 320)

    XCTAssertTrue(model.isFollowupInput)
    XCTAssertFalse(model.isSubmitted)
    XCTAssertEqual(model.inputPlaceholder, "继续问…")
    XCTAssertEqual(model.answerText, "")

    model.submitInputText()
    try await settle(milliseconds: 80)

    XCTAssertTrue(model.isSubmitted)
    XCTAssertFalse(model.isFollowupInput)
    XCTAssertEqual(model.answerText, "# Answer")
    XCTAssertEqual(model.stateLabel, "done")
  }

  func test_popoverHideAndClearHistoryCascade() async throws {
    let model = NativeCardModel()

    model.siriOpen(payload: ["q": "first", "content": "# Answer"])
    try await settle(milliseconds: 80)
    model.siriDone(payload: ["fadeMs": 60000])
    try await settle(milliseconds: 80)

    guard let turn = model.history.first else {
      return XCTFail("expected history turn")
    }
    model.showPopover(for: turn)
    XCTAssertTrue(model.popoverVisible)
    model.schedulePopoverHide()
    try await settle(milliseconds: 520)
    XCTAssertFalse(model.popoverVisible)

    model.clearHistoryCascade()
    try await settle(milliseconds: 520)

    XCTAssertTrue(model.history.isEmpty)
    XCTAssertFalse(model.isHistoryShown)
    XCTAssertFalse(model.popoverVisible)
  }

  func test_imageDropStagesAttachment() async throws {
    let model = NativeCardModel()
    let image = NSImage(size: NSSize(width: 2, height: 2))
    image.lockFocus()
    NSColor.red.setFill()
    NSRect(x: 0, y: 0, width: 2, height: 2).fill()
    image.unlockFocus()

    let provider = NSItemProvider(object: image)

    XCTAssertTrue(model.handleDrop(providers: [provider]))
    try await settle(milliseconds: 300)

    XCTAssertEqual(model.phase, .input)
    XCTAssertEqual(model.stagedImage?.mime, "image/png")
    XCTAssertEqual(model.stagedImage?.name, "drop.png")
    XCTAssertEqual(model.stateLabel, "idle")
  }

  func test_plainTextClipboardDoesNotBypassTextFieldPasteSemantics() {
    let model = NativeCardModel()
    model.inputText = "before"
    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    defer { pasteboard.clearContents() }
    pasteboard.setString(" after", forType: .string)

    XCTAssertFalse(model.stageImageFromClipboard())
    XCTAssertEqual(model.inputText, "before")
  }

  func test_imageClipboardStagesAttachment() async throws {
    let model = NativeCardModel()
    let image = NSImage(size: NSSize(width: 2, height: 2))
    image.lockFocus()
    NSColor.blue.setFill()
    NSRect(x: 0, y: 0, width: 2, height: 2).fill()
    image.unlockFocus()
    guard let tiff = image.tiffRepresentation else {
      return XCTFail("expected test image tiff")
    }

    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    defer { pasteboard.clearContents() }
    pasteboard.setData(tiff, forType: .tiff)

    XCTAssertTrue(model.stageImageFromClipboard())
    XCTAssertEqual(model.stagedImage?.mime, "image/png")
    XCTAssertEqual(model.stagedImage?.name, "screen.png")
  }

  func test_imageClipboardIgnoredWhileTurnIsActive() async throws {
    let model = NativeCardModel()
    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    defer { pasteboard.clearContents() }
    let image = NSImage(size: NSSize(width: 2, height: 2))
    image.lockFocus()
    NSColor.green.setFill()
    NSRect(x: 0, y: 0, width: 2, height: 2).fill()
    image.unlockFocus()
    guard let tiff = image.tiffRepresentation else {
      return XCTFail("expected test image tiff")
    }
    pasteboard.setData(tiff, forType: .tiff)

    model.siriOpen(payload: ["q": "active", "streaming": true])
    try await settle(milliseconds: 80)
    XCTAssertEqual(model.phase, .submitting)
    XCTAssertFalse(model.stageImageFromClipboard())
    XCTAssertNil(model.stagedImage)
    XCTAssertEqual(model.phase, .submitting)

    model.siriAppend(payload: ["token": "streaming answer"])
    try await settle(milliseconds: 700)
    XCTAssertEqual(model.phase, .streaming)
    XCTAssertFalse(model.stageImageFromClipboard())
    XCTAssertNil(model.stagedImage)
    XCTAssertEqual(model.phase, .streaming)
  }

  func test_answerParserRecognizesLegacyHtmlPrimitives() {
    let html = """
    <div class="tool done"><span class="tool-tag">hue</span><span class="tool-name">set bedroom lamp</span><span class="tool-status">done</span></div>
    <div class="choose"><span class="opt picked">A</span><span class="opt faded">B</span></div>
    <div class="confirm-gate"><span class="gate-btn allow picked">allow</span><span class="gate-btn deny faded">deny</span></div>
    <div class="tts ducked"><span class="bar"></span><span class="bar"></span></div>
    <span class="display">23°</span>
    <span class="display-label">bedroom</span>
    """

    let blocks = NativeAnswerParser.parse(html)

    guard case .tool(let tool) = blocks[0] else {
      return XCTFail("expected first block to be tool")
    }
    XCTAssertEqual(tool.tag, "hue")
    XCTAssertEqual(tool.name, "set bedroom lamp")
    XCTAssertEqual(tool.status, "done")

    guard case .choice(let options) = blocks[1] else {
      return XCTFail("expected second block to be choice")
    }
    XCTAssertEqual(options.map(\.label), ["A", "B"])
    XCTAssertTrue(options[0].picked)
    XCTAssertTrue(options[1].faded)

    guard case .confirmGate(let buttons) = blocks[2] else {
      return XCTFail("expected third block to be confirm gate")
    }
    XCTAssertTrue(buttons[0].allow)
    XCTAssertTrue(buttons[0].picked)
    XCTAssertTrue(buttons[1].deny)
    XCTAssertTrue(buttons[1].faded)

    guard case .tts(let style) = blocks[3] else {
      return XCTFail("expected fourth block to be tts")
    }
    XCTAssertTrue(style.classes.contains("ducked"))

    XCTAssertEqual(blocks[4], .display("23°"))
    XCTAssertEqual(blocks[5], .displayLabel("bedroom"))
  }

  func test_answerParserRecognizesMarkdownTables() {
    let markdown = """
    | name | value |
    | --- | :---: |
    | bedroom | 23° |
    | lamp | on |
    """

    let blocks = NativeAnswerParser.parse(markdown)

    XCTAssertEqual(blocks, [
      .table(
        headers: ["name", "value"],
        rows: [
          ["bedroom", "23°"],
          ["lamp", "on"],
        ]
      ),
    ])
  }

  func test_answerParserRecognizesLowerHeadingsAndOrderedLists() {
    let markdown = """
    ### Details
    #### Small
    1. First
    2) Second
    """

    let blocks = NativeAnswerParser.parse(markdown)

    XCTAssertEqual(blocks, [
      .heading3("Details"),
      .heading4("Small"),
      .numbered("1.", "First"),
      .numbered("2.", "Second"),
    ])
  }

  func test_answerParserCombinesContiguousParagraphLines() {
    let markdown = """
    bedroom · 客厅 22°  
    _(via siri:open IPC)_

    next paragraph
    """

    let blocks = NativeAnswerParser.parse(markdown)

    XCTAssertEqual(blocks, [
      .paragraph("bedroom · 客厅 22°\n_(via siri:open IPC)_"),
      .paragraph("next paragraph"),
    ])
  }

  private func settle(milliseconds: UInt64 = 20) async throws {
    try await Task.sleep(nanoseconds: milliseconds * 1_000_000)
  }
}
