import AVFoundation
import Observation
import SwiftUI

/// Reads the QR on the Mac's screen. The camera runs only between `start()` and `stop()`; the settings page calls them as it becomes the current page and as it leaves.
@Observable
final class QRScanner: NSObject, AVCaptureMetadataOutputObjectsDelegate {
    enum Availability { case checking, ready, noCamera, denied }

    private(set) var availability = Availability.checking
    @ObservationIgnored var onCode: ((String) -> Void)?
    @ObservationIgnored let session = AVCaptureSession()
    @ObservationIgnored private let queue = DispatchQueue(label: "com.allen.jarvis.qr")
    @ObservationIgnored private var configured = false
    @ObservationIgnored private var wanted = false

    func start() {
        wanted = true
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-fakeCameraDenied") { availability = .denied; return }
        #endif
        guard AVCaptureDevice.default(for: .video) != nil else { availability = .noCamera; return }      // the simulator: no camera, no question
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized: run()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { ok in
                DispatchQueue.main.async { if ok { if self.wanted { self.run() } } else { self.availability = .denied } }
            }
        default: availability = .denied
        }
    }

    func stop() {
        wanted = false
        queue.async { if self.session.isRunning { self.session.stopRunning() } }
    }

    private func run() {
        queue.async {
            if !self.configured {
                guard let cam = AVCaptureDevice.default(for: .video), let input = try? AVCaptureDeviceInput(device: cam), self.session.canAddInput(input) else {
                    DispatchQueue.main.async { self.availability = .noCamera }
                    return
                }
                self.session.addInput(input)
                let out = AVCaptureMetadataOutput()
                guard self.session.canAddOutput(out) else { DispatchQueue.main.async { self.availability = .noCamera }; return }
                self.session.addOutput(out)
                out.setMetadataObjectsDelegate(self, queue: .main)
                out.metadataObjectTypes = [.qr]
                self.configured = true
            }
            DispatchQueue.main.async { self.availability = .ready }
            if self.wanted, !self.session.isRunning { self.session.startRunning() }
        }
    }

    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput objects: [AVMetadataObject], from connection: AVCaptureConnection) {
        guard wanted, let text = objects.compactMap({ ($0 as? AVMetadataMachineReadableCodeObject)?.stringValue }).first else { return }
        onCode?(text)
    }
}

/// The camera's picture, filling its frame.
struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    final class View: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var preview: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }
    func makeUIView(context: Context) -> View {
        let v = View()
        v.preview.session = session
        v.preview.videoGravity = .resizeAspectFill
        return v
    }
    func updateUIView(_ v: View, context: Context) {}
}
