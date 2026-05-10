import AppKit
import QuartzCore
import SwiftUI

@MainActor
final class NativeCardController: NSObject {
  let panel: CardPanel
  let model: NativeCardModel
  private let hostingView: NSHostingView<NativeCardView>
  private let containerView: NSView
  private let fade: FadeController
  private let audioDucker = SystemAudioDucker()
  private var ws: WSClient?
  private var bridgeDispatcher: NativeControllerBridge?
  private var hotkey: HotkeyManager?
  private var globalMouseMonitor: Any?
  private var localMouseMonitor: Any?
  private var localKeyMonitor: Any?
  private var localKeyUpMonitor: Any?
  private var layoutPollWork: DispatchWorkItem?
  private var heightAnimationTask: Task<Void, Never>?
  private var heightAnimationGeneration = 0
  private var userHidden = false
  private var userMovedPanel = false
  private var nativeEnterDown = false

  override init() {
    let panel = CardPanel()
    let model = NativeCardModel()
    let view = NativeCardView(model: model)
    let containerView = NSView(frame: NSRect(x: 0, y: 0, width: NativeCardModel.panelWidth, height: 120))
    self.panel = panel
    self.model = model
    self.hostingView = NSHostingView(rootView: view)
    self.containerView = containerView
    self.fade = FadeController(panel: panel)
    super.init()

    containerView.wantsLayer = true
    containerView.layer?.backgroundColor = NSColor.clear.cgColor
    hostingView.translatesAutoresizingMaskIntoConstraints = false
    hostingView.wantsLayer = true
    hostingView.layer?.backgroundColor = NSColor.clear.cgColor
    containerView.addSubview(hostingView)
    NSLayoutConstraint.activate([
      hostingView.leadingAnchor.constraint(equalTo: containerView.leadingAnchor),
      hostingView.trailingAnchor.constraint(equalTo: containerView.trailingAnchor),
      hostingView.topAnchor.constraint(equalTo: containerView.topAnchor),
      hostingView.bottomAnchor.constraint(equalTo: containerView.bottomAnchor),
    ])
    panel.contentView = containerView

    model.onNeedsLayout = { [weak self] in self?.updatePanelHeight() }
    model.onNeedsLayoutAnimation = { [weak self] duration in self?.animatePanelHeight(for: duration) }
    model.onRequestShow = { [weak self] in self?.showIfAllowed() }
    model.onRequestClose = { [weak self] in self?.hideFromUser() }
    model.onRequestFadeOut = { [weak self] ms in self?.fadeOut(durationMs: ms) }
    model.onRequestCancelFade = { [weak self] in self?.cancelFade() }
    model.onRequestMovePanel = { [weak self] dx, dy in self?.movePanel(dx: dx, dy: dy) }
    model.onRequestResetPosition = { [weak self] in self?.resetPosition() }

    let dispatcher = NativeControllerBridge(controller: self)
    self.bridgeDispatcher = dispatcher
    let ws = WSClient(dispatcher: dispatcher)
    ws.turnIsOpen = { [weak model] in
      guard let phase = DispatchQueue.main.sync(execute: { model?.phase }) else { return false }
      return phase == .submitting || phase == .streaming
    }
    ws.connect()
    self.ws = ws

    let hotkey = HotkeyManager()
    _ = hotkey.register { [weak self] in self?.toggleHotkey() }
    self.hotkey = hotkey

    startPasteShortcutMonitor()

    if let debugMode = ProcessInfo.processInfo.environment["INHERENT_DEBUG_FAKE_TURNS"] {
      let scenarioModes = [
        "basic", "multi-turn", "overflow", "drip-plain", "drip-fade", "followup-restore",
        "popover", "voice-listening", "voice-transcribing", "voice-accepted", "voice-empty", "voice-error",
      ]
      let delay: TimeInterval = scenarioModes.contains(debugMode) ? 0 : 1.0
      DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
        self?.runFakeTurns()
      }
    }

    if let snapshotPath = ProcessInfo.processInfo.environment["INHERENT_DEBUG_SNAPSHOT_PATH"] {
      let delayMs = Double(ProcessInfo.processInfo.environment["INHERENT_DEBUG_SNAPSHOT_DELAY_MS"] ?? "") ?? 2600
      DispatchQueue.main.asyncAfter(deadline: .now() + delayMs / 1000.0) { [weak self] in
        self?.writeDebugSnapshot(to: snapshotPath)
      }
    }
    if let snapshotDir = ProcessInfo.processInfo.environment["INHERENT_DEBUG_SNAPSHOT_DIR"] {
      scheduleDebugSnapshots(toDirectory: snapshotDir)
    }
  }

  func showInitial() {
    if let screen = NSScreen.main {
      panel.anchorTopRight(of: screen)
    }
    panel.orderFrontRegardless()
    model.requestInitialLayout()
    updatePanelHeight()
    fade.hideInstant()
  }

  func shutdown() {
    heightAnimationTask?.cancel()
    model.shutdown()
    audioDucker.restoreAll()
    ws?.shutdownNow()
    hotkey?.unregister()
    if let m = globalMouseMonitor { NSEvent.removeMonitor(m); globalMouseMonitor = nil }
    if let m = localMouseMonitor { NSEvent.removeMonitor(m); localMouseMonitor = nil }
    if let m = localKeyMonitor { NSEvent.removeMonitor(m); localKeyMonitor = nil }
    if let m = localKeyUpMonitor { NSEvent.removeMonitor(m); localKeyUpMonitor = nil }
  }

  func startPassthroughMonitor() {
    globalMouseMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved, .leftMouseDragged, .leftMouseUp]) { [weak self] _ in
      self?.updatePassthrough()
    }
    localMouseMonitor = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved, .leftMouseDragged, .leftMouseUp]) { [weak self] event in
      self?.updatePassthrough()
      return event
    }
    updatePassthrough()
  }

  private func showIfAllowed() {
    guard !userHidden else { return }
    fade.showInstant()
    updatePassthrough()
  }

  private func hideFromUser() {
    userHidden = true
    ws?.discardOpenTurn()
    fade.hideInstant()
  }

  private func fadeOut(durationMs: Int) {
    fade.fadeOut(durationMs: durationMs) { [weak self] in
      self?.userHidden = true
      self?.ws?.discardOpenTurn()
      self?.fade.hideInstant()
    }
  }

  private func cancelFade() {
    guard !userHidden else { return }
    fade.showInstant()
    updatePassthrough()
  }

  private func movePanel(dx: CGFloat, dy: CGFloat) {
    var frame = panel.frame
    frame.origin.x += dx
    frame.origin.y += dy
    panel.setFrame(frame, display: true, animate: false)
    userMovedPanel = true
  }

  private func resetPosition() {
    userMovedPanel = false
    if let screen = DisplayManager.cursorScreen() ?? NSScreen.main {
      panel.anchorTopRight(of: screen)
    }
    updatePassthrough()
  }

  private func updatePanelHeight() {
    heightAnimationGeneration += 1
    heightAnimationTask?.cancel()
    heightAnimationTask = nil
    applyPanelHeight(targetPanelHeight())
  }

  private func targetPanelHeight() -> CGFloat {
    hostingView.layoutSubtreeIfNeeded()
    let fitting = hostingView.fittingSize
    let measured = max(DisplayManager.MIN_HEIGHT, fitting.height)
    return DisplayManager.clampPanelHeight(measured, on: panel.screen)
  }

  private func applyPanelHeight(_ height: CGFloat) {
    if abs(height - panel.frame.height) < 0.5 { return }
    let next = DisplayManager.applyHeight(to: panel.frame, newHeight: height)
    panel.setFrame(next, display: true, animate: false)
    updatePassthrough()
  }

  private func animatePanelHeight(for duration: TimeInterval) {
    heightAnimationGeneration += 1
    heightAnimationTask?.cancel()
    heightAnimationTask = nil
    let generation = heightAnimationGeneration

    DispatchQueue.main.async { [weak self] in
      guard let self, generation == self.heightAnimationGeneration else { return }
      self.startPanelHeightAnimation(for: duration, generation: generation)
    }
  }

  private func startPanelHeightAnimation(for duration: TimeInterval, generation: Int) {
    let target = targetPanelHeight()
    let startHeight = panel.frame.height
    if abs(target - startHeight) < 0.5 { return }

    let start = CACurrentMediaTime()
    let clampedDuration = max(0.06, duration)

    heightAnimationTask = Task { @MainActor [weak self] in
      guard let self else { return }
      while !Task.isCancelled {
        guard generation == self.heightAnimationGeneration else { return }
        let elapsed = CACurrentMediaTime() - start
        let progress = min(1, elapsed / clampedDuration)
        let eased = Self.cssEase(progress)
        let nextHeight = startHeight + CGFloat(eased) * (target - startHeight)
        self.applyPanelHeight(nextHeight)

        guard progress < 1 else {
          self.applyPanelHeight(target)
          self.heightAnimationTask = nil
          return
        }
        try? await Task.sleep(nanoseconds: 16_666_667)
      }
    }
  }

  private static func cssEase(_ progress: Double) -> Double {
    let x1 = 0.32
    let y1 = 0.94
    let x2 = 0.60
    let y2 = 1.00
    var low = 0.0
    var high = 1.0
    for _ in 0..<10 {
      let mid = (low + high) / 2
      if cubicBezierValue(mid, x1, x2) < progress {
        low = mid
      } else {
        high = mid
      }
    }
    return cubicBezierValue((low + high) / 2, y1, y2)
  }

  private static func cubicBezierValue(_ t: Double, _ p1: Double, _ p2: Double) -> Double {
    let inv = 1 - t
    return 3 * inv * inv * t * p1 + 3 * inv * t * t * p2 + t * t * t
  }

  private func pollLayout(for duration: TimeInterval) {
    layoutPollWork?.cancel()
    let start = Date()
    func tick() {
      updatePanelHeight()
      guard Date().timeIntervalSince(start) < duration else {
        layoutPollWork = nil
        return
      }
      let work = DispatchWorkItem { tick() }
      layoutPollWork = work
      DispatchQueue.main.asyncAfter(deadline: .now() + 1.0 / 60.0, execute: work)
    }
    tick()
  }

  private func updatePassthrough() {
    guard !userHidden else { return }
    let cursor = NSEvent.mouseLocation
    let shouldIgnore = NativeCardHitTest.shouldIgnoreMouse(
      at: cursor,
      panelFrame: panel.frame,
      popoverVisible: model.popoverVisible
    )
    if panel.ignoresMouseEvents != shouldIgnore {
      panel.ignoresMouseEvents = shouldIgnore
    }
  }

  private func writeDebugSnapshot(to path: String) {
    containerView.layoutSubtreeIfNeeded()
    containerView.displayIfNeeded()
    let bounds = containerView.bounds
    guard bounds.width > 0, bounds.height > 0,
          let rep = containerView.bitmapImageRepForCachingDisplay(in: bounds) else {
      NSLog("[native-card] debug snapshot failed: invalid bounds \(bounds)")
      return
    }
    containerView.cacheDisplay(in: bounds, to: rep)
    guard let data = rep.representation(using: .png, properties: [:]) else {
      NSLog("[native-card] debug snapshot failed: PNG encoding")
      return
    }
    do {
      try data.write(to: URL(fileURLWithPath: path), options: .atomic)
      NSLog("[native-card] debug snapshot wrote \(path)")
    } catch {
      NSLog("[native-card] debug snapshot failed: \(error)")
    }
  }

  private func scheduleDebugSnapshots(toDirectory directory: String) {
    let env = ProcessInfo.processInfo.environment
    let startMs = Double(env["INHERENT_DEBUG_SNAPSHOT_START_MS"] ?? "") ?? 0
    let intervalMs = Double(env["INHERENT_DEBUG_SNAPSHOT_INTERVAL_MS"] ?? "") ?? 100
    let count = max(1, Int(env["INHERENT_DEBUG_SNAPSHOT_COUNT"] ?? "") ?? 1)
    do {
      try FileManager.default.createDirectory(
        at: URL(fileURLWithPath: directory),
        withIntermediateDirectories: true
      )
    } catch {
      NSLog("[native-card] debug snapshot sequence failed: \(error)")
      return
    }

    for index in 0..<count {
      let delay = (startMs + Double(index) * intervalMs) / 1000.0
      DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
        let path = URL(fileURLWithPath: directory)
          .appendingPathComponent(String(format: "frame-%04d.png", index))
          .path
        self?.writeDebugSnapshot(to: path)
      }
    }
  }

  private func focusForKeyboardInput(openInput: Bool = false) {
    if !NSApp.isActive {
      NSApp.activate(ignoringOtherApps: true)
    }
    if !panel.isVisible {
      panel.orderFrontRegardless()
    }
    panel.makeKey()
    panel.makeFirstResponder(hostingView)
    panel.ignoresMouseEvents = false
    userHidden = false
    fade.showInstant()
    if openInput {
      model.openInputFromHotkey()
    } else {
      model.focusInput()
    }
  }

  private func runFakeTurns() {
    NSLog("[native-card] DEBUG: running fake turns")
    let mode = ProcessInfo.processInfo.environment["INHERENT_DEBUG_FAKE_TURNS"] ?? "1"
    switch mode {
    case "basic":
      runBasicScenario()
      return
    case "multi-turn":
      runMultiTurnScenario()
      return
    case "overflow":
      runOverflowScenario()
      return
    case "drip-plain", "drip-fade":
      runDripScenario()
      return
    case "followup-restore":
      runFollowupRestoreScenario()
      return
    case "popover":
      runPopoverScenario()
      return
    case "voice-listening", "voice-transcribing", "voice-accepted", "voice-empty", "voice-error":
      runVoiceScenario(mode: mode)
      return
    default:
      break
    }

    let turns: [(String, String)] = [
      ("test 1", "alpha response"),
      ("test 2", "beta response with a bit more text"),
      ("test 3", "gamma - third turn"),
    ]
    var delay: TimeInterval = 0
    for (question, answer) in turns {
      DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
        self?.handleSiriOpen(payload: ["q": question, "streaming": true])
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 0.05) { [weak self] in
        self?.model.siriAppend(payload: ["token": answer])
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 0.20) { [weak self] in
        self?.model.siriDone(payload: ["fadeMs": 60000])
      }
      delay += 0.6
    }

    if mode == "collapse" {
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 1.5) { [weak self] in
        self?.model.clearHistoryCascade()
      }
    } else if mode == "fade" {
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 0.8) { [weak self] in
        self?.fadeOut(durationMs: 1500)
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 1.55) { [weak self] in
        self?.cancelFade()
      }
    }
  }

  private func runBasicScenario() {
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
      self?.handleSiriOpen(payload: [
        "content": "# 现在 23°\n\nbedroom · 客厅 22°  \n_(via siri:open IPC)_",
        "kind": "text",
      ])
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
        self?.model.siriDone(payload: ["fadeMs": 5000])
      }
    }
  }

  private func runMultiTurnScenario() {
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
      self?.handleSiriOpen(payload: [
        "content": "# Turn 1\n\n_(first turn — fades after 1s)_",
        "kind": "text",
      ])
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
        self?.model.siriDone(payload: ["fadeMs": 1000])
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + 6.0) { [weak self] in
        self?.handleSiriOpen(payload: [
          "content": "# Turn 2\n\n_(second turn, 6s after first)_",
          "kind": "text",
        ])
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
          self?.model.siriDone(payload: ["fadeMs": 5000])
        }
      }
    }
  }

  private func runFollowupRestoreScenario() {
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
      self?.handleSiriOpen(payload: [
        "q": "first",
        "content": "# Answer\n\n_(ready for follow-up)_",
        "kind": "text",
      ])
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
        self?.model.siriDone(payload: ["fadeMs": 60000])
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [weak self] in
        self?.model.handleGlobalEnterDown()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in
          self?.model.handleEnterUp()
        }
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + 1.7) { [weak self] in
        self?.model.submitInputText()
      }
    }
  }

  private func runOverflowScenario() {
    let sections = (1...40).map { idx in
      """
      ## 段 \(idx)

      这是第 \(idx) 段文字, 用来测试卡片在内容超过 800px 时是否能正确显示并允许内部滚动。卡片应当 ≤ 800px 高度, 内容超出部分通过卡内滚动条访问。
      """
    }.joined(separator: "\n\n")
    let content = "# 长内容溢出测试\n\n\(sections)"
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
      self?.handleSiriOpen(payload: ["content": content, "kind": "text"])
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
        self?.model.siriDone(payload: ["fadeMs": 8000])
      }
    }
  }

  private func runDripScenario() {
    let parts = [
      "# 流式输出",
      "测试\n\n",
      "正在",
      "生成响应",
      "中…\n\n",
      "**关键发现**",
      "：\n\n",
      "- 第一项",
      "：温度",
      " 23°\n",
      "- 第二项",
      "：湿度",
      " 65%\n",
      "- 第三项",
      "：气压",
      " 1013 hPa\n\n",
      "```python\n",
      "def hello():\n",
      "    print(\"Hello,",
      " jarvis!\")\n",
      "    return 42\n",
      "```\n\n",
      "测试结束。",
    ]
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
      self?.handleSiriOpen(payload: ["content": "", "streaming": true, "kind": "text"])
      var delay: TimeInterval = 0.1
      for token in parts {
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
          self?.model.siriAppend(payload: ["token": token])
        }
        delay += 0.18
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
        self?.model.siriDone(payload: ["fadeMs": 5000])
      }
    }
  }

  private func runVoiceScenario(mode: String) {
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
      self?.handleVoiceState(payload: ["phase": "listening"])
      guard mode != "voice-listening" else { return }
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) { [weak self] in
        switch mode {
        case "voice-transcribing":
          self?.handleVoiceState(payload: ["phase": "transcribing"])
        case "voice-accepted":
          self?.handleVoiceState(payload: ["phase": "accepted", "text": "客厅几度"])
        case "voice-empty":
          self?.handleVoiceState(payload: ["phase": "empty"])
        case "voice-error":
          self?.handleVoiceState(payload: ["phase": "error"])
        default:
          break
        }
      }
    }
  }

  private func runPopoverScenario() {
    let turns: [(String, String)] = [
      ("test 1", "alpha response"),
      ("test 2", "beta response with a bit more text"),
      ("test 3", "gamma - third turn"),
    ]
    var delay: TimeInterval = 0.5
    for (question, answer) in turns {
      DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
        self?.handleSiriOpen(payload: ["q": question, "streaming": true])
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 0.05) { [weak self] in
        self?.model.siriAppend(payload: ["token": answer])
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 0.20) { [weak self] in
        self?.model.siriDone(payload: ["fadeMs": 60000])
      }
      delay += 0.6
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + delay + 0.8) { [weak self] in
      guard let self, let turn = self.model.history.last else { return }
      self.model.showPopover(for: turn)
    }
  }

  private func startPasteShortcutMonitor() {
    localKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown]) { [weak self] event in
      guard let self else { return event }
      if self.isPasteShortcut(event) {
        if self.isTextFieldFirstResponder { return event }
        return self.model.stageImageFromClipboard() ? nil : event
      }
      if self.isCommandZero(event) {
        self.resetPosition()
        return nil
      }
      if event.keyCode == 53 && !self.isTextFieldFirstResponder {
        self.model.handleEscape()
        return nil
      }
      if event.keyCode == 36 || event.keyCode == 76 {
        if !self.nativeEnterDown {
          self.nativeEnterDown = true
          self.userHidden = false
          self.fade.showInstant()
          self.panel.ignoresMouseEvents = false
          self.model.handleGlobalEnterDown()
        }
        return nil
      }
      return event
    }
    localKeyUpMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyUp]) { [weak self] event in
      guard let self else { return event }
      if (event.keyCode == 36 || event.keyCode == 76), self.nativeEnterDown {
        self.nativeEnterDown = false
        self.model.handleEnterUp()
        return nil
      }
      return event
    }
  }

  private func isPasteShortcut(_ event: NSEvent) -> Bool {
    guard event.charactersIgnoringModifiers?.lowercased() == "v" else { return false }
    let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
    return flags.contains(.command) || flags.contains(.control)
  }

  private func isCommandZero(_ event: NSEvent) -> Bool {
    guard event.charactersIgnoringModifiers == "0" else { return false }
    let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
    return flags.contains(.command)
  }

  private var isTextFieldFirstResponder: Bool {
    panel.firstResponder is NativeTextField || panel.firstResponder is NSTextView
  }

  func toggleHotkey() {
    let cursor = NSEvent.mouseLocation
    let frames = NSScreen.screens.map { $0.frame }
    let cursorIdx = DisplayManager.indexOfFrameContaining(point: cursor, frames: frames)
    let cursorScreen = NSScreen.screens[cursorIdx]

    if panel.alphaValue > 0.5 {
      let cardCenter = CGPoint(x: panel.frame.midX, y: panel.frame.midY)
      let cardIdx = DisplayManager.indexOfFrameContaining(point: cardCenter, frames: frames)
      if cardIdx != cursorIdx {
        panel.anchorTopRight(of: cursorScreen)
        focusForKeyboardInput(openInput: true)
        return
      }
      fade.hideInstant()
      userHidden = true
      ws?.discardOpenTurn()
      return
    }

    userHidden = false
    panel.anchorTopRight(of: cursorScreen)
    focusForKeyboardInput(openInput: true)
  }

  fileprivate func handleSiriOpen(payload: [String: Any]?) {
    let streaming = (payload?["streaming"] as? Bool) ?? false
    let content = payload?["content"] as? String
    guard streaming || !(content?.isEmpty ?? true) else { return }

    userHidden = false
    if !userMovedPanel, let screen = DisplayManager.cursorScreen() {
      panel.anchorTopRight(of: screen)
    }
    fade.showInstant()
    model.siriOpen(payload: payload)
  }

  fileprivate func handleSiriAppend(payload: [String: Any]?) {
    model.siriAppend(payload: payload)
  }

  fileprivate func handleSiriDone(payload: [String: Any]?) {
    model.siriDone(payload: payload)
  }

  fileprivate func handleSiriReset() {
    fade.hideInstant()
    userHidden = true
    model.siriReset()
  }

  fileprivate func handleVoiceState(payload: [String: Any]?) {
    userHidden = false
    if !userMovedPanel, let screen = DisplayManager.cursorScreen() {
      panel.anchorTopRight(of: screen)
    }
    fade.showInstant()
    model.voiceState(payload: payload)
  }
}

enum NativeCardHitTest {
  struct Regions: Equatable {
    let card: NSRect
    let pill: NSRect
    let popover: NSRect
  }

  static func regions(for panelFrame: NSRect) -> Regions {
    let cardTop = panelFrame.maxY - NativeCardModel.pillReservedTop
    let cardLeft = panelFrame.maxX - NativeCardModel.cardWidth
    let cardRect = NSRect(
      x: cardLeft,
      y: panelFrame.minY,
      width: NativeCardModel.cardWidth,
      height: cardTop - panelFrame.minY
    )
    let pillWidth: CGFloat = 136
    let pillRect = NSRect(
      x: cardLeft + NativeCardModel.cardWidth / 2 - pillWidth / 2,
      y: cardTop,
      width: pillWidth,
      height: 35
    )
    let popoverRect = NSRect(
      x: panelFrame.minX,
      y: panelFrame.minY,
      width: NativeCardModel.popoverWidth,
      height: cardTop - panelFrame.minY
    )
    return Regions(card: cardRect, pill: pillRect, popover: popoverRect)
  }

  static func shouldIgnoreMouse(at point: NSPoint, panelFrame: NSRect, popoverVisible: Bool) -> Bool {
    let regions = regions(for: panelFrame)
    let inCard = pointInRoundedRect(point, regions.card, 30)
    let inPill = pointInRoundedRect(point, regions.pill, regions.pill.height / 2)
    let inPopover = popoverVisible && pointInRoundedRect(point, regions.popover, 30)
    return !(inCard || inPill || inPopover)
  }

  private static func pointInRoundedRect(_ point: NSPoint, _ rect: NSRect, _ radius: CGFloat) -> Bool {
    if !rect.contains(point) { return false }
    let r = min(radius, min(rect.width, rect.height) / 2)
    if r <= 0 { return true }
    if point.x < rect.minX + r && point.y > rect.maxY - r {
      let dx = (rect.minX + r) - point.x
      let dy = point.y - (rect.maxY - r)
      return dx * dx + dy * dy <= r * r
    }
    if point.x > rect.maxX - r && point.y > rect.maxY - r {
      let dx = point.x - (rect.maxX - r)
      let dy = point.y - (rect.maxY - r)
      return dx * dx + dy * dy <= r * r
    }
    if point.x < rect.minX + r && point.y < rect.minY + r {
      let dx = (rect.minX + r) - point.x
      let dy = (rect.minY + r) - point.y
      return dx * dx + dy * dy <= r * r
    }
    if point.x > rect.maxX - r && point.y < rect.minY + r {
      let dx = point.x - (rect.maxX - r)
      let dy = (rect.minY + r) - point.y
      return dx * dx + dy * dy <= r * r
    }
    return true
  }
}

private final class NativeControllerBridge: BridgeDispatcher {
  weak var controller: NativeCardController?

  init(controller: NativeCardController) {
    self.controller = controller
  }

  func siriOpen(payload: [String: Any]?) {
    Task { @MainActor in controller?.handleSiriOpen(payload: payload) }
  }

  func siriAppend(payload: [String: Any]?) {
    Task { @MainActor in controller?.handleSiriAppend(payload: payload) }
  }

  func siriDone(payload: [String: Any]?) {
    Task { @MainActor in controller?.handleSiriDone(payload: payload) }
  }

  func siriReset() {
    Task { @MainActor in controller?.handleSiriReset() }
  }

  func voiceState(payload: [String: Any]?) {
    Task { @MainActor in controller?.handleVoiceState(payload: payload) }
  }
}
