import XCTest
@testable import InherentCard

final class SubmitRequestTests: XCTestCase {
  func test_buildRequest_url() {
    let req = SubmitRequest.build(text: "hello")
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
      boundary: "test-boundary"
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
    let req = VoiceSubmitRequest.build(wavData: wav, boundary: "test-boundary")
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
}
