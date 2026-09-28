import {chromium} from 'playwright';
import {createServer} from 'vite';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {writeFileSync,unlinkSync,mkdirSync} from 'node:fs';
import assert from 'node:assert/strict';
// The native docking area has one black, non-interactive receiving drop.
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const port=Number(process.env.DOCKING_DROP_PORT ?? 5243);
const fixture=`.docking-drop-check-${process.pid}.html`;
const filename=path.join(root,fixture), checks=[],errors=[];
const check=(name,value)=>{assert.ok(value,name);checks.push(name)};
writeFileSync(filename,`<html><body style="margin:0;background:#7f98b8"><button id="under" style="position:absolute;left:295px;top:34px;width:50px;height:28px">Target</button><div style="position:absolute;left:227.5px;top:0;width:185px;height:32px;background:#000"></div><div id="root"></div><script type="module">
import React,{useState} from 'react';import {createRoot} from 'react-dom/client';import {DockingDrop} from '/src/DuskDashboard.tsx';import '/src/companion.css';
function App(){const [near,setNear]=useState(false);window.near=setNear;return React.createElement(DockingDrop,{near,width:640,top:32,center:320})};createRoot(document.getElementById('root')).render(React.createElement(App));
</script></body></html>`);
let server,browser;
try{
 server=await createServer({root,server:{host:'127.0.0.1',port,strictPort:true,hmr:false,watch:null},logLevel:'error'});await server.listen();
 browser=await chromium.launch({channel:'chrome',headless:true});const page=await browser.newPage({viewport:{width:640,height:200},deviceScaleFactor:2});page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}/${fixture}`);await page.waitForFunction(()=>document.querySelector('.dashboard-docking-drop path')?.dataset.depth==='0');
 check('idle drop has no painted path',await page.locator('.dashboard-docking-drop path').getAttribute('d')==='');
 await page.evaluate(()=>window.near(true));await page.waitForTimeout(45);
 check('enter grows rather than jumping straight to full depth',await page.locator('.dashboard-docking-drop path').evaluate(p=>Number(p.dataset.depth)>0&&Number(p.dataset.depth)<22));
 await page.waitForFunction(()=>document.querySelector('.dashboard-docking-drop path')?.dataset.depth==='22',null,{timeout:2000});
 const box=await page.locator('.dashboard-docking-drop path').evaluate(p=>{const b=p.getBBox();return{x:b.x,y:b.y,w:b.width,h:b.height,fill:getComputedStyle(p).fill}});
 check('reference curve settles at exactly 22 pt with a 140 pt base',Math.abs(box.h-22)<.001&&box.w===140&&box.x===250);
 check('pure black curve meets the island lower edge without a separate material',box.y===32&&box.fill==='rgb(0, 0, 0)');
 check('drop does not capture pointer hits',await page.evaluate(()=>document.elementFromPoint(320,45)?.id==='under'&&getComputedStyle(document.querySelector('.dashboard-docking-drop')).pointerEvents==='none'));
 check('drop is hidden from accessibility and has no hit region',await page.locator('.dashboard-docking-drop').getAttribute('aria-hidden')==='true'&&await page.locator('.dashboard-docking-drop [data-hit]').count()===0);
 mkdirSync(`${root}/evidence/docking-drop`,{recursive:true});await page.screenshot({path:`${root}/evidence/docking-drop/receiving.png`,clip:{x:200,y:0,width:240,height:85}});
 await page.evaluate(()=>window.near(false));await page.waitForTimeout(45);
 check('leaving reverses toward the island',await page.locator('.dashboard-docking-drop path').evaluate(p=>Number(p.dataset.depth)>0&&Number(p.dataset.depth)<22));
 await page.waitForFunction(()=>document.querySelector('.dashboard-docking-drop path')?.getAttribute('d')==='',null,{timeout:2000});
 check('exit clears the painted path completely',await page.locator('.dashboard-docking-drop path').getAttribute('d')==='');
 await page.evaluate(()=>window.near(true));await page.waitForTimeout(30);await page.evaluate(()=>window.near(false));await page.waitForFunction(()=>document.querySelector('.dashboard-docking-drop path')?.getAttribute('d')==='',null,{timeout:2000});
 check('quick entry and reversal leave no stale drop',true);
 await page.emulateMedia({reducedMotion:'reduce'});await page.evaluate(()=>window.near(true));await page.waitForFunction(()=>document.querySelector('.dashboard-docking-drop path')?.dataset.depth==='22');
 check('reduced motion reaches 22 pt directly',await page.locator('.dashboard-docking-drop path').getAttribute('data-depth')==='22');
 await page.evaluate(()=>window.near(false));await page.waitForFunction(()=>document.querySelector('.dashboard-docking-drop path')?.getAttribute('d')==='');
 check('reduced motion removes the drop directly',await page.locator('.dashboard-docking-drop path').getAttribute('data-depth')==='0');
 check('no browser errors',errors.length===0);
 writeFileSync(`${root}/evidence/docking-drop/checks.json`,JSON.stringify({checks,errors},null,2));console.log(`${checks.length} docking drop checks passed; ${errors.length} browser errors`);
}finally{await browser?.close();await server?.close();unlinkSync(filename)}
