import AppKit
import QuartzCore

/// Generation counter that supersedes in-flight fade ticks. Each `fadeOut`
/// captures `next()`; the completion handler checks `isCurrent(token)` and
/// bails if the value has advanced. Any of the instant-state methods on
/// FadeController advance the counter, which invalidates outstanding tokens.
final class FadeGeneration {
  private var counter: Int = 0
  private let lock = NSLock()

  func next() -> Int {
    lock.lock(); defer { lock.unlock() }
    counter += 1
    return counter
  }

  func isCurrent(_ token: Int) -> Bool {
    lock.lock(); defer { lock.unlock() }
    return token == counter
  }

  func cancel() {
    lock.lock(); defer { lock.unlock() }
    counter += 1
  }
}

/// Sole owner of `panel.alphaValue`. All visibility transitions — instant or
/// animated — go through this controller so they share one cancellation
/// model. See CardPanel.swift's "State ownership contract" for the full
/// invariant: alpha is owned exclusively by FadeController; nowhere else
/// should panel.alphaValue be assigned.
final class FadeController {
  private let panel: CardPanel
  private let generation = FadeGeneration()

  init(panel: CardPanel) {
    self.panel = panel
  }

  /// Show panel instantly. Cancels any in-flight fade so a delayed fadeOut
  /// completion cannot overwrite this. alpha=1 + orderFront if not visible.
  func showInstant() {
    invalidateAndSetAlpha(1)
    if !panel.isVisible { panel.orderFrontRegardless() }
  }

  /// Hide panel instantly. Cancels any in-flight fade. alpha=0 +
  /// ignoresMouseEvents=true (the panel is hit-testable until alpha=0 alone,
  /// so click-through must be claimed at the same time as visibility loss).
  func hideInstant() {
    invalidateAndSetAlpha(0)
    panel.ignoresMouseEvents = true
  }

  /// Linearly fades panel.alpha to 0 over `durationMs`. The completion handler
  /// runs only if no instant-state call (showInstant / hideInstant / restore)
  /// has fired in the interim.
  func fadeOut(durationMs: Int, onComplete: @escaping () -> Void) {
    if panel.alphaValue <= 0.01 { return }
    let token = generation.next()
    let duration = max(0.06, min(2.0, Double(durationMs) / 1000.0))

    NSAnimationContext.runAnimationGroup({ ctx in
      ctx.duration = duration
      ctx.timingFunction = CAMediaTimingFunction(name: .linear)
      panel.animator().alphaValue = 0
    }, completionHandler: { [weak self] in
      guard let self, self.generation.isCurrent(token) else { return }
      onComplete()
    })
  }

  /// Mouse-back-from-fade recovery: cancel an in-flight fadeOut and snap
  /// alpha back to opaque WITHOUT touching ignoresMouseEvents or order.
  /// Used by the cancelFade IPC handler when the user moves the mouse back
  /// onto a fading card.
  func restore() {
    invalidateAndSetAlpha(1)
  }

  private func invalidateAndSetAlpha(_ value: CGFloat) {
    generation.cancel()
    // Zero-duration animation context interrupts any running CAAnimation
    // and forces the model value through immediately. Without this, the
    // CAAnimation kicked off by `panel.animator().alphaValue = 0` keeps
    // running until its end value even after the completion token is
    // invalidated, so the panel would visually keep fading toward 0.
    NSAnimationContext.runAnimationGroup({ ctx in
      ctx.duration = 0
      panel.animator().alphaValue = value
    })
    // The animator model value can lag behind an interrupted fade on some
    // WebKit/CALayer timing paths. Commit the model value directly as the last
    // step so a stale fade animation cannot leave the panel half-transparent.
    panel.alphaValue = value
  }
}
