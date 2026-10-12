import UIKit

// A picture he sent, full screen over everything: black, pinch and double-tap to zoom, pan when zoomed, swipe left and right between the pictures of the message,
// swipe down to close with the picture following the finger. A UIKit controller presented from the top of the app (so it covers the panel, the composer and any sheet).

/// One picture of the message being looked at: its attachment id (nil: only the small copy in hand), whether the brain has swept it, and the small copy to show until the real one is read.
struct ViewerPicture: Equatable {
    var id: String?
    var gone = false
    var small: UIImage?
}

/// One picture in its own scroll view: pinch to zoom, double-tap to zoom in on the point and out again, drag to look around when zoomed.
final class ZoomPage: UIScrollView, UIScrollViewDelegate {
    let imageView = UIImageView()
    let note = UILabel()
    private var fitted: (CGSize, CGSize)?        // the image's size and the bounds it was fitted to
    var reduceMotion = false
    var isZoomed: Bool { zoomScale > minimumZoomScale + 0.01 }

    override init(frame: CGRect) {
        super.init(frame: frame)
        delegate = self
        minimumZoomScale = 1; maximumZoomScale = 4
        showsHorizontalScrollIndicator = false; showsVerticalScrollIndicator = false
        contentInsetAdjustmentBehavior = .never
        imageView.contentMode = .scaleToFill
        addSubview(imageView)
        note.textColor = UIColor.white.withAlphaComponent(0.7); note.font = .systemFont(ofSize: 15); note.textAlignment = .center
        addSubview(note)
        let two = UITapGestureRecognizer(target: self, action: #selector(doubleTap(_:)))
        two.numberOfTapsRequired = 2
        addGestureRecognizer(two)
        isAccessibilityElement = true; accessibilityLabel = "图片"; accessibilityTraits = .image
    }
    required init?(coder: NSCoder) { fatalError() }

    func show(_ image: UIImage?, note text: String? = nil) {
        imageView.image = image
        note.text = text; note.isHidden = text == nil
        fitted = nil
        setNeedsLayout()
    }

    func viewForZooming(in scrollView: UIScrollView) -> UIView? { imageView }
    func scrollViewDidZoom(_ scrollView: UIScrollView) { center() }

    override func layoutSubviews() {
        super.layoutSubviews()
        note.frame = CGRect(x: 24, y: bounds.midY - 20, width: max(0, bounds.width - 48), height: 40)
        guard let img = imageView.image, img.size.width > 0, bounds.width > 0 else { return }
        if fitted?.0 != img.size || fitted?.1 != bounds.size {
            fitted = (img.size, bounds.size)
            zoomScale = 1
            let k = min(bounds.width / img.size.width, bounds.height / img.size.height)
            imageView.frame = CGRect(origin: .zero, size: CGSize(width: img.size.width * k, height: img.size.height * k))
            contentSize = imageView.frame.size
        }
        center()
    }

    /// a picture smaller than the screen sits in the middle of it, zoomed or not
    private func center() {
        var f = imageView.frame
        f.origin.x = max(0, (bounds.width - f.width) / 2)
        f.origin.y = max(0, (bounds.height - f.height) / 2)
        if imageView.frame != f { imageView.frame = f }
    }

    @objc private func doubleTap(_ g: UITapGestureRecognizer) {
        if isZoomed { setZoomScale(minimumZoomScale, animated: !reduceMotion); return }
        guard imageView.image != nil else { return }
        let p = g.location(in: imageView), s = 2.5, w = bounds.width / s, h = bounds.height / s
        zoom(to: CGRect(x: p.x - w / 2, y: p.y - h / 2, width: w, height: h), animated: !reduceMotion)
    }
}

final class PictureViewer: UIViewController, UIGestureRecognizerDelegate, UIScrollViewDelegate {
    private let pictures: [ViewerPicture]
    private var index: Int
    private let pager = UIScrollView()
    private var pages: [ZoomPage] = []
    private var loaded: Set<Int> = []
    private let close = UIButton(type: .system)
    private let counter = UILabel()
    private let reduceMotion = UIAccessibility.isReduceMotionEnabled
    private var dragging = false

    init(_ pictures: [ViewerPicture], index: Int) {
        self.pictures = pictures; self.index = min(max(0, index), max(0, pictures.count - 1))
        super.init(nibName: nil, bundle: nil)
        modalPresentationStyle = .overFullScreen
        modalTransitionStyle = .crossDissolve
        modalPresentationCapturesStatusBarAppearance = true
    }
    required init?(coder: NSCoder) { fatalError() }

    override var prefersStatusBarHidden: Bool { true }
    override var preferredStatusBarUpdateAnimation: UIStatusBarAnimation { .fade }

    /// Open the viewer on the top controller of the app. Nothing happens while one is already open.
    static func present(_ pictures: [ViewerPicture], index: Int) {
        guard !pictures.isEmpty,
              var top = UIApplication.shared.connectedScenes.compactMap({ ($0 as? UIWindowScene)?.keyWindow }).first?.rootViewController else { return }
        while let next = top.presentedViewController { if next is PictureViewer { return }; top = next }
        top.present(PictureViewer(pictures, index: index), animated: !UIAccessibility.isReduceMotionEnabled)
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        pager.isPagingEnabled = true; pager.delegate = self
        pager.showsHorizontalScrollIndicator = false; pager.contentInsetAdjustmentBehavior = .never
        pager.backgroundColor = .clear
        view.addSubview(pager)
        for _ in pictures {
            let p = ZoomPage(); p.reduceMotion = reduceMotion; p.backgroundColor = .clear
            pager.addSubview(p); pages.append(p)
        }
        close.setImage(UIImage(systemName: "xmark", withConfiguration: UIImage.SymbolConfiguration(pointSize: 15, weight: .semibold)), for: .normal)
        close.tintColor = .white; close.backgroundColor = UIColor.white.withAlphaComponent(0.16); close.layer.cornerRadius = 18
        close.accessibilityLabel = "关闭"
        close.addTarget(self, action: #selector(closeTapped), for: .touchUpInside)
        view.addSubview(close)
        counter.textColor = UIColor.white.withAlphaComponent(0.75); counter.font = .monospacedDigitSystemFont(ofSize: 14, weight: .medium); counter.textAlignment = .center
        view.addSubview(counter)
        let pan = UIPanGestureRecognizer(target: self, action: #selector(drag(_:)))
        pan.delegate = self
        view.addGestureRecognizer(pan)
        updateCounter()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        if !dragging { pager.frame = view.bounds }
        let w = view.bounds.width, h = view.bounds.height
        pager.contentSize = CGSize(width: w * CGFloat(pictures.count), height: h)
        for (i, p) in pages.enumerated() { p.frame = CGRect(x: w * CGFloat(i), y: 0, width: w, height: h) }
        if !dragging && pager.contentOffset.x != w * CGFloat(index) { pager.contentOffset = CGPoint(x: w * CGFloat(index), y: 0) }
        let top = view.safeAreaInsets.top + 10
        close.frame = CGRect(x: 16, y: top, width: 36, height: 36)
        counter.frame = CGRect(x: w / 2 - 40, y: top, width: 80, height: 36)
        loadAround()
    }

    private func updateCounter() { counter.text = pictures.count > 1 ? "\(index + 1) / \(pictures.count)" : nil }

    @objc private func closeTapped() { dismiss(animated: !reduceMotion) }

    // MARK: pictures

    /// the one in view and its neighbours are decoded; the ones further away are let go
    private func loadAround() {
        for i in pictures.indices {
            if abs(i - index) <= 1 {
                if !loaded.contains(i) { loaded.insert(i); load(i) }
            } else if loaded.contains(i) {
                loaded.remove(i); pages[i].show(nil)
            }
        }
    }

    private func load(_ i: Int) {
        let pic = pictures[i], page = pages[i]
        page.show(pic.small)
        guard let id = pic.id else { return }
        Task { @MainActor in
            let got = await PictureStore.shared.image(id: id, gone: pic.gone, maxPixel: 4096)
            guard loaded.contains(i) else { return }
            switch got {
            case .image(let img): page.show(img)
            case .gone: page.show(pic.small, note: pic.small == nil ? "图片已过期" : nil)
            case .failed: page.show(pic.small, note: pic.small == nil ? "图片没取到" : nil)
            }
        }
    }

    func scrollViewDidScroll(_ scrollView: UIScrollView) {
        guard scrollView === pager, view.bounds.width > 0 else { return }
        let i = min(max(0, Int((pager.contentOffset.x / view.bounds.width).rounded())), pictures.count - 1)
        if i != index { index = i; updateCounter(); loadAround() }
    }

    func scrollViewDidEndDecelerating(_ scrollView: UIScrollView) {
        guard scrollView === pager else { return }
        for (i, p) in pages.enumerated() where i != index && p.isZoomed { p.setZoomScale(p.minimumZoomScale, animated: false) }       // a zoomed one left behind starts fitted
    }

    // MARK: swipe down to close

    func gestureRecognizerShouldBegin(_ g: UIGestureRecognizer) -> Bool {
        guard let pan = g as? UIPanGestureRecognizer else { return true }
        let v = pan.velocity(in: view)
        return !pages[index].isZoomed && v.y > 0 && abs(v.y) > abs(v.x)
    }

    @objc private func drag(_ g: UIPanGestureRecognizer) {
        let t = g.translation(in: view), fade = 1 - min(1, abs(t.y) / 360) * 0.75
        switch g.state {
        case .began: dragging = true
        case .changed:
            // the picture follows the finger; with Reduce Motion it stays and only the black behind it thins
            if !reduceMotion { pager.transform = CGAffineTransform(translationX: t.x, y: t.y).scaledBy(x: max(0.7, fade), y: max(0.7, fade)) }
            view.backgroundColor = UIColor.black.withAlphaComponent(fade)
            close.alpha = fade; counter.alpha = fade
        case .ended, .cancelled:
            dragging = false
            if g.state == .ended && (t.y > 120 || g.velocity(in: view).y > 900) {
                let end = { self.view.backgroundColor = .clear; self.close.alpha = 0; self.counter.alpha = 0; if !self.reduceMotion { self.pager.transform = CGAffineTransform(translationX: t.x, y: self.view.bounds.height * 0.6).scaledBy(x: 0.7, y: 0.7) }; self.pager.alpha = 0 }
                UIView.animate(withDuration: reduceMotion ? 0.15 : 0.22, delay: 0, options: .curveEaseIn, animations: end) { _ in self.dismiss(animated: false) }
            } else {
                let back = { self.pager.transform = .identity; self.view.backgroundColor = .black; self.close.alpha = 1; self.counter.alpha = 1 }
                if reduceMotion { UIView.animate(withDuration: 0.15, animations: back) }
                else { UIView.animate(withDuration: 0.35, delay: 0, usingSpringWithDamping: 0.85, initialSpringVelocity: 0, animations: back) }
            }
        default: break
        }
    }
}
