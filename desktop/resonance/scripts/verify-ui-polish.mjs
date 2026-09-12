import { _electron as electron } from 'playwright';
import { mkdirSync, writeFileSync } from 'node:fs';
import assert from 'node:assert/strict';
const audit = process.argv.includes('--audit');
const dir = `evidence/polish-${audit ? 'before' : 'after'}`;
mkdirSync(dir, { recursive: true });
const app = await electron.launch({ args: ['.', '--verify'], cwd: process.cwd() });
try {
 const page = await app.firstWindow();
 await page.evaluate(() => localStorage.clear()); await page.reload(); await page.waitForSelector('canvas');
 const errors=[]; page.on('pageerror',e=>errors.push(e.message));
 // Actual renderer on a neutral preview backdrop; native desktop compositing is separate.
 await page.addStyleTag({content:'body{background:#57646d}'});
 await page.emulateMedia({reducedMotion:'reduce'}); await page.mouse.move(1,1);
 await page.screenshot({path: `${dir}/voice.png`});
 await page.emulateMedia({reducedMotion:'no-preference'});
 await page.locator('.voice-pill').click({button:'right'});await page.getByRole('button',{name:'体验通知叠层'}).click();
 await page.getByRole('button',{name:'展开 2 条通知'}).click();await page.waitForTimeout(250);
 await page.getByRole('button',{name:'继续讨论周末徒步路线'}).hover();await page.waitForTimeout(150);
 await page.screenshot({path:`${dir}/reply-hover.png`});
 const typography=await page.locator('.result').first().evaluate(e=>{
  const title=e.querySelector('strong').getBoundingClientRect(), summary=e.querySelector('.summary').getBoundingClientRect();
  const reply=e.querySelector('.result-actions').getBoundingClientRect(), status=e.querySelector('.result-status').getBoundingClientRect();
  return {separateLines:summary.top>=title.bottom,alignedActions:Math.abs(reply.top-status.top)<.1};
 });
 if(!audit){assert.ok(typography.separateLines);assert.ok(typography.alignedActions);}

 await page.evaluate(()=>{
  const el=document.querySelector('.result');window.__replySamples=[];
  const observer=new MutationObserver(()=>{observer.disconnect();const t=performance.now();function sample(){window.__replySamples.push({ms:performance.now()-t,height:el.getBoundingClientRect().height});if(performance.now()-t<280)requestAnimationFrame(sample)}sample()});observer.observe(el,{attributes:true,attributeFilter:['class']});
 });
 await page.getByRole('button',{name:'继续讨论周末徒步路线'}).click();await page.waitForTimeout(300);
 const reply=await page.evaluate(()=>window.__replySamples);
 await page.getByRole('textbox',{name:'回复周末徒步路线的内容'}).fill('第二条路线可以展开说说吗？');
 await page.screenshot({path:`${dir}/reply-open.png`});
 const dims=await page.evaluate(()=>({center:document.querySelector('.wave-button').getBoundingClientRect().toJSON(),icons:[...document.querySelectorAll('.control-row [data-capsule-icon]')].filter(e=>e.dataset.capsuleIcon!=='close').map(e=>e.getBoundingClientRect().width),support:CSS.supports('interpolate-size','allow-keywords')}));
 const replySmooth=reply.some(v=>v.height>56 && v.height<90);
 if(!audit){assert.ok(replySmooth,'reply height must pass through intermediate values');assert.equal(dims.center.width,90);assert.equal(dims.center.height,36);assert.ok(dims.icons.every(n=>n===20));}
 await page.getByRole('textbox',{name:'回复周末徒步路线的内容'}).press('Escape');
 await page.getByRole('button',{name:'收起通知'}).click();await page.waitForTimeout(220);
 await page.getByRole('button',{name:'展开文字输入'}).click();await page.waitForTimeout(250);
 const separatorsAbsent=await page.locator('.control-row').evaluate(e=>['::before','::after'].every(p=>getComputedStyle(e,p).content==='none'));
 if(!audit)assert.ok(separatorsAbsent,'composer has no separators in the outer gaps');
 await page.emulateMedia({reducedMotion:'reduce'});
 assert.equal(await page.locator('.control-row').evaluate(e=>getComputedStyle(e,'::before').transitionDuration),'0s');
 assert.equal(errors.length,0);
 writeFileSync(`${dir}/verification.json`,JSON.stringify({replySmooth,separatorsAbsent,typography,reply,dims,errors},null,2));
 console.log(JSON.stringify({audit,replySmooth,separatorsAbsent,support:dims.support,errors}));
} finally {await app.close()}
