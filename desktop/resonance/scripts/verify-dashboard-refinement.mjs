// Run after npm run build. DASHBOARD_REFINE_URL can reuse a local Vite server.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = process.env.DASHBOARD_REFINE_EVIDENCE_DIR ?? path.join(root, 'evidence/dashboard-refinement');
const port = Number(process.env.DASHBOARD_REFINE_PORT ?? 5203);
const url = process.env.DASHBOARD_REFINE_URL ?? `http://127.0.0.1:${port}`;
const server = process.env.DASHBOARD_REFINE_URL ? null : spawn(path.join(root, 'node_modules/.bin/vite'), ['preview', '--port', String(port), '--strictPort'], { cwd: root, stdio: 'ignore' });
mkdirSync(dir, { recursive: true });
const browser=await chromium.launch({headless:true,channel:'chrome'});
try {
for (let i = 0; i < 50; i++) { try { await fetch(url); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
const page=await browser.newPage({viewport:{width:640,height:720},deviceScaleFactor:2});
await page.clock.setFixedTime(new Date('2026-09-27T12:00:00'));
const errors=[];page.on('pageerror',error=>errors.push(error.message));
await page.addInitScript(()=>{window.jarvis={placement:async()=>({docked:false,topInset:32,notchWidth:185,surfaceWidth:640,compactWidth:0,displayId:1}),onPlacement:()=>()=>{},onDisplayLeave:()=>()=>{},onDictation:()=>()=>{},wearing:()=>{},displayReady:()=>{},companionSettings:()=>{},onCursor:cb=>{window.__cursor=cb;return()=>{}},onCommand:()=>()=>{},passthrough:()=>{},focus:async()=>{},material:()=>{}}});
await page.goto(`${url}/?companion=1`);await page.waitForTimeout(400);
await page.locator('.companion-island-target').click();await page.waitForTimeout(700);
const metrics=await page.evaluate(()=>{
 const panel=document.querySelector('.companion-dashboard'),input=document.querySelector('.ad .cmp input'),text=document.querySelector('.ad .fy-t'),ring=document.querySelector('.ad .dial');
 return {panel:panel.getBoundingClientRect().toJSON(),input:input.getBoundingClientRect().toJSON(),placeholder:input.placeholder,opacity:getComputedStyle(input).opacity,fill:getComputedStyle(ring).getPropertyValue('--fill'),fillAnimation:getComputedStyle(ring,'::before').animationName,transition:getComputedStyle(ring).transitionDuration,textOverflows:text.scrollWidth>text.clientWidth,header:document.querySelector('.next-event').textContent};
});
assert.ok(Math.abs(metrics.panel.width-360)<.5);assert.ok(metrics.panel.bottom<=720);assert.ok(metrics.input.bottom<metrics.panel.bottom);assert.equal(metrics.placeholder,'Ask Jarvis…');assert.equal(metrics.opacity,'1');assert.equal(metrics.fill.trim(),'38');assert.equal(metrics.fillAnimation,'none');assert.equal(metrics.transition,'0.46s');assert.equal(metrics.textOverflows,false);assert.match(metrics.header,/Call with Mom/);
await page.screenshot({path:path.join(dir, 'home.png')});
const before=await page.locator('.ad .dial').first().evaluate(el=>getComputedStyle(el,'::before').backgroundImage);
await page.evaluate(()=>document.querySelector('.ad').style.setProperty('--glow','255 0 0'));
const after=await page.locator('.ad .dial').first().evaluate(el=>getComputedStyle(el,'::before').backgroundImage);assert.equal(before,after);
await page.evaluate(()=>document.querySelector('.ad').style.removeProperty('--glow'));
await page.locator('.companion-island-target').click();await page.waitForTimeout(250);await page.locator('.companion-island-target').click();
const reopened=await page.locator('.ad .dial').first().evaluate(el=>getComputedStyle(el).getPropertyValue('--fill'));assert.equal(reopened.trim(),'38');
await page.waitForTimeout(500);
await page.locator('.ad [data-row="usage"]').click();await page.waitForTimeout(350);
assert.equal(await page.locator('.ad .page .dial').first().evaluate(el=>getComputedStyle(el).getPropertyValue('--fill').trim()),'38');
await page.screenshot({path:path.join(dir, 'usage.png')});
// A value change interpolates on the meter itself, from the actual previous value.
await page.locator('.ad .page .dial').first().evaluate(el=>el.style.setProperty('--fill','58'));
await page.waitForTimeout(100);const middle=Number(await page.locator('.ad .page .dial').first().evaluate(el=>getComputedStyle(el).getPropertyValue('--fill')));assert.ok(middle>38&&middle<58);
await page.waitForTimeout(420);assert.equal(Number(await page.locator('.ad .page .dial').first().evaluate(el=>getComputedStyle(el).getPropertyValue('--fill'))),58);
const overlap = await page.evaluate(() => new Promise(resolve => {
  let worst = 0;
  const start = performance.now();
  document.querySelector('.ad .pg-back').click();
  const sample = () => {
    const words = document.querySelector('.ad .pg-body');
    const home = Math.max(...[...document.querySelectorAll('.ad .home-inner > *')].map(el => Number(getComputedStyle(el).opacity)));
    worst = Math.max(worst, Math.min(words ? Number(getComputedStyle(words).opacity) : 0, home));
    if (performance.now() - start < 300) requestAnimationFrame(sample); else resolve(worst);
  };
  requestAnimationFrame(sample);
}));
assert.ok(overlap < .15, 'return must not overlap page and home words');
assert.equal(await page.locator('.ad .page').count(),0);
await page.setViewportSize({width:640,height:600});await page.waitForTimeout(500);assert.ok((await page.locator('.companion-dashboard').boundingBox()).y+(await page.locator('.companion-dashboard').boundingBox()).height<=600);
assert.deepEqual(errors,[]);writeFileSync(path.join(dir, 'checks.json'),JSON.stringify({metrics,reopened,middle,overlap,errors},null,2));console.log(JSON.stringify({ok:true,metrics,reopened,middle,errors}));
} finally {await browser.close();server?.kill()}
