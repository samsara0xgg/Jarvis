import Foundation

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

protocol NativeBackendSubmitting {
  func submit(text: String) async -> SubmitResult
  func submitImage(text: String, imageData: Data, mime: String, name: String) async -> SubmitResult
  func submitVoice(wavData: Data) async -> VoiceSubmitResult
}

// MARK: - Dispatch protocol + router

protocol BridgeDispatcher: AnyObject {
  func siriOpen(payload: [String: Any]?)
  func siriAppend(payload: [String: Any]?)
  func siriDone(payload: [String: Any]?)
  func siriReset()
  func voiceState(payload: [String: Any]?)
}

enum BridgeMessageRouter {
  static func dispatch(json: [String: Any], to target: BridgeDispatcher) {
    guard let op = json["op"] as? String else { return }
    let payload = json["payload"] as? [String: Any]
    switch op {
    case "open":   target.siriOpen(payload: payload)
    case "append": target.siriAppend(payload: payload)
    case "done":   target.siriDone(payload: payload)
    case "reset":  target.siriReset()
    case "voice":  target.voiceState(payload: payload)
    default:       break
    }
  }
}

// MARK: - WebSocket client

extension BridgeBackend {
  static let WS_URL = URL(string: "ws://127.0.0.1:8006/inherent/ws")!
}

struct ReconnectBackoff {
  static let schedule: [TimeInterval] = [1, 2, 4, 8, 16]
  private var attempt = 0
  mutating func next() -> TimeInterval {
    let idx = min(attempt, ReconnectBackoff.schedule.count - 1)
    attempt += 1
    return ReconnectBackoff.schedule[idx]
  }
  mutating func reset() { attempt = 0 }
}

struct BridgeTurnGate {
  private(set) var turnOpen = false

  mutating func forceReset() {
    turnOpen = false
  }

  mutating func shouldDispatch(op: String, payload: [String: Any]?) -> Bool {
    switch op {
    case "open":
      let streaming = (payload?["streaming"] as? Bool) ?? false
      let content = payload?["content"] as? String
      guard streaming || !(content?.isEmpty ?? true) else { return false }
      turnOpen = true
      return true
    case "append":
      return turnOpen
    case "done":
      guard turnOpen else { return false }
      turnOpen = false
      return true
    case "reset":
      turnOpen = false
      return true
    case "voice":
      return true
    default:
      return true
    }
  }
}

final class WSClient {
  weak var dispatcher: BridgeDispatcher?
  private var task: URLSessionWebSocketTask?
  private let session = URLSession(configuration: .default)
  private var backoff = ReconnectBackoff()
  private var reconnectWork: DispatchWorkItem?
  private var watchdog: DispatchWorkItem?
  private var shutdown = false
  private var turnGate = BridgeTurnGate()
  private let q = DispatchQueue(label: "com.allen.jarvis.inherent.ws", qos: .userInitiated)

  /// Public turn-state snapshot the controller updates so reconnect logic can
  /// decide whether to force a siri:reset on (re)connect.
  var turnIsOpen: () -> Bool = { false }

  init(dispatcher: BridgeDispatcher) { self.dispatcher = dispatcher }

  func connect() {
    q.async { [weak self] in self?.connectOnQueue() }
  }

  private func connectOnQueue() {
    if shutdown { return }
    let task = session.webSocketTask(with: BridgeBackend.WS_URL)
    self.task = task
    task.resume()
    NSLog("[bridge] WS connecting")
    if turnIsOpen() {
      NSLog("[bridge] (re)connect mid-turn; forcing reset")
      turnGate.forceReset()
      DispatchQueue.main.async { self.dispatcher?.siriReset() }
    }
    receiveLoop()
  }

  func shutdownNow() {
    q.async { [weak self] in
      guard let self else { return }
      self.shutdown = true
      self.task?.cancel(with: .goingAway, reason: nil)
      self.task = nil
      self.reconnectWork?.cancel()
      self.watchdog?.cancel()
    }
  }

  func discardOpenTurn() {
    q.async { [weak self] in
      self?.turnGate.forceReset()
      self?.clearWatchdog()
    }
  }

  private func armWatchdog() {
    // Caller is on q.
    watchdog?.cancel()
    let work = DispatchWorkItem { [weak self] in
      NSLog("[bridge] watchdog: no done within 30s; forcing reset")
      self?.turnGate.forceReset()
      DispatchQueue.main.async { self?.dispatcher?.siriReset() }
    }
    watchdog = work
    q.asyncAfter(deadline: .now() + 30, execute: work)
  }

  private func clearWatchdog() {
    // Caller is on q.
    watchdog?.cancel()
    watchdog = nil
  }

  private func scheduleReconnect() {
    if shutdown { return }
    let delay = backoff.next()
    NSLog("[bridge] reconnecting in \(delay)s")
    let work = DispatchWorkItem { [weak self] in self?.connectOnQueue() }
    reconnectWork = work
    q.asyncAfter(deadline: .now() + delay, execute: work)
  }

  private func receiveLoop() {
    task?.receive { [weak self] result in
      // URLSession completion runs on URLSession's queue — hop to our serial q.
      self?.q.async {
        guard let self else { return }
        switch result {
        case .success(let msg):
          self.handle(message: msg)
          self.receiveLoop()
        case .failure(let err):
          NSLog("[bridge] WS receive failed: \(err); will reconnect")
          self.task = nil
          self.scheduleReconnect()
        }
      }
    }
  }

  private func handle(message msg: URLSessionWebSocketTask.Message) {
    // Caller is already on q.
    let data: Data
    switch msg {
    case .string(let s): data = Data(s.utf8)
    case .data(let d):   data = d
    @unknown default:    return
    }
    guard let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
      NSLog("[bridge] WS JSON parse failed")
      return
    }
    guard shouldDispatch(json: json) else {
      backoff.reset()
      return
    }
    // Dispatcher calls are AppKit-touching — hop to main.
    DispatchQueue.main.async { [weak self] in
      guard let dispatcher = self?.dispatcher else { return }
      BridgeMessageRouter.dispatch(json: json, to: dispatcher)
    }
    // First successful receive proves the connection is alive — reset backoff.
    // (Task 9 deviation: kept here on first receive, not moved back to connect().)
    backoff.reset()
  }

  private func shouldDispatch(json: [String: Any]) -> Bool {
    guard let op = json["op"] as? String else { return false }
    let payload = json["payload"] as? [String: Any]
    let accepted = turnGate.shouldDispatch(op: op, payload: payload)
    if !accepted {
      switch op {
      case "open":
        NSLog("[bridge] ignoring empty non-streaming open")
      case "append":
        NSLog("[bridge] ignoring append while turn idle")
      case "done":
        NSLog("[bridge] ignoring done while turn idle")
      default:
        break
      }
      return false
    }
    switch op {
    case "open":
      armWatchdog()
    case "done", "reset":
      clearWatchdog()
    default:
      break
    }
    return true
  }
}

// MARK: - Submit

enum SubmitRequest {
  static let endpoint = URL(string: "http://127.0.0.1:8006/inherent/submit")!

  static func build(text: String) -> URLRequest {
    var req = URLRequest(url: endpoint)
    req.httpMethod = "POST"
    req.setValue("application/json", forHTTPHeaderField: "Content-Type")
    req.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text])
    return req
  }

  static func classify(status: Int?, error: Error?) -> SubmitResult {
    if error != nil { return SubmitResult(ok: false, reason: "network") }
    guard let status else { return SubmitResult(ok: false, reason: "no_response") }
    if (200...299).contains(status) { return SubmitResult(ok: true, reason: nil) }
    return SubmitResult(ok: false, reason: "http_\(status)")
  }
}

enum ImageSubmitRequest {
  static let endpoint = URL(string: "http://127.0.0.1:8006/inherent/image-submit")!

  static func build(
    text: String,
    imageData: Data,
    mime: String,
    name: String,
    boundary: String = UUID().uuidString
  ) -> URLRequest {
    var req = URLRequest(url: endpoint)
    req.httpMethod = "POST"
    req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

    let safeName = name
      .replacingOccurrences(of: "/", with: "_")
      .replacingOccurrences(of: "\\", with: "_")
    let fileName = safeName.isEmpty ? "image.png" : safeName
    let contentType = mime.isEmpty ? "image/png" : mime

    var body = Data()
    body.appendUTF8("--\(boundary)\r\n")
    body.appendUTF8("Content-Disposition: form-data; name=\"text\"\r\n\r\n")
    body.appendUTF8(text)
    body.appendUTF8("\r\n")
    body.appendUTF8("--\(boundary)\r\n")
    body.appendUTF8("Content-Disposition: form-data; name=\"image\"; filename=\"\(fileName)\"\r\n")
    body.appendUTF8("Content-Type: \(contentType)\r\n\r\n")
    body.append(imageData)
    body.appendUTF8("\r\n--\(boundary)--\r\n")
    req.httpBody = body
    return req
  }

  static func classify(status: Int?, error: Error?) -> SubmitResult {
    if error != nil { return SubmitResult(ok: false, reason: "network") }
    guard let status else { return SubmitResult(ok: false, reason: "no_response") }
    if (200...299).contains(status) { return SubmitResult(ok: true, reason: nil) }
    return SubmitResult(ok: false, reason: "http_\(status)")
  }
}

enum VoiceSubmitRequest {
  static let endpoint = URL(string: "http://127.0.0.1:8006/inherent/asr-submit")!

  static func build(wavData: Data, boundary: String = UUID().uuidString) -> URLRequest {
    var req = URLRequest(url: endpoint)
    req.httpMethod = "POST"
    req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

    var body = Data()
    body.appendUTF8("--\(boundary)\r\n")
    body.appendUTF8("Content-Disposition: form-data; name=\"audio\"; filename=\"inherent.wav\"\r\n")
    body.appendUTF8("Content-Type: audio/wav\r\n\r\n")
    body.append(wavData)
    body.appendUTF8("\r\n--\(boundary)--\r\n")
    req.httpBody = body
    return req
  }

  static func classify(data: Data?, status: Int?, error: Error?) -> VoiceSubmitResult {
    if error != nil { return VoiceSubmitResult(ok: false, reason: "network", status: nil, text: nil, emotion: nil) }
    guard let status else { return VoiceSubmitResult(ok: false, reason: "no_response", status: nil, text: nil, emotion: nil) }
    guard (200...299).contains(status) else {
      return VoiceSubmitResult(ok: false, reason: "http_\(status)", status: nil, text: nil, emotion: nil)
    }
    var payload: [String: Any] = [:]
    if let data,
       let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
      payload = json
    }
    return VoiceSubmitResult(
      ok: true,
      reason: nil,
      status: payload["status"] as? String,
      text: payload["text"] as? String,
      emotion: payload["emotion"] as? String
    )
  }
}

final class BridgeBackend: NativeBackendSubmitting {
  func submit(text: String) async -> SubmitResult {
    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.isEmpty { return SubmitResult(ok: false, reason: "empty") }
    let req = SubmitRequest.build(text: trimmed)
    do {
      let (_, response) = try await URLSession.shared.data(for: req)
      let status = (response as? HTTPURLResponse)?.statusCode
      return SubmitRequest.classify(status: status, error: nil)
    } catch {
      return SubmitRequest.classify(status: nil, error: error)
    }
  }

  func submitImage(text: String, imageData: Data, mime: String, name: String) async -> SubmitResult {
    if imageData.isEmpty {
      return SubmitResult(ok: false, reason: "empty_image")
    }
    let req = ImageSubmitRequest.build(text: text, imageData: imageData, mime: mime, name: name)
    do {
      let (_, response) = try await URLSession.shared.data(for: req)
      let status = (response as? HTTPURLResponse)?.statusCode
      return ImageSubmitRequest.classify(status: status, error: nil)
    } catch {
      return ImageSubmitRequest.classify(status: nil, error: error)
    }
  }

  func submitVoice(wavData: Data) async -> VoiceSubmitResult {
    if wavData.count <= 44 {
      return VoiceSubmitResult(ok: false, reason: "empty_audio", status: nil, text: nil, emotion: nil)
    }
    let req = VoiceSubmitRequest.build(wavData: wavData)
    do {
      let (data, response) = try await URLSession.shared.data(for: req)
      let status = (response as? HTTPURLResponse)?.statusCode
      return VoiceSubmitRequest.classify(data: data, status: status, error: nil)
    } catch {
      return VoiceSubmitRequest.classify(data: nil, status: nil, error: error)
    }
  }
}

private extension Data {
  mutating func appendUTF8(_ string: String) {
    append(Data(string.utf8))
  }
}
