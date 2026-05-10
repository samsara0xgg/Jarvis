import AppKit
import SwiftUI
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

  func test_externalVoiceListeningAndTranscribingStatesMatchWebLifecycle() async throws {
    let model = NativeCardModel()
    model.inputText = "draft"

    model.voiceState(payload: ["phase": "listening"])
    try await settle(milliseconds: 80)

    XCTAssertEqual(model.phase, .listening)
    XCTAssertTrue(model.isListening)
    XCTAssertFalse(model.isSubmitted)
    XCTAssertEqual(model.inputText, "")
    XCTAssertEqual(model.inputPlaceholder, "正在听…")
    XCTAssertTrue(model.inputDisabled)
    XCTAssertEqual(model.stateLabel, "listening")

    model.voiceState(payload: ["phase": "transcribing"])
    try await settle(milliseconds: 80)

    XCTAssertEqual(model.phase, .transcribing)
    XCTAssertFalse(model.isListening)
    XCTAssertEqual(model.inputPlaceholder, "识别中…")
    XCTAssertTrue(model.inputDisabled)
    XCTAssertEqual(model.stateLabel, "transcribing")
    XCTAssertEqual(model.stateVariant, .neutral)
  }

  func test_externalVoiceEmptyAndErrorStatesRecoverToRetryShell() async throws {
    let model = NativeCardModel()

    model.voiceState(payload: ["phase": "empty"])
    try await settle(milliseconds: 80)

    XCTAssertEqual(model.phase, .idle)
    XCTAssertFalse(model.isListening)
    XCTAssertFalse(model.isSubmitted)
    XCTAssertEqual(model.inputText, "")
    XCTAssertEqual(model.inputPlaceholder, "问点什么…")
    XCTAssertTrue(model.inputDisabled)
    XCTAssertEqual(model.stateLabel, "no speech")
    XCTAssertEqual(model.stateVariant, .warn)

    model.voiceState(payload: ["phase": "error"])
    try await settle(milliseconds: 80)

    XCTAssertEqual(model.phase, .error)
    XCTAssertFalse(model.isListening)
    XCTAssertFalse(model.isSubmitted)
    XCTAssertEqual(model.inputText, "")
    XCTAssertEqual(model.inputPlaceholder, "问点什么…")
    XCTAssertTrue(model.inputDisabled)
    XCTAssertEqual(model.stateLabel, "error")
    XCTAssertEqual(model.stateVariant, .error)
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

  func test_newSiriOpenCancelsPendingFollowupInputTransition() async throws {
    let model = NativeCardModel()

    model.siriOpen(payload: ["q": "first", "content": "# Answer"])
    try await settle(milliseconds: 80)
    model.siriDone(payload: ["fadeMs": 60000])
    try await settle(milliseconds: 80)

    model.handleGlobalEnterDown()
    model.handleEnterUp()
    try await settle(milliseconds: 80)
    XCTAssertTrue(model.isFollowupEntering)

    model.siriOpen(payload: ["q": "second", "content": "# Second"])
    try await settle(milliseconds: 360)

    XCTAssertEqual(model.phase, .streaming)
    XCTAssertTrue(model.isSubmitted)
    XCTAssertFalse(model.isFollowupInput)
    XCTAssertFalse(model.isFollowupEntering)
    XCTAssertEqual(model.questionText, "second")
    XCTAssertEqual(model.answerText, "# Second")
    XCTAssertEqual(model.stateLabel, "streaming")
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

  func test_nonImageFileDropIsIgnoredLikeWeb() async throws {
    let model = NativeCardModel()
    let url = FileManager.default.temporaryDirectory
      .appendingPathComponent("jarvis-inherent-drop-\(UUID().uuidString).txt")
    try Data("not an image".utf8).write(to: url)
    defer { try? FileManager.default.removeItem(at: url) }

    let provider = NSItemProvider(object: url as NSURL)

    XCTAssertTrue(model.handleDrop(providers: [provider]))
    try await settle(milliseconds: 300)

    XCTAssertNil(model.stagedImage)
    XCTAssertEqual(model.stateLabel, "")
    XCTAssertEqual(model.phase, .idle)
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
    guard let png = makePNG(width: 4, height: 3) else {
      return XCTFail("expected test image png")
    }

    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    defer { pasteboard.clearContents() }
    pasteboard.setData(png, forType: .png)

    XCTAssertTrue(model.stageImageFromClipboard())
    XCTAssertEqual(model.stagedImage?.mime, "image/png")
    XCTAssertEqual(model.stagedImage?.name, "screen.png")
    XCTAssertEqual(model.stagedImage?.meta, "4×3")
    XCTAssertTrue(model.attachmentEdgeFlash)
    try await settle(milliseconds: 820)
    XCTAssertFalse(model.attachmentEdgeFlash)
  }

  func test_jpegClipboardStagesAttachment() async throws {
    let model = NativeCardModel()
    let image = NSImage(size: NSSize(width: 2, height: 2))
    image.lockFocus()
    NSColor.orange.setFill()
    NSRect(x: 0, y: 0, width: 2, height: 2).fill()
    image.unlockFocus()
    guard let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff),
          let jpeg = rep.representation(using: .jpeg, properties: [:]) else {
      return XCTFail("expected test image jpeg")
    }

    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    defer { pasteboard.clearContents() }
    pasteboard.setData(jpeg, forType: NSPasteboard.PasteboardType("public.jpeg"))

    XCTAssertTrue(model.stageImageFromClipboard())
    XCTAssertEqual(model.stagedImage?.mime, "image/jpeg")
    XCTAssertEqual(model.stagedImage?.name, "screen.jpg")
    XCTAssertEqual(model.stagedImage?.label, "screen")
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

  func test_imageClipboardAfterCompletedAnswerSurvivesFollowupTransition() async throws {
    let model = NativeCardModel()
    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    defer { pasteboard.clearContents() }
    let image = NSImage(size: NSSize(width: 2, height: 2))
    image.lockFocus()
    NSColor.purple.setFill()
    NSRect(x: 0, y: 0, width: 2, height: 2).fill()
    image.unlockFocus()
    guard let tiff = image.tiffRepresentation else {
      return XCTFail("expected test image tiff")
    }
    pasteboard.setData(tiff, forType: .tiff)

    model.siriOpen(payload: ["q": "first", "content": "# Answer"])
    try await settle(milliseconds: 80)
    model.siriDone(payload: ["fadeMs": 60000])
    try await settle(milliseconds: 80)

    XCTAssertTrue(model.stageImageFromClipboard())
    XCTAssertTrue(model.isFollowupEntering)
    XCTAssertNil(model.stagedImage)

    try await settle(milliseconds: 360)
    XCTAssertEqual(model.phase, .input)
    XCTAssertTrue(model.isFollowupInput)
    XCTAssertEqual(model.inputPlaceholder, "继续问…")
    XCTAssertEqual(model.stagedImage?.mime, "image/png")
    XCTAssertEqual(model.stagedImage?.name, "screen.png")
  }

  func test_submitStagedImageUsesImageEndpointAndFallbackQuestion() async throws {
    let backend = FakeNativeBackend()
    let model = NativeCardModel(backend: backend)
    guard let png = makePNG(width: 4, height: 3) else {
      return XCTFail("expected test image png")
    }
    model.stagedImage = NativeImageAttachment(
      data: png,
      mime: "image/png",
      name: "screen.png",
      label: "screen",
      meta: "4×3"
    )

    model.submitInputText()
    try await settle(milliseconds: 80)

    XCTAssertEqual(backend.submitTexts, [])
    XCTAssertEqual(backend.imagePayloads.count, 1)
    XCTAssertEqual(backend.imagePayloads[0].text, "")
    XCTAssertEqual(backend.imagePayloads[0].mime, "image/png")
    XCTAssertEqual(backend.imagePayloads[0].name, "screen.png")
    XCTAssertEqual(backend.imagePayloads[0].imageData, png)
    XCTAssertEqual(model.questionText, "请看这张图片")
    XCTAssertTrue(model.isSubmitted)
    XCTAssertEqual(model.phase, .submitting)
    XCTAssertEqual(model.stateLabel, "thinking")
  }

  func test_submitStagedImageFailureShowsOfflineState() async throws {
    let backend = FakeNativeBackend()
    backend.imageResult = SubmitResult(ok: false, reason: "network")
    let model = NativeCardModel(backend: backend)
    guard let png = makePNG(width: 2, height: 2) else {
      return XCTFail("expected test image png")
    }
    model.inputText = "这是什么"
    model.stagedImage = NativeImageAttachment(
      data: png,
      mime: "image/png",
      name: "screen.png",
      label: "screen",
      meta: "2×2"
    )

    model.submitInputText()
    try await settle(milliseconds: 80)

    XCTAssertEqual(backend.submitTexts, [])
    XCTAssertEqual(backend.imagePayloads.count, 1)
    XCTAssertEqual(backend.imagePayloads[0].text, "这是什么")
    XCTAssertEqual(model.questionText, "这是什么")
    XCTAssertEqual(model.phase, .error)
    XCTAssertEqual(model.stateLabel, "offline")
    XCTAssertEqual(model.stateVariant, .error)
  }

  func test_answerParserEscapesRawHtmlLikeMarkdownIt() {
    let html = #"<div class="tool done"><span class="tool-tag">hue</span></div>"#

    XCTAssertEqual(NativeAnswerParser.parse(html), [.paragraph(html)])

    let attributed = NativeInlineStyler.attributed(
      markdown: html,
      baseColor: Color.white.opacity(0.92)
    )
    XCTAssertEqual(String(attributed.characters), html)
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
    ##### Tiny
    ###### Smallest
    1. First
    2) Second
    """

    let blocks = NativeAnswerParser.parse(markdown)

    XCTAssertEqual(blocks, [
      .heading3("Details"),
      .heading4("Small"),
      .heading5("Tiny"),
      .heading6("Smallest"),
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

  func test_answerParserRecognizesMarkdownItBulletAndFenceVariants() {
    let markdown = """
    intro
    ##### Boundary
    * Star
    + Plus
    ~~~swift
    let value = 1
    ~~~~
    ```
    ~~~ stays inside backtick fence
    ```
    """

    XCTAssertEqual(NativeAnswerParser.parse(markdown), [
      .paragraph("intro"),
      .heading5("Boundary"),
      .bullet("Star"),
      .bullet("Plus"),
      .code("let value = 1", language: "swift"),
      .code("~~~ stays inside backtick fence", language: nil),
    ])
  }

  func test_answerParserCombinesContiguousBlockquoteLines() {
    let markdown = """
    > first line
    > second line
    >
    > fourth line

    after
    """

    XCTAssertEqual(NativeAnswerParser.parse(markdown), [
      .blockquote("first line\nsecond line\n\nfourth line"),
      .paragraph("after"),
    ])
  }

  func test_answerParserRecognizesSetextHeadings() {
    let markdown = """
    Main title
    ===

    Sub title
    ---

    ***

    Body
    """

    XCTAssertEqual(NativeAnswerParser.parse(markdown), [
      .heading1("Main title"),
      .heading2("Sub title"),
      .rule,
      .paragraph("Body"),
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

  func test_inlineMarkdownStylerKeepsWebInlineColorsAndLinks() {
    let attributed = NativeInlineStyler.attributed(
      markdown: "**Bold** *em* ~~gone~~ `code` [linked](https://example.com) https://plain.example",
      baseColor: Color.white.opacity(0.92)
    )

    var seen: Set<String> = []
    for run in attributed.runs {
      let text = String(attributed[run.range].characters)
      switch text {
      case "Bold":
        XCTAssertEqual(run.foregroundColor, NativeInlineStyler.strongColor)
        seen.insert("strong")
      case "em":
        XCTAssertEqual(run.foregroundColor, NativeInlineStyler.emphasisColor)
        seen.insert("emphasis")
      case "gone":
        XCTAssertEqual(run.foregroundColor, NativeInlineStyler.deletedColor)
        seen.insert("deleted")
      case "code":
        XCTAssertEqual(run.foregroundColor, NativeInlineStyler.inlineCodeColor)
        XCTAssertEqual(run.backgroundColor, NativeInlineStyler.inlineCodeBackgroundColor)
        seen.insert("code")
      case "linked":
        XCTAssertEqual(run.link?.absoluteString, "https://example.com")
        XCTAssertEqual(run.foregroundColor, NativeInlineStyler.linkColor)
        seen.insert("markdown-link")
      case "https://plain.example":
        XCTAssertEqual(run.link?.absoluteString, "https://plain.example")
        XCTAssertEqual(run.foregroundColor, NativeInlineStyler.linkColor)
        seen.insert("bare-link")
      default:
        break
      }
    }

    XCTAssertEqual(seen, ["strong", "emphasis", "deleted", "code", "markdown-link", "bare-link"])
  }

  func test_inlineMarkdownStylerKeepsRunColorDuringCharacterFade() {
    let markdown = "[link](https://example.com) `code`"
    let visibleCount = NativeAnswerParser.plainInlineText(markdown).count
    let attributed = NativeInlineStyler.attributed(
      markdown: markdown,
      baseColor: Color.white.opacity(0.92),
      range: 0..<visibleCount,
      characterBirthTimes: Array(repeating: 9.875, count: visibleCount),
      now: 10
    )

    for run in attributed.runs {
      let text = String(attributed[run.range].characters)
      if text == "link" {
        XCTAssertEqual(run.foregroundColor, NativeInlineStyler.linkColor.opacity(0.5))
      } else if text == "code" {
        XCTAssertEqual(run.foregroundColor, NativeInlineStyler.inlineCodeColor.opacity(0.5))
        XCTAssertEqual(run.backgroundColor, NativeInlineStyler.inlineCodeBackgroundColor)
      }
    }
  }

  func test_characterFadeDiffPreservesTextWhenMarkdownMarkersClose() {
    let previous = "**Bold**"
    let previousBirths: [TimeInterval] = [1, 2, 3, 4, 5, 6, 7, 8]

    let currentBirths = NativeCardModel.diffBirthTimes(
      previous: previous,
      previousBirthTimes: previousBirths,
      current: "Bold"
    )

    XCTAssertEqual(currentBirths, [3, 4, 5, 6])
  }

  func test_codeHighlighterMatchesWebPythonTokenColors() {
    let attributed = NSAttributedString(NativeCodeText.highlighted(
      """
      def hello():
          print("Hello, jarvis!")
          return 42
      """,
      language: "python"
    ))

    assertForeground(in: attributed, token: "def", hex: 0xFF7B72)
    assertForeground(in: attributed, token: "hello", hex: 0xD2A8FF)
    assertForeground(in: attributed, token: "print", hex: 0x79C0FF)
    assertForeground(in: attributed, token: "\"Hello, jarvis!\"", hex: 0xA5D6FF)
    assertForeground(in: attributed, token: "return", hex: 0xFF7B72)
    assertForeground(in: attributed, token: "42", hex: 0x79C0FF)
  }

  func test_codeRendererTrimsHighlightedSourceLikeWebShiki() {
    XCTAssertEqual(
      NativeCodeText.renderedSource("def hello():\n    return 42\n  \n", language: "python"),
      "def hello():\n    return 42"
    )
    XCTAssertEqual(
      NativeCodeText.renderedSource("raw fence\n  \n", language: nil),
      "raw fence\n  \n"
    )
  }

  private func settle(milliseconds: UInt64 = 20) async throws {
    try await Task.sleep(nanoseconds: milliseconds * 1_000_000)
  }

  private func makePNG(width: Int, height: Int) -> Data? {
    guard let rep = NSBitmapImageRep(
      bitmapDataPlanes: nil,
      pixelsWide: width,
      pixelsHigh: height,
      bitsPerSample: 8,
      samplesPerPixel: 4,
      hasAlpha: true,
      isPlanar: false,
      colorSpaceName: .deviceRGB,
      bytesPerRow: 0,
      bitsPerPixel: 0
    ) else {
      return nil
    }

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    NSColor.blue.setFill()
    NSRect(x: 0, y: 0, width: CGFloat(width), height: CGFloat(height)).fill()
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])
  }

  private func assertForeground(
    in attributed: NSAttributedString,
    token: String,
    hex expected: Int,
    file: StaticString = #filePath,
    line: UInt = #line
  ) {
    let range = (attributed.string as NSString).range(of: token)
    XCTAssertNotEqual(range.location, NSNotFound, "missing token \(token)", file: file, line: line)
    guard range.location != NSNotFound,
          let color = attributed.attribute(.foregroundColor, at: range.location, effectiveRange: nil) as? NSColor,
          let rgb = color.usingColorSpace(.sRGB) else {
      return XCTFail("missing foreground color for \(token)", file: file, line: line)
    }
    let actual =
      (Int(round(rgb.redComponent * 255)) << 16) |
      (Int(round(rgb.greenComponent * 255)) << 8) |
      Int(round(rgb.blueComponent * 255))
    XCTAssertEqual(actual, expected, file: file, line: line)
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
