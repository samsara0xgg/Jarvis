// jarvis-ambient-sounds: Apple SoundAnalysis over the microphone audio the daemon already
// captures (ADR 0151).
//
// stdin:  raw 16 kHz mono signed 16-bit little-endian PCM, the canonical ingress frames.
// stdout: one JSON line per classifier window that has any label at or above 0.3, never
//         silence or speech:  {"t": <window end, seconds of audio received>,
//                               "labels": [["cough", 0.97], ...]}
// Audio is analysed and dropped; nothing is written to disk. Exits when stdin closes.

import AVFoundation
import Foundation
import SoundAnalysis

let kRate = 16_000.0
let kFloor = 0.3
let kSkip: Set<String> = ["silence", "speech"]

final class Observer: NSObject, SNResultsObserving {
    func request(_ request: SNRequest, didProduce result: SNResult) {
        guard let result = result as? SNClassificationResult else { return }
        let labels = result.classifications
            .filter { $0.confidence >= kFloor && !kSkip.contains($0.identifier) }
            .map { "[\"\($0.identifier)\",\(String(format: "%.3f", $0.confidence))]" }
        if labels.isEmpty { return }
        let end = (result.timeRange.start + result.timeRange.duration).seconds
        print("{\"t\":\(String(format: "%.3f", end)),\"labels\":[\(labels.joined(separator: ","))]}")
    }

    func request(_ request: SNRequest, didFailWithError error: Error) {
        FileHandle.standardError.write(Data("jarvis-ambient-sounds: \(error)\n".utf8))
        exit(2)
    }
}

setvbuf(stdout, nil, _IOLBF, 0)
guard let format = AVAudioFormat(
    commonFormat: .pcmFormatFloat32, sampleRate: kRate, channels: 1, interleaved: false)
else { exit(1) }
let analyzer = SNAudioStreamAnalyzer(format: format)
let observer = Observer()
do {
    let request = try SNClassifySoundRequest(classifierIdentifier: .version1)
    request.overlapFactor = 0.5
    try analyzer.add(request, withObserver: observer)
} catch {
    FileHandle.standardError.write(Data("jarvis-ambient-sounds: \(error)\n".utf8))
    exit(1)
}

var carry = Data()
var position: AVAudioFramePosition = 0
while true {
    let chunk = FileHandle.standardInput.availableData
    if chunk.isEmpty { break }
    carry.append(chunk)
    let frames = carry.count / 2
    if frames == 0 { continue }
    guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))
    else { exit(1) }
    buffer.frameLength = AVAudioFrameCount(frames)
    let out = buffer.floatChannelData![0]
    carry.withUnsafeBytes { raw in
        for i in 0..<frames {
            let lo = UInt16(raw[2 * i]), hi = UInt16(raw[2 * i + 1])
            out[i] = Float(Int16(bitPattern: lo | (hi << 8))) / 32768.0
        }
    }
    carry.removeFirst(frames * 2)
    analyzer.analyze(buffer, atAudioFramePosition: position)
    position += AVAudioFramePosition(frames)
}
analyzer.completeAnalysis()
