import WebKit

protocol IPCBridgeDelegate: AnyObject {
  func ipc(didReceive op: String, payload: [String: Any])
  func ipcSubmit(text: String) async -> SubmitResult
  func ipcSubmitImage(text: String, imageData: Data, mime: String, name: String) async -> SubmitResult
  func ipcSubmitVoice(wavData: Data) async -> VoiceSubmitResult
  func ipcPasteClipboard() -> ClipboardPasteResult
}

struct SubmitResult {
  let ok: Bool
  let reason: String?
}

struct VoiceSubmitResult {
  let ok: Bool
  let reason: String?
  let status: String?
  let text: String?
  let emotion: String?
}

struct ClipboardPasteResult {
  let ok: Bool
  let text: String?
}

final class IPCBridge: NSObject, WKScriptMessageHandler, WKScriptMessageHandlerWithReply {
  weak var delegate: IPCBridgeDelegate?
  private weak var webView: WKWebView?

  func install(into config: WKWebViewConfiguration) {
    let userContent = config.userContentController

    if let shimURL = Bundle.main.url(forResource: "cardAPIShim", withExtension: "js"),
       let shim = try? String(contentsOf: shimURL) {
      userContent.addUserScript(WKUserScript(
        source: shim,
        injectionTime: .atDocumentStart,
        forMainFrameOnly: true
      ))
      NSLog("[ipc] cardAPIShim.js installed (\(shim.count) bytes)")
    } else {
      NSLog("[ipc] cardAPIShim.js missing from bundle")
    }

    userContent.add(self, name: "cardAPI")
    userContent.addScriptMessageHandler(self, contentWorld: .page, name: "cardAPISubmit")
  }

  func attach(webView: WKWebView) { self.webView = webView }

  // MARK: - JS -> Swift (fire-and-forget)
  func userContentController(_ uc: WKUserContentController, didReceive msg: WKScriptMessage) {
    NSLog("[ipc] in: name=\(msg.name) body=\(msg.body)")
    guard msg.name == "cardAPI", let body = msg.body as? [String: Any], let op = body["op"] as? String else {
      NSLog("[ipc] malformed message: \(msg.body)")
      return
    }
    var payload = body
    payload.removeValue(forKey: "op")
    delegate?.ipc(didReceive: op, payload: payload)
  }

  // MARK: - JS -> Swift (with reply, used for submit)
  func userContentController(
    _ uc: WKUserContentController,
    didReceive msg: WKScriptMessage,
    replyHandler: @escaping (Any?, String?) -> Void
  ) {
    NSLog("[ipc] reply-in: name=\(msg.name) body=\(msg.body)")
    guard let body = msg.body as? [String: Any], let op = body["op"] as? String else {
      NSLog("[ipc] reply malformed: \(msg.body)")
      replyHandler(nil, "malformed")
      return
    }
    if op == "duckAudio" || op == "restoreAudio" {
      delegate?.ipc(didReceive: op, payload: [:])
      replyHandler(["ok": true], nil)
    } else if op == "pasteClipboard" {
      let result = delegate?.ipcPasteClipboard() ?? ClipboardPasteResult(ok: false, text: nil)
      replyHandler(["ok": result.ok, "text": result.text as Any], nil)
    } else if op == "submit", let text = body["text"] as? String {
      NSLog("[ipc] submit op text=\(text.count) chars")
      Task { @MainActor in
        let result = await (delegate?.ipcSubmit(text: text) ?? SubmitResult(ok: false, reason: "no_delegate"))
        NSLog("[ipc] submit result ok=\(result.ok) reason=\(result.reason ?? "nil")")
        replyHandler(["ok": result.ok, "reason": result.reason as Any], nil)
      }
    } else if op == "submitImage", let imageBase64 = body["imageBase64"] as? String {
      guard let imageData = Data(base64Encoded: imageBase64), !imageData.isEmpty else {
        NSLog("[ipc] submitImage invalid image payload")
        replyHandler(["ok": false, "reason": "bad_image"], nil)
        return
      }
      let text = body["text"] as? String ?? ""
      let mime = body["mime"] as? String ?? "image/png"
      let name = body["name"] as? String ?? "image.png"
      NSLog("[ipc] submitImage op bytes=\(imageData.count) mime=\(mime)")
      Task { @MainActor in
        let result = await (delegate?.ipcSubmitImage(
          text: text,
          imageData: imageData,
          mime: mime,
          name: name
        ) ?? SubmitResult(ok: false, reason: "no_delegate"))
        NSLog("[ipc] submitImage result ok=\(result.ok) reason=\(result.reason ?? "nil")")
        replyHandler(["ok": result.ok, "reason": result.reason as Any], nil)
      }
    } else if op == "submitVoice", let wavBase64 = body["wavBase64"] as? String {
      guard let wavData = Data(base64Encoded: wavBase64), !wavData.isEmpty else {
        NSLog("[ipc] submitVoice invalid wav payload")
        replyHandler(["ok": false, "reason": "bad_audio"], nil)
        return
      }
      NSLog("[ipc] submitVoice op bytes=\(wavData.count)")
      Task { @MainActor in
        let result = await (delegate?.ipcSubmitVoice(wavData: wavData) ?? VoiceSubmitResult(
          ok: false,
          reason: "no_delegate",
          status: nil,
          text: nil,
          emotion: nil
        ))
        NSLog("[ipc] submitVoice result ok=\(result.ok) status=\(result.status ?? "nil") reason=\(result.reason ?? "nil")")
        replyHandler([
          "ok": result.ok,
          "reason": result.reason as Any,
          "status": result.status as Any,
          "text": result.text as Any,
          "emotion": result.emotion as Any,
        ], nil)
      }
    } else {
      NSLog("[ipc] reply unknown op: \(op)")
      replyHandler(nil, "unknown_op_\(op)")
    }
  }

  // MARK: - Swift -> JS dispatch
  func dispatchEvent(_ name: String, detail: [String: Any]? = nil) {
    guard let webView else { return }
    let detailJSON: String
    if let detail, let data = try? JSONSerialization.data(withJSONObject: detail),
       let s = String(data: data, encoding: .utf8) {
      detailJSON = s
    } else {
      detailJSON = "null"
    }
    let escaped = name
      .replacingOccurrences(of: "\\", with: "\\\\")
      .replacingOccurrences(of: "'", with: "\\'")
    let js = "window.dispatchEvent(new CustomEvent('\(escaped)', {detail: \(detailJSON)}))"
    webView.evaluateJavaScript(js) { _, err in
      if let err { NSLog("[ipc] dispatch \(name) failed: \(err)") }
    }
  }
}
