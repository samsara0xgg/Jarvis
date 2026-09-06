import XCTest
@testable import InherentCard

final class SubmitRequestTests: XCTestCase {
  func test_buildRequest_url() {
    let req = SubmitRequest.build(text: "hello", environment: [:])
    XCTAssertEqual(req.url?.absoluteString, "http://127.0.0.1:8006/inherent/submit")
    XCTAssertEqual(req.httpMethod, "POST")
    XCTAssertEqual(req.value(forHTTPHeaderField: "Content-Type"), "application/json")
  }

  func test_buildRequest_body() throws {
    let req = SubmitRequest.build(text: "你好")
    let body = try XCTUnwrap(req.httpBody)
    let json = try JSONSerialization.jsonObject(with: body) as? [String: String]
    XCTAssertEqual(json, ["text": "你好"])
  }

  func test_classifyResponse_200() {
    let result = SubmitRequest.classify(status: 200, error: nil)
    XCTAssertTrue(result.ok)
    XCTAssertNil(result.reason)
  }

  func test_classifyResponse_400() {
    let result = SubmitRequest.classify(status: 400, error: nil)
    XCTAssertFalse(result.ok)
    XCTAssertEqual(result.reason, "http_400")
  }

  func test_classifyResponse_networkError() {
    let result = SubmitRequest.classify(status: nil, error: NSError(domain: "test", code: -1))
    XCTAssertFalse(result.ok)
    XCTAssertEqual(result.reason, "network")
  }

  func test_classifyResponse_500() {
    let result = SubmitRequest.classify(status: 500, error: nil)
    XCTAssertFalse(result.ok)
    XCTAssertEqual(result.reason, "http_500")
  }

  func test_imageBuildRequest_urlAndMultipartBody() throws {
    let image = Data([9, 8, 7])
    let req = ImageSubmitRequest.build(
      text: "这是什么",
      imageData: image,
      mime: "image/png",
      name: "screen.png",
      boundary: "test-boundary",
      environment: [:]
    )
    XCTAssertEqual(req.url?.absoluteString, "http://127.0.0.1:8006/inherent/image-submit")
    XCTAssertEqual(req.httpMethod, "POST")
    XCTAssertEqual(
      req.value(forHTTPHeaderField: "Content-Type"),
      "multipart/form-data; boundary=test-boundary"
    )

    let body = try XCTUnwrap(req.httpBody)
    let text = String(data: body, encoding: .utf8) ?? ""
    XCTAssertTrue(text.contains("name=\"text\""))
    XCTAssertTrue(text.contains("这是什么"))
    XCTAssertTrue(text.contains("name=\"image\"; filename=\"screen.png\""))
    XCTAssertTrue(text.contains("Content-Type: image/png"))
  }

  func test_imageClassifyNetworkError() {
    let result = ImageSubmitRequest.classify(
      status: nil,
      error: NSError(domain: "test", code: -1)
    )
    XCTAssertFalse(result.ok)
    XCTAssertEqual(result.reason, "network")
  }

  func test_voiceBuildRequest_urlAndMultipartHeaders() throws {
    let wav = Data([0, 1, 2, 3])
    let req = VoiceSubmitRequest.build(wavData: wav, boundary: "test-boundary", environment: [:])
    XCTAssertEqual(req.url?.absoluteString, "http://127.0.0.1:8006/inherent/asr-submit")
    XCTAssertEqual(req.httpMethod, "POST")
    XCTAssertEqual(
      req.value(forHTTPHeaderField: "Content-Type"),
      "multipart/form-data; boundary=test-boundary"
    )

    let body = try XCTUnwrap(req.httpBody)
    let text = String(data: body, encoding: .utf8) ?? ""
    XCTAssertTrue(text.contains("name=\"audio\"; filename=\"inherent.wav\""))
    XCTAssertTrue(text.contains("Content-Type: audio/wav"))
  }

  func test_voiceClassifyAcceptedPayload() throws {
    let payload = Data(#"{"status":"accepted","text":"客厅几度","emotion":"neutral"}"#.utf8)
    let result = VoiceSubmitRequest.classify(data: payload, status: 200, error: nil)
    XCTAssertTrue(result.ok)
    XCTAssertNil(result.reason)
    XCTAssertEqual(result.status, "accepted")
    XCTAssertEqual(result.text, "客厅几度")
    XCTAssertEqual(result.emotion, "neutral")
  }

  func test_voiceClassifyNetworkError() {
    let result = VoiceSubmitRequest.classify(
      data: nil,
      status: nil,
      error: NSError(domain: "test", code: -1)
    )
    XCTAssertFalse(result.ok)
    XCTAssertEqual(result.reason, "network")
  }

  // MARK: - Endpoint port override

  private let overridePort = [BridgeEndpoint.portEnvironmentKey: "8009"]

  func test_submitURL_defaultPortWhenUnset() {
    let req = SubmitRequest.build(text: "hello", environment: [:])
    XCTAssertEqual(req.url?.absoluteString, "http://127.0.0.1:8006/inherent/submit")
  }

  func test_submitURL_overriddenPort() {
    let req = SubmitRequest.build(text: "hello", environment: overridePort)
    XCTAssertEqual(req.url?.absoluteString, "http://127.0.0.1:8009/inherent/submit")
  }

  func test_imageSubmitURL_defaultPortWhenUnset() {
    let req = ImageSubmitRequest.build(
      text: "t", imageData: Data([1]), mime: "image/png", name: "a.png", environment: [:]
    )
    XCTAssertEqual(req.url?.absoluteString, "http://127.0.0.1:8006/inherent/image-submit")
  }

  func test_imageSubmitURL_overriddenPort() {
    let req = ImageSubmitRequest.build(
      text: "t", imageData: Data([1]), mime: "image/png", name: "a.png",
      environment: overridePort
    )
    XCTAssertEqual(req.url?.absoluteString, "http://127.0.0.1:8009/inherent/image-submit")
  }

  func test_voiceSubmitURL_defaultPortWhenUnset() {
    let req = VoiceSubmitRequest.build(wavData: Data([0, 1, 2, 3]), environment: [:])
    XCTAssertEqual(req.url?.absoluteString, "http://127.0.0.1:8006/inherent/asr-submit")
  }

  func test_voiceSubmitURL_overriddenPort() {
    let req = VoiceSubmitRequest.build(wavData: Data([0, 1, 2, 3]), environment: overridePort)
    XCTAssertEqual(req.url?.absoluteString, "http://127.0.0.1:8009/inherent/asr-submit")
  }

  func test_bridgeSocketURL_defaultPortWhenUnset() {
    XCTAssertEqual(
      BridgeBackend.wsURL(environment: [:]).absoluteString,
      "ws://127.0.0.1:8006/inherent/ws"
    )
  }

  func test_bridgeSocketURL_overriddenPort() {
    XCTAssertEqual(
      BridgeBackend.wsURL(environment: overridePort).absoluteString,
      "ws://127.0.0.1:8009/inherent/ws"
    )
  }

  func test_realtimeV2ConnectRequest_defaultPortWhenUnset() {
    let req = RealtimeTransportV2.connectRequest(token: "tok", environment: [:])
    XCTAssertEqual(req.url?.absoluteString, "ws://127.0.0.1:8006/inherent/ws/v2")
    XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer tok")
  }

  func test_realtimeV2ConnectRequest_overriddenPort() {
    let req = RealtimeTransportV2.connectRequest(token: "tok", environment: overridePort)
    XCTAssertEqual(req.url?.absoluteString, "ws://127.0.0.1:8009/inherent/ws/v2")
  }

  func test_malformedPort_keepsDefaultEverywhere() {
    for raw in ["abc", "", "0", "70000", "80 09"] {
      let env = [BridgeEndpoint.portEnvironmentKey: raw]
      XCTAssertEqual(
        SubmitRequest.build(text: "hello", environment: env).url?.absoluteString,
        "http://127.0.0.1:8006/inherent/submit",
        "rejected value: \(raw)"
      )
      XCTAssertEqual(BridgeEndpoint.port(environment: env), 8006, "rejected value: \(raw)")
    }
  }
}
