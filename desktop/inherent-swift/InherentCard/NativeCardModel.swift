import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

enum NativeTurnPhase: Equatable {
  case idle
  case input
  case submitting
  case streaming
  case done
  case listening
  case transcribing
  case transition
  case error
}

enum NativeStateVariant: String {
  case thinking
  case idle
  case warn
  case error
  case success
  case neutral
}

struct NativeHistoryTurn: Identifiable, Equatable {
  let id = UUID()
  let question: String
  let answer: String
  var fresh = true
  var fading = false
}

struct NativeImageAttachment: Equatable {
  let data: Data
  let mime: String
  let name: String
  let label: String
  let meta: String
}

private struct NativeFollowupSnapshot {
  let inputText: String
  let placeholder: String
  let questionText: String
  let answerText: String
  let attachment: NativeImageAttachment?
  let stateLabel: String
  let stateVariant: NativeStateVariant?
  let phase: NativeTurnPhase
}

@MainActor
final class NativeCardModel: ObservableObject {
  static let cardWidth: CGFloat = 360
  static let popoverWidth: CGFloat = 300
  static let popoverGap: CGFloat = 18
  static let panelWidth: CGFloat = 678
  static let pillReservedTop: CGFloat = 38

  @Published var inputText = ""
  @Published var inputPlaceholder = "问点什么…"
  @Published var inputDisabled = false
  @Published var questionText = ""
  @Published var answerText = ""
  @Published var answerCharacterBirthTimes: [TimeInterval] = []
  @Published var stateLabel = ""
  @Published var stateVariant: NativeStateVariant?
  @Published var phase: NativeTurnPhase = .idle
  @Published var isSubmitted = false
  @Published var isListening = false
  @Published var isDropTarget = false
  @Published var attachmentEdgeFlash = false
  @Published var isThinking = false
  @Published var isHistoryShown = false
  @Published var isFollowupEntering = false
  @Published var isFollowupInput = false
  @Published var isFollowupRestoring = false
  @Published var history: [NativeHistoryTurn] = []
  @Published var activeHistoryID: NativeHistoryTurn.ID?
  @Published var stagedImage: NativeImageAttachment?
  @Published var focusNonce = 0

  var onNeedsLayout: (() -> Void)?
  var onNeedsLayoutAnimation: ((TimeInterval) -> Void)?
  var onRequestShow: (() -> Void)?
  var onRequestClose: (() -> Void)?
  var onRequestFadeOut: ((Int) -> Void)?
  var onRequestCancelFade: (() -> Void)?
  var onRequestMovePanel: ((CGFloat, CGFloat) -> Void)?
  var onRequestResetPosition: (() -> Void)?

  private let backend = BridgeBackend()
  private let audioDucker = SystemAudioDucker()
  private let voiceRecorder = NativeVoiceRecorder()
  private var voiceAudioDucked = false
  private var voiceListening = false
  private var voiceStartGeneration = 0
  private var voiceInputSnapshot: (value: String, placeholder: String)?

  private var inFlightQuestion: String?
  private var inputActive = false
  private var bridgeTurnOpen = false
  private var streamingStarted = false
  private var followupSnapshot: NativeFollowupSnapshot?
  private var followupDraftActive = false
  private var targetAnswer = ""
  private var shownAnswer = ""
  private var visibleAnswerText = ""
  private var dripWork: DispatchWorkItem?
  private var lastDripTick: Date?
  private var lastDripLayout = Date.distantPast
  private var dripCarry = 0.0
  private var fadeWork: DispatchWorkItem?
  private var popoverHideWork: DispatchWorkItem?
  private var attachmentEdgeFlashWork: DispatchWorkItem?
  private var enterHoldWork: DispatchWorkItem?
  private var enterHoldFired = false
  private var enterHoldShortAction: (() -> Void)?
  private var enterHoldStarted = Date()

  private let imageMaxBytes = 15 * 1024 * 1024
  private let dripMs: TimeInterval = 0.030
  private let catchupMs: TimeInterval = 0.008
  private let catchupThreshold = 40
  private let followupEnterMs: TimeInterval = 0.240
  private let followupRestoreMs: TimeInterval = 0.340
  private let voiceHoldMs: TimeInterval = 0.220
  private let voiceMinWavBytes = 48
  private let animateAnswerCharacters = ProcessInfo.processInfo.environment["INHERENT_DEBUG_FAKE_TURNS"] == "drip-fade"

  var popoverVisible: Bool { activeHistoryID != nil }

  var activeHistoryTurn: NativeHistoryTurn? {
    guard let activeHistoryID else { return nil }
    return history.first { $0.id == activeHistoryID }
  }

  var historyCount: Int {
    history.filter { !$0.fading }.count
  }

  var selectedPopoverTop: CGFloat {
    guard let activeHistoryID,
          let idx = history.firstIndex(where: { $0.id == activeHistoryID }) else {
      return 0
    }
    let visibleIndex = max(0, min(idx, 2))
    return 8 + CGFloat(visibleIndex) * 31
  }

  var stateColor: Color {
    switch stateVariant {
    case .thinking: return Color(red: 0.37, green: 0.78, blue: 1.0)
    case .idle: return Color.white.opacity(0.40)
    case .warn: return Color(red: 1.0, green: 0.72, blue: 0.30)
    case .error: return Color(red: 1.0, green: 0.42, blue: 0.42)
    case .success: return Color(red: 0.55, green: 0.85, blue: 0.70)
    case .neutral: return Color.white.opacity(0.58)
    case nil: return Color(red: 0.37, green: 0.78, blue: 1.0)
    }
  }

  func shutdown() {
    cancelFade()
    clearDrip()
    cancelEnterHoldTimer()
    attachmentEdgeFlashWork?.cancel()
    cancelActiveVoiceCapture()
    audioDucker.restoreAll()
  }

  func requestInitialLayout() {
    onNeedsLayout?()
  }

  func focusInput() {
    inputDisabled = false
    focusNonce += 1
    onRequestShow?()
    requestLayout()
  }

  func openInputFromHotkey() {
    enterInputMode()
  }

  func close() {
    cancelFade()
    cancelEnterHoldTimer()
    cancelActiveVoiceCapture()
    onRequestClose?()
  }

  func cancelNativeFade() {
    cancelFade()
    onRequestCancelFade?()
  }

  func beginDrag() {
    requestLayout()
  }

  func movePanel(dx: CGFloat, dy: CGFloat) {
    onRequestMovePanel?(dx, dy)
  }

  func resetPosition() {
    onRequestResetPosition?()
  }

  func setDropTarget(_ value: Bool) {
    isDropTarget = value
  }

  func stageImageFromClipboard() -> Bool {
    stageImageFromPasteboard(NSPasteboard.general, source: "paste")
  }

  func handleDrop(providers: [NSItemProvider]) -> Bool {
    for provider in providers {
      if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
        provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { [weak self] item, _ in
          let url = Self.url(fromProviderItem: item)
          DispatchQueue.main.async {
            guard let self, let url else { return }
            _ = self.stageImageFile(url, source: "drop")
          }
        }
        return true
      }
      if provider.canLoadObject(ofClass: NSImage.self) {
        provider.loadObject(ofClass: NSImage.self) { [weak self] object, _ in
          guard let image = object as? NSImage,
                let data = Self.pngData(from: image) else { return }
          DispatchQueue.main.async {
            _ = self?.stageImage(data: data, mime: "image/png", name: "drop.png", source: "drop")
          }
        }
        return true
      }
    }
    return false
  }

  func clearStagedImage() {
    stagedImage = nil
    requestLayout()
  }

  func submitInputText() {
    let trimmed = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
    let image = stagedImage
    if trimmed.isEmpty && image == nil {
      if followupDraftActive { _ = restoreFollowupSnapshot() }
      return
    }

    let displayText = trimmed.isEmpty ? "请看这张图片" : trimmed
    inputActive = true
    inFlightQuestion = displayText
    clearFollowupDraft()
    cancelFade()
    clearDrip()
    targetAnswer = ""
    shownAnswer = ""
    setAnswerText("")
    questionText = displayText
    inputDisabled = true
    isSubmitted = true
    isFollowupEntering = false
    isFollowupInput = false
    isFollowupRestoring = false
    isListening = false
    isThinking = true
    streamingStarted = false
    phase = .submitting
    setState("thinking", .thinking)
    requestLayout(animatedFor: 0.70)

    let submittedImage = image
    Task {
      let result: SubmitResult
      if let submittedImage {
        result = await backend.submitImage(
          text: trimmed,
          imageData: submittedImage.data,
          mime: submittedImage.mime,
          name: submittedImage.name
        )
      } else {
        result = await backend.submit(text: trimmed)
      }
      await MainActor.run {
        if !result.ok {
          self.isThinking = false
          let reason = result.reason ?? "unknown"
          self.setState(reason == "network" ? "offline" : "error · \(reason)", .error)
          self.phase = .error
          self.scheduleFade(3000)
        }
      }
    }
  }

  func handleEnterDown(shortAction: (() -> Void)? = nil) {
    if enterHoldWork != nil { return }
    enterHoldFired = false
    enterHoldShortAction = shortAction
    enterHoldStarted = Date()
    let work = DispatchWorkItem { [weak self] in
      Task { @MainActor in
        guard let self else { return }
        self.enterHoldFired = true
        await self.beginEnterVoiceCapture()
      }
    }
    enterHoldWork = work
    DispatchQueue.main.asyncAfter(deadline: .now() + voiceHoldMs, execute: work)
  }

  func handleEnterUp() {
    guard let work = enterHoldWork else { return }
    enterHoldWork = nil
    work.cancel()
    let held = Date().timeIntervalSince(enterHoldStarted)
    if !enterHoldFired && held >= voiceHoldMs {
      enterHoldFired = true
      Task {
        await beginEnterVoiceCapture()
        await finishEnterVoiceCapture()
      }
      return
    }
    if !enterHoldFired {
      let action = enterHoldShortAction
      enterHoldShortAction = nil
      action?()
      return
    }
    enterHoldShortAction = nil
    Task { await finishEnterVoiceCapture() }
  }

  func handleGlobalEnterDown() {
    if canEnterFollowupInput() {
      handleEnterDown { [weak self] in self?.enterInputMode(followup: true) }
    } else {
      handleEnterDown()
    }
  }

  func handleEscape() {
    cancelEnterHoldTimer()
    cancelActiveVoiceCapture()
    close()
  }

  func toggleHistoryShown() {
    guard !history.isEmpty else { return }
    isHistoryShown.toggle()
    if !isHistoryShown { activeHistoryID = nil }
    requestLayout(animatedFor: 0.42)
  }

  func clearHistoryCascade() {
    activeHistoryID = nil
    guard !history.isEmpty else {
      isHistoryShown = false
      requestLayout(animatedFor: 0.42)
      return
    }
    isHistoryShown = false
    for offset in history.indices.reversed().enumerated() {
      let delay = Double(offset.offset) * 0.05
      DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
        guard let self,
              let idx = self.history.firstIndex(where: { $0.id == self.history[offset.element].id }) else { return }
        self.history[idx].fading = true
      }
    }
    let total = Double(history.count) * 0.05 + 0.38
    DispatchQueue.main.asyncAfter(deadline: .now() + total) { [weak self] in
      self?.history.removeAll()
      self?.requestLayout()
    }
    requestLayout(animatedFor: total + 0.08)
  }

  func showPopover(for turn: NativeHistoryTurn) {
    cancelPopoverHide()
    activeHistoryID = turn.id
    requestLayout(animatedFor: 0.32)
  }

  func hidePopover() {
    cancelPopoverHide()
    activeHistoryID = nil
    requestLayout(animatedFor: 0.30)
  }

  func schedulePopoverHide() {
    cancelPopoverHide()
    let work = DispatchWorkItem { [weak self] in self?.hidePopover() }
    popoverHideWork = work
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.45, execute: work)
  }

  func cancelPopoverHide() {
    popoverHideWork?.cancel()
    popoverHideWork = nil
  }

  func setHovering(_ hovering: Bool) {
    if hovering {
      cancelNativeFade()
    }
  }

  private func cancelEnterHoldTimer() {
    enterHoldWork?.cancel()
    enterHoldWork = nil
    enterHoldShortAction = nil
    enterHoldFired = false
  }

  private func canEnterFollowupInput() -> Bool {
    if !inputDisabled { return false }
    if phase == .submitting || phase == .streaming || streamingStarted { return false }
    if isThinking { return false }
    if phase == .done || phase == .error { return true }
    return !answerText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
  }

  private func captureResponseSnapshot() -> NativeFollowupSnapshot? {
    let answer = targetAnswer.isEmpty ? answerText : targetAnswer
    guard !answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
    return NativeFollowupSnapshot(
      inputText: inputText,
      placeholder: inputPlaceholder,
      questionText: questionText,
        answerText: answer,
      attachment: stagedImage,
      stateLabel: stateLabel.isEmpty ? (phase == .error ? "error" : "done") : stateLabel,
      stateVariant: stateVariant ?? (phase == .error ? .error : .success),
      phase: phase == .error ? .error : .done
    )
  }

  private func clearFollowupDraft() {
    followupSnapshot = nil
    followupDraftActive = false
  }

  private func restoreFollowupSnapshot() -> Bool {
    guard let snapshot = followupSnapshot else { return false }
    cancelFade()
    onRequestCancelFade?()
    clearDrip()
    hidePopover()
    inputText = snapshot.inputText
    inputPlaceholder = snapshot.placeholder
    questionText = snapshot.questionText
    stagedImage = snapshot.attachment
    inputDisabled = true
    isSubmitted = true
    isFollowupInput = false
    isFollowupEntering = false
    isFollowupRestoring = true
    isListening = false
    isThinking = false
    targetAnswer = snapshot.answerText
    shownAnswer = snapshot.answerText
    setAnswerText(snapshot.answerText)
    setState(snapshot.stateLabel, snapshot.stateVariant)
    phase = snapshot.phase
    followupDraftActive = false
    inputActive = false
    streamingStarted = false
    requestLayout(animatedFor: followupRestoreMs + 0.22)
    DispatchQueue.main.asyncAfter(deadline: .now() + followupRestoreMs) { [weak self] in
      self?.isFollowupRestoring = false
    }
    scheduleFade(5000)
    return true
  }

  private func resetInputState(placeholder: String = "问点什么…") {
    inputText = ""
    inputPlaceholder = placeholder
    inputDisabled = false
    questionText = ""
    stagedImage = nil
    setAnswerText("")
    targetAnswer = ""
    shownAnswer = ""
    isSubmitted = false
    isListening = false
    isThinking = false
    isFollowupEntering = false
    isFollowupRestoring = false
  }

  private func enterInputMode(followup: Bool = false) {
    if followup {
      followupSnapshot = captureResponseSnapshot()
      followupDraftActive = false
    } else {
      clearFollowupDraft()
    }
    cancelFade()
    onRequestCancelFade?()
    clearDrip()
    hidePopover()
    targetAnswer = ""
    shownAnswer = ""

    let finish = { [weak self] in
      guard let self else { return }
      self.resetInputState(placeholder: followup ? "继续问…" : "问点什么…")
      self.isFollowupInput = followup
      self.setState(followup ? "input" : "idle", followup ? nil : .idle)
      self.phase = .input
      self.inputActive = true
      self.followupDraftActive = followup && self.followupSnapshot != nil
      self.focusNonce += 1
      self.requestLayout(animatedFor: followup ? 0.70 : 0.54)
    }

    if followup && isSubmitted {
      isFollowupEntering = true
      setState("input", nil)
      phase = .transition
      requestLayout(animatedFor: followupEnterMs + 0.16)
      DispatchQueue.main.asyncAfter(deadline: .now() + followupEnterMs) {
        finish()
      }
    } else {
      finish()
    }
  }

  private func stageImageFile(_ url: URL, source: String) -> Bool {
    guard canStageImage() else { return false }
    guard let mime = Self.imageMimeType(for: url) else {
      imageStageError("image only")
      return false
    }
    do {
      if let bytes = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize,
         bytes > imageMaxBytes {
        imageStageError("image > 15MB")
        return false
      }
      let data = try Data(contentsOf: url)
      return stageImage(data: data, mime: mime, name: url.lastPathComponent, source: source)
    } catch {
      NSLog("[native-card] failed to read image: \(error)")
      return false
    }
  }

  private func stageImageFromPasteboard(_ pasteboard: NSPasteboard, source: String) -> Bool {
    guard canStageImage() else { return false }
    let options: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
    if let urls = pasteboard.readObjects(forClasses: [NSURL.self], options: options) as? [NSURL],
       let url = urls.map({ $0 as URL }).first(where: { Self.imageMimeType(for: $0) != nil }) {
      return stageImageFile(url, source: source)
    }

    if let data = pasteboard.data(forType: .png) {
      return stageImage(data: data, mime: "image/png", name: "screen.png", source: source)
    }
    if let data = pasteboard.data(forType: .tiff),
       let image = NSImage(data: data),
       let png = Self.pngData(from: image) {
      return stageImage(data: png, mime: "image/png", name: "screen.png", source: source)
    }
    return false
  }

  private func stageImage(data: Data, mime: String, name: String, source: String) -> Bool {
    guard canStageImage() else { return false }
    guard !data.isEmpty else { return false }
    guard data.count <= imageMaxBytes else {
      imageStageError("image > 15MB")
      return false
    }
    if inputDisabled && canEnterFollowupInput() {
      enterInputMode(followup: true)
    } else if inputDisabled {
      enterInputMode()
    }
    let image = NSImage(data: data)
    let dimensions = image.map { "\(Int($0.size.width))×\(Int($0.size.height))" }
    let label = Self.normalizedImageName(name: name, source: source)
    stagedImage = NativeImageAttachment(
      data: data,
      mime: mime,
      name: name.isEmpty ? "\(source).png" : name,
      label: label,
      meta: dimensions ?? Self.formatBytes(data.count)
    )
    isDropTarget = false
    inputActive = true
    phase = .input
    setState(stateLabel.isEmpty ? "idle" : stateLabel, stateVariant ?? .idle)
    flashAttachmentEdge()
    focusNonce += 1
    requestLayout(animatedFor: 0.42)
    return true
  }

  private func imageStageError(_ message: String) {
    isDropTarget = false
    setState(message, .warn)
    flashAttachmentEdge()
    requestLayout(animatedFor: 0.42)
  }

  private func flashAttachmentEdge() {
    attachmentEdgeFlashWork?.cancel()
    attachmentEdgeFlash = false
    attachmentEdgeFlash = true
    let work = DispatchWorkItem { [weak self] in
      self?.attachmentEdgeFlash = false
    }
    attachmentEdgeFlashWork = work
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.72, execute: work)
  }

  private func canStageImage() -> Bool {
    phase != .submitting && phase != .streaming && phase != .listening && phase != .transcribing
  }

  private func beginEnterVoiceCapture() async {
    guard canStartVoiceHold() else { return }
    voiceStartGeneration += 1
    let generation = voiceStartGeneration
    cancelFade()
    onRequestCancelFade?()
    hidePopover()

    if canEnterFollowupInput() {
      enterInputMode(followup: true)
      try? await Task.sleep(nanoseconds: UInt64((followupEnterMs + 0.08) * 1_000_000_000))
    } else if inputDisabled {
      enterInputMode()
      try? await Task.sleep(nanoseconds: 80_000_000)
    }
    guard generation == voiceStartGeneration else { return }

    voiceInputSnapshot = (inputText, inputPlaceholder)
    voiceListening = true
    inputText = ""
    inputPlaceholder = "正在听…"
    inputDisabled = true
    isListening = true
    isThinking = false
    phase = .listening
    setState("listening", nil)
    requestLayout()

    await duckSystemAudioForVoice()
    do {
      try await voiceRecorder.start()
      if generation != voiceStartGeneration {
        _ = await voiceRecorder.stop()
        await restoreSystemAudioForVoice()
      }
    } catch {
      await restoreSystemAudioForVoice()
      voiceListening = false
      isListening = false
      restoreVoiceDraftForRetry()
      inputDisabled = false
      setState((error as? NativeVoiceRecorderError) == .microphoneDenied ? "mic denied" : "mic error", .error)
      phase = .input
      focusNonce += 1
      requestLayout()
    }
  }

  private func finishEnterVoiceCapture() async {
    guard voiceListening else { return }
    voiceListening = false
    voiceStartGeneration += 1
    isListening = false

    let wavData = await voiceRecorder.stop()
    await restoreSystemAudioForVoice()

    guard let wavData, wavData.count > voiceMinWavBytes else {
      returnToVoiceRetryState(label: "no speech", variant: .warn)
      return
    }

    inputText = ""
    inputPlaceholder = "识别中…"
    inputDisabled = true
    phase = .transcribing
    setState("transcribing", .neutral)
    requestLayout()

    let result = await backend.submitVoice(wavData: wavData)
    if !result.ok {
      let reason = result.reason ?? "unknown"
      returnToVoiceRetryState(label: reason == "network" ? "offline" : "error · \(reason)", variant: .error)
      return
    }
    let text = (result.text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    if result.status == "empty" || text.isEmpty {
      returnToVoiceRetryState(label: "no speech", variant: .warn)
      return
    }

    voiceInputSnapshot = nil
    clearFollowupDraft()
    inFlightQuestion = text
    inputText = text
    questionText = text
    inputPlaceholder = "问点什么…"
    inputDisabled = true
    isSubmitted = true
    isFollowupInput = false
    isFollowupEntering = false
    isFollowupRestoring = false
    inputActive = true
    streamingStarted = false
    isThinking = true
    phase = .submitting
    setState("thinking", .thinking)
    requestLayout(animatedFor: 0.70)
  }

  private func canStartVoiceHold() -> Bool {
    if voiceListening { return false }
    if stagedImage != nil { return false }
    if phase == .submitting || phase == .streaming || phase == .listening || phase == .transcribing { return false }
    return !isThinking
  }

  private func restoreVoiceDraftForRetry() {
    inputText = voiceInputSnapshot?.value ?? ""
    inputPlaceholder = voiceInputSnapshot?.placeholder ?? (followupDraftActive ? "继续问…" : "问点什么…")
    voiceInputSnapshot = nil
  }

  private func returnToVoiceRetryState(label: String, variant: NativeStateVariant) {
    isThinking = false
    isListening = false
    restoreVoiceDraftForRetry()
    inputDisabled = false
    setState(label, variant)
    inputActive = true
    phase = .input
    focusNonce += 1
    requestLayout()
  }

  private func cancelActiveVoiceCapture() {
    voiceStartGeneration += 1
    guard voiceListening else { return }
    voiceListening = false
    isListening = false
    Task {
      _ = await voiceRecorder.stop()
      await restoreSystemAudioForVoice()
    }
  }

  private func duckSystemAudioForVoice() async {
    guard !voiceAudioDucked else { return }
    voiceAudioDucked = true
    if !audioDucker.duck() {
      voiceAudioDucked = false
    }
  }

  private func restoreSystemAudioForVoice() async {
    guard voiceAudioDucked else { return }
    voiceAudioDucked = false
    audioDucker.restore()
  }

  private func clearDrip() {
    dripWork?.cancel()
    dripWork = nil
    lastDripTick = nil
    lastDripLayout = .distantPast
    dripCarry = 0
  }

  private func startDrip() {
    if dripWork != nil { return }
    let work = DispatchWorkItem { [weak self] in
      Task { @MainActor in
        guard let self else { return }
        self.dripWork = nil
        guard self.shownAnswer.count < self.targetAnswer.count else { return }
        let lag = self.targetAnswer.count - self.shownAnswer.count
        let now = Date()
        let perCharacter = lag > self.catchupThreshold ? self.catchupMs : self.dripMs
        let elapsed = self.lastDripTick.map { now.timeIntervalSince($0) } ?? perCharacter
        self.lastDripTick = now
        self.dripCarry += max(elapsed, perCharacter) / perCharacter
        var step = min(lag, max(1, Int(self.dripCarry.rounded(.down))))
        if lag > self.catchupThreshold {
          step = max(step, min(lag, 4))
        }
        self.dripCarry = max(0, self.dripCarry - Double(step))
        let nextIndex = self.targetAnswer.index(self.targetAnswer.startIndex, offsetBy: self.shownAnswer.count + step)
        self.shownAnswer = String(self.targetAnswer[..<nextIndex])
        self.setAnswerText(self.shownAnswer, animateCharacters: true)
        let shouldMeasureLayout = now.timeIntervalSince(self.lastDripLayout) >= (1.0 / 30.0)
          || self.shownAnswer.last == "\n"
          || self.shownAnswer.count == self.targetAnswer.count
        if shouldMeasureLayout {
          self.lastDripLayout = now
          self.requestLayout()
        }
        guard self.shownAnswer.count < self.targetAnswer.count else {
          self.lastDripTick = nil
          self.dripCarry = 0
          return
        }
        let remainingLag = self.targetAnswer.count - self.shownAnswer.count
        let nextPerCharacter = remainingLag > self.catchupThreshold ? self.catchupMs : self.dripMs
        let delay = max(1.0 / 30.0, nextPerCharacter)
        self.scheduleDrip(after: delay)
      }
    }
    dripWork = work
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.001, execute: work)
  }

  private func scheduleDrip(after delay: TimeInterval) {
    let work = DispatchWorkItem { [weak self] in
      Task { @MainActor in
        guard let self else { return }
        self.dripWork = nil
        self.startDrip()
      }
    }
    dripWork = work
    DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
  }

  private func estimatedDripRemainingMs(remaining: Int) -> Int {
    var lag = max(0, remaining)
    var total = 0.0
    while lag > 0 {
      if lag > catchupThreshold {
        lag -= min(lag, 4)
        total += catchupMs
      } else {
        lag -= 1
        total += dripMs
      }
    }
    return Int(ceil(total * 1000))
  }

  private func scheduleFade(_ ms: Int) {
    cancelFade()
    let work = DispatchWorkItem { [weak self] in
      self?.onRequestFadeOut?(280)
    }
    fadeWork = work
    DispatchQueue.main.asyncAfter(deadline: .now() + Double(ms) / 1000.0, execute: work)
  }

  private func cancelFade() {
    fadeWork?.cancel()
    fadeWork = nil
  }

  private func setState(_ label: String, _ variant: NativeStateVariant? = nil) {
    stateLabel = label
    stateVariant = label.isEmpty ? nil : variant
  }

  private func setAnswerText(_ text: String, animateCharacters: Bool = false) {
    answerText = text
    let visibleText = NativeAnswerParser.visibleText(text)
    guard animateAnswerCharacters && animateCharacters else {
      visibleAnswerText = visibleText
      answerCharacterBirthTimes = []
      return
    }
    answerCharacterBirthTimes = Self.diffBirthTimes(
      previous: visibleAnswerText,
      previousBirthTimes: answerCharacterBirthTimes,
      current: visibleText
    )
    visibleAnswerText = visibleText
  }

  private static func diffBirthTimes(
    previous: String,
    previousBirthTimes: [TimeInterval],
    current: String
  ) -> [TimeInterval] {
    let now = Date().timeIntervalSinceReferenceDate
    let previousChars = Array(previous)
    let currentChars = Array(current)
    var output = Array(repeating: now, count: currentChars.count)
    let minCount = min(previousChars.count, currentChars.count)
    var prefix = 0
    while prefix < minCount, previousChars[prefix] == currentChars[prefix] {
      output[prefix] = prefix < previousBirthTimes.count ? previousBirthTimes[prefix] : now
      prefix += 1
    }
    var suffix = 0
    while suffix < previousChars.count - prefix,
          suffix < currentChars.count - prefix,
          previousChars[previousChars.count - 1 - suffix] == currentChars[currentChars.count - 1 - suffix] {
      let currentIndex = currentChars.count - 1 - suffix
      let previousIndex = previousChars.count - 1 - suffix
      output[currentIndex] = previousIndex < previousBirthTimes.count ? previousBirthTimes[previousIndex] : now
      suffix += 1
    }
    return output
  }

  private func requestLayout(animatedFor duration: TimeInterval = 0.0) {
    if duration > 0 {
      onNeedsLayoutAnimation?(duration)
    } else {
      onNeedsLayout?()
    }
    onRequestShow?()
  }

  private func pushTurn(question: String, answer: String) {
    let q = question.trimmingCharacters(in: .whitespacesAndNewlines)
    let a = answer.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !q.isEmpty, !a.isEmpty else { return }
    history.append(NativeHistoryTurn(question: q, answer: a))
    isHistoryShown = true
    requestLayout(animatedFor: 0.42)
    if let id = history.last?.id {
      DispatchQueue.main.asyncAfter(deadline: .now() + 1.10) { [weak self] in
        guard let self,
              let idx = self.history.firstIndex(where: { $0.id == id }) else { return }
        self.history[idx].fresh = false
      }
    }
  }

  nonisolated private static func url(fromProviderItem item: NSSecureCoding?) -> URL? {
    if let url = item as? URL { return url }
    if let data = item as? Data,
       let raw = String(data: data, encoding: .utf8) {
      return URL(string: raw)
    }
    if let raw = item as? String {
      return URL(string: raw)
    }
    return nil
  }

  nonisolated private static func imageMimeType(for url: URL) -> String? {
    let ext = url.pathExtension.lowercased()
    if ext == "jpg" { return "image/jpeg" }
    if ["png", "jpeg", "webp", "gif"].contains(ext),
       let mime = UTType(filenameExtension: ext)?.preferredMIMEType {
      return mime == "image/jpg" ? "image/jpeg" : mime
    }
    return nil
  }

  nonisolated private static func pngData(from image: NSImage) -> Data? {
    guard let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff) else {
      return nil
    }
    return rep.representation(using: .png, properties: [:])
  }

  nonisolated private static func normalizedImageName(name: String, source: String) -> String {
    let raw = name.trimmingCharacters(in: .whitespacesAndNewlines)
    if source == "paste" || raw.isEmpty || raw.range(of: #"^image\.(png|jpe?g|webp|gif)$"#, options: .regularExpression) != nil {
      return "screen"
    }
    if raw.count > 28 {
      return "\(raw.prefix(24))…"
    }
    return raw
  }

  nonisolated private static func formatBytes(_ bytes: Int) -> String {
    if bytes <= 0 { return "" }
    if bytes < 1024 * 1024 {
      return "\(max(1, Int(round(Double(bytes) / 1024.0))))KB"
    }
    let mb = Double(bytes) / Double(1024 * 1024)
    return mb < 10 ? String(format: "%.1fMB", mb) : "\(Int(round(mb)))MB"
  }
}

extension NativeCardModel: BridgeDispatcher {
  nonisolated func siriOpen(payload: [String: Any]?) {
    Task { @MainActor in
      let streaming = (payload?["streaming"] as? Bool) ?? false
      let content = payload?["content"] as? String ?? ""
      guard streaming || !content.isEmpty else { return }

      if bridgeTurnOpen {
        inFlightQuestion = nil
        inputActive = false
        questionText = ""
      }
      bridgeTurnOpen = true
      clearFollowupDraft()
      cancelFade()
      clearDrip()
      targetAnswer = ""
      shownAnswer = ""
      setAnswerText("")
      streamingStarted = false
      isFollowupEntering = false
      isFollowupInput = false
      isFollowupRestoring = false
      let q = (payload?["q"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
      if !q.isEmpty && inFlightQuestion == nil { inFlightQuestion = q }
      if !q.isEmpty { questionText = q }
      if !inputActive {
        inputText = q
        inputDisabled = true
        isSubmitted = true
      }
      setState("thinking", .thinking)
      isThinking = true
      phase = .submitting
      if !streaming && !content.isEmpty {
        targetAnswer = content
        shownAnswer = content
        setAnswerText(content)
        streamingStarted = true
        isThinking = false
        setState("streaming", .thinking)
        phase = .streaming
      }
      onRequestShow?()
      requestLayout(animatedFor: 0.70)
    }
  }

  nonisolated func siriAppend(payload: [String: Any]?) {
    Task { @MainActor in
      guard let token = payload?["token"] as? String else { return }
      guard phase == .submitting || phase == .streaming else { return }
      if !streamingStarted {
        streamingStarted = true
        isThinking = false
        setState("streaming", .thinking)
        phase = .streaming
      }
      targetAnswer += token
      startDrip()
    }
  }

  nonisolated func siriDone(payload: [String: Any]?) {
    Task { @MainActor in
      guard phase == .submitting || phase == .streaming else { return }
      let remaining = max(0, targetAnswer.count - shownAnswer.count)
      let dripRemainingMs = estimatedDripRemainingMs(remaining: remaining)
      isThinking = false
      inputActive = false
      streamingStarted = false
      phase = .done
      let flipDone = { [weak self] in
        guard let self, self.phase == .done else { return }
        self.setState("done", .success)
      }
      if dripRemainingMs > 0 {
        DispatchQueue.main.asyncAfter(deadline: .now() + Double(dripRemainingMs) / 1000.0) {
          flipDone()
        }
      } else {
        flipDone()
      }
      if !targetAnswer.isEmpty {
        pushTurn(question: inFlightQuestion ?? "语音", answer: targetAnswer)
      }
      inFlightQuestion = nil
      bridgeTurnOpen = false
      let fadeMs = (payload?["fadeMs"] as? Int) ?? (payload?["fadeMs"] as? Double).map(Int.init) ?? 5000
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.60 + Double(dripRemainingMs) / 1000.0) { [weak self] in
        self?.requestLayout()
      }
      scheduleFade(fadeMs + dripRemainingMs)
    }
  }

  nonisolated func siriReset() {
    Task { @MainActor in
      clearFollowupDraft()
      clearDrip()
      targetAnswer = ""
      shownAnswer = ""
      setAnswerText("")
      inFlightQuestion = nil
      inputActive = false
      bridgeTurnOpen = false
      streamingStarted = false
      phase = .idle
      hidePopover()
      resetInputState()
      setState("", nil)
      requestLayout(animatedFor: 0.54)
    }
  }

  nonisolated func voiceState(payload: [String: Any]?) {
    Task { @MainActor in
      let phaseName = payload?["phase"] as? String
      switch phaseName {
      case "listening":
        beginExternalVoiceCapture()
      case "transcribing":
        setExternalVoiceTranscribing()
      case "accepted":
        acceptExternalVoiceText(payload?["text"] as? String)
      case "empty":
        failExternalVoiceState(label: "no speech", variant: .warn)
      case "error":
        failExternalVoiceState(label: "error", variant: .error)
      default:
        break
      }
    }
  }

  private func beginExternalVoiceCapture() {
    voiceStartGeneration += 1
    voiceInputSnapshot = nil
    clearFollowupDraft()
    cancelFade()
    clearDrip()
    hidePopover()
    stagedImage = nil
    questionText = ""
    targetAnswer = ""
    shownAnswer = ""
    setAnswerText("")
    inputText = ""
    inputPlaceholder = "正在听…"
    inputDisabled = true
    isSubmitted = false
    isListening = true
    isThinking = false
    inputActive = false
    streamingStarted = false
    phase = .listening
    setState("listening", nil)
    requestLayout(animatedFor: 0.54)
  }

  private func setExternalVoiceTranscribing() {
    isListening = false
    inputText = ""
    inputPlaceholder = "识别中…"
    inputDisabled = true
    isThinking = false
    phase = .transcribing
    setState("transcribing", .neutral)
    requestLayout()
  }

  private func acceptExternalVoiceText(_ text: String?) {
    let trimmed = (text ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else {
      failExternalVoiceState(label: "no speech", variant: .warn)
      return
    }
    clearFollowupDraft()
    clearDrip()
    targetAnswer = ""
    shownAnswer = ""
    setAnswerText("")
    stagedImage = nil
    inFlightQuestion = trimmed
    inputText = trimmed
    questionText = trimmed
    inputPlaceholder = "问点什么…"
    inputDisabled = true
    isListening = false
    isSubmitted = true
    isThinking = true
    inputActive = true
    streamingStarted = false
    phase = .submitting
    setState("thinking", .thinking)
    requestLayout(animatedFor: 0.70)
  }

  private func failExternalVoiceState(label: String, variant: NativeStateVariant) {
    isListening = false
    inputText = ""
    inputPlaceholder = "问点什么…"
    inputDisabled = true
    isThinking = false
    inputActive = false
    streamingStarted = false
    phase = variant == .error ? .error : .idle
    setState(label, variant)
    requestLayout()
    scheduleFade(1800)
  }
}
