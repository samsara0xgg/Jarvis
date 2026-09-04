import AppKit

@main
class InherentCardApp: NSObject, NSApplicationDelegate {
  var controller: NativeCardController?
  let watchdog = ParentWatchdog()

  static func main() {
    let app = NSApplication.shared
    let delegate = InherentCardApp()
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    app.run()
  }

  func applicationDidFinishLaunching(_ notification: Notification) {
    watchdog.startIfRequested()

    let controller = NativeCardController()
    self.controller = controller
    controller.showInitial()
    controller.startPassthroughMonitor()

    let mainMenu = NSMenu()
    let appMenuItem = NSMenuItem()
    mainMenu.addItem(appMenuItem)
    let appMenu = NSMenu()
    appMenuItem.submenu = appMenu

    let editMenuItem = NSMenuItem()
    mainMenu.addItem(editMenuItem)
    let editMenu = NSMenu(title: "Edit")
    editMenuItem.submenu = editMenu
    let paste = NSMenuItem(title: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    paste.keyEquivalentModifierMask = .command
    editMenu.addItem(paste)

    #if DEBUG
    let reload = NSMenuItem(title: "Reload Card", action: #selector(reloadCard), keyEquivalent: "r")
    reload.target = self
    reload.keyEquivalentModifierMask = .command
    appMenu.addItem(reload)

    let quit = NSMenuItem(title: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    appMenu.addItem(quit)

    #endif
    NSApp.mainMenu = mainMenu
  }

  #if DEBUG
  @objc func reloadCard() { controller?.model.siriReset() }
  #endif

  func applicationWillTerminate(_ notification: Notification) {
    watchdog.stop()
    controller?.shutdown()
  }
}
