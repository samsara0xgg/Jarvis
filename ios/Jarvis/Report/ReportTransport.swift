import Foundation

/// How a batch of frames reaches the brain. The app wires an `HTTPReportTransport` to the paired brain.
protocol ReportTransport {
    /// Sends one batch (within `ReportBatch` limits) and returns one ack per frame, in order.
    func send(_ frames: [ReportFrame]) async throws -> [FrameAck]
    /// The request `send` would make, for a batch the network refused: the background session (ReportUploads) delivers it when the network is
    /// back. nil where there is no such thing (a transport that is not HTTP).
    func backgroundRequest(for frames: [ReportFrame]) -> URLRequest?
}

extension ReportTransport {
    func backgroundRequest(for frames: [ReportFrame]) -> URLRequest? { nil }
}

enum ReportTransportError: Error {
    /// No brain is paired yet; the frames stay queued.
    case notConfigured
    /// 401 or 403: the token is wrong, revoked, or not a paired device's. Frames stay queued.
    case unauthorized
    /// 400 or 413: the brain will not take this batch as sent; sending it again cannot help.
    case rejected(Int)
    /// Any other status, or an answer that is not `{"acks": [...]}`. Frames stay queued.
    case server(Int)
}

/// `POST {brain}/inherent/device/events` with `Authorization: Bearer <device token>` (ADR 0197, ADR 0170).
/// `endpoint` is asked at each send, so a pairing made later is picked up; it returns nil while unpaired.
struct HTTPReportTransport: ReportTransport {
    typealias Endpoint = (base: URL, token: String)

    static let path = "inherent/device/events"

    let endpoint: () -> Endpoint?
    let session: URLSession

    init(session: URLSession = HTTPReportTransport.defaultSession, endpoint: @escaping () -> Endpoint?) {
        self.session = session
        self.endpoint = endpoint
    }

    static let defaultSession: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 20
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    /// The POST for a batch, without its body (the body is `ReportBatch.body(frames)`).
    func request() throws -> URLRequest {
        guard let endpoint = endpoint() else { throw ReportTransportError.notConfigured }
        var request = URLRequest(url: endpoint.base.appending(path: Self.path))
        request.httpMethod = "POST"
        request.setValue("Bearer \(endpoint.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return request
    }

    func backgroundRequest(for frames: [ReportFrame]) -> URLRequest? { try? request() }

    func send(_ frames: [ReportFrame]) async throws -> [FrameAck] {
        var request = try request()
        request.httpBody = ReportBatch.body(frames)
        let (data, response) = try await session.data(for: request)
        return try Self.interpret(status: (response as? HTTPURLResponse)?.statusCode ?? 0, data: data)
    }

    /// The brain's answer to a batch, from a request made here or by the background session.
    static func interpret(status: Int, data: Data) throws -> [FrameAck] {
        switch status {
        case 200: break
        case 401, 403: throw ReportTransportError.unauthorized
        case 400, 413: throw ReportTransportError.rejected(status)
        default: throw ReportTransportError.server(status)
        }
        struct Answer: Decodable { let acks: [FrameAck] }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard let answer = try? decoder.decode(Answer.self, from: data) else { throw ReportTransportError.server(status) }
        return answer.acks
    }
}
