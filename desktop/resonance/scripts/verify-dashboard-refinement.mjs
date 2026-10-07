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
await page.goto(`${url}/?companion=1`);
// Hardware and desktop stand-in: the cutout must disappear into the attached black band.
await page.addStyleTag({content:`html,body{height:100%}body{background:linear-gradient(160deg,#7f98b8,#5d7898 55%,#4a6484)!important}
 body::before{content:'';position:fixed;inset:0 0 auto;height:32px;background:rgb(255 255 255/.18)}
 body::after{content:'';position:fixed;z-index:10;pointer-events:none;top:0;left:227.5px;width:185px;height:32px;background:#000;border-radius:0 0 10px 10px}`});
await page.waitForTimeout(400);
await page.locator('.companion-island-target').click();await page.waitForTimeout(700);
const metrics=await page.evaluate(()=>{
 const panel=document.querySelector('.companion-dashboard'),input=document.querySelector('.ad .cmp textarea'),text=document.querySelector('.ad .fy-t'),ring=document.querySelector('.ad .dial');
 return {panel:panel.getBoundingClientRect().toJSON(),corner:document.querySelector('.ad .corner').getBoundingClientRect().toJSON(),cornerButtons:[...document.querySelectorAll('.ad .corner .cb')].map(el=>el.getBoundingClientRect().toJSON()),input:input.getBoundingClientRect().toJSON(),placeholder:input.placeholder,placeholderColour:getComputedStyle(input,'::placeholder').color,opacity:getComputedStyle(input).opacity,fill:getComputedStyle(ring).getPropertyValue('--fill'),fillAnimation:getComputedStyle(ring,'::before').animationName,transition:getComputedStyle(ring).transitionDuration,textOverflows:text.scrollWidth>text.clientWidth,header:document.querySelector('.next-event').textContent,forYouBackground:getComputedStyle(document.querySelector('.ad .r-foryou')).backgroundColor};
});
assert.ok(Math.abs(metrics.panel.width-360)<.5);assert.ok(metrics.panel.bottom<=720);assert.ok(metrics.input.bottom<metrics.panel.bottom);assert.equal(metrics.placeholder,'Ask Jarvis…');assert.equal(metrics.opacity,'1');assert.equal(metrics.fill.trim(),'38');assert.equal(metrics.fillAnimation,'none');assert.equal(metrics.transition,'0.46s');assert.equal(metrics.textOverflows,false);assert.match(metrics.header,/Call with Mom/);
assert.ok(Math.abs(metrics.panel.top-32)<.5,'attached sheet must meet the hardware notch without a gap');
assert.ok(Math.abs(metrics.corner.top-metrics.panel.top)<.5&&metrics.corner.height===28,'the next-event header must occupy the 28 pt black band');
assert.ok(metrics.cornerButtons.every(b=>b.top>=metrics.panel.top&&b.bottom<=metrics.panel.top+28.5),'all three header controls must fit inside the black band');
assert.notEqual(metrics.forYouBackground,'rgba(0, 0, 0, 0)','For You must retain its warm surface');
// The input has no pill: its placeholder is written on the dusk's foot, so the surface behind it is the dusk's darkest, with its violet corner as the upper bound.
// Text contrast must be repaired in the foreground, without flattening the approved gradient.
const rgb=metrics.placeholderColour.match(/[\d.]+/g).map(Number),alpha=rgb[3]??1,background=[27,20,48];
const luminance=colour=>colour.reduce((sum,c,i)=>{const v=c/255;return sum+(v<=.04045?v/12.92:((v+.055)/1.055)**2.4)*[.2126,.7152,.0722][i];},0);
const contrast=(luminance(background.map((c,i)=>rgb[i]*alpha+c*(1-alpha)))+.05)/(luminance(background)+.05);
assert.ok(contrast>=4.5,`secondary text must stay readable over dusk (${contrast.toFixed(2)}:1)`);
await page.screenshot({path:path.join(dir, 'home.png')});
// The live header/data/status elements retain their meaning when her mood changes.
await page.locator('.ad .td').first().click();
const semanticColours=()=>page.evaluate(()=>{
 const pick=(selector,pseudo)=>{const style=getComputedStyle(document.querySelector(selector),pseudo);return [style.color,style.backgroundColor,style.backgroundImage,style.boxShadow];};
 return {ring:pick('.ad .dial','::before'),time:pick('.ad .ev b'),reminder:pick('.ad .nd.is-note'),completed:pick('.ad .td[aria-pressed="true"] i'),request:pick('.ad .r-foryou'),project:pick('.ad .cols i')};
});
const before=await semanticColours();
await page.evaluate(()=>document.querySelector('.ad').style.setProperty('--glow','255 0 0'));
const after=await semanticColours();assert.deepEqual(before,after,'data and semantic status colours must not follow her glow');
await page.evaluate(()=>document.querySelector('.ad').style.removeProperty('--glow'));
for(const [section,selectors] of [['now',['.now-card .who','.tl .is-observed','.tl .is-stated','.legend .o','.legend .s']],['agents',['.tagc.sub','.shimmer']]]){
 await page.locator(`.ad [data-row="${section}"]`).click();await page.waitForTimeout(300);
 const capture=()=>page.evaluate(selectors=>selectors.map(selector=>{const el=document.querySelector(`.ad .page ${selector}`);if(!el)throw new Error(`Missing semantic example ${selector}`);const style=getComputedStyle(el,selector.startsWith('.tl')?'::before':null);return [style.color,style.backgroundColor,style.backgroundImage,style.borderColor];}),selectors);
 const baseline=await capture();await page.evaluate(()=>document.querySelector('.ad').style.setProperty('--glow','255 0 0'));
 assert.deepEqual(await capture(),baseline,`${section} metadata must retain fixed semantic colours`);
 await page.evaluate(()=>document.querySelector('.ad').style.removeProperty('--glow'));
 await page.locator('.ad .pg-back').click();await page.waitForTimeout(300);
}
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
assert.deepEqual(errors,[]);writeFileSync(path.join(dir, 'checks.json'),JSON.stringify({metrics,contrast,semanticColours:after,reopened,middle,overlap,errors},null,2));console.log(JSON.stringify({ok:true,metrics,contrast,reopened,middle,overlap,errors}));
} finally {await browser.close();server?.kill()}
