import AppKit
import WebKit
import UniformTypeIdentifiers

enum TurnState { case idle, open }

final class CardController: NSObject, IPCBridgeDelegate {
  let panel: CardPanel
  let webView: WKWebView
  let ipc: IPCBridge
  let consoleBridge: ConsoleBridge

  override init() {
    // Order matters: ipc.install mutates config.userContentController, which must
    // happen BEFORE WKWebView(configuration:) is constructed.
    let config = WKWebViewConfiguration()
    let ipc = IPCBridge()
    ipc.install(into: config)
    let console = ConsoleBridge()
    console.install(into: config)

    // DEBUG diagnostic: log every keydown + focusin reaching the document.
    // Helps diagnose whether keystrokes are arriving at the WKWebView at all
    // when the user clicks the input field. Remove after input handling is verified.
    let debugScript = """
      window.addEventListener('error', function(e) {
        console.log('[debug] window.error', e.message, 'src=' + (e.filename || 'inline'), 'line=' + e.lineno);
      });
      window.addEventListener('unhandledrejection', function(e) {
        console.log('[debug] unhandledrejection', e.reason && (e.reason.message || e.reason));
      });
      document.addEventListener('keydown', function(e) {
        console.log('[debug] keydown', e.key, 'target=' + (e.target && (e.target.id || e.target.tagName)));
      }, true);
      document.addEventListener('focusin', function(e) {
        console.log('[debug] focusin', e.target && (e.target.id || e.target.tagName));
      }, true);
      console.log('[debug] keylogger installed');
    """
    config.userContentController.addUserScript(WKUserScript(
      source: debugScript,
      injectionTime: .atDocumentEnd,
      forMainFrameOnly: true
    ))

    self.panel = CardPanel()
    self.webView = CardWebView.make(configuration: config)
    self.ipc = ipc
    self.consoleBridge = console
    super.init()

    webView.uiDelegate = self
    if let dropWebView = webView as? FirstMouseWebView {
      dropWebView.nativeImageDropHandler = { [weak self] url in
        self?.stageNativeImageFile(url, source: "drop") ?? false
      }
      dropWebView.nativeImageDropPasteboardHandler = { [weak self] pasteboard in
        self?.stageNativeImageFromPasteboard(pasteboard, source: "drop") ?? false
      }
      dropWebView.nativeImagePasteHandler = { [weak self] pasteboard in
        self?.stageNativeImageFromPasteboard(pasteboard, source: "paste") ?? false
      }
      dropWebView.nativeUserInteractionHandler = { [weak self] in
        self?.focusForKeyboardInput()
      }
    }
    ipc.delegate = self
    ipc.attach(webView: webView)

    // Connect WS bridge.
    let ws = WSClient(dispatcher: self)
    ws.turnIsOpen = { [weak self] in self?.turnState == .open }
    ws.connect()
    self.ws = ws

    let hotkey = HotkeyManager()
    _ = hotkey.register { [weak self] in self?.toggleHotkey() }
    self.hotkey = hotkey
    startPasteShortcutMonitor()

    // Wrap the WKWebView in a host NSView so panel.contentView has stable
    // non-optional anchor targets (added in the Task 3 cleanup pass).
    let host = CardDropHostView()
    host.nativeImageDropPasteboardHandler = { [weak self] pasteboard in
      self?.stageNativeImageFromPasteboard(pasteboard, source: "drop") ?? false
    }
    host.translatesAutoresizingMaskIntoConstraints = false
    host.addSubview(webView)
    NSLayoutConstraint.activate([
      webView.topAnchor.constraint(equalTo: host.topAnchor),
      webView.bottomAnchor.constraint(equalTo: host.bottomAnchor),
      webView.leadingAnchor.constraint(equalTo: host.leadingAnchor),
      webView.trailingAnchor.constraint(equalTo: host.trailingAnchor),
    ])
    panel.contentView = host

    do {
      try CardWebView.loadCard(into: webView)
    } catch CardWebViewError.projectRootNotSet {
      NSLog("[inherent] failed to load card.html: JARVIS_PROJECT_ROOT not set")
    } catch CardWebViewError.cardHtmlMissing(let url) {
      NSLog("[inherent] failed to load card.html: file not found at \(url.path)")
    } catch {
      NSLog("[inherent] failed to load card.html: \(error)")
    }

    if let _ = ProcessInfo.processInfo.environment["INHERENT_DEBUG_FAKE_TURNS"] {
      DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
        self?.runFakeTurns()
      }
    }

    // Dev-only: hot-reload the webview when card.html / card.js / card.css
    // change on disk. Saves a manual kill+launch every time you tweak a
    // CSS rule. Disabled in non-dev (no JARVIS_PROJECT_ROOT) so production
    // builds don't poll the filesystem.
    if ProcessInfo.processInfo.environment["JARVIS_PROJECT_ROOT"] != nil {
      startHotReload()
    }
  }

  /// Debug-only: fires 3 synthetic siri:open/append/done cycles to populate
  /// the history strip with chips, so screenshot harnesses can drive the
  /// collapse-direction + fade-recovery test paths without a live LLM.
  private func runFakeTurns() {
    NSLog("[inherent] DEBUG: running fake siri turns")
    let turns: [(String, String)] = [
      ("test 1", "alpha response"),
      ("test 2", "beta response with a bit more text"),
      ("test 3", "gamma — third turn"),
    ]
    var delay: Double = 0
    for (q, a) in turns {
      DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
        self?.siriOpen(payload: ["q": q])
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 0.05) { [weak self] in
        self?.siriAppend(payload: ["token": a])
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 0.20) { [weak self] in
        self?.siriDone(payload: ["fadeMs": 60000])
      }
      delay += 0.6
    }
    let mode = ProcessInfo.processInfo.environment["INHERENT_DEBUG_FAKE_TURNS"] ?? "1"
    if mode == "collapse" {
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 1.5) { [weak self] in
        NSLog("[inherent] DEBUG: triggering cascadeClear")
        self?.webView.evaluateJavaScript(
          "document.getElementById('pill-clear').click()",
          completionHandler: nil
        )
      }
    } else if mode == "fade" {
      // Drive a slow fade so the mid-flight cancel is observable, then cancel
      // via the IPC surface to simulate a hover-back recovery from .idle state.
      let fadeMs = 1500
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 0.8) { [weak self] in
        guard let self else { return }
        NSLog("[fade-test] starting fadeOut ms=\(fadeMs) alpha=\(self.panel.alphaValue)")
        self.fade.fadeOut(durationMs: fadeMs) { [weak self] in
          self?.userHidden = true
          self?.fade.hideInstant()
          self?.turnState = .idle
          NSLog("[fade-test] completion ran (should NOT print after cancel)")
        }
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 0.8 + 0.5) { [weak self] in
        guard let self else { return }
        NSLog("[fade-test] alpha mid-fade @0.5s = \(self.panel.alphaValue)")
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 0.8 + 0.75) { [weak self] in
        guard let self else { return }
        NSLog("[fade-test] firing cancelFade (mid-fade alpha=\(self.panel.alphaValue))")
        self.ipc(didReceive: "cancelFade", payload: [:])
      }
      DispatchQueue.main.asyncAfter(deadline: .now() + delay + 0.8 + 1.5) { [weak self] in
        guard let self else { return }
        NSLog("[fade-test] alpha post-cancel @1.5s = \(self.panel.alphaValue)")
      }
    }
  }

  func showInitial() {
    if let screen = NSScreen.main { panel.anchorTopRight(of: screen) }
    panel.orderFrontRegardless()
  }

  func shutdown() {
    audioDucker.restoreAll()
    ws?.shutdownNow()
    hotkey?.unregister()
    if let m = globalMouseMonitor { NSEvent.removeMonitor(m); globalMouseMonitor = nil }
    if let m = localMouseMonitor { NSEvent.removeMonitor(m); localMouseMonitor = nil }
    if let m = localKeyMonitor { NSEvent.removeMonitor(m); localKeyMonitor = nil }
  }

  /// NSPanel intercepts every click that lands inside its frame by default,
  /// which makes the 38px transparent strip above the card-wrap (where the
  /// history pill floats) eat clicks even when the pill itself is hidden.
  /// Toggle `ignoresMouseEvents` per-frame against the cursor position so
  /// only the actual interactive regions (card body, the DOM-reported pill
  /// body above it, and the popover when expanded) capture clicks; everything
  /// else passes through to whatever app sits behind the panel.
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

  /// Watch desktop/inherent/card.{html,js,css} for changes and reload the
  /// webview when any of them is modified. Atomic-write editors (vim, VS Code
  /// "save") rename a temp file over the target — so the underlying fd we're
  /// watching gets a `delete`/`rename` event, not `write`. After any event we
  /// re-open the path and re-arm. A 200ms debounce coalesces multi-file saves
  /// (e.g. css + js modified together) into a single reload.
  func startHotReload() {
    guard let projectRoot = ProcessInfo.processInfo.environment["JARVIS_PROJECT_ROOT"] else { return }
    let inherent = URL(fileURLWithPath: projectRoot).appendingPathComponent("desktop/inherent")
    for name in ["card.html", "card.js", "card.css"] {
      watchHotReloadFile(inherent.appendingPathComponent(name))
    }
    NSLog("[hot-reload] watching desktop/inherent/{card.html,card.js,card.css}")
  }

  private func watchHotReloadFile(_ url: URL) {
    let fd = open(url.path, O_EVTONLY)
    guard fd >= 0 else {
      NSLog("[hot-reload] open failed: \(url.lastPathComponent)")
      return
    }
    let source = DispatchSource.makeFileSystemObjectSource(
      fileDescriptor: fd,
      eventMask: [.write, .extend, .delete, .rename, .attrib],
      queue: DispatchQueue.main
    )
    source.setEventHandler { [weak self, weak source] in
      source?.cancel()
      self?.scheduleHotReload(rewatch: url)
    }
    source.setCancelHandler { close(fd) }
    source.resume()
    hotReloadSources.append(source)
  }

  private let nativeImageMaxBytes = 15 * 1024 * 1024
  private let nativeImageExtensions: Set<String> = ["png", "jpg", "jpeg", "webp", "gif"]

  private func imageMimeType(for url: URL) -> String? {
    let ext = url.pathExtension.lowercased()
    if ext == "jpg" { return "image/jpeg" }
    if nativeImageExtensions.contains(ext),
       let mime = UTType(filenameExtension: ext)?.preferredMIMEType {
      return mime == "image/jpg" ? "image/jpeg" : mime
    }
    return nil
  }

  private func stageNativeImageFile(_ url: URL, source: String) -> Bool {
    guard let mime = imageMimeType(for: url) else { return false }
    do {
      if let bytes = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize,
         bytes > nativeImageMaxBytes {
        showNativeImageError("image > 15MB")
        return false
      }
      let data = try Data(contentsOf: url)
      return stageNativeImage(
        data,
        mime: mime,
        name: url.lastPathComponent.isEmpty ? "image.png" : url.lastPathComponent,
        source: source
      )
    } catch {
      NSLog("[native-image] failed to read dropped file: \(error)")
      return false
    }
  }

  private func stageNativeImageFromPasteboard(_ pasteboard: NSPasteboard, source: String = "paste") -> Bool {
    let options: [NSPasteboard.ReadingOptionKey: Any] = [
      .urlReadingFileURLsOnly: true
    ]
    if let urls = pasteboard.readObjects(forClasses: [NSURL.self], options: options) as? [NSURL],
       let url = urls.map({ $0 as URL }).first(where: { imageMimeType(for: $0) != nil }) {
      return stageNativeImageFile(url, source: source)
    }

    let filenamesType = NSPasteboard.PasteboardType("NSFilenamesPboardType")
    if let paths = pasteboard.propertyList(forType: filenamesType) as? [String],
       let url = paths.map({ URL(fileURLWithPath: $0) }).first(where: { imageMimeType(for: $0) != nil }) {
      return stageNativeImageFile(url, source: source)
    }

    if let value = pasteboard.propertyList(forType: .fileURL) as? String,
       let url = URL(string: value),
       imageMimeType(for: url) != nil {
      return stageNativeImageFile(url, source: source)
    }

    if let data = pasteboard.data(forType: .png) {
      return stageNativeImage(data, mime: "image/png", name: "screen.png", source: source)
    }

    if let data = pasteboard.data(forType: .tiff),
       let image = NSImage(data: data),
       let png = pngData(from: image) {
      return stageNativeImage(png, mime: "image/png", name: "screen.png", source: source)
    }

    return false
  }

  private func pasteboardPlainText(_ pasteboard: NSPasteboard) -> String? {
    let text = pasteboard.string(forType: .string)
    guard let text, !text.isEmpty else { return nil }
    return text
  }

  private func startPasteShortcutMonitor() {
    localKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown]) { [weak self] event in
      guard let self, self.isPasteShortcut(event) else { return event }
      return self.stageNativeImageFromPasteboard(.general) ? nil : event
    }
  }

  private func isPasteShortcut(_ event: NSEvent) -> Bool {
    guard event.charactersIgnoringModifiers?.lowercased() == "v" else { return false }
    let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
    return flags.contains(.command) || flags.contains(.control)
  }

  private func focusForKeyboardInput(openInput: Bool = false) {
    if !NSApp.isActive {
      NSApp.activate(ignoringOtherApps: true)
    }
    if !panel.isVisible {
      panel.orderFrontRegardless()
    }
    panel.makeKey()
    panel.makeFirstResponder(webView)
    panel.ignoresMouseEvents = false
    if openInput {
      ipc.dispatchEvent("card:openInput", detail: nil)
    }
  }

  private func pngData(from image: NSImage) -> Data? {
    guard let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff) else {
      return nil
    }
    return rep.representation(using: .png, properties: [:])
  }

  private func stageNativeImage(_ data: Data, mime: String, name: String, source: String) -> Bool {
    guard !data.isEmpty else { return false }
    guard data.count <= nativeImageMaxBytes else {
      showNativeImageError("image > 15MB")
      return false
    }
    focusForKeyboardInput()

    let payload: [String: Any] = [
      "base64": data.base64EncodedString(),
      "mime": mime,
      "name": name,
      "source": source,
    ]
    guard let json = try? JSONSerialization.data(withJSONObject: payload),
          let jsonString = String(data: json, encoding: .utf8) else {
      return false
    }
    webView.evaluateJavaScript("window.jarvisStageImageAttachment && window.jarvisStageImageAttachment(\(jsonString))") { _, err in
      if let err {
        NSLog("[native-image] stage JS failed: \(err)")
      }
    }
    return true
  }

  private func showNativeImageError(_ message: String) {
    guard let data = try? JSONSerialization.data(withJSONObject: ["message": message]),
          let jsonString = String(data: data, encoding: .utf8) else {
      return
    }
    webView.evaluateJavaScript("window.jarvisImageAttachmentError && window.jarvisImageAttachmentError(\(jsonString))") { _, err in
      if let err {
        NSLog("[native-image] error JS failed: \(err)")
      }
    }
  }

  private func scheduleHotReload(rewatch url: URL) {
    hotReloadDebounce?.cancel()
    let work = DispatchWorkItem { [weak self] in
      guard let self else { return }
      NSLog("[hot-reload] reloading webview")
      self.webView.reload()
      // Re-arm watchers for any URLs we know about. Atomic write replaced
      // the inode, so all three need re-watching to be safe.
      self.hotReloadSources.removeAll()
      self.startHotReload()
    }
    hotReloadDebounce = work
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2, execute: work)
    _ = url  // touched only as a hint; rewatch happens via startHotReload()
  }

  private func updatePassthrough() {
    guard !userHidden else { return }
    if isDragging {
      panel.ignoresMouseEvents = false
      return
    }
    let cursor = NSEvent.mouseLocation
    let frame = panel.frame

    let cardWidth: CGFloat = 360
    let pillReservedTop: CGFloat = 38
    let popoverWidth: CGFloat = 300
    let cardRadius: CGFloat = 30        // matches CSS --radius-card
    let popoverRadius: CGFloat = 30     // matches CSS --radius-card

    let cardTop = frame.maxY - pillReservedTop
    let cardLeft = frame.maxX - cardWidth
    let cardRect = NSRect(x: cardLeft, y: frame.minY, width: cardWidth, height: cardTop - frame.minY)
    let inCard = pointInRoundedRect(cursor, cardRect, cardRadius)

    let pillRect = rectFromDOM(pillHitRegion, in: frame)
      ?? fallbackPillRect(frame: frame, cardLeft: cardLeft, cardTop: cardTop, cardWidth: cardWidth)
    let inPill = pointInRoundedRect(cursor, pillRect, pillRect.height / 2)

    // Panel width is now fixed at 678; popover visibility is signaled by
    // card.js via cardAPI.setWidth → popoverActive flag (set in case "setWidth").
    // When inactive, the left 318px popover slot is excluded from hit-test so
    // clicks pass through to whatever app sits behind.
    let popoverRect = NSRect(
      x: frame.minX, y: frame.minY,
      width: popoverWidth, height: cardTop - frame.minY
    )
    let inPopover = popoverActive && pointInRoundedRect(cursor, popoverRect, popoverRadius)

    let nextIgnore = !(inCard || inPill || inPopover)
    if panel.ignoresMouseEvents != nextIgnore {
      panel.ignoresMouseEvents = nextIgnore
    }
  }

  /// Point-in-rounded-rect test. Returns true iff `p` lies inside `rect` AND
  /// — when in any of the four corner squares of side `radius` — also inside
  /// the inscribed corner circle. The four cut-off corner regions outside the
  /// circle pass through, matching the CSS border-radius silhouette.
  private func pointInRoundedRect(_ p: NSPoint, _ rect: NSRect, _ radius: CGFloat) -> Bool {
    if !rect.contains(p) { return false }
    let r = min(radius, min(rect.width, rect.height) / 2)
    if r <= 0 { return true }
    // Top-left
    if p.x < rect.minX + r && p.y > rect.maxY - r {
      let dx = (rect.minX + r) - p.x
      let dy = p.y - (rect.maxY - r)
      return dx * dx + dy * dy <= r * r
    }
    // Top-right
    if p.x > rect.maxX - r && p.y > rect.maxY - r {
      let dx = p.x - (rect.maxX - r)
      let dy = p.y - (rect.maxY - r)
      return dx * dx + dy * dy <= r * r
    }
    // Bottom-left
    if p.x < rect.minX + r && p.y < rect.minY + r {
      let dx = (rect.minX + r) - p.x
      let dy = (rect.minY + r) - p.y
      return dx * dx + dy * dy <= r * r
    }
    // Bottom-right
    if p.x > rect.maxX - r && p.y < rect.minY + r {
      let dx = p.x - (rect.maxX - r)
      let dy = (rect.minY + r) - p.y
      return dx * dx + dy * dy <= r * r
    }
    return true
  }

  private func fallbackPillRect(frame: NSRect, cardLeft: CGFloat, cardTop: CGFloat, cardWidth: CGFloat) -> NSRect {
    // Used only before the web layer reports the real .q2x7-pill rect. Keep it
    // close to the rendered pill, not the old 220px hover-region wrapper.
    let width: CGFloat = 136
    let height: CGFloat = 35
    return NSRect(
      x: cardLeft + cardWidth / 2 - width / 2,
      y: cardTop,
      width: width,
      height: height
    )
  }

  private func rectFromDOM(_ rect: DOMHitRect?, in frame: NSRect) -> NSRect? {
    guard let rect, rect.w > 0, rect.h > 0 else { return nil }
    return NSRect(
      x: frame.minX + rect.x,
      y: frame.maxY - rect.y - rect.h,
      width: rect.w,
      height: rect.h
    )
  }

  private var userHidden = false
  private var turnState: TurnState = .idle
  private lazy var fade = FadeController(panel: panel)
  private let backend = BridgeBackend()
  private let audioDucker = SystemAudioDucker()
  private var ws: WSClient?
  private var hotkey: HotkeyManager?
  private var globalMouseMonitor: Any?
  private var localMouseMonitor: Any?
  private var localKeyMonitor: Any?
  private var isDragging = false
  private var userMovedPanel = false
  // Reflects card.js's intent — does the popover want to be visible right
  // now? Used by updatePassthrough to decide whether to hit-test the left
  // 318px popover slot. Not the panel's width (panel is fixed at 678).
  private var popoverActive = false
  private struct DOMHitRect {
    let x: CGFloat
    let y: CGFloat
    let w: CGFloat
    let h: CGFloat
  }
  private var pillHitRegion: DOMHitRect?
  private var hotReloadSources: [DispatchSourceFileSystemObject] = []
  private var hotReloadDebounce: DispatchWorkItem?

  // MARK: - IPCBridgeDelegate
  func ipc(didReceive op: String, payload: [String: Any]) {
    switch op {
    case "resize":
      if let h = payload["h"] as? Double {
        let clamped = DisplayManager.clampHeight(CGFloat(h))
        if abs(clamped - panel.frame.height) < 0.5 { return }
        let next = DisplayManager.applyHeight(to: panel.frame, newHeight: clamped)
        panel.setFrame(next, display: true, animate: false)
      }
    case "setWidth":
      // Panel width is fixed at 678; this IPC no longer resizes the panel.
      // It only signals popover intent so updatePassthrough can decide
      // whether to hit-test the popover slot. JS callers stay unchanged
      // (cardAPI.setWidth(360 | 678)) — Swift just interprets the value.
      if let w = payload["w"] as? Double {
        popoverActive = (w > DisplayManager.CARD_WIDTH + 1)
        updatePassthrough()
      }
    case "setHitRegions":
      if let pill = payload["pill"] as? [String: Any],
         let x = CGFloat.fromJSONNumber(pill["x"]),
         let y = CGFloat.fromJSONNumber(pill["y"]),
         let w = CGFloat.fromJSONNumber(pill["w"]),
         let h = CGFloat.fromJSONNumber(pill["h"]) {
        pillHitRegion = DOMHitRect(x: x, y: y, w: w, h: h)
      } else {
        pillHitRegion = nil
      }
      updatePassthrough()
    case "show":
      // Implicit show from card.js flushHeight() — only honor if user hasn't
      // manually hidden the card. Explicit user paths (siriOpen,
      // toggleHotkey-show) clear userHidden BEFORE calling fade.showInstant
      // directly, bypassing this case. Dedupe: if already visible at full
      // alpha, skip — flushHeightFor polls ~25× per growth animation.
      if !userHidden {
        if panel.alphaValue >= 1 && panel.isVisible { return }
        fade.showInstant()
      }
    case "close":
      userHidden = true
      fade.hideInstant()
    case "fadeOut":
      let ms = (payload["ms"] as? Double).map(Int.init) ?? 280
      fade.fadeOut(durationMs: ms) { [weak self] in
        self?.userHidden = true
        self?.fade.hideInstant()
        self?.turnState = .idle
      }
    case "movePanel":
      let dx = (payload["dx"] as? Double) ?? 0
      let dy = (payload["dy"] as? Double) ?? 0
      var f = panel.frame
      f.origin.x += CGFloat(dx)
      f.origin.y += CGFloat(dy)
      panel.setFrame(f, display: true, animate: false)
      userMovedPanel = true
    case "setDragging":
      isDragging = (payload["v"] as? Bool) ?? false
      // While dragging keep the panel fully receptive to mouse events so the
      // cursor leaving an interactive sub-region mid-drag doesn't flip
      // ignoresMouseEvents=true and break the drag in flight. updatePassthrough
      // re-evaluates as soon as dragging ends.
      if isDragging { panel.ignoresMouseEvents = false }
      else { updatePassthrough() }
    case "resetPosition":
      userMovedPanel = false
      if let screen = DisplayManager.cursorScreen() ?? NSScreen.main {
        panel.anchorTopRight(of: screen)
      }
      updatePassthrough()
    case "cancelFade":
      // Hover on the visible card (or its hover-region pill) should always
      // recover an in-flight fade, regardless of turn state — the original
      // Electron gate on `turnState == .open` left the panel stuck at a
      // mid-fade alpha after siri:done flipped the state to .idle, so a
      // user moving the mouse back onto a fading card couldn't restore it.
      // The panel's own `ignoresMouseEvents` gate already blocks hover events
      // on a fully-hidden (alpha=0) panel, so resurrecting an already-dead
      // card via stray hover is not a risk here.
      if !userHidden {
        fade.showInstant()
      }
    case "duckAudio":
      _ = audioDucker.duck()
    case "restoreAudio":
      audioDucker.restore()
    default:
      NSLog("[ipc] unknown op: \(op)")
    }
  }

  func ipcSubmit(text: String) async -> SubmitResult {
    NSLog("[ipc] submit text=\(text.prefix(80))")
    return await backend.submit(text: text)
  }

  func ipcSubmitImage(text: String, imageData: Data, mime: String, name: String) async -> SubmitResult {
    NSLog("[ipc] submitImage bytes=\(imageData.count) mime=\(mime)")
    return await backend.submitImage(text: text, imageData: imageData, mime: mime, name: name)
  }

  func ipcSubmitVoice(wavData: Data) async -> VoiceSubmitResult {
    NSLog("[ipc] submitVoice bytes=\(wavData.count)")
    return await backend.submitVoice(wavData: wavData)
  }

  func ipcPasteClipboard() -> ClipboardPasteResult {
    let pasteboard = NSPasteboard.general
    if stageNativeImageFromPasteboard(pasteboard) {
      return ClipboardPasteResult(ok: true, text: nil)
    }
    if let text = pasteboardPlainText(pasteboard) {
      focusForKeyboardInput()
      return ClipboardPasteResult(ok: true, text: text)
    }
    return ClipboardPasteResult(ok: false, text: nil)
  }
}

extension CardController: WKUIDelegate {
  func webView(
    _ webView: WKWebView,
    requestMediaCapturePermissionFor origin: WKSecurityOrigin,
    initiatedByFrame frame: WKFrameInfo,
    type: WKMediaCaptureType,
    decisionHandler: @escaping (WKPermissionDecision) -> Void
  ) {
    switch type {
    case .microphone, .cameraAndMicrophone:
      NSLog("[media] granting WKWebView microphone capture for inherent card")
      decisionHandler(.grant)
    default:
      decisionHandler(.deny)
    }
  }
}

// MARK: - BridgeDispatcher

extension CardController: BridgeDispatcher {
  func siriOpen(payload: [String: Any]?) {
    turnState = .open
    userHidden = false
    // Auto-anchor only if the user hasn't dragged the card to a custom spot.
    // Once they've moved it, every subsequent siri turn keeps the position.
    if !userMovedPanel, let screen = DisplayManager.cursorScreen() {
      panel.anchorTopRight(of: screen)
    }
    fade.showInstant()
    ipc.dispatchEvent("siri:open", detail: payload)
  }

  func siriAppend(payload: [String: Any]?) {
    guard turnState == .open else { return }
    ipc.dispatchEvent("siri:append", detail: payload)
  }

  func siriDone(payload: [String: Any]?) {
    guard turnState == .open else { return }
    ipc.dispatchEvent("siri:done", detail: payload)
    turnState = .idle
  }

  func siriReset() {
    fade.hideInstant()
    userHidden = true
    ipc.dispatchEvent("siri:reset", detail: nil)
    turnState = .idle
  }

  func voiceState(payload: [String: Any]?) {
    userHidden = false
    if !userMovedPanel, let screen = DisplayManager.cursorScreen() {
      panel.anchorTopRight(of: screen)
    }
    fade.showInstant()
    ipc.dispatchEvent("card:voice", detail: payload)
  }
}

// MARK: - Hotkey toggle

extension CardController {
  /// Three-branch toggle invoked by Cmd+Space.
  func toggleHotkey() {
    let cursor = NSEvent.mouseLocation
    let frames = NSScreen.screens.map { $0.frame }
    let cursorIdx = DisplayManager.indexOfFrameContaining(point: cursor, frames: frames)
    let cursorScreen = NSScreen.screens[cursorIdx]

    if panel.alphaValue > 0.5 {
      let cardCenter = CGPoint(x: panel.frame.midX, y: panel.frame.midY)
      let cardIdx = DisplayManager.indexOfFrameContaining(point: cardCenter, frames: frames)
      if cardIdx != cursorIdx {
        // visible on different display → relocate, refocus
        panel.anchorTopRight(of: cursorScreen)
        focusForKeyboardInput(openInput: true)
        return
      }
      // visible on same display → hide
      fade.hideInstant()
      userHidden = true
      return
    }

    // hidden → show on cursor display, focus input
    userHidden = false
    fade.showInstant()
    panel.anchorTopRight(of: cursorScreen)
    focusForKeyboardInput(openInput: true)
  }
}

private extension CGFloat {
  static func fromJSONNumber(_ value: Any?) -> CGFloat? {
    if let value = value as? CGFloat { return value }
    if let value = value as? Double { return CGFloat(value) }
    if let value = value as? Int { return CGFloat(value) }
    if let value = value as? NSNumber { return CGFloat(truncating: value) }
    if let value = value as? String, let parsed = Double(value) { return CGFloat(parsed) }
    return nil
  }
}
