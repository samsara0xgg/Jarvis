// Focused browser acceptance for the design's hover intent, geometry, flyback and approval behavior.
import { chromium } from 'playwright';
import { createServer } from 'vite';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const port = Number(process.env.REFINEMENT_PORT ?? 5207), base = `http://127.0.0.1:${port}`;
const evidence = process.env.REFINEMENT_EVIDENCE_DIR ?? path.join(root, 'evidence/notch-refinement');
mkdirSync(evidence, { recursive: true });
// Other agents can edit neighboring surfaces during acceptance. Freeze this
// fixture's modules instead of letting an unrelated HMR reload reset its state.
const server = await createServer({ root, server:{host:'127.0.0.1',port,strictPort:true,hmr:false,watch:null},logLevel:'error' });
await server.listen();
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
    import {NoticeCard,useNotices} from '/src/Notices.tsx';
    import {ActionCard} from '/src/ActionCard.tsx';
    import '/src/design-tokens.css';
    const e=React.createElement,{useRef,useState}=React,{createRoot}=ReactDOM;
    const common={agent:'claude',project:'jarvis',where:'Ghostty',age:'now',you:'Refine the desktop',last:'Working',kind:'interactive'};
    const agents=[{...common,id:'wait',state:'wait',title:'Wire the companion'}, {...common,id:'work',state:'work',title:'Build the notices'}, {...common,id:'done',state:'done',title:'Review the panel'}];
    function App(){
      const [rows,setRows]=useState(agents),[note,setNote]=useState(null),[unread,setUnread]=useState(new Set()),[keys,setKeys]=useState(0),[parked,setParked]=useState(new Map()),[held,setHeld]=useState(false),[live,setLive]=useState(false);
      const cursor=useRef({x:750,y:700});
      const notices=useNotices({port:null,agents:rows,hold:held,watched:null,viewing:null,cue:()=>{},answer:(req,body)=>new Promise((resolve,reject)=>{window.results.push({...body,request:req.id});window.finishAnswer=resolve;window.failAnswer=reject;})});
      const current=notices.current;
      const liveNote=current && current.kind==='req'?{key:current.key,id:current.id,onClose:notices.fold,card:e(NoticeCard,{key:current.key,n:current,agent:rows.find(a=>a.id===current.id),card:notices.card,count:notices.count,look:'spark',onPark:()=>notices.park([current.id]),onOpen:()=>{},onChange:notices.bump,onResolve:(text,body)=>notices.resolve(current,text,body)})}:null;
      const resolve=(text,body)=>window.results.push(body);
      const request=(id,always=true)=>({key:id,kind:'req',id:'wait',at:0,req:{id,tool:'Bash',input:{command:'npm run build',description:'Build the desktop app'},cwd:'/x/jarvis',always:always?"Don't ask again for Bash(npm run build:*)":''}});
      window.results??=[];
      window.h={
        liveRequest:id=>{setLive(true);setRows(agents.map(a=>a.id==='wait'?{...a,request:request(id).req}:a));}, cursor:(x,y)=>cursor.current={x,y}, rows:setRows, keys:()=>setKeys(n=>n+1), close:()=>setNote(null),
        pop:()=>{setUnread(new Set(['done']));setNote({key:'pop',pop:['done'],onClose:()=>setNote(null)});},
        approval:(id='req',always=true)=>{const n=request(id,always);setNote({key:id,id:'wait',onClose:()=>setNote(null),card:e(NoticeCard,{key:id,n,agent:agents[0],card:{qi:0,picks:[],review:false,feedback:false,ok:''},count:1,look:'spark',onPark:()=>setNote(null),onOpen:()=>{},onChange:()=>{},onResolve:resolve})});},
        action:()=>setNote({key:'action',onClose:()=>setNote(null),card:e(ActionCard,{key:'action',card:{id:'action',tool:'mcp__test',action:'Run the task',source:'',letter:false,args:{command:'npm run build'}},lang:'en',onDecide:decision=>window.results.push({decision})})}),
        one:()=>setRows([agents[1]]),all:()=>setRows(agents)
      };
      return e(Notch,{look:'spark',agents:rows,unread,parked,archived:new Set(),geo:{width:800,top:32,notchR:492.5,lobeL:243.5},cursor,note:live?liveNote:note,quiet:false,port:null,keys,onKeys:setHeld,onViewing:()=>{},onNoteHover:live?notices.setHover:()=>{},act:{jump:()=>{},answer:live?notices.focus:()=>{},read:()=>{},back:live?notices.back:()=>setNote(null),archive:()=>{},park:ids=>setParked(new Map(ids.map(id=>[id,Date.now()]))),unpark:()=>{}}});
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
  check('approval exposes only Deny and Allow primary buttons', (await page.locator('.nc-choice button').allTextContents()).map(s=>s.trim()).join('|') === 'Deny esc|Allow ⌘⏎');
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
  await page.evaluate(() => window.h.approval('escape'));await note.waitFor();await page.waitForTimeout(350);
  const beforeEscape=await page.evaluate(() => window.results.length);
  await page.getByLabel('Separate composer').focus();await page.keyboard.press('Escape');
  check('Escape in another input does not deny an approval', await page.evaluate(() => window.results.length) === beforeEscape);
  await page.evaluate(() => {
    const menu=document.createElement('div');menu.id='test-menu';menu.role='menu';menu.innerHTML='<button role="menuitem">Menu item</button>';menu.style='position:absolute;left:20px;top:650px';document.body.append(menu);menu.querySelector('button').focus();
  });
  await page.keyboard.press('Escape');
  check('Escape in an external menu does not deny an approval', await page.evaluate(() => window.results.length) === beforeEscape);
  await page.evaluate(() => document.querySelector('#test-menu').remove());
  await page.locator('.nc-choice .btn-warm').focus();
  await page.evaluate(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',repeat:true,bubbles:true,cancelable:true})));
  check('a repeated Escape never dispatches Deny', await page.evaluate(() => window.results.length) === beforeEscape);
  await page.keyboard.press('Escape');
  check('Escape explicitly denies the visible approval', (await page.evaluate(() => window.results.at(-1))).decision === 'deny');
  await page.evaluate(() => window.h.close());await page.waitForTimeout(450);
  await page.evaluate(() => window.h.liveRequest('held-card'));await note.waitFor();await page.waitForTimeout(400);
  await page.mouse.click(541.5,16);await drop.waitFor();await page.waitForTimeout(420);
  check('clicking the wing holds the visible notice and opens the list', await shown() === 1 && await note.count() === 0);
  check('holding an approval sends no decision', !(await page.evaluate(() => window.results)).some(r=>r.request==='held-card'));
  await page.keyboard.press('Escape');await note.waitFor();await page.waitForTimeout(350);
  await page.locator('.nc-choice .btn-warm').waitFor({state:'visible',timeout:2000});
  await page.screenshot({path:path.join(evidence,'05-restored-approval.png')});
  check('closing the list restores the same queued approval', await page.locator('.nc-choice .btn-warm').isVisible());
  await page.locator('.nc-choice .btn-warm').focus();await page.keyboard.press('Meta+Enter');
  await page.waitForTimeout(160);
  check('an approval never flies before its response succeeds', (await page.locator('.notch').getAttribute('data-flights')) === '0' && await note.count() === 1);
  await page.evaluate(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',metaKey:true,repeat:true,bubbles:true,cancelable:true})));
  await page.keyboard.press('Meta+Enter');
  check('repeat and a second Cmd Enter cannot duplicate a pending request', (await page.evaluate(() => window.results)).filter(r=>r.request==='held-card').length === 1);
  await page.evaluate(() => window.failAnswer(new Error('network unavailable')));await page.waitForTimeout(180);
  check('a failed response keeps the card visible with no flight and a retry message', await note.count() === 1 && (await page.locator('.notch').getAttribute('data-flights')) === '0' && (await page.locator('.nc [role="alert"]').textContent()).includes('Try again'));
  await page.locator('.nc-choice .btn-warm').click();
  await page.evaluate(() => window.finishAnswer(true));
  await page.waitForFunction(() => document.querySelector('.notch').dataset.flights === '1');
  check('a confirmed Allow folds the card and returns its star to Working', await note.count() === 0);
  const exit = await page.locator('.notch-note .notch-pane-in').evaluate(el=>({duration:getComputedStyle(el).transitionDuration,ease:getComputedStyle(el).transitionTimingFunction}));
  check('notice content exits in 98 ms using the reverse easing', exit.duration === '0.098s' && exit.ease === 'cubic-bezier(0.7, 0, 0.84, 0)');
  await page.screenshot({path:path.join(evidence,'04-approved-flight.png')});
  await page.waitForTimeout(1000);
  await page.evaluate(() => window.h.liveRequest('denied-card'));await note.waitFor();await page.waitForTimeout(350);
  await page.mouse.click(541.5,16);await drop.waitFor();await page.waitForTimeout(350);
  await drop.locator('.a-row[data-id="wait"]').click();await note.waitFor();await page.locator('.nc-choice .btn-warm').waitFor({state:'visible',timeout:2000});
  await page.locator('.nc-choice .btn-warm').focus();await page.keyboard.press('Escape');
  check('Escape sends one real Deny even when the list opened the approval', (await page.evaluate(() => window.results)).filter(r=>r.request==='denied-card'&&r.decision==='deny').length === 1);
  await page.evaluate(() => window.finishAnswer(true));
  await page.waitForFunction(() => document.querySelector('.notch').dataset.flights === '1');
  check('a confirmed Deny also returns its star to Working', await note.count() === 0);
  await page.waitForTimeout(1000);
  await drop.waitFor();await page.keyboard.press('Escape');await page.waitForTimeout(400);
  await page.evaluate(() => window.h.liveRequest('stale-card'));await note.waitFor();await page.waitForTimeout(350);
  await page.locator('.nc-choice .btn-warm').click();await page.evaluate(() => window.finishAnswer(false));await page.waitForTimeout(160);
  check('a response already answered elsewhere never plays a success flight', await note.count() === 1 && (await page.locator('.notch').getAttribute('data-flights')) === '0' && (await page.locator('.nc-ok').textContent()).includes('Already answered'));
  check('no browser runtime errors', errors.length === 0);
  writeFileSync(path.join(evidence,'checks.json'),JSON.stringify({checks,errors},null,2));
  console.log(checks.join('\n'));
} finally { await browser.close(); await server.close(); rmSync(harness, { force:true }); }
