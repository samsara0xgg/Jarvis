// ADR-0006 D9 spike: capture one acoustic run with and without VoiceProcessingIO.
//
// D9 keeps natural speakerphone barge-in disabled until a backend "proves
// residual echo, near-end recall, double-talk, and false-cancel targets".
// Nothing in the repository can open an AEC-capable stream, so this host
// buys the measurement without adding one: it plays a far-end WAV through
// the default output while recording the default input, with
// `inputNode.setVoiceProcessingEnabled(true)` on one run and off on the
// other. The two captures are scored by
// `scripts/spike_voiceprocessingio_aec.py`.
//
// One self-contained file, run directly, AVFoundation + Foundation only:
//
//   swift scripts/spike_voiceprocessingio_aec.swift \
//     --far-end ~/.jarvis-lane-b-test/aec-spike/far-end.wav \
//     --out     ~/.jarvis-lane-b-test/aec-spike/capture-aec-on.wav \
//     --aec on --lead 2.0 --tail 2.0
//
// The far-end file is played through the engine's own output node so that
// voice processing sees it as its reference signal; a separate player
// process would give the canceller nothing to subtract. The capture is
// written as 16 kHz mono PCM16 — the WAV shape `FileReplayBackend` already
// enforces — and a `<out>.json` sidecar carries the measured window offsets
// and the format facts so the analysis side never has to guess them.

import AVFoundation
import Foundation

// MARK: - Arguments

func argument(_ name: String) -> String? {
  let arguments = CommandLine.arguments
  guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else {
    return nil
  }
  return arguments[index + 1]
}

func fail(_ message: String, code: Int32) -> Never {
  FileHandle.standardError.write(Data((message + "\n").utf8))
  exit(code)
}

guard let farEndPath = argument("--far-end"), let outPath = argument("--out") else {
  fail("usage: --far-end <wav> --out <wav> --aec on|off [--lead 2.0] [--tail 2.0]", code: 2)
}
let aecEnabled = (argument("--aec") ?? "off").lowercased() == "on"
let leadSeconds = Double(argument("--lead") ?? "2.0") ?? 2.0
let tailSeconds = Double(argument("--tail") ?? "2.0") ?? 2.0
let maxPlaybackSeconds = 20.0

// MARK: - Capture sink

/// Converts every input tap buffer to 16 kHz mono PCM16 and counts frames.
final class CaptureSink {
  private let lock = NSLock()
  private let outputFormat: AVAudioFormat
  private var converter: AVAudioConverter?
  private var pcm = Data()
  private var written = 0
  private(set) var conversionFailure: String?
  private(set) var tapFormatDescription: String?
  private(set) var channelMapNote = "none"

  init(outputFormat: AVAudioFormat) {
    self.outputFormat = outputFormat
  }

  /// Frames written to the capture so far — the sample clock the windows use.
  var frames: Int {
    lock.lock()
    defer { lock.unlock() }
    return written
  }

  /// The captured 16 kHz mono PCM16 bytes.
  var data: Data {
    lock.lock()
    defer { lock.unlock() }
    return pcm
  }

  func append(_ buffer: AVAudioPCMBuffer) {
    lock.lock()
    if converter == nil {
      tapFormatDescription = describe(buffer.format)
      let built = AVAudioConverter(from: buffer.format, to: outputFormat)
      // Voice processing hands back a 9-channel deinterleaved bus whose
      // channels all carry the same processed signal. An explicit map takes
      // channel 0 rather than relying on a default 9->1 downmix that
      // AVAudioConverter may refuse outright.
      if let built, buffer.format.channelCount != outputFormat.channelCount {
        built.channelMap = [0]
        channelMapNote = "channelMap [0] of \(buffer.format.channelCount) channels"
      }
      converter = built
      if built == nil {
        conversionFailure = "no AVAudioConverter from \(buffer.format) to \(outputFormat)"
      }
    }
    guard let converter else {
      lock.unlock()
      return
    }
    lock.unlock()

    let ratio = outputFormat.sampleRate / buffer.format.sampleRate
    let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
    guard let converted = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
      return
    }
    var supplied = false
    var error: NSError?
    _ = converter.convert(to: converted, error: &error) { _, status in
      if supplied {
        status.pointee = .noDataNow
        return nil
      }
      supplied = true
      status.pointee = .haveData
      return buffer
    }
    if let error {
      lock.lock()
      conversionFailure = "\(error)"
      lock.unlock()
      return
    }
    let count = Int(converted.frameLength)
    guard count > 0, let channel = converted.int16ChannelData?.pointee else { return }
    let bytes = UnsafeRawBufferPointer(start: channel, count: count * MemoryLayout<Int16>.size)
    lock.lock()
    pcm.append(contentsOf: bytes)
    written += count
    lock.unlock()
  }
}

// MARK: - Helpers

func describe(_ format: AVAudioFormat) -> String {
  "\(Int(format.sampleRate)) Hz, \(format.channelCount) ch, \(commonFormatName(format.commonFormat))"
}

func commonFormatName(_ value: AVAudioCommonFormat) -> String {
  switch value {
  case .pcmFormatFloat32: return "pcmFormatFloat32"
  case .pcmFormatFloat64: return "pcmFormatFloat64"
  case .pcmFormatInt16: return "pcmFormatInt16"
  case .pcmFormatInt32: return "pcmFormatInt32"
  case .otherFormat: return "otherFormat"
  @unknown default: return "unknown"
  }
}

func formatFacts(_ format: AVAudioFormat) -> [String: Any] {
  [
    "sample_rate_hz": format.sampleRate,
    "channels": Int(format.channelCount),
    "common_format": commonFormatName(format.commonFormat),
  ]
}

func wavData(pcm16LE: Data, sampleRate: Int) -> Data {
  var data = Data(capacity: 44 + pcm16LE.count)
  func ascii(_ value: String) { data.append(Data(value.utf8)) }
  func u32(_ value: UInt32) {
    var little = value.littleEndian
    withUnsafeBytes(of: &little) { data.append(contentsOf: $0) }
  }
  func u16(_ value: UInt16) {
    var little = value.littleEndian
    withUnsafeBytes(of: &little) { data.append(contentsOf: $0) }
  }
  ascii("RIFF")
  u32(UInt32(36 + pcm16LE.count))
  ascii("WAVE")
  ascii("fmt ")
  u32(16)
  u16(1)
  u16(1)
  u32(UInt32(sampleRate))
  u32(UInt32(sampleRate * 2))
  u16(2)
  u16(16)
  ascii("data")
  u32(UInt32(pcm16LE.count))
  data.append(pcm16LE)
  return data
}

func requestMicrophoneAccess() -> AVAuthorizationStatus {
  let status = AVCaptureDevice.authorizationStatus(for: .audio)
  guard status == .notDetermined else { return status }
  let semaphore = DispatchSemaphore(value: 0)
  AVCaptureDevice.requestAccess(for: .audio) { _ in semaphore.signal() }
  _ = semaphore.wait(timeout: .now() + 60)
  return AVCaptureDevice.authorizationStatus(for: .audio)
}

// MARK: - Run

let status = requestMicrophoneAccess()
guard status == .authorized else {
  fail(
    """
    microphone-permission: DENIED — AVCaptureDevice.authorizationStatus(for: .audio) == \
    \(status.rawValue) (0 notDetermined, 1 restricted, 2 denied, 3 authorized). \
    A `swift` script has no bundle identifier; grant the terminal application \
    microphone access in System Settings > Privacy & Security > Microphone.
    """,
    code: 3)
}

let farEndURL = URL(fileURLWithPath: (farEndPath as NSString).expandingTildeInPath)
let outputURL = URL(fileURLWithPath: (outPath as NSString).expandingTildeInPath)

guard let farEndFile = try? AVAudioFile(forReading: farEndURL) else {
  fail("cannot read far-end file: \(farEndURL.path)", code: 4)
}
let farEndSeconds = Double(farEndFile.length) / farEndFile.processingFormat.sampleRate
guard farEndSeconds <= maxPlaybackSeconds else {
  fail("far-end is \(farEndSeconds)s; the household-noise bound is \(maxPlaybackSeconds)s", code: 5)
}

let engine = AVAudioEngine()
let input = engine.inputNode
let formatBefore = input.inputFormat(forBus: 0)
// Instantiate the main mixer before toggling voice processing. Enabling it
// first and letting the mixer be created by the later `connect` makes
// `engine.start()` fail with -10875 (kAUInitialize on the output node).
_ = engine.mainMixerNode
var voiceProcessingErrors: [String] = []
if aecEnabled {
  // Input only: enabling voice processing on the input node also enables it
  // on the output node, which is what feeds the canceller its reference.
  do {
    try input.setVoiceProcessingEnabled(true)
  } catch {
    voiceProcessingErrors.append("inputNode: \(error)")
  }
}
let formatAfter = input.inputFormat(forBus: 0)
guard formatAfter.channelCount > 0 else {
  fail("input node reports 0 channels — no usable capture device", code: 6)
}

guard
  let captureFormat = AVAudioFormat(
    commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)
else {
  fail("cannot build a 16 kHz mono int16 format", code: 7)
}
let sink = CaptureSink(outputFormat: captureFormat)

let player = AVAudioPlayerNode()
engine.attach(player)
engine.connect(player, to: engine.mainMixerNode, format: farEndFile.processingFormat)
input.installTap(onBus: 0, bufferSize: 4096, format: nil) { buffer, _ in sink.append(buffer) }

engine.prepare()
do {
  try engine.start()
} catch {
  fail("engine start failed: \(error)", code: 8)
}

Thread.sleep(forTimeInterval: leadSeconds)
let silenceEnd = sink.frames

let playbackDone = DispatchSemaphore(value: 0)
player.scheduleFile(farEndFile, at: nil, completionCallbackType: .dataPlayedBack) { _ in
  playbackDone.signal()
}
let playbackStartedAt = Date()
player.play()
let playStart = sink.frames
_ = playbackDone.wait(timeout: .now() + farEndSeconds + 5.0)
let playbackWall = Date().timeIntervalSince(playbackStartedAt)
let playEnd = sink.frames

Thread.sleep(forTimeInterval: tailSeconds)
let tailEnd = sink.frames

player.stop()
input.removeTap(onBus: 0)
engine.stop()

let facts: [String: Any] = [
  "aec_requested": aecEnabled,
  "voice_processing_errors": voiceProcessingErrors,
  "input_format_before_voice_processing": formatFacts(formatBefore),
  "input_format_with_voice_processing": formatFacts(formatAfter),
  "input_format_changed_by_voice_processing": describe(formatBefore) != describe(formatAfter),
  "tap_buffer_format": sink.tapFormatDescription ?? "none",
  "tap_channel_map": sink.channelMapNote,
  "int16_16k_mono_obtainable": sink.conversionFailure == nil && sink.frames > 0,
  "conversion_failure": sink.conversionFailure ?? "",
  "voice_processing_agc_enabled": input.isVoiceProcessingAGCEnabled,
  "voice_processing_bypassed": input.isVoiceProcessingBypassed,
  "input_device_id": Int(input.auAudioUnit.deviceID),
  "output_device_id": Int(engine.outputNode.auAudioUnit.deviceID),
  "default_audio_capture_device": AVCaptureDevice.default(for: .audio)?.localizedName ?? "none",
  "default_audio_capture_uid": AVCaptureDevice.default(for: .audio)?.uniqueID ?? "none",
  "far_end_wav": farEndURL.path,
  "far_end_seconds": farEndSeconds,
  "playback_wall_seconds": playbackWall,
]

let sidecar: [String: Any] = [
  "capture_wav": outputURL.path,
  "aec_enabled": aecEnabled,
  "sample_rate_hz": 16_000,
  "channels": 1,
  "windows": [
    "silence_start": 0,
    "silence_end": silenceEnd,
    "play_start": playStart,
    "play_end": playEnd,
    "tail_start": playEnd,
    "tail_end": tailEnd,
  ],
  "format_facts": facts,
]

try? FileManager.default.createDirectory(
  at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
do {
  try wavData(pcm16LE: sink.data, sampleRate: 16_000).write(to: outputURL)
  let json = try JSONSerialization.data(
    withJSONObject: sidecar, options: [.prettyPrinted, .sortedKeys])
  try json.write(to: URL(fileURLWithPath: outputURL.path + ".json"))
  FileHandle.standardOutput.write(json)
  FileHandle.standardOutput.write(Data("\n".utf8))
} catch {
  fail("cannot write capture: \(error)", code: 9)
}

let summary = """
capture: \(outputURL.path)
  aec_requested=\(aecEnabled) voice_processing_errors=\(voiceProcessingErrors)
  input format before=\(describe(formatBefore)) after=\(describe(formatAfter)) \
changed=\(describe(formatBefore) != describe(formatAfter))
  tap buffer format=\(sink.tapFormatDescription ?? "none") map=\(sink.channelMapNote) \
16k-mono-int16 obtainable=\(sink.conversionFailure == nil && sink.frames > 0)
  agc=\(input.isVoiceProcessingAGCEnabled) bypassed=\(input.isVoiceProcessingBypassed)
  devices: input_id=\(input.auAudioUnit.deviceID) output_id=\(engine.outputNode.auAudioUnit.deviceID) \
capture_device=\(AVCaptureDevice.default(for: .audio)?.localizedName ?? "none")
  windows (16 kHz samples): silence [0, \(silenceEnd)) play [\(playStart), \(playEnd)) \
tail [\(playEnd), \(tailEnd))
  far-end=\(farEndURL.lastPathComponent) \(String(format: "%.2f", farEndSeconds))s \
playback wall \(String(format: "%.2f", playbackWall))s

"""
FileHandle.standardOutput.write(Data(summary.utf8))
