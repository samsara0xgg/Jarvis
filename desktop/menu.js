// menu.js — right-click context menu, ported from OLV's src/main/menu-manager.ts.
// Keeps OLV items verbatim in spirit; translated to vanilla JS. User can refine later.

const { Menu, BrowserWindow, ipcMain, screen, app } = require('electron');

/**
 * MenuManager — wires up the right-click context menu and the tray menu.
 *
 * Construction takes a callbacks object so main.js can control mode switches
 * and quit behaviour without menu.js needing to know the WindowManager class.
 */
class MenuManager {
  /**
   * @param {Object} callbacks
   * @param {(mode: 'window'|'pet') => void} callbacks.onModeChange
   * @param {() => void} [callbacks.onQuit]
   * @param {() => void} [callbacks.onHide]
   * @param {() => void} [callbacks.onShow]
   */
  constructor(callbacks = {}) {
    this.currentMode = 'window';
    this.configFiles = []; // [{filename, name}] — user fills in later
    this.onModeChange = callbacks.onModeChange || (() => {});
    this.onQuit = callbacks.onQuit || (() => app.quit());
    this.onHide = callbacks.onHide || (() => {
      BrowserWindow.getAllWindows().forEach((w) => w.hide());
    });
    this.onShow = callbacks.onShow || (() => {
      BrowserWindow.getAllWindows().forEach((w) => w.show());
    });

    this._setupContextMenu();
  }

  /** Mode-radio submenu block used by both tray and context menus. */
  _getModeMenuItems() {
    return [
      {
        label: 'Window Mode',
        type: 'radio',
        checked: this.currentMode === 'window',
        click: () => this.setMode('window'),
      },
      {
        label: 'Pet Mode',
        type: 'radio',
        checked: this.currentMode === 'pet',
        click: () => this.setMode('pet'),
      },
    ];
  }

  /** Build a context menu for the given event.sender webContents. */
  _buildContextMenuTemplate(event) {
    const inPet = this.currentMode === 'pet';

    // --- Stubs: these channels are ported from OLV's menu-manager.ts for menu shape
    // parity. The Jarvis renderer does not currently listen on these channels — they
    // are no-ops until wired up by the user. See spec §5.4 and §11. ---
    return [
      {
        label: 'Toggle Microphone',
        click: () => event.sender.send('mic-toggle'),
      },
      {
        label: 'Interrupt',
        click: () => event.sender.send('interrupt'),
      },
      { type: 'separator' },
      ...(inPet
        ? [{
            label: 'Toggle Mouse Passthrough',
            click: () => event.sender.send('toggle-force-ignore-mouse'),
          }]
        : []),
      {
        label: 'Toggle Scrolling to Resize',
        click: () => event.sender.send('toggle-scroll-to-resize'),
      },
      ...(inPet
        ? [{
            label: 'Toggle InputBox and Subtitle',
            click: () => event.sender.send('toggle-input-subtitle'),
          }]
        : []),
      { type: 'separator' },
      ...this._getModeMenuItems(),
      { type: 'separator' },
      {
        label: 'Switch Character',
        visible: inPet && this.configFiles.length > 0,
        submenu: this.configFiles.map((config) => ({
          label: config.name,
          click: () => event.sender.send('switch-character', config.filename),
        })),
      },
      { type: 'separator' },
      {
        label: 'Hide',
        click: () => this.onHide(),
      },
      {
        label: 'Exit',
        click: () => this.onQuit(),
      },
    ];
  }

  /**
   * Attach context-menu handler to a webContents.
   * Call this once per BrowserWindow that should show the right-click menu.
   */
  attach(webContents) {
    webContents.on('context-menu', (event, params) => {
      // Preserve the native Electron `context-menu` payload so popup anchors
      // to the click location. OLV uses a renderer-initiated `show-context-menu`
      // IPC; for simplicity and parity we let webContents.on('context-menu') fire.
      const win = BrowserWindow.fromWebContents(webContents);
      if (!win) return;
      const template = this._buildContextMenuTemplate({ sender: webContents });
      const menu = Menu.buildFromTemplate(template);
      menu.popup({
        window: win,
        x: params ? params.x : Math.round(screen.getCursorScreenPoint().x),
        y: params ? params.y : Math.round(screen.getCursorScreenPoint().y),
      });
    });
  }

  /**
   * OLV also supports a renderer-initiated context menu via IPC
   * ('show-context-menu'). We keep the listener so existing OLV-style renderers
   * also work if they ever get ported in.
   */
  _setupContextMenu() {
    ipcMain.on('show-context-menu', (event) => {
      const win = BrowserWindow.fromWebContents(event.sender);
      if (!win) return;
      const screenPoint = screen.getCursorScreenPoint();
      const menu = Menu.buildFromTemplate(this._buildContextMenuTemplate(event));
      menu.popup({
        window: win,
        x: Math.round(screenPoint.x),
        y: Math.round(screenPoint.y),
      });
    });
  }

  /** Called by main.js after the window mode changes (or via radio click). */
  setMode(mode) {
    this.currentMode = mode;
    this.onModeChange(mode);
  }

  setCurrentMode(mode) {
    this.currentMode = mode;
  }

  updateConfigFiles(files) {
    this.configFiles = Array.isArray(files) ? files : [];
  }
}

module.exports = { MenuManager };
