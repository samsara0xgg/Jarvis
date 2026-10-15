// Stray HTML in her answer is dealt with where the Mac shows it (rows written before the brain strips it): reducer-free, no window.
//   node scripts/verify-answer-text.mjs
import { register } from 'node:module';
import assert from 'node:assert/strict';
register('data:text/javascript,' + encodeURIComponent(`export async function resolve(s, c, next) {
  try { return await next(s, c); } catch (e) { if (s.startsWith('.') && !/\\.\\w+$/.test(s)) return next(s + '.ts', c); throw e; } }`));
const { visible, plain } = await import('../src/model.ts');
let n = 0;
const check = (name, got, want) => { assert.equal(got, want, name); n++; console.log(`PASS ${name}`); };
check('br in every spelling is a line break', visible('第一行<br>第二行<BR/>第三行<br />末'), '第一行\n第二行\n第三行\n末');
check('other tags go and their text stays', visible("<p>你好</p> <b>x</b> <span style='c'>y</span>"), '你好 x y');
check('a less-than sign and non-HTML angle brackets stay', visible('a < b and <3 and List<String>'), 'a < b and <3 and List<String>');
check('code in backticks keeps its tags', visible('`<br>` stays'), '`<br>` stays');
check('the envelope still works around it', visible('<voice>好的<br>晚安</voice><document>一<br>二</document>'), '一\n二');
check('plain reads it too', plain('**好**<br>的'), '好\n的');
check('a half-streamed br is dropped', visible('好的<br'), '好的');
console.log(`${n} checks passed`);
