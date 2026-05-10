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

  func test_emptyNonStreamingOpenIsIgnored() async throws {
    let model = NativeCardModel()

    model.siriOpen(payload: ["content": "", "kind": "text"])
    try await settle(milliseconds: 80)

    XCTAssertEqual(model.phase, .idle)
    XCTAssertFalse(model.isSubmitted)
    XCTAssertEqual(model.answerText, "")
    XCTAssertEqual(model.stateLabel, "")
  }

  func test_appendAndDoneWithoutOpenAreIgnored() async throws {
    let model = NativeCardModel()

    model.siriAppend(payload: ["token": "orphan token"])
    try await settle(milliseconds: 120)
    XCTAssertEqual(model.phase, .idle)
    XCTAssertEqual(model.answerText, "")
    XCTAssertTrue(model.history.isEmpty)

    model.siriDone(payload: ["fadeMs": 3000])
    try await settle(milliseconds: 120)
    XCTAssertEqual(model.phase, .idle)
    XCTAssertEqual(model.stateLabel, "")
    XCTAssertTrue(model.history.isEmpty)
  }

  func test_newOpenWhileTurnOpenResetsInFlightQuestion() async throws {
    let model = NativeCardModel()

    model.siriOpen(payload: ["q": "old question", "streaming": true])
    try await settle(milliseconds: 80)
    XCTAssertEqual(model.questionText, "old question")

    model.siriOpen(payload: ["content": "# B final", "kind": "text"])
    try await settle(milliseconds: 80)
    model.siriDone(payload: ["fadeMs": 60000])
    try await settle(milliseconds: 80)

    XCTAssertEqual(model.answerText, "# B final")
    XCTAssertEqual(model.questionText, "")
    XCTAssertEqual(model.history.count, 1)
    XCTAssertEqual(model.history.first?.question, "语音")
    XCTAssertEqual(model.history.first?.answer, "# B final")
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

  func test_enterHoldVoiceAcceptedSubmitsTranscriptWithoutRealMicrophone() async throws {
    let wav = Data(repeating: 7, count: 80)
    let backend = FakeNativeBackend(
      voiceResult: VoiceSubmitResult(ok: true, reason: nil, status: "accepted", text: "打开客厅灯", emotion: nil)
    )
    let recorder = FakeVoiceRecorder(stopData: wav)
    let ducker = FakeAudioDucker()
    let model = NativeCardModel(backend: backend, audioDucker: ducker, voiceRecorder: recorder)
    model.inputText = "draft"

    model.handleEnterDown()
    try await settle(milliseconds: 280)
    XCTAssertEqual(model.phase, .listening)
    XCTAssertEqual(model.inputPlaceholder, "正在听…")

    model.handleEnterUp()
    try await settle(milliseconds: 120)

    XCTAssertEqual(recorder.startCount, 1)
    XCTAssertEqual(recorder.stopCount, 1)
    XCTAssertEqual(ducker.duckCount, 1)
    XCTAssertEqual(ducker.restoreCount, 1)
    XCTAssertEqual(backend.voicePayloads, [wav])
    XCTAssertEqual(model.phase, .submitting)
    XCTAssertEqual(model.questionText, "打开客厅灯")
    XCTAssertEqual(model.inputText, "打开客厅灯")
    XCTAssertEqual(model.stateLabel, "thinking")
    XCTAssertEqual(model.stateVariant, .thinking)
  }

  func test_enterHoldVoiceEmptyAudioRestoresDraftAndDoesNotSubmit() async throws {
    let backend = FakeNativeBackend()
    let recorder = FakeVoiceRecorder(stopData: Data(repeating: 0, count: 44))
    let ducker = FakeAudioDucker()
    let model = NativeCardModel(backend: backend, audioDucker: ducker, voiceRecorder: recorder)
    model.inputText = "keep draft"

    model.handleEnterDown()
    try await settle(milliseconds: 280)
    model.handleEnterUp()
    try await settle(milliseconds: 120)

    XCTAssertEqual(backend.voicePayloads, [])
    XCTAssertEqual(recorder.startCount, 1)
    XCTAssertEqual(recorder.stopCount, 1)
    XCTAssertEqual(model.phase, .input)
    XCTAssertEqual(model.inputText, "keep draft")
    XCTAssertEqual(model.inputPlaceholder, "问点什么…")
    XCTAssertFalse(model.inputDisabled)
    XCTAssertEqual(model.stateLabel, "no speech")
    XCTAssertEqual(model.stateVariant, .warn)
  }

  func test_enterHoldVoiceNetworkErrorRestoresDraftForRetry() async throws {
    let backend = FakeNativeBackend(
      voiceResult: VoiceSubmitResult(ok: false, reason: "network", status: nil, text: nil, emotion: nil)
    )
    let recorder = FakeVoiceRecorder(stopData: Data(repeating: 4, count: 80))
    let ducker = FakeAudioDucker()
    let model = NativeCardModel(backend: backend, audioDucker: ducker, voiceRecorder: recorder)
    model.inputText = "retry draft"

    model.handleEnterDown()
    try await settle(milliseconds: 280)
    model.handleEnterUp()
    try await settle(milliseconds: 120)

    XCTAssertEqual(backend.voicePayloads.count, 1)
    XCTAssertEqual(model.phase, .input)
    XCTAssertEqual(model.inputText, "retry draft")
    XCTAssertEqual(model.inputPlaceholder, "问点什么…")
    XCTAssertFalse(model.inputDisabled)
    XCTAssertEqual(model.stateLabel, "offline")
    XCTAssertEqual(model.stateVariant, .error)
  }

  func test_enterHoldVoiceMicrophoneDeniedRestoresDraftForRetry() async throws {
    let backend = FakeNativeBackend()
    let recorder = FakeVoiceRecorder(startError: NativeVoiceRecorderError.microphoneDenied)
    let ducker = FakeAudioDucker()
    let model = NativeCardModel(backend: backend, audioDucker: ducker, voiceRecorder: recorder)
    model.inputText = "typed draft"

    model.handleEnterDown()
    try await settle(milliseconds: 280)
    model.handleEnterUp()
    try await settle(milliseconds: 120)

    XCTAssertEqual(recorder.startCount, 1)
    XCTAssertEqual(recorder.stopCount, 0)
    XCTAssertEqual(backend.voicePayloads, [])
    XCTAssertEqual(ducker.duckCount, 1)
    XCTAssertEqual(ducker.restoreCount, 1)
    XCTAssertEqual(model.phase, .input)
    XCTAssertEqual(model.inputText, "typed draft")
    XCTAssertEqual(model.inputPlaceholder, "问点什么…")
    XCTAssertFalse(model.inputDisabled)
    XCTAssertEqual(model.stateLabel, "mic denied")
    XCTAssertEqual(model.stateVariant, .error)
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
    XCTAssertTrue(model.attachmentEdgeFlash)
    try await settle(milliseconds: 820)
    XCTAssertFalse(model.attachmentEdgeFlash)
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

  func test_answerParserPreservesFenceLanguage() {
    let markdown = """
    ```python
    def hello():
        return 42
    ```
    """

    XCTAssertEqual(
      NativeAnswerParser.parse(markdown),
      [.code("def hello():\n    return 42", language: "python")]
    )
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

private final class FakeNativeBackend: NativeBackendSubmitting {
  var submitTexts: [String] = []
  var imagePayloads: [(text: String, imageData: Data, mime: String, name: String)] = []
  var voicePayloads: [Data] = []
  var submitResult = SubmitResult(ok: true, reason: nil)
  var imageResult = SubmitResult(ok: true, reason: nil)
  var voiceResult: VoiceSubmitResult

  init(
    voiceResult: VoiceSubmitResult = VoiceSubmitResult(
      ok: true,
      reason: nil,
      status: "accepted",
      text: "voice text",
      emotion: nil
    )
  ) {
    self.voiceResult = voiceResult
  }

  func submit(text: String) async -> SubmitResult {
    submitTexts.append(text)
    return submitResult
  }

  func submitImage(text: String, imageData: Data, mime: String, name: String) async -> SubmitResult {
    imagePayloads.append((text, imageData, mime, name))
    return imageResult
  }

  func submitVoice(wavData: Data) async -> VoiceSubmitResult {
    voicePayloads.append(wavData)
    return voiceResult
  }
}

private final class FakeVoiceRecorder: NativeVoiceRecording {
  var startCount = 0
  var stopCount = 0
  var startError: Error?
  var stopData: Data?

  init(stopData: Data? = Data(repeating: 1, count: 80), startError: Error? = nil) {
    self.stopData = stopData
    self.startError = startError
  }

  func start() async throws {
    startCount += 1
    if let startError { throw startError }
  }

  func stop() async -> Data? {
    stopCount += 1
    return stopData
  }
}

private final class FakeAudioDucker: NativeAudioDucking {
  var duckCount = 0
  var restoreCount = 0
  var restoreAllCount = 0
  var duckResult = true

  func duck() -> Bool {
    duckCount += 1
    return duckResult
  }

  func restore() {
    restoreCount += 1
  }

  func restoreAll() {
    restoreAllCount += 1
  }
}
