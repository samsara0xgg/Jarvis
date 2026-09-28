// GPU and live-canvas acceptance for the fixed self-lit material and both notch homes.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const port = Number(process.env.CHARACTER_MATERIAL_PORT ?? 5213), base = `http://127.0.0.1:${port}`;
const evidence = process.env.CHARACTER_MATERIAL_EVIDENCE_DIR ?? path.join(root, 'evidence/character-material');
mkdirSync(evidence, { recursive: true });
const harness = path.join(root, '.character-material.html');
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['--host', '127.0.0.1', '--port', String(port), '--strictPort'], { cwd: root, stdio: 'ignore' });
let browser;
const checks = [], check = (name, pass) => { assert.ok(pass, name); checks.push(name); };
try {
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  for (let i = 0; i < 50; i++) { try { await fetch(base); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  writeFileSync(harness, `<!doctype html><html><body><div id="root"></div>
    <style>body{margin:0;background:#607c9a}button{border:0}</style>
    <script type="module">
    import React from 'react'; import {createRoot} from 'react-dom/client';
    import {CompanionBall} from '/src/CompanionBall.tsx';
    import {Core,SKIN_KEYS} from '/src/starCore.ts';
    import '/src/design-tokens.css'; import '/src/companion.css';
    const e=React.createElement;
    function App(){
      const [state,set]=React.useState({place:'home',expr:'02',pressed:false,home:'dark',homeFinish:'refined'});
      const look=React.useRef(null),handle=React.useRef(null);
      window.h={set:change=>set(s=>({...s,...change})),handle};
      return e(CompanionBall,{width:360,height:220,lobe:{left:64,right:152,height:32,notched:true},
        target:{...state,anchors:{home:{x:96,y:16},peek:{x:96,y:34.6},out:{x:96,y:72},dock:{x:180,y:63}}},
        look,handle,skin:'glass',label:'Her',onPress:()=>{},onRelease:()=>{},onCancel:()=>{},onMove:()=>{}});
    }
    window.materials=()=>SKIN_KEYS.map(skin=>{
      const core=new Core(skin); for(let now=0;now<1200;now+=16)core.update(now,.016,{expr:'02',look:null,still:true,pressed:false,charge:0});
      const cv=document.createElement('canvas');cv.width=cv.height=160;const c=cv.getContext('2d');c.translate(80,80);
      if(!core.render(160,3))throw Error('WebGL2 unavailable');
      core.glass(c,60,160);const glassAlpha=c.getImageData(0,0,160,160).data.reduce((n,v,i)=>n+(i%4===3?v:0),0);
      core.inside(c,60,160);const data=c.getImageData(0,0,160,160).data;
      let lit=0;for(let i=0;i<data.length;i+=4)if(Math.max(data[i],data[i+1],data[i+2])>35&&data[i+3]>100)lit++;
      return {skin,glassAlpha,lit};
    });
    window.homeLight=()=>{const cv=document.querySelector('.companion-canvas'),c=cv.getContext('2d'),d=cv.width/360;
      const data=c.getImageData(76*d,14*d,4*d,4*d).data;let sum=0;for(let i=0;i<data.length;i+=4)sum+=(data[i]+data[i+1]+data[i+2])*data[i+3]/255;return sum;};
    createRoot(document.getElementById('root')).render(e(App));
    </script></body></html>`);
  const page = await browser.newPage({ viewport: { width: 360, height: 220 }, deviceScaleFactor: 2 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto(`${base}/.character-material.html`);
  await page.locator('.companion-canvas[data-home-finish="refined"]').waitFor();
  const materials = await page.evaluate(() => window.materials());
  check('all six skins keep emissive stars with a transparent reflection layer', materials.length === 6 && materials.every(m => m.glassAlpha === 0 && m.lit > 50));
  await page.waitForTimeout(1200);
  const refined = await page.locator('canvas').evaluate(cv => cv.toDataURL());
  await page.screenshot({ path: path.join(evidence, 'refined-home.png') });
  await page.evaluate(() => window.h.set({ homeFinish: 'original' })); await page.waitForTimeout(100);
  const original = await page.locator('canvas').evaluate(cv => cv.toDataURL());
  check('original and refined notch finishes both render and differ', refined !== original && await page.evaluate(() => window.homeLight()) > 0);
  await page.screenshot({ path: path.join(evidence, 'original-home.png') });
  await page.evaluate(() => window.h.set({ home: 'eyes' })); await page.waitForTimeout(100);
  check('eyes-only keeps the surrounding home dark', await page.evaluate(() => window.homeLight()) === 0);
  await page.evaluate(() => window.h.set({ home: 'dark', homeFinish: 'refined', place: 'out' }));
  await page.waitForTimeout(700); const warm = await page.evaluate(() => window.homeLight());
  await page.screenshot({ path: path.join(evidence, 'refined-away-warm.png') });
  await page.waitForFunction(() => document.querySelector('canvas').dataset.homeWarmth === '0');
  const cold = await page.evaluate(() => window.homeLight());
  check('the refined home cools over 2.5 seconds', warm > cold);
  check('a pilot light remains after the warmth is gone', await page.locator('canvas').evaluate(cv => {
    const d=cv.width/360,data=cv.getContext('2d').getImageData(95*d,16*d,2*d,2*d).data;return data.some((v,i)=>i%4===3&&v>0);
  }));
  await page.screenshot({ path: path.join(evidence, 'refined-away-pilot.png') });
  await page.evaluate(() => window.h.set({ homeFinish: 'original' })); await page.waitForTimeout(100);
  check('the original home stays dark while she is out', await page.locator('canvas').evaluate(cv => {
    const d=cv.width/360,data=cv.getContext('2d').getImageData(65*d,0,80*d,32*d).data;return data.every((v,i)=>i%4!==3||v===0);
  }));
  check('no browser runtime errors', errors.length === 0);
  writeFileSync(path.join(evidence, 'checks.json'), JSON.stringify({ checks, materials, warm, cold, errors }, null, 2));
  console.log(`Character material: ${checks.length} checks passed.`);
  for (const name of checks) console.log(`  ${name}`);
} finally { await browser?.close(); server.kill(); rmSync(harness, { force: true }); }
