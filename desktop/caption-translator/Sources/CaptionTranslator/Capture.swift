import AppKit
import ScreenCaptureKit
import CoreImage
import CaptionCore

enum CaptureIssue: LocalizedError {
    case resized
    case recognition(String)
    var errorDescription: String? {
        switch self {
        case .resized: return "窗口大小已改变，请重新框选字幕区域。"
        case .recognition(let detail): return "无法识别字幕：\(detail)"
        }
    }
}

private func frameGeometry(_ attachments: [SCStreamFrameInfo: Any]) -> [CGSize] {
    [SCStreamFrameInfo.screenRect, .contentRect].compactMap { key in
        let value = attachments[key]
        if let rect = value as? CGRect { return rect.size }
        if let dictionary = value as? [String: Any] {
            return CGRect(dictionaryRepresentation: dictionary as CFDictionary)?.size
        }
        return nil
    }
}

struct WindowPreview: @unchecked Sendable {
    let image: CGImage
    let geometry: [CGSize]
}

final class CaptionFrames: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let region: CGRect
    let receive: @Sendable (Result<String, Error>) -> Void
    private let context = CIContext(options: [.cacheIntermediates: false])
    private var initialGeometry: [CGSize]?
    private var geometryFailed = false
    private var recognitionFailures = 0

    init(region: CGRect, expectedGeometry: [CGSize]?, receive: @escaping @Sendable (Result<String, Error>) -> Void) {
        self.region = region
        self.initialGeometry = expectedGeometry
        self.receive = receive
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sampleBuffer.isValid,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
              let status = attachments.first?[.status] as? Int,
              status == SCFrameStatus.complete.rawValue,
              let buffer = sampleBuffer.imageBuffer else { return }
        guard !geometryFailed else { return }
        let geometry = frameGeometry(attachments[0])
        if let initialGeometry, initialGeometry.count == geometry.count,
           zip(initialGeometry, geometry).contains(where: { abs($0.width - $1.width) > 2 || abs($0.height - $1.height) > 2 }) {
            geometryFailed = true
            receive(.failure(CaptureIssue.resized))
            return
        }
        if initialGeometry == nil { initialGeometry = geometry }
        autoreleasepool {
            let image = CIImage(cvPixelBuffer: buffer)
            guard let cgImage = context.createCGImage(image, from: image.extent) else { return }
            do {
                let text = try CaptionOCR.recognize(cgImage, region: region)
                recognitionFailures = 0
                receive(.success(text))
            } catch {
                // A transient blank/transition frame must not stop a session.
                recognitionFailures += 1
                if recognitionFailures == 4 { receive(.failure(CaptureIssue.recognition(error.localizedDescription))) }
            }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) { receive(.failure(error)) }
}

/// Preview uses the same stream authorization as the system window picker.
/// SCScreenshotManager can separately require broad screen-recording access.
final class PreviewFrames: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let receive: @Sendable (Result<WindowPreview, Error>) -> Void
    private let context = CIContext(options: [.cacheIntermediates: false])
    init(receive: @escaping @Sendable (Result<WindowPreview, Error>) -> Void) { self.receive = receive }
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sampleBuffer.isValid, let buffer = sampleBuffer.imageBuffer,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
              let status = attachments.first?[.status] as? Int,
              status == SCFrameStatus.complete.rawValue else { return }
        let image = CIImage(cvPixelBuffer: buffer)
        if let frame = context.createCGImage(image, from: image.extent) {
            receive(.success(WindowPreview(image: frame, geometry: frameGeometry(attachments[0]))))
        }
    }
    func stream(_ stream: SCStream, didStopWithError error: Error) { receive(.failure(error)) }
}

@MainActor
private final class PreviewResult {
    var value: Result<WindowPreview, Error>?
}

@MainActor
func capturePreview(for filter: SCContentFilter) async throws -> WindowPreview {
    let result = PreviewResult()
    let receiver = PreviewFrames { frame in
        Task { @MainActor in if result.value == nil { result.value = frame } }
    }
    let stream = SCStream(filter: filter, configuration: captureConfiguration(for: filter), delegate: receiver)
    try stream.addStreamOutput(receiver, type: .screen, sampleHandlerQueue: DispatchQueue(label: "caption.preview"))
    do {
        try await stream.startCapture()
        let deadline = Date().addingTimeInterval(5)
        while result.value == nil && Date() < deadline {
            try await Task.sleep(for: .milliseconds(100))
        }
        try await stream.stopCapture()
        if let value = result.value { return try value.get() }
        throw NSError(domain: "CaptionPreview", code: 1, userInfo: [NSLocalizedDescriptionKey: "窗口没有返回画面，请确认它未被最小化。"])
    } catch {
        try? await stream.stopCapture()
        throw error
    }
}

func captureConfiguration(for filter: SCContentFilter) -> SCStreamConfiguration {
    let config = SCStreamConfiguration()
    let scale = min(Double(filter.pointPixelScale), 2400 / max(filter.contentRect.width, filter.contentRect.height))
    config.width = max(2, Int(filter.contentRect.width * scale))
    config.height = max(2, Int(filter.contentRect.height * scale))
    config.minimumFrameInterval = CMTime(value: 1, timescale: 2)
    config.queueDepth = 3
    config.showsCursor = false
    config.capturesAudio = false
    config.pixelFormat = kCVPixelFormatType_32BGRA
    config.ignoreShadowsSingleWindow = true
    config.scalesToFit = true
    return config
}
