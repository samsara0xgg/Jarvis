import CryptoKit
import Foundation

/// The ADR-0014 D21 client half: authenticated, idempotent v2 text and ASR
/// submissions, and the receipt that starts the
/// `request_id -> input_event_uid/turn_id -> response_group_id` chain.
///
/// The point of this file is that a lost HTTP response is survivable.  The
/// client chooses the `request_id` before it sends anything, so a retry of the
/// *byte-identical* request resolves the original receipt on the server instead
/// of starting a second turn.  A different payload under the same id is the one
/// thing that must never be retried — it means the client's own bookkeeping is
/// wrong — so it surfaces as a typed error.
///
/// There is no `URLSession` here.  The transport is injected, which is what
/// keeps this module free of the network and lets the tests drive every branch
/// with recorded requests.

// MARK: - Transport

/// One HTTP request this client wants performed.
public struct InputSubmissionHTTPRequest: Sendable, Equatable {
  public var path: String
  public var contentType: String
  public var bearerToken: String
  public var body: Data

  public init(path: String, contentType: String, bearerToken: String, body: Data) {
    self.path = path
    self.contentType = contentType
    self.bearerToken = bearerToken
    self.body = body
  }
}

public struct InputSubmissionHTTPResponse: Sendable, Equatable {
  public var statusCode: Int
  public var body: Data

  public init(statusCode: Int, body: Data) {
    self.statusCode = statusCode
    self.body = body
  }
}

/// What performs the request.  A thrown error means the answer was *lost*, not
/// refused: the client may retry the identical request exactly once.
public protocol InputSubmissionTransport: Sendable {
  func perform(_ request: InputSubmissionHTTPRequest) async throws -> InputSubmissionHTTPResponse
}

// MARK: - Receipt and failures

/// The D21 accepted response, common to both routes.
public struct InputSubmissionReceipt: Decodable, Equatable, Sendable {
  public var status: String
  public var requestID: String
  public var inputEventUID: String
  public var turnID: String
  public var sessionID: String?
  /// ASR only; absent on a text receipt.
  public var utteranceID: String?
  public var text: String?
  public var emotion: String?

  enum CodingKeys: String, CodingKey {
    case status
    case requestID = "request_id"
    case inputEventUID = "input_event_uid"
    case turnID = "turn_id"
    case sessionID = "session_id"
    case utteranceID = "utterance_id"
    case text
    case emotion
  }
}

public enum InputSubmissionError: Error, Equatable, Sendable {
  /// HTTP 409: this `request_id` already named a different payload.  Retrying
  /// cannot help and a fresh id would submit the turn twice, so the caller has
  /// to decide.
  case payloadConflict
  /// HTTP 403: the bearer token is missing or wrong.
  case unauthorized
  case refused(statusCode: Int)
  /// The answer arrived but did not decode as a receipt.
  case malformedReceipt
  /// Both the first attempt and its one retry were lost.
  case unreachable
}

// MARK: - The client

public actor InputSubmissionClient {
  public static let textPath = "/inherent/submit/v2"
  public static let asrPath = "/inherent/asr-submit/v2"
  private static let multipartBoundary = "jarvis-inherent-v2"

  private let transport: InputSubmissionTransport
  private let clientInstanceID: String
  private let bearerToken: String
  private let newRequestID: @Sendable () -> String
  private let nowMs: @Sendable () -> Int

  /// - Parameters:
  ///   - clientInstanceID: Injected rather than minted here — the identity
  ///     lives in the `InherentCard` target, which this module cannot reach.
  ///   - newRequestID: One fresh id per submission; a UUID by default.
  public init(
    transport: InputSubmissionTransport,
    clientInstanceID: String,
    bearerToken: String,
    newRequestID: @escaping @Sendable () -> String = { UUID().uuidString },
    nowMs: @escaping @Sendable () -> Int = { Int(Date().timeIntervalSince1970 * 1000) }
  ) {
    self.transport = transport
    self.clientInstanceID = clientInstanceID
    self.bearerToken = bearerToken
    self.newRequestID = newRequestID
    self.nowMs = nowMs
  }

  /// Submit text and return the durable receipt.
  ///
  /// The returned `pending` is what the reducer holds until a
  /// `response.opened` carries the same `source_client_request_id`.
  public func submitText(
    _ text: String
  ) async throws -> (pending: PendingInputState, receipt: InputSubmissionReceipt) {
    let requestID = newRequestID()
    let submittedAtMs = nowMs()
    let body: [String: JSONValue] = [
      "request_id": .string(requestID),
      "client_instance_id": .string(clientInstanceID),
      "client_created_at_ms": .int(submittedAtMs),
      "text": .string(text),
    ]
    let request = InputSubmissionHTTPRequest(
      path: Self.textPath,
      contentType: "application/json",
      bearerToken: bearerToken,
      body: JSONValue.object(body).encodedForSubmission()
    )
    let receipt = try await sendOnce(request)
    let pending = PendingInputState(
      requestID: requestID, text: text, submittedAtMs: submittedAtMs
    )
    return (pending, receipt)
  }

  /// Submit one recorded WAV.  The digest the server verifies is computed here
  /// over the exact bytes that are uploaded.
  public func submitAudio(
    wav: Data, language: String = "zh-CN"
  ) async throws -> (pending: PendingInputState, receipt: InputSubmissionReceipt) {
    let requestID = newRequestID()
    let submittedAtMs = nowMs()
    let digest = SHA256.hash(data: wav).map { String(format: "%02x", $0) }.joined()
    let request = InputSubmissionHTTPRequest(
      path: Self.asrPath,
      contentType: "multipart/form-data; boundary=\(Self.multipartBoundary)",
      bearerToken: bearerToken,
      body: Self.multipartBody(
        fields: [
          "request_id": requestID,
          "client_instance_id": clientInstanceID,
          "client_created_at_ms": String(submittedAtMs),
          "audio_sha256": digest,
          "language": language,
        ],
        wav: wav
      )
    )
    let receipt = try await sendOnce(request)
    let pending = PendingInputState(
      requestID: requestID, text: "", submittedAtMs: submittedAtMs
    )
    return (pending, receipt)
  }

  /// Send, and on a *lost* answer send the identical bytes exactly once more.
  ///
  /// One retry, not a loop: the receipt makes a retry safe, it does not make an
  /// unreachable daemon reachable, and a second lost answer is information the
  /// caller needs rather than something to keep hiding.
  private func sendOnce(
    _ request: InputSubmissionHTTPRequest
  ) async throws -> InputSubmissionReceipt {
    let response: InputSubmissionHTTPResponse
    do {
      response = try await transport.perform(request)
    } catch {
      // The answer was *lost*, not refused. The request is idempotent, so
      // resend the identical bytes once. A refusal never reaches here: it
      // arrives as a response and `decode` turns it into a typed error with
      // no second send.
      guard let retried = try? await transport.perform(request) else {
        throw InputSubmissionError.unreachable
      }
      response = retried
    }
    return try Self.decode(response)
  }

  private static func decode(
    _ response: InputSubmissionHTTPResponse
  ) throws -> InputSubmissionReceipt {
    switch response.statusCode {
    case 200:
      guard
        let receipt = try? JSONDecoder().decode(
          InputSubmissionReceipt.self, from: response.body
        )
      else { throw InputSubmissionError.malformedReceipt }
      return receipt
    case 403: throw InputSubmissionError.unauthorized
    case 409: throw InputSubmissionError.payloadConflict
    default: throw InputSubmissionError.refused(statusCode: response.statusCode)
    }
  }

  private static func multipartBody(fields: [String: String], wav: Data) -> Data {
    var body = Data()
    // Sorted so the same submission always produces the same bytes: a retry
    // that differed by field order would still be accepted, but the client
    // could no longer prove to itself that it resent the identical request.
    for (key, value) in fields.sorted(by: { $0.key < $1.key }) {
      body.append(Data("--\(multipartBoundary)\r\n".utf8))
      body.append(Data("Content-Disposition: form-data; name=\"\(key)\"\r\n\r\n".utf8))
      body.append(Data("\(value)\r\n".utf8))
    }
    body.append(Data("--\(multipartBoundary)\r\n".utf8))
    body.append(
      Data(
        "Content-Disposition: form-data; name=\"audio\"; filename=\"u.wav\"\r\n".utf8
      )
    )
    body.append(Data("Content-Type: audio/wav\r\n\r\n".utf8))
    body.append(wav)
    body.append(Data("\r\n--\(multipartBoundary)--\r\n".utf8))
    return body
  }
}

// MARK: - Request-body rendering

extension JSONValue {
  /// Render this value as the UTF-8 bytes of one request body.
  ///
  /// `JSONValue` already models every shape the wire uses and is what the rest
  /// of this module decodes into; it is simply `Decodable` only, so the one
  /// direction this file needs lives here.  Object keys are emitted sorted so
  /// the same submission always produces the same bytes — a retry that differed
  /// by key order would still be accepted, but the client could no longer prove
  /// to itself that it resent the identical request.
  func encodedForSubmission() -> Data {
    Data(rendered().utf8)
  }

  private func rendered() -> String {
    switch self {
    case .null: return "null"
    case .bool(let value): return value ? "true" : "false"
    case .int(let value): return String(value)
    case .double(let value): return String(value)
    case .string(let value): return Self.quote(value)
    case .array(let values): return "[\(values.map { $0.rendered() }.joined(separator: ","))]"
    case .object(let fields):
      let body = fields.sorted { $0.key < $1.key }.map { pair in
        "\(Self.quote(pair.key)):\(pair.value.rendered())"
      }
      return "{\(body.joined(separator: ","))}"
    }
  }

  private static func quote(_ value: String) -> String {
    var escaped = ""
    for character in value.unicodeScalars {
      switch character {
      case "\"": escaped += "\\\""
      case "\\": escaped += "\\\\"
      case "\n": escaped += "\\n"
      case "\r": escaped += "\\r"
      case "\t": escaped += "\\t"
      default:
        if character.value < 0x20 {
          escaped += String(format: "\\u%04x", character.value)
        } else {
          escaped.unicodeScalars.append(character)
        }
      }
    }
    return "\"\(escaped)\""
  }
}
