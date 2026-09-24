import Foundation
import Vision
import CoreGraphics

public enum CaptionOCR {
    /// The region uses normalized image coordinates with the origin at top left.
    public static func recognize(_ image: CGImage, region: CGRect = CGRect(x: 0, y: 0, width: 1, height: 1)) throws -> String {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["en-US"]
        request.usesLanguageCorrection = true
        let clipped = region.intersection(CGRect(x: 0, y: 0, width: 1, height: 1))
        guard !clipped.isEmpty, !clipped.isNull else { return "" }
        request.regionOfInterest = CGRect(x: clipped.minX, y: 1 - clipped.maxY, width: clipped.width, height: clipped.height)
        try VNImageRequestHandler(cgImage: image).perform([request])
        let observations = request.results ?? []
        let heights = observations.filter {
            ($0.topCandidates(1).first?.string.filter(\.isLetter).count ?? 0) >= 5
        }.map { $0.boundingBox.height }.sorted()
        let typicalHeight = heights.isEmpty ? 0 : heights[heights.count / 2]
        let lines = observations.filter {
            // Scrolling caption panels can clip a line anywhere inside the
            // captured window. Its unusually short glyphs otherwise become
            // high-confidence gibberish and contaminate the retained context.
            $0.boundingBox.height >= typicalHeight * 0.72
        }.sorted {
            if abs($0.boundingBox.midY - $1.boundingBox.midY) < 0.012 {
                return $0.boundingBox.minX < $1.boundingBox.minX
            }
            return $0.boundingBox.midY > $1.boundingBox.midY
        }
        return lines.compactMap { observation in
            guard let candidate = observation.topCandidates(1).first, candidate.confidence >= 0.35,
                  candidate.string.filter(\.isLetter).count >= 2 else { return nil }
            return candidate.string
        }.joined(separator: " ")
    }
}
