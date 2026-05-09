import WebKit

final class ConsoleBridge: NSObject, WKScriptMessageHandler {
  func install(into config: WKWebViewConfiguration) {
    if let url = Bundle.main.url(forResource: "consoleBridge", withExtension: "js"),
       let src = try? String(contentsOf: url) {
      let script = WKUserScript(source: src, injectionTime: .atDocumentStart, forMainFrameOnly: true)
      config.userContentController.addUserScript(script)
      NSLog("[console] consoleBridge.js installed (\(src.count) bytes)")
    } else {
      NSLog("[console] consoleBridge.js missing from bundle")
    }
    config.userContentController.add(self, name: "console")
  }

  func userContentController(_ uc: WKUserContentController, didReceive msg: WKScriptMessage) {
    guard let body = msg.body as? [String: Any],
          let level = body["level"] as? String,
          let text  = body["text"]  as? String else { return }
    FileHandle.standardError.write(Data("[card.\(level)] \(text)\n".utf8))
  }
}
