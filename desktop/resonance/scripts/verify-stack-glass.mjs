import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
const dir = 'evidence/stack-glass';
mkdirSync(dir, { recursive: true });
const app = await electron.launch({ args: ['.', '--verify'], cwd: process.cwd() });
const checks = [];
const check = (label, pass) => { assert.ok(pass, label); checks.push(label); console.log(`PASS ${label}`); };
try {
 const page = await app.firstWindow(); const errors = [];
 page.on('pageerror', e => errors.push(e.message));
 await app.evaluate(({ ipcMain }) => ipcMain.on('material', (_e, p) => { globalThis.__stackMaterial = p; }));
 await page.evaluate(() => localStorage.clear()); await page.reload(); await page.waitForSelector('canvas');
 // A constant renderer backdrop isolates alpha compositing from wallpaper changes.
 await page.addStyleTag({ content: 'body {background:#509dc4}' });
 await page.getByRole('button', {name:'打开通知，2 条未读示例'}).click(); await page.waitForTimeout(350);
 const inspect = () => page.locator('.result').evaluateAll(es => es.map(e => ({...e.getBoundingClientRect().toJSON(), clip:getComputedStyle(e).clipPath})));
 let cards = await inspect();
 check('rear card has a rounded front-silhouette cutout', cards[0].clip === 'none' && cards[1].clip.startsWith('path(evenodd'));
 let native = await app.evaluate(() => globalThis.__stackMaterial);
 const rear = native.rects.find(r => Math.abs(r.y - cards[1].y)<.1 && Math.abs(r.width - cards[1].width)<.1);
 check('native rear glass receives the same front-card cutout', rear.occlusion && Math.abs(rear.x + rear.occlusion.x - cards[0].x)<.1 && Math.abs(rear.y + rear.occlusion.y - cards[0].y)<.1 && rear.occlusion.width === cards[0].width);
 await page.screenshot({path:`${dir}/stacked.png`});
 // Hiding the rear card should not change any pixel safely inside the front card.
 // Drop shadows are disabled only for this numerical comparison, since rear shadows
 // can legitimately extend into the desktop beyond the front silhouette.
 const shadowStyle = await page.addStyleTag({content:'.result {box-shadow:none!important}'});
 const clip = {x: cards[0].x+32, y:cards[0].y+12, width:cards[0].width-64, height:cards[0].height-24};
 const both = await page.screenshot({clip});
 await page.locator('.result').nth(1).evaluate(e => e.style.visibility='hidden');
 const front = await page.screenshot({clip});
 check('rear card does not darken pixels inside the front card', both.equals(front));
 await page.locator('.result').nth(1).evaluate(e => e.style.removeProperty('visibility'));
 await shadowStyle.evaluate(e=>e.remove());
 await page.getByRole('button',{name:'展开 2 条通知'}).hover(); await page.waitForTimeout(180);
 check('hover still leaves the stack collapsed', await page.locator('.inbox.is-stacked').count()===1);
 await page.getByRole('button',{name:'展开 2 条通知'}).click(); await page.waitForTimeout(350);
 cards = await inspect(); native = await app.evaluate(() => globalThis.__stackMaterial);
 check('expanded cards restore full CSS surfaces', cards.every(c=>c.clip==='none'));
 check('expanded native surfaces have no cutouts', native.rects.every(r=>!r.occlusion));
 await page.mouse.move(1,1); await page.screenshot({path:`${dir}/expanded.png`});
 await page.getByRole('button',{name:'收起为叠层'}).click(); await page.waitForTimeout(350);
 check('collapse restores the rear cutout', (await inspect())[1].clip.startsWith('path(evenodd'));
 await page.getByRole('button',{name:'展开 2 条通知'}).click(); await page.waitForTimeout(300);
 await page.getByRole('button',{name:'标记周末徒步路线已查看'}).click(); await page.waitForTimeout(350);
 check('remaining card becomes a complete front surface', (await inspect()).length===1 && (await inspect())[0].clip==='none');
 check('no renderer errors', errors.length===0);
 writeFileSync(`${dir}/verification.json`,JSON.stringify({checks,rear,errors},null,2));
} finally { await app.close(); }
