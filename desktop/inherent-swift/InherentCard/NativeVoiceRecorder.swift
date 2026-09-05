import AVFoundation
import Foundation

enum NativeVoiceRecorderError: Error, Equatable {
  case microphoneDenied
  case inputUnavailable
  case alreadyRecording
}

protocol NativeVoiceRecording: AnyObject {
  func start() async throws
  func stop() async -> Data?
}

/// Native replacement for the renderer-side AudioWorklet recorder. It records
/// mono PCM from AVAudioEngine and packages the result as a WAV file for the
/// existing `/inherent/asr-submit` endpoint.
final class NativeVoiceRecorder: NativeVoiceRecording {
  private let lock = NSLock()
  private var engine: AVAudioEngine?
  private var chunks: [Data] = []
  private var sampleRate: Double = 16_000
  private var isRecording = false

  func start() async throws {
    guard await Self.requestMicrophoneAccess() else {
      throw NativeVoiceRecorderError.microphoneDenied
    }

    let alreadyRecording = withLock { () -> Bool in
      if isRecording { return true }
      chunks.removeAll()
      isRecording = true
      return false
    }
    if alreadyRecording {
      throw NativeVoiceRecorderError.alreadyRecording
    }

    let engine = AVAudioEngine()
    let input = engine.inputNode
    let format = input.inputFormat(forBus: 0)
    guard format.channelCount > 0 else {
      withLock { isRecording = false }
      throw NativeVoiceRecorderError.inputUnavailable
    }

    sampleRate = max(1, format.sampleRate)
    input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
      self?.append(buffer: buffer, format: format)
    }

    do {
      engine.prepare()
      try engine.start()
      withLock { self.engine = engine }
    } catch {
      input.removeTap(onBus: 0)
      withLock {
        isRecording = false
        self.engine = nil
        self.chunks.removeAll()
      }
      throw error
    }
  }

  func stop() async -> Data? {
    let snapshot = withLock { () -> (AVAudioEngine?, Double, Data)? in
      guard isRecording || engine != nil else { return nil }
      let currentEngine = engine
      engine = nil
      isRecording = false
      let rate = sampleRate
      let pcm = chunks.reduce(into: Data()) { partial, chunk in partial.append(chunk) }
      chunks.removeAll()
      return (currentEngine, rate, pcm)
    }
    guard let (currentEngine, rate, pcm) = snapshot else { return nil }

    currentEngine?.inputNode.removeTap(onBus: 0)
    currentEngine?.stop()
    return Self.wavData(pcm16LE: pcm, sampleRate: Int(rate.rounded()))
  }

  private func append(buffer: AVAudioPCMBuffer, format: AVAudioFormat) {
    guard let channel = buffer.floatChannelData?.pointee else { return }
    let frames = Int(buffer.frameLength)
    guard frames > 0 else { return }

    var data = Data(capacity: frames * 2)
    for i in 0..<frames {
      let clamped = min(1, max(-1, channel[i]))
      let value = clamped < 0
        ? Int16(clamped * 32768)
        : Int16(clamped * Float(Int16.max))
      var littleEndian = value.littleEndian
      withUnsafeBytes(of: &littleEndian) { data.append(contentsOf: $0) }
    }

    lock.lock()
    if isRecording { chunks.append(data) }
    lock.unlock()
  }

  private func withLock<T>(_ body: () throws -> T) rethrows -> T {
    lock.lock()
    defer { lock.unlock() }
    return try body()
  }

  private static func requestMicrophoneAccess() async -> Bool {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized:
      return true
    case .notDetermined:
      return await withCheckedContinuation { continuation in
        AVCaptureDevice.requestAccess(for: .audio) { granted in
          continuation.resume(returning: granted)
        }
      }
    case .denied, .restricted:
      return false
    @unknown default:
      return false
    }
  }

  private static func wavData(pcm16LE: Data, sampleRate: Int) -> Data {
    var data = Data(capacity: 44 + pcm16LE.count)
    data.appendASCII("RIFF")
    data.appendUInt32LE(UInt32(36 + pcm16LE.count))
    data.appendASCII("WAVE")
    data.appendASCII("fmt ")
    data.appendUInt32LE(16)
    data.appendUInt16LE(1)
    data.appendUInt16LE(1)
    data.appendUInt32LE(UInt32(max(1, sampleRate)))
    data.appendUInt32LE(UInt32(max(1, sampleRate) * 2))
    data.appendUInt16LE(2)
    data.appendUInt16LE(16)
    data.appendASCII("data")
    data.appendUInt32LE(UInt32(pcm16LE.count))
    data.append(pcm16LE)
    return data
  }
}

private extension Data {
  mutating func appendASCII(_ value: String) {
    append(Data(value.utf8))
  }

  mutating func appendUInt16LE(_ value: UInt16) {
    var v = value.littleEndian
    Swift.withUnsafeBytes(of: &v) { append(contentsOf: $0) }
  }

  mutating func appendUInt32LE(_ value: UInt32) {
    var v = value.littleEndian
    Swift.withUnsafeBytes(of: &v) { append(contentsOf: $0) }
  }
}
