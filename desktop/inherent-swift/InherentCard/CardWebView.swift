import Foundation
import WebKit

enum CardWebViewError: Error {
  case projectRootNotSet
  case cardHtmlMissing(URL)
}

/// WKWebView subclass that opts the first click into delivery — without this
/// override, a click landing on a chip while the panel is not key gets
/// consumed by AppKit's window-activation pass and never reaches the JS
/// click handler. The user has to click twice to open a popover. Returning
/// true from `acceptsFirstMouse(for:)` makes inactive-panel clicks both
/// promote the panel to key AND fire as normal mouse events on the view.
final class FirstMouseWebView: WKWebView {
  var nativeImageDropHandler: ((URL) -> Bool)?
  var nativeImageDropPasteboardHandler: ((NSPasteboard) -> Bool)?
  var nativeImagePasteHandler: ((NSPasteboard) -> Bool)?
  var nativeUserInteractionHandler: (() -> Void)?

  override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

  override func mouseDown(with event: NSEvent) {
    nativeUserInteractionHandler?()
    super.mouseDown(with: event)
  }

  override func performKeyEquivalent(with event: NSEvent) -> Bool {
    let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
    if event.charactersIgnoringModifiers?.lowercased() == "v",
       flags.contains(.command) || flags.contains(.control),
       nativeImagePasteHandler?(NSPasteboard.general) == true {
      return true
    }
    return super.performKeyEquivalent(with: event)
  }

  @objc func paste(_ sender: Any?) {
    if nativeImagePasteHandler?(NSPasteboard.general) == true { return }
    guard let text = NSPasteboard.general.string(forType: .string), !text.isEmpty else { return }
    guard let data = try? JSONSerialization.data(withJSONObject: ["text": text]),
          let json = String(data: data, encoding: .utf8) else {
      return
    }
    evaluateJavaScript("window.jarvisPastePlainText && window.jarvisPastePlainText(\(json))", completionHandler: nil)
  }

  override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
    if canAcceptImageDrag(sender.draggingPasteboard) { return .copy }
    return super.draggingEntered(sender)
  }

  override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
    if canAcceptImageDrag(sender.draggingPasteboard) { return .copy }
    return super.draggingUpdated(sender)
  }

  override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
    if nativeImageDropPasteboardHandler?(sender.draggingPasteboard) == true {
      return true
    }
    guard let url = firstDraggedFileURL(sender) else {
      return super.performDragOperation(sender)
    }
    return nativeImageDropHandler?(url) ?? false
  }

  private func canAcceptImageDrag(_ pasteboard: NSPasteboard) -> Bool {
    if firstDraggedFileURL(pasteboard) != nil { return true }
    if pasteboard.data(forType: .png) != nil || pasteboard.data(forType: .tiff) != nil {
      return true
    }
    let typeNames = (pasteboard.types ?? []).map(\.rawValue)
    return typeNames.contains { type in
      type.contains("image") || type.contains("file-url") || type == "NSFilenamesPboardType"
    }
  }

  private func firstDraggedFileURL(_ sender: NSDraggingInfo) -> URL? {
    firstDraggedFileURL(sender.draggingPasteboard)
  }

  private func firstDraggedFileURL(_ pasteboard: NSPasteboard) -> URL? {
    let options: [NSPasteboard.ReadingOptionKey: Any] = [
      .urlReadingFileURLsOnly: true
    ]
    let urls = pasteboard.readObjects(
      forClasses: [NSURL.self],
      options: options
    ) as? [NSURL]
    if let url = urls?.first as URL? {
      return url
    }

    if let value = pasteboard.propertyList(forType: .fileURL) as? String {
      return URL(string: value)
    }
    if let value = pasteboard.propertyList(forType: .URL) as? String {
      return URL(string: value) ?? URL(fileURLWithPath: value)
    }
    let filenamesType = NSPasteboard.PasteboardType("NSFilenamesPboardType")
    if let paths = pasteboard.propertyList(forType: filenamesType) as? [String],
       let path = paths.first {
      return URL(fileURLWithPath: path)
    }
    return nil
  }
}

final class CardDropHostView: NSView {
  var nativeImageDropPasteboardHandler: ((NSPasteboard) -> Bool)?

  override init(frame frameRect: NSRect) {
    super.init(frame: frameRect)
    registerForDraggedTypes(Self.draggedTypes)
  }

  required init?(coder: NSCoder) {
    super.init(coder: coder)
    registerForDraggedTypes(Self.draggedTypes)
  }

  override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
    canAcceptImageDrag(sender.draggingPasteboard) ? .copy : []
  }

  override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
    canAcceptImageDrag(sender.draggingPasteboard) ? .copy : []
  }

  override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
    nativeImageDropPasteboardHandler?(sender.draggingPasteboard) ?? false
  }

  private static let draggedTypes: [NSPasteboard.PasteboardType] = [
    .fileURL,
    .URL,
    .png,
    .tiff,
    NSPasteboard.PasteboardType("NSFilenamesPboardType"),
  ]

  private func canAcceptImageDrag(_ pasteboard: NSPasteboard) -> Bool {
    if pasteboard.canReadObject(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) {
      return true
    }
    if pasteboard.data(forType: .png) != nil || pasteboard.data(forType: .tiff) != nil {
      return true
    }
    let typeNames = (pasteboard.types ?? []).map(\.rawValue)
    return typeNames.contains { type in
      type.contains("image") || type.contains("file-url") || type == "NSFilenamesPboardType"
    }
  }
}

enum CardWebView {
  static func make(configuration: WKWebViewConfiguration) -> WKWebView {
    #if DEBUG
    configuration.preferences.setValue(true, forKey: "developerExtrasEnabled")
    #endif
    // card.js loads ES modules from https://esm.sh (markdown-it-async, shiki).
    // WKWebView with loadFileURL blocks cross-origin script imports under the
    // file:// origin's CORS policy; Electron permits them. The private SPI
    // _allowUniversalAccessFromFileURLs lives on WKWebViewConfiguration and
    // is exposed via KVC under the underscore-stripped name.
    configuration.setValue(true, forKey: "allowUniversalAccessFromFileURLs")
    let view = FirstMouseWebView(frame: .zero, configuration: configuration)
    view.setValue(false, forKey: "drawsBackground")  // transparent so panel bg shows through
    view.allowsLinkPreview = false
    view.translatesAutoresizingMaskIntoConstraints = false
    view.registerForDraggedTypes([
      .fileURL,
      .URL,
      .png,
      .tiff,
      NSPasteboard.PasteboardType("NSFilenamesPboardType"),
    ])
    return view
  }

  /// Loads `card.html` from the jarvis repo at `$JARVIS_PROJECT_ROOT/desktop/inherent/`.
  /// The env var is set by the launcher (Task 13) and points at the repo root, so the
  /// renderer assets remain a single source of truth shared with the Electron version.
  static func loadCard(into webView: WKWebView) throws {
    guard let projectRoot = ProcessInfo.processInfo.environment["JARVIS_PROJECT_ROOT"] else {
      throw CardWebViewError.projectRootNotSet
    }
    let projectURL = URL(fileURLWithPath: projectRoot, isDirectory: true)
    let cardURL = projectURL.appendingPathComponent("desktop/inherent/card.html")
    guard FileManager.default.fileExists(atPath: cardURL.path) else {
      throw CardWebViewError.cardHtmlMissing(cardURL)
    }
    webView.loadFileURL(cardURL, allowingReadAccessTo: projectURL)
  }
}
