import AppKit
import Carbon.HIToolbox

final class HotkeyManager {
  private var hotKeyRef: EventHotKeyRef?
  private var eventHandler: EventHandlerRef?
  private var callback: (() -> Void)?

  /// Registers a global hotkey via Carbon's RegisterEventHotKey. Returns true on success.
  /// Defaults to Cmd+Space. macOS Spotlight binds the same combo by default — the user
  /// must disable that binding for this to fire (System Settings → Keyboard).
  func register(modifiers: UInt32 = UInt32(cmdKey), keyCode: UInt32 = UInt32(kVK_Space), onPressed: @escaping () -> Void) -> Bool {
    let hotKeyID = EventHotKeyID(signature: 0x6A765343 /* 'jvSC' */, id: 1)
    let target = GetApplicationEventTarget()
    let status = RegisterEventHotKey(keyCode, modifiers, hotKeyID, target, 0, &hotKeyRef)
    guard status == noErr else {
      NSLog("[hotkey] register failed: \(status)")
      return false
    }
    self.callback = onPressed

    var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
    let context = Unmanaged.passUnretained(self).toOpaque()
    InstallEventHandler(target, { (_, _, ctx) -> OSStatus in
      guard let ctx else { return OSStatus(eventNotHandledErr) }
      let mgr = Unmanaged<HotkeyManager>.fromOpaque(ctx).takeUnretainedValue()
      DispatchQueue.main.async { mgr.callback?() }
      return noErr
    }, 1, &spec, context, &eventHandler)
    NSLog("[hotkey] registered Cmd+Space")
    return true
  }

  func unregister() {
    if let hotKeyRef { UnregisterEventHotKey(hotKeyRef) }
    if let eventHandler { RemoveEventHandler(eventHandler) }
    hotKeyRef = nil
    eventHandler = nil
    callback = nil
  }
}
