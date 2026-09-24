import AppKit
import SwiftUI
import ScreenCaptureKit
import Translation
import AVFoundation
import CaptionCore

@MainActor
final class CaptionModel: NSObject, ObservableObject, SCContentSharingPickerObserver {
    @Published var sourceName = "未选择窗口"
    @Published var preview: CGImage?
    @Published var showingCrop = false
    @Published var region = CGRect(x: 0, y: 0, width: 1, height: 1)
    @Published var isRunning = false
    @Published var isBusy = false
    @Published var isPreparing = false
    @Published var languageReady = false
    @Published var sourceText = ""
    @Published var translatedText = ""
    @Published var translatedSource = ""
    @Published var status = "准备就绪"
    @Published var errorMessage: String?
    @Published var translationFailed = false
    @Published var needsRegionSelection = false
    @Published var pinned = true
    @Published var showEnglish = true
    @Published var fontSize: Double = 23
    @Published var translationConfig: TranslationSession.Configuration?
    @Published var translationMilliseconds: Int?

    private var filter: SCContentFilter?
    private var stream: SCStream?
    private var frames: CaptionFrames?
    private var cropGeometry: [CGSize]?
    private var pendingPreviewGeometry: [CGSize]?
    private var tracker = CaptionTracker()
    private var generation = UUID()
    private var pendingText: String?
    private var pendingSince = Date()
    private var changedAt = Date()
    private var lastSubmitted = ""
    private var replayTask: Task<Void, Never>?
    private var replayURL: URL?
    private var lastTextAt = Date()
    private var staleTask: Task<Void, Never>?

    var hasSource: Bool { filter != nil || replayURL != nil }
    var canEditRegion: Bool { filter != nil }

    override init() {
        super.init()
        SCContentSharingPicker.shared.add(self)
        let arguments = ProcessInfo.processInfo.arguments
        if let index = arguments.firstIndex(of: "--replay"), arguments.count > index + 1 {
            replayURL = URL(fileURLWithPath: arguments[index + 1])
            sourceName = "录屏回放验证"
        }
        Task {
            languageReady = await LanguageAvailability().status(from: .init(identifier: "en"), to: .init(identifier: "zh-Hans")) == .installed
        }
    }

    func chooseWindow() {
        guard !isBusy else { return }
        errorMessage = nil
        translationFailed = false
        NSApp.activate(ignoringOtherApps: true)
        let picker = SCContentSharingPicker.shared
        var configuration = SCContentSharingPickerConfiguration()
        configuration.allowedPickerModes = .singleWindow
        configuration.excludedBundleIDs = [Bundle.main.bundleIdentifier ?? "local.allen.caption-translator"]
        configuration.allowsChangingSelectedContent = true
        picker.defaultConfiguration = configuration
        picker.isActive = true
        picker.present(using: .window)
    }

    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker, didCancelFor stream: SCStream?) {}

    nonisolated func contentSharingPickerStartDidFailWithError(_ error: Error) {
        Task { @MainActor in self.errorMessage = "无法打开窗口选择器：\(error.localizedDescription)" }
    }

    nonisolated func contentSharingPicker(_ picker: SCContentSharingPicker, didUpdateWith filter: SCContentFilter, for stream: SCStream?) {
        Task { @MainActor in await self.select(filter) }
    }

    private func select(_ selection: SCContentFilter) async {
        guard !isBusy else { return }
        isBusy = true
        await stop()
        isBusy = true
        needsRegionSelection = false
        cropGeometry = nil
        filter = selection
        replayURL = nil
        sourceName = selection.includedWindows.first?.title
            ?? selection.includedWindows.first?.owningApplication?.applicationName ?? "所选窗口"
        region = CGRect(x: 0, y: 0, width: 1, height: 1)
        sourceText = ""
        translatedText = ""
        translatedSource = ""
        await loadPreview()
        isBusy = false
    }

    func editRegion() async {
        guard !isBusy else { return }
        isBusy = true
        await stop()
        isBusy = true
        await loadPreview()
        isBusy = false
    }

    private func loadPreview() async {
        guard let filter else { return }
        do {
            let captured = try await capturePreview(for: filter)
            preview = captured.image
            pendingPreviewGeometry = captured.geometry
            showingCrop = true
            status = "框选英文字幕"
        } catch {
            errorMessage = captureFailure(error)
        }
    }

    func confirmRegion(_ selection: CGRect) {
        region = selection
        cropGeometry = pendingPreviewGeometry
        needsRegionSelection = false
        showingCrop = false
    }

    func prepareLanguages() {
        errorMessage = nil
        translationFailed = false
        isPreparing = true
        if translationConfig == nil {
            translationConfig = .init(source: .init(identifier: "en"), target: .init(identifier: "zh-Hans"))
        } else {
            translationConfig?.invalidate()
        }
    }

    func start() async {
        guard hasSource, !isBusy, !isRunning, !needsRegionSelection else { return }
        isBusy = true
        errorMessage = nil
        translationFailed = false
        generation = UUID()
        let run = generation
        tracker = CaptionTracker()
        pendingText = nil
        lastSubmitted = ""
        lastTextAt = Date()
        isRunning = true
        status = "等待英文字幕"
        if translationConfig == nil { prepareLanguages() }
        if let replayURL {
            replayTask = Task { await replay(replayURL, run: run) }
        } else if let filter {
            let receiver = CaptionFrames(region: region, expectedGeometry: cropGeometry) { [weak self] result in
                Task { @MainActor in
                    guard let self, self.generation == run, self.isRunning else { return }
                    switch result {
                    case .success(let text): self.ingest(text)
                    case .failure(let error):
                        self.errorMessage = self.captureFailure(error)
                        await self.stop()
                    }
                }
            }
            let newStream = SCStream(filter: filter, configuration: captureConfiguration(for: filter), delegate: receiver)
            do {
                try newStream.addStreamOutput(receiver, type: .screen, sampleHandlerQueue: DispatchQueue(label: "caption.ocr", qos: .userInitiated))
                frames = receiver
                stream = newStream
                try await newStream.startCapture()
                guard generation == run, isRunning else {
                    try? await newStream.stopCapture()
                    return
                }
            } catch {
                guard generation == run else { return }
                errorMessage = captureFailure(error)
                await stop()
            }
        }
        guard generation == run, isRunning else { return }
        staleTask = Task {
            while !Task.isCancelled && self.isRunning && self.generation == run {
                try? await Task.sleep(for: .seconds(2))
                guard !Task.isCancelled, self.isRunning, self.generation == run else { return }
                if Date().timeIntervalSince(self.lastTextAt) > 8 {
                    self.status = self.sourceText.isEmpty ? "未识别到英文，请检查字幕区域" : "等待字幕更新"
                }
            }
        }
        isBusy = false
    }

    func stop() async {
        isBusy = true
        generation = UUID()
        let stoppedGeneration = generation
        isRunning = false
        pendingText = nil
        replayTask?.cancel()
        replayTask = nil
        staleTask?.cancel()
        staleTask = nil
        let oldStream = stream
        let oldFrames = frames
        stream = nil
        frames = nil
        status = translatedText.isEmpty ? "准备就绪" : "已暂停"
        if let oldStream { try? await oldStream.stopCapture() }
        withExtendedLifetime(oldFrames) {}
        guard generation == stoppedGeneration else { return }
        isBusy = false
    }

    private func ingest(_ text: String) {
        guard let context = tracker.ingest(text) else { return }
        sourceText = context
        lastTextAt = Date()
        if pendingText == nil { pendingSince = Date() }
        pendingText = context
        changedAt = Date()
        status = "正在识别"
    }

    private func captureFailure(_ error: Error) -> String {
        if let issue = error as? CaptureIssue {
            needsRegionSelection = true
            switch issue {
            case .resized: return "窗口大小已改变，翻译已暂停。请点击「重新框选」以更新字幕区域。"
            case .recognition(let detail): return "连续几帧无法识别，翻译已暂停。请确认窗口仍显示字幕，然后重新框选。\n\(detail)"
            }
        }
        if (error as NSError).domain == SCStreamErrorDomain && (error as NSError).code == -3801 {
            return "macOS 尚未允许捕获这个窗口。请重新选择并确认系统授权；如果仍被拒绝，请在「系统设置 → 隐私与安全性 → 屏幕与系统音频录制」中允许「字幕翻译」，然后重新打开应用。"
        }
        return "无法读取所选窗口。请确认窗口仍然打开且未最小化，然后重新选择。\n\(error.localizedDescription)"
    }

    func translate(using session: TranslationSession) async {
        do {
            isPreparing = true
            try await session.prepareTranslation()
            guard !Task.isCancelled else { return }
            languageReady = true
            isPreparing = false
            while !Task.isCancelled {
                try await Task.sleep(for: .milliseconds(150))
                guard isRunning, let text = pendingText, text != lastSubmitted,
                      Date().timeIntervalSince(changedAt) >= 0.55 || Date().timeIntervalSince(pendingSince) >= 1.5 else { continue }
                pendingText = nil
                lastSubmitted = text
                let run = generation
                status = "正在翻译"
                let began = Date()
                let response = try await session.translate(text)
                guard !Task.isCancelled, generation == run, isRunning else { continue }
                translatedText = response.targetText
                translatedSource = text
                translationMilliseconds = Int(Date().timeIntervalSince(began) * 1000)
                status = "实时翻译中"
                writeReplayEvidenceIfRequested()
            }
        } catch is CancellationError {
            isPreparing = false
        } catch {
            guard !Task.isCancelled else { return }
            isPreparing = false
            translationFailed = true
            errorMessage = "本地翻译未就绪。请点击「准备本地翻译」重试；首次使用需要下载语言包。\n\(error.localizedDescription)"
            translationConfig = nil
            await stop()
        }
    }

    private func replay(_ url: URL, run: UUID) async {
        do {
            let asset = AVURLAsset(url: url)
            let duration = try await asset.load(.duration).seconds
            let generator = AVAssetImageGenerator(asset: asset)
            generator.appliesPreferredTrackTransform = true
            for tick in 0..<Int(duration * 2) {
                try Task.checkCancellation()
                let (image, _) = try await generator.image(at: CMTime(seconds: Double(tick) / 2, preferredTimescale: 600))
                let text = try await Task.detached { try CaptionOCR.recognize(image) }.value
                guard generation == run, isRunning else { return }
                ingest(text)
                try await Task.sleep(for: .milliseconds(500))
            }
            // Keep the final frame available while its translation completes.
            status = "回放结束，等待最后一句翻译"
        } catch is CancellationError {} catch {
            errorMessage = "无法读取录屏：\(error.localizedDescription)"
            await stop()
        }
    }

    private func writeReplayEvidenceIfRequested() {
        let args = ProcessInfo.processInfo.arguments
        guard replayURL != nil, let index = args.firstIndex(of: "--evidence"), args.count > index + 1 else { return }
        let report: [String: Any] = ["source": translatedSource, "translation": translatedText,
                                     "translation_ms": translationMilliseconds ?? 0, "backend": "Apple Translation (on device)"]
        if let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: URL(fileURLWithPath: args[index + 1]), options: .atomic)
        }
    }
}
