// Live acceptance helper: launch the real app and observe geometry/state only.
// Gestures are performed separately through Computer Use, not synthesized here.
import { _electron as electron } from 'playwright';
import { writeFileSync, mkdirSync } from 'node:fs';
const packaged = process.argv.includes('--packaged');
const app = await electron.launch({ ...(packaged ? { executablePath: 'build/Jarvis Resonance.app/Contents/MacOS/Electron', args: [] } : { args: ['.'] }), cwd: process.cwd() });
const page = await app.firstWindow();
await page.waitForSelector('.control-row');
await page.evaluate(() => {
 window.__dragEvidence = [];
 for (const type of ['pointerdown', 'pointermove', 'pointerup', 'lostpointercapture']) window.addEventListener(type, e => {
  window.__dragEvidence.push({ type, x: e.screenX, y: e.screenY, buttons: e.buttons });
  if (window.__dragEvidence.length > 30) window.__dragEvidence.shift();
 }, true);
});
if (process.argv.includes('--focus')) await app.evaluate(({ BrowserWindow, app }) => { const w = BrowserWindow.getAllWindows()[0]; app.dock?.show(); w.setFocusable(true); w.show(); w.focus(); });
if (process.argv.includes('--silent')) await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.setAudioMuted(true));
mkdirSync('evidence', { recursive: true });
const observe = async () => {
 const native = await app.evaluate(({ BrowserWindow, screen }) => { const w = BrowserWindow.getAllWindows()[0]; return { bounds: w.getBounds(), visible: w.isVisible(), focused: w.isFocused(), focusable: w.isFocusable(), displays: screen.getAllDisplays().map(d => ({ bounds: d.bounds, workArea: d.workArea, scaleFactor: d.scaleFactor })) }; });
 const controls = await page.locator('.control-row button').evaluateAll(elements => elements.map(e => { const r = e.getBoundingClientRect(); return { label: e.getAttribute('aria-label'), pressed: e.getAttribute('aria-pressed'), x: r.x, y: r.y, width: r.width, height: r.height }; }));
 const report = { at: new Date().toISOString(), native, controls, pointerEvents: await page.evaluate(() => window.__dragEvidence) };
 writeFileSync('evidence/live-desktop.json', JSON.stringify(report, null, 2));
 return report;
};
console.log(JSON.stringify(await observe()));
const timer = setInterval(() => { observe().catch(() => {}); }, 500);
process.on('SIGINT', async () => { clearInterval(timer); await app.close(); process.exit(0); });
