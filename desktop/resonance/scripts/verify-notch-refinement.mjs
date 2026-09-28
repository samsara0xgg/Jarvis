// Focused browser acceptance for the design's hover intent, geometry, flyback and approval behavior.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const port = Number(process.env.REFINEMENT_PORT ?? 5207), base = `http://127.0.0.1:${port}`;
const evidence = process.env.REFINEMENT_EVIDENCE_DIR ?? path.join(root, 'evidence/notch-refinement');
mkdirSync(evidence, { recursive: true });
const server = spawn(path.join(root, 'node_modules/.bin/vite'), ['--host', '127.0.0.1', '--port', String(port), '--strictPort'], { cwd: root, stdio: 'ignore' });
const harness = path.join(root, '.notch-refinement.html');
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
const checks = [], check = (name, value) => { assert.ok(value, name); checks.push(name); };
try {
  for (let i = 0; i < 50; i++) { try { await fetch(base); break; } catch { await new Promise(r => setTimeout(r, 100)); } }
  const page = await browser.newPage({ viewport: { width: 800, height: 760 }, deviceScaleFactor: 2 });
  const errors = [];
  page.on('pageerror', error => { errors.push(error.message); console.error(error.message); });
  page.on('console', message => { if(message.type()==='error')console.error(message.text()); });
  writeFileSync(harness, `<!doctype html><html><body><div id="root"></div>
    <style>*{box-sizing:border-box}body{margin:0;background:#607c9a}button{border:0;background:none;font:inherit;cursor:pointer}#root{--glow:157 180 255;--warm-c:#ffc98f;--p-ink:#eef0fb;--p-muted:rgb(196 204 238 / .68)}</style>
    <script type="module">
    import React from 'react';
    import ReactDOM from 'react-dom/client';
    import {Notch} from '/src/Notch.tsx';
    import {NoticeCard} from '/src/Notices.tsx';
    import {ActionCard} from '/src/ActionCard.tsx';
    import '/src/design-tokens.css';
    const e=React.createElement,{useRef,useState}=React,{createRoot}=ReactDOM;
    const common={agent:'claude',project:'jarvis',where:'Ghostty',age:'now',you:'Refine the desktop',last:'Working',kind:'interactive'};
    const agents=[{...common,id:'wait',state:'wait',title:'Wire the companion'}, {...common,id:'work',state:'work',title:'Build the notices'}, {...common,id:'done',state:'done',title:'Review the panel'}];
    function App(){
      const [rows,setRows]=useState(agents),[note,setNote]=useState(null),[unread,setUnread]=useState(new Set()),[keys,setKeys]=useState(0),[parked,setParked]=useState(new Map());
      const cursor=useRef({x:750,y:700});
      const resolve=(text,body)=>window.results.push(body);
      const request=(id,always=true)=>({key:id,kind:'req',id:'wait',at:0,req:{id,tool:'Bash',input:{command:'npm run build',description:'Build the desktop app'},cwd:'/x/jarvis',always:always?"Don't ask again for Bash(npm run build:*)":''}});
      window.results??=[];
      window.h={
        cursor:(x,y)=>cursor.current={x,y}, rows:setRows, keys:()=>setKeys(n=>n+1), close:()=>setNote(null),
        pop:()=>{setUnread(new Set(['done']));setNote({key:'pop',pop:['done'],onClose:()=>setNote(null)});},
        approval:(id='req',always=true)=>{const n=request(id,always);setNote({key:id,id:'wait',onClose:()=>setNote(null),card:e(NoticeCard,{key:id,n,agent:agents[0],card:{qi:0,picks:[],review:false,feedback:false,ok:''},count:1,look:'spark',onPark:()=>setNote(null),onOpen:()=>{},onChange:()=>{},onResolve:resolve})});},
        action:()=>setNote({key:'action',onClose:()=>setNote(null),card:e(ActionCard,{key:'action',card:{id:'action',tool:'mcp__test',action:'Run the task',source:'',letter:false,args:{command:'npm run build'}},lang:'en',onDecide:decision=>window.results.push({decision})})}),
        one:()=>setRows([agents[1]]),all:()=>setRows(agents)
      };
      return e(Notch,{look:'spark',agents:rows,unread,parked,archived:new Set(),geo:{width:800,top:32,notchR:492.5,lobeL:243.5},cursor,note,quiet:false,port:null,keys,onKeys:()=>{},onViewing:()=>{},onNoteHover:()=>{},act:{jump:()=>{},answer:()=>{},read:()=>{},back:()=>setNote(null),archive:()=>{},park:ids=>setParked(new Map(ids.map(id=>[id,Date.now()]))),unpark:()=>{}}});
    }
    createRoot(document.getElementById('root')).render(e(App));
    </script></body></html>`);
  await page.goto(`${base}/.notch-refinement.html`);
  await page.waitForFunction(() => !!window.h);
  const drop = page.locator('.notch-drop.is-open'), note = page.locator('.notch-note.is-open');
  const move = (x, y) => page.evaluate(([x, y]) => window.h.cursor(x, y), [x, y]);
  const shown = async () => drop.count();
  await page.waitForTimeout(500);
  const wingWidth = () => page.locator('.notch-hit').evaluate(el => el.getBoundingClientRect().width);
  check('wing has a stable 112 pt footprint for three groups', Math.abs(await wingWidth() - 112) < 1);
  await page.evaluate(async () => {
    const start = performance.now();
    await new Promise(resolve => { const tick=now=>{const elapsed=now-start;window.h.cursor(489+elapsed*.5,16);if(elapsed<245)requestAnimationFrame(tick);else resolve();};requestAnimationFrame(tick); });
  });
  check('passing through the wing at .5 pt/ms does not open the panel', await shown() === 0);
  await move(541.5, 16); await page.waitForTimeout(95);
  check('hover does not open before 140 ms', await shown() === 0);
  await drop.waitFor({ timeout: 2000 }); await page.waitForTimeout(400);
  check('slowing down and dwelling opens a 440 pt list', Math.abs((await drop.boundingBox()).width - 440) < 1);
  await move(541.5, 115); await page.waitForTimeout(350);
  check('moving from the wing into the panel keeps it open', await shown() === 1);
  await move(760, 600); await page.waitForTimeout(180);
  check('exit keeps the panel open during its 280 ms grace', await shown() === 1);
  await page.waitForTimeout(250); check('exit closes the panel after the grace', await shown() === 0);
  await page.mouse.click(541.5,16); await drop.waitFor({ timeout: 1000 });
  check('a click opens the list without a hover dwell', await shown() === 1);
  await move(760,600); await page.waitForTimeout(600);
  await page.evaluate(() => window.h.one()); await page.waitForTimeout(400);
  check('changing from three groups to one does not jump the wing width', Math.abs(await wingWidth() - 112) < 1);
  await page.evaluate(() => {window.h.all();window.h.pop();}); await note.waitFor(); await page.waitForTimeout(550);
  check('pop width is exactly 360 pt', Math.abs((await note.boundingBox()).width - 360) < 1);
  check('pop keeps its session out of the wing count until it lands', (await page.locator('.notch').getAttribute('data-counts')).includes('turn1'));
  await page.screenshot({ path:path.join(evidence,'01-pop.png') });
  await page.locator('.notch-note .c-x').click();
  await page.waitForFunction(() => document.querySelector('.notch').dataset.flights === '1');
  check('dismissing a pop starts its return flight', true);
  await page.screenshot({ path:path.join(evidence,'02-flight.png') });
  await page.waitForTimeout(650);
  check('the 460 ms flight lands and adds the count', (await page.locator('.notch').getAttribute('data-flights')) === '0' && (await page.locator('.notch').getAttribute('data-counts')).includes('turn2'));
  await page.evaluate(() => window.h.approval()); await note.waitFor(); await page.waitForTimeout(550);
  check('approval uses the 440 pt card width', Math.abs((await note.boundingBox()).width - 440) < 1);
  check('approval exposes only Deny and Allow primary buttons', (await page.locator('.nc-choice button').allTextContents()).map(s=>s.trim()).join('|') === 'Deny|Allow ⌘⏎');
  check('Always is unchecked and names the command scope and project', !await page.locator('.nc-always input').isChecked() && (await page.locator('.nc-always').textContent()).includes('npm run build:*') && (await page.locator('.nc-always').textContent()).includes('jarvis'));
  check('Park and Open in Ghostty are title icon buttons', await page.locator('.nc-actions button').count() === 2 && (await page.locator('.nc-actions').textContent()).trim() === '');
  await page.locator('.nc-choice .btn-warm').focus(); await page.keyboard.press('Enter');
  check('bare Enter does not approve a focused primary button', await page.evaluate(() => window.results.length) === 0);
  await page.keyboard.press('Meta+Enter');
  check('Cmd Enter allows once while Always is unchecked', (await page.evaluate(() => window.results.at(-1))).decision === 'allow');
  await page.evaluate(() => {
    const form=document.createElement('form');form.id='composer';form.style='position:absolute;top:600px;left:20px';form.innerHTML='<input aria-label="Separate composer" value="hello">';
    window.composerSent=0;form.addEventListener('submit',e=>{e.preventDefault();window.composerSent++;});document.body.append(form);
  });
  await page.getByLabel('Separate composer').focus();await page.keyboard.press('Enter');
  check('notice approval does not steal Enter from another composer', await page.evaluate(() => window.composerSent) === 1);
  const beforeOutside=await page.evaluate(() => window.results.length);await page.keyboard.press('Meta+Enter');
  check('Cmd Enter in another composer does not approve the notice', await page.evaluate(() => window.results.length) === beforeOutside);
  await page.evaluate(() => window.h.approval('remember')); await page.waitForTimeout(450);
  await page.locator('.nc-always input').check(); await page.screenshot({path:path.join(evidence,'03-approval.png')});
  await page.keyboard.press('Meta+Enter');
  check('Cmd Enter persists permission only after the opt-in is checked', (await page.evaluate(() => window.results.at(-1))).decision === 'always');
  await page.evaluate(() => window.h.action()); await page.waitForTimeout(500);
  const beforeAction=await page.evaluate(() => window.results.length);
  await page.getByLabel('Separate composer').focus();await page.keyboard.press('Enter');
  check('action card does not steal Enter from another composer', await page.evaluate(() => window.composerSent) >= 2);
  await page.keyboard.press('Meta+Enter');
  check('Cmd Enter in another composer does not accept the action', await page.evaluate(() => window.results.length) === beforeAction);
  await page.locator('.ac-go').focus();await page.keyboard.press('Enter');
  check('Jarvis action cards also ignore bare Enter', await page.evaluate(() => window.results.length) === beforeAction);
  await page.keyboard.press('Meta+Enter');
  check('Jarvis action cards accept Cmd Enter', (await page.evaluate(() => window.results.at(-1))).decision === 'accept');
  await page.evaluate(() => window.h.close()); await page.waitForTimeout(600);
  const afterClosed=await page.evaluate(() => window.results.length);await page.keyboard.press('Meta+Enter');
  check('closed cards cannot consume a shortcut', await page.evaluate(() => window.results.length) === afterClosed);
  check('no browser runtime errors', errors.length === 0);
  writeFileSync(path.join(evidence,'checks.json'),JSON.stringify({checks,errors},null,2));
  console.log(checks.join('\n'));
} finally { await browser.close(); server.kill(); rmSync(harness, { force:true }); }
