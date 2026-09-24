import Foundation
import AVFoundation
import Translation
import CaptionCore

@main
struct CaptionProbe {
    static func main() async throws {
        guard CommandLine.arguments.count >= 2 else {
            print("Usage: CaptionProbe recording.mov [--ocr-only]")
            return
        }
        let asset = AVURLAsset(url: URL(fileURLWithPath: CommandLine.arguments[1]))
        let duration = try await asset.load(.duration).seconds
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        var tracker = CaptionTracker()
        var samples: [[String: Any]] = []
        var latest = ""
        for tick in 0..<Int(duration * 2) {
            let time = Double(tick) / 2
            let (image, _) = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600))
            let start = Date()
            let text = try CaptionOCR.recognize(image)
            let elapsed = Int(Date().timeIntervalSince(start) * 1000)
            if let update = tracker.ingest(text) {
                latest = update
                samples.append(["second": time, "ocr_ms": elapsed, "visible_text": text, "merged_context": update])
            }
        }
        guard latest.localizedCaseInsensitiveContains("compassion"), latest.localizedCaseInsensitiveContains("shoes") else {
            throw NSError(domain: "CaptionProbe", code: 1, userInfo: [NSLocalizedDescriptionKey: "The supplied recording did not yield the expected caption words."])
        }
        let availability = await LanguageAvailability().status(from: .init(identifier: "en"), to: .init(identifier: "zh-Hans"))
        var report: [String: Any] = ["frames": Int(duration * 2), "updates": samples.count,
                                    "samples": samples, "translation_language_status": String(describing: availability)]
        if !CommandLine.arguments.contains("--ocr-only") {
            let session = TranslationSession(installedSource: .init(identifier: "en"), target: .init(identifier: "zh-Hans"))
            let start = Date()
            let response = try await session.translate(latest)
            guard response.targetText.unicodeScalars.contains(where: { (0x3400...0x9fff).contains(Int($0.value)) }) else {
                throw NSError(domain: "CaptionProbe", code: 2, userInfo: [NSLocalizedDescriptionKey: "Translation returned no Chinese text."])
            }
            report["translation"] = response.targetText
            report["translation_ms"] = Int(Date().timeIntervalSince(start) * 1000)
        }
        let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
        print(String(decoding: data, as: UTF8.self))
    }
}
