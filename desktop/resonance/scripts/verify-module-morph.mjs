import { _electron as electron } from 'playwright';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';

const dir = 'evidence/module-morph';
mkdirSync(dir, { recursive: true });
const app = await electron.launch({ args: ['.', '--verify'] });
const checks = [], traces = [];
const check = (label, value) => { assert.ok(value, label); checks.push(label); console.log(label); };
try {
  const page = await app.firstWindow();
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.getByRole('button', { name: 'Dashboard', exact: true }).click();
  await page.waitForFunction(() => [...document.querySelectorAll('.stack-module')].every(e => e.dataset.motion === 'settled'));
  for (const index of [1, 2]) {
    const trace = await page.evaluate(async index => {
      const card = document.querySelector(`[data-module="${index}"]`);
      const shell = card.closest('.stack-body');
      const surface = card.closest('.dashboard-surface');
      const summary = card.querySelector('.module-summary'), detail = card.querySelector('.module-detail');
      const sample = () => {
        const r = card.getBoundingClientRect();
        return { width: r.width, height: r.height, x: r.x, y: r.y, body: +getComputedStyle(shell).opacity,
          summary: +getComputedStyle(summary).opacity, detail: +getComputedStyle(detail).opacity,
          background: getComputedStyle(card).background, padding: getComputedStyle(surface).paddingTop,
          same: card === document.querySelector(`[data-module="${index}"]`) };
      };
      const initial = sample(), frames = [];
      summary.click();
      const start = performance.now();
      while (performance.now() - start < 1000) { await new Promise(requestAnimationFrame); frames.push(sample()); }
      const expanded = sample();
      surface.querySelector('.dashboard-home-bar').click();
      const returning = [], back = performance.now();
      while (performance.now() - back < 1000) { await new Promise(requestAnimationFrame); returning.push(sample()); }
      return { index, initial, expanded, frames, returning, final: sample() };
    }, index);
    traces.push(trace);
    const frames = [...trace.frames, ...trace.returning];
    check(`module ${index}: same card retained through expansion and return`, frames.every(f => f.same));
    check(`module ${index}: outer content never fades during resize`, frames.every(f => f.body > .98));
    check(`module ${index}: no empty-content interval`, frames.every(f => f.summary + f.detail > .99));
    check(`module ${index}: outer padding stays unchanged`, frames.every(f => f.padding === trace.initial.padding));
    // Quota intentionally blends its material into the outer panel as it grows.
    if (index === 1) check('Codex card retains its background', frames.every(f => f.background === trace.initial.background));
    check(`module ${index}: card grows through intermediate widths`, trace.expanded.width > trace.initial.width * 1.8 && trace.frames.filter(f => f.width > trace.initial.width + 5 && f.width < trace.expanded.width - 5).length >= 4);
    check(`module ${index}: return reaches original geometry`, ['x', 'y', 'width', 'height'].every(k => Math.abs(trace.final[k] - trace.initial[k]) < 1));
  }
  check('no renderer errors', errors.length === 0);
  writeFileSync(`${dir}/verification.json`, JSON.stringify({ checks, traces, errors }, null, 2));
} finally { await app.close(); }
