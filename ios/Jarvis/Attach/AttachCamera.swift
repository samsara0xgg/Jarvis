import AVFoundation
import SwiftUI
import UIKit
import Observation

// The camera behind the tile and the viewfinder. The session runs only while one of them can be seen; the simulator has no camera,
// so there a calm placeholder stands in and the shutter makes a generated picture, which keeps the whole flow testable.

@Observable
final class AttachCamera {
    enum Status { case idle, running, denied, unavailable }
    private(set) var status: Status = .idle
    private(set) var capturing = false
    private(set) var front = false

    @ObservationIgnored let session = AVCaptureSession()
    @ObservationIgnored private let queue = DispatchQueue(label: "jarvis.attach.camera")
    @ObservationIgnored private var output: AVCapturePhotoOutput?   // made on the queue: building one asks the camera daemon and blocks for ~170 ms
    @ObservationIgnored private var layers: [AVCaptureVideoPreviewLayer] = []   // the live previews, queue only
    @ObservationIgnored private var links: [ObjectIdentifier: AVCaptureConnection] = [:]
    @ObservationIgnored private var configured = false
    @ObservationIgnored private var wanted = false
    @ObservationIgnored private var delegate: PhotoDelegate?

    // MARK: start, stop

    func setActive(_ on: Bool) {
        wanted = on
        #if targetEnvironment(simulator)
        status = .unavailable
        #else
        guard on else {
            queue.async { if self.session.isRunning { self.session.stopRunning() } }
            if status == .running { status = .idle }
            return
        }
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: start()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { ok in
                DispatchQueue.main.async { if ok { if self.wanted { self.start() } } else { self.status = .denied } }
            }
        default: status = .denied
        }
        #endif
    }

    private func start() {
        let front = self.front
        queue.async {
            if !self.configured && !self.configure(front: front) {
                DispatchQueue.main.async { self.status = .unavailable }
                return
            }
            if !self.session.isRunning { self.session.startRunning() }
            DispatchQueue.main.async { self.status = self.wanted ? .running : .idle }
        }
    }

    private static func device(front: Bool) -> AVCaptureDevice? {
        AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: front ? .front : .back)
    }

    private func configure(front: Bool) -> Bool {
        session.beginConfiguration(); defer { session.commitConfiguration() }
        session.sessionPreset = .photo
        guard let d = Self.device(front: front), let input = try? AVCaptureDeviceInput(device: d), session.canAddInput(input) else { return false }
        session.addInput(input)
        let output = AVCapturePhotoOutput()
        guard session.canAddOutput(output) else { return false }
        session.addOutput(output)
        self.output = output
        configured = true
        return true
    }

    func flip() {
        front.toggle()
        let front = self.front
        queue.async {
            guard self.configured else { return }
            self.session.beginConfiguration()
            self.session.inputs.forEach { self.session.removeInput($0) }
            if let d = Self.device(front: front), let i = try? AVCaptureDeviceInput(device: d), self.session.canAddInput(i) { self.session.addInput(i) }
            self.session.commitConfiguration()
            self.links.removeAll()   // they went with the old input
            self.join()
        }
    }

    // MARK: the previews

    /// Each preview joins the session through a connection made on the queue. Assigning `layer.session` on the main thread waits on the
    /// camera daemon: ~150 ms for the first preview and about 9 s for a second one, which was the tap on 相机 hanging.
    func connect(_ layer: AVCaptureVideoPreviewLayer) { queue.async { self.layers.append(layer); self.join() } }

    func disconnect(_ layer: AVCaptureVideoPreviewLayer) {
        queue.async {
            self.layers.removeAll { $0 === layer }
            if let c = self.links.removeValue(forKey: ObjectIdentifier(layer)) { self.session.removeConnection(c) }
        }
    }

    private func join() {
        guard configured, let port = (session.inputs.first as? AVCaptureDeviceInput)?.ports(for: .video, sourceDeviceType: nil, sourceDevicePosition: .unspecified).first else { return }
        session.beginConfiguration(); defer { session.commitConfiguration() }
        for l in layers where links[ObjectIdentifier(l)] == nil {
            let c = AVCaptureConnection(inputPort: port, videoPreviewLayer: l)
            guard session.canAddConnection(c) else { continue }
            session.addConnection(c)
            if c.isVideoRotationAngleSupported(90) { c.videoRotationAngle = 90 }   // the app is portrait only
            links[ObjectIdentifier(l)] = c
        }
    }

    // MARK: the shutter

    func capture(_ done: @escaping (AttachShot?) -> Void) {
        guard !capturing else { return }
        capturing = true
        let finish: (AttachShot?) -> Void = { [weak self] s in
            DispatchQueue.main.async { self?.capturing = false; self?.delegate = nil; done(s) }
        }
        #if targetEnvironment(simulator)
        DispatchQueue.global(qos: .userInitiated).async { finish(Self.make(Self.placeholder())) }
        #else
        guard status == .running else { capturing = false; return done(nil) }
        let d = PhotoDelegate { data in finish(data.flatMap(Self.make)) }
        delegate = d
        queue.async {
            guard let output = self.output else { return finish(nil) }
            if let c = output.connection(with: .video), c.isVideoRotationAngleSupported(90) { c.videoRotationAngle = 90 }   // the app is portrait only
            let s = output.availablePhotoCodecTypes.contains(.jpeg) ? AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg]) : AVCapturePhotoSettings()
            output.capturePhoto(with: s, delegate: d)
        }
        #endif
    }

    /// the photo's data becomes the temp JPEG, the frozen frame and the strip's small copy
    private static func make(_ data: Data) -> AttachShot? {
        let url = AttachTemp.newFile("照片.jpg")
        guard let src = CGImageSourceCreateWithData(data as CFData, nil), (try? data.write(to: url)) != nil,
              let preview = AttachImage.downsample(src, maxPixel: 1300), let thumb = AttachImage.downsample(src, maxPixel: 240) else { return nil }
        return AttachShot(preview: preview, thumb: thumb, url: url)
    }

    /// what the simulator's shutter takes: a soft gradient with the time on it
    private static func placeholder() -> Data {
        let size = CGSize(width: 900, height: 1200)
        let f = UIGraphicsImageRendererFormat(); f.scale = 1
        let hue = CGFloat(Date().timeIntervalSince1970.truncatingRemainder(dividingBy: 60) / 60)
        return UIGraphicsImageRenderer(size: size, format: f).jpegData(withCompressionQuality: 0.85) { c in
            let colors = [UIColor(hue: hue, saturation: 0.55, brightness: 0.55, alpha: 1).cgColor,
                          UIColor(hue: (hue + 0.12).truncatingRemainder(dividingBy: 1), saturation: 0.6, brightness: 0.2, alpha: 1).cgColor]
            let g = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors as CFArray, locations: [0, 1])!
            c.cgContext.drawLinearGradient(g, start: .zero, end: CGPoint(x: size.width, y: size.height), options: [])
            let t = "模拟拍摄 " + Date().formatted(date: .omitted, time: .standard)
            (t as NSString).draw(at: CGPoint(x: 60, y: 90), withAttributes: [.font: UIFont.systemFont(ofSize: 56, weight: .semibold), .foregroundColor: UIColor.white.withAlphaComponent(0.85)])
        }
    }

    private final class PhotoDelegate: NSObject, AVCapturePhotoCaptureDelegate {
        let done: (Data?) -> Void
        init(_ done: @escaping (Data?) -> Void) { self.done = done }
        func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
            done(error == nil ? photo.fileDataRepresentation() : nil)
        }
    }
}

/// a live preview of the camera's session, filling its frame
struct AttachCameraPreview: UIViewRepresentable {
    let camera: AttachCamera
    func makeUIView(context: Context) -> PreviewView {
        let v = PreviewView(session: camera.session); v.camera = camera; camera.connect(v.preview); return v
    }
    func updateUIView(_ v: PreviewView, context: Context) {}
    static func dismantleUIView(_ v: PreviewView, coordinator: ()) { v.camera?.disconnect(v.preview) }

    final class PreviewView: UIView {
        let preview: AVCaptureVideoPreviewLayer
        weak var camera: AttachCamera?
        init(session: AVCaptureSession) {
            preview = AVCaptureVideoPreviewLayer(sessionWithNoConnection: session)
            preview.videoGravity = .resizeAspectFill
            super.init(frame: .zero)
            layer.addSublayer(preview)
        }
        required init?(coder: NSCoder) { fatalError() }
        override func layoutSubviews() {
            super.layoutSubviews()
            CATransaction.begin(); CATransaction.setDisableActions(true); preview.frame = bounds; CATransaction.commit()
        }
    }
}
