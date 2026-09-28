import { chromium } from 'playwright';
import { createServer } from 'vite';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { writeFileSync, unlinkSync, mkdirSync } from 'node:fs';
import assert from 'node:assert/strict';
// Unsubmitted edits survive a card remount; restoring them never approves or answers.
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const port = Number(process.env.ACTION_DRAFT_PORT ?? 5242);
const fixture = `.action-draft-check-${process.pid}.html`;
const filename = path.join(root, fixture);
const checks = [], errors = [];
const check = (name, value) => { assert.ok(value, name); checks.push(name); };
writeFileSync(filename, `<html><body><div id="root"></div><script type="module">
import React,{useState} from 'react'; import {createRoot} from 'react-dom/client';
import {ActionCard,QuestionCard} from '/src/ActionCard.tsx';
const e=React.createElement,root=createRoot(document.getElementById('root'));
window.jarvis={focus:async()=>{}}; let generation=0;
const card={id:'c1',tool:'email',action:'Send',source:'gmail',letter:true,args:{to:'friend@example.com',subject:'Original subject',body:'Original body'}};
const question={id:'q1',question:'Where and when?',fields:[{label:'Location'},{label:'Priority',choices:['Low','High'],value:'Low'}]};
function Harness({mode}) {
 const [version,setVersion]=useState(0),[action,setAction]=useState({subject:'Saved subject',body:'Saved body'}),[answer,setAnswer]=useState({Location:'Home',Priority:'Low'});
 window.remount=()=>setVersion(v=>v+1);
 const controlled=mode.endsWith('controlled');
 return mode.startsWith('action') ? e(ActionCard,{key:version,card,lang:'en',draft:controlled?action:undefined,onDraft:controlled?value=>{window.drafts.push(value);setAction(value)}:undefined,onDecide:(decision,edits)=>window.results.push({decision,edits})})
 : e(QuestionCard,{key:version,question,lang:'en',draft:controlled?answer:undefined,onDraft:controlled?value=>{window.drafts.push(value);setAnswer(value)}:undefined,onAnswer:answers=>window.results.push(answers)});
}
window.mount=mode=>{window.results=[];window.drafts=[];root.render(e(Harness,{key:++generation,mode}))};
window.mount('action-controlled');
</script></body></html>`);
let server, browser;
try {
 server=await createServer({root,server:{host:'127.0.0.1',port,strictPort:true,hmr:false,watch:null},logLevel:'error'}); await server.listen();
 browser=await chromium.launch({channel:'chrome',headless:true}); const page=await browser.newPage(); page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${port}/${fixture}`); await page.locator('.ac-subject').waitFor();
 check('controlled letter restores subject and body',await page.locator('.ac-subject').inputValue()==='Saved subject'&&await page.locator('.ac-body').inputValue()==='Saved body');
 check('restoring a draft does not emit an edit or decision',await page.evaluate(()=>!results.length&&!drafts.length));
 await page.locator('.ac-body').fill('Edited body');
 check('editing one field preserves the other field in the emitted draft',await page.evaluate(()=>drafts.at(-1).subject==='Saved subject'&&drafts.at(-1).body==='Edited body'));
 await page.evaluate(()=>window.remount()); await page.waitForTimeout(50);
 check('controlled letter draft survives card remount without submission',await page.locator('.ac-body').inputValue()==='Edited body'&&await page.evaluate(()=>results.length===0&&drafts.length===1));
 await page.getByRole('button',{name:'Send'}).click();
 check('letter submits the restored and edited values only on explicit approval',await page.evaluate(()=>results.length===1&&results[0].decision==='accept'&&results[0].edits.subject==='Saved subject'&&results[0].edits.body==='Edited body'));
 await page.evaluate(()=>window.mount('action-default')); await page.waitForTimeout(50);
 check('uncontrolled letter retains original defaults',await page.locator('.ac-subject').inputValue()==='Original subject'&&await page.locator('.ac-body').inputValue()==='Original body');
 await page.locator('.ac-subject').fill('Local edit'); await page.keyboard.press('Enter');
 check('ordinary Enter still cannot approve a letter',await page.evaluate(()=>results.length===0));
 await page.keyboard.press('Meta+Enter');
 check('Cmd Enter approves once with the uncontrolled edit',await page.evaluate(()=>results.length===1&&results[0].edits.subject==='Local edit'));
 await page.evaluate(()=>window.mount('question-controlled')); await page.locator('.qc-input').waitFor();
 check('controlled question restores text and selected choice without submission',await page.locator('.qc-input').inputValue()==='Home'&&await page.getByRole('radio',{name:'Low',exact:true}).getAttribute('aria-checked')==='true'&&await page.evaluate(()=>!results.length&&!drafts.length));
 await page.locator('.qc-input').fill('Office'); await page.getByRole('radio',{name:'High',exact:true}).click(); await page.evaluate(()=>window.remount()); await page.waitForTimeout(50);
 check('question text and choice survive card remount',await page.locator('.qc-input').inputValue()==='Office'&&await page.getByRole('radio',{name:'High',exact:true}).getAttribute('aria-checked')==='true'&&await page.evaluate(()=>results.length===0));
 await page.getByRole('button',{name:'Done'}).click();
 check('question answers exactly the current restored draft',await page.evaluate(()=>results.length===1&&results[0].Location==='Office'&&results[0].Priority==='High'));
 await page.evaluate(()=>window.mount('question-default')); await page.waitForTimeout(50); await page.locator('.qc-input').fill('Local answer'); await page.getByRole('button',{name:'Done'}).click();
 check('uncontrolled question still edits and answers with defaults',await page.evaluate(()=>results.length===1&&results[0].Location==='Local answer'&&results[0].Priority==='Low'));
 check('no browser errors',errors.length===0);
 mkdirSync(`${root}/evidence/action-draft`,{recursive:true}); writeFileSync(`${root}/evidence/action-draft/checks.json`,JSON.stringify({checks,errors},null,2));
 console.log(`${checks.length} action draft checks passed; ${errors.length} browser errors`);
} finally { await browser?.close(); await server?.close(); unlinkSync(filename); }
