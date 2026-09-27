// Rebuild the README artwork from privacy-safe crops of actual Jarvis screenshots.
// npm install --prefix /tmp/jarvis-readme-render sharp
// SHARP_MODULE=/tmp/jarvis-readme-render/node_modules/sharp node docs/readme-showcase/render.mjs
// Original captures are intentionally local-only under desktop/resonance/evidence/readme-live.
import { createRequire } from 'node:module';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const sharp = require(process.env.SHARP_MODULE || 'sharp');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const out = path.join(root, 'docs/assets/readme');
const raw = path.join(root, 'desktop/resonance/evidence/readme-live');
await mkdir(out, { recursive: true });
const crops = {
  agents: ['agents.png', 340, 152, 600, 966],
  ball: ['agents.png', 588, 78, 104, 104],
  conversation: ['conversation.png', 340, 586, 600, 600],
  plugins: ['plugins.png', 350, 280, 580, 485],
  usage: ['usage.png', 340, 152, 600, 962],
  project: ['projects.png', 375, 286, 530, 156],
  strip: ['agents.png', 510, 0, 442, 62],
};
// Run --capture-crops only after collecting new native screenshots. Normal rebuilds need no private captures.
if (process.argv.includes('--capture-crops')) {
  for (const [name, [source, left, top, width, height]] of Object.entries(crops)) {
    await sharp(path.join(raw, source)).extract({ left, top, width, height }).webp({ lossless: true }).toFile(path.join(out, `${name}-crop.webp`));
  }
}
const data = {};
for (const key of Object.keys(crops)) data[key] = `data:image/png;base64,${(await sharp(await readFile(path.join(out, `${key}-crop.webp`))).png().toBuffer()).toString('base64')}`;
const esc = s => s.replaceAll('&', '&amp;').replaceAll('<', '&lt;');
const text = (x,y,lines,size=32,color='#B7C0D5',weight=400,leading=1.45) => lines.map((s,i) => `<text x="${x}" y="${y+i*size*leading}" fill="${color}" font-size="${size}" font-weight="${weight}" font-family="Avenir Next, sans-serif">${esc(s)}</text>`).join('');
let serial=0;
const shot=(name,x,y,w,h,r=24) => { const id=`clip${serial++}`; return `<defs><clipPath id="${id}"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${r}"/></clipPath></defs><image xlink:href="${data[name]}" x="${x}" y="${y}" width="${w}" height="${h}" clip-path="url(#${id})"/>`; };
const line=(x,y,w) => `<path d="M${x} ${y}h${w}" stroke="#30384E" stroke-width="2"/>`;
const canvas=(height,body) => `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="1600" height="${height}" viewBox="0 0 1600 ${height}"><rect width="1600" height="${height}" fill="#0C1020"/>${body}</svg>`;
const figures = {
  'agents-live': canvas(1080,
    text(88,173,['Your coding agents.','One place to look.'],72,'#F3F5FD',600,1.18)+
    text(88,390,['Claude Code and Codex, together.','See what is working and what needs you.'],30)+
    line(88,496,680)+
    text(88,557,['Catch the moments that need you.'],31,'#F3F5FD',500)+
    text(88,610,['Waiting sessions rise to the top,','with their project and current status.'],28)+
    text(88,750,['Keep the rest in view.'],31,'#F3F5FD',500)+
    text(88,803,['A small strip of stars lives at the top','of your screen while you work.'],28)+
    shot('strip',88,909,663,93,22)+
    shot('agents',944,138,540,869.4,40)+
    shot('ball',1167,65,94,94,47)+
    text(947,1040,['Live app capture · cropped'],22,'#909CB8')),
  'conversation-plugins-live': canvas(1130,
    text(88,137,['Talk it through. Bring your tools.'],68,'#F3F5FD',600)+
    text(88,210,['A real English conversation, alongside the apps connected to Jarvis.'],30)+
    line(88,259,1424)+
    text(88,331,['Follow the conversation'],36,'#F3F5FD',500)+
    text(858,331,['Connect your everyday apps'],36,'#F3F5FD',500)+
    shot('conversation',88,381,636,636,24)+
    shot('plugins',858,381,636,531.8,24)+
    text(858,1007,['Gmail, Linear, Microsoft and Notion.','Connected in this running installation.'],25)+
    text(88,1091,['Live app captures · cropped'],22,'#909CB8')),
  'usage-projects-live': canvas(1080,
    text(88,174,['Know your limits.','See your work.'],72,'#F3F5FD',600,1.18)+
    text(88,395,['Plan usage, reset times and API spend','in one view, without opening every account.'],29)+
    line(88,498,680)+
    text(88,560,['Claude + Codex + OpenAI'],32,'#F3F5FD',500)+
    text(88,615,['Subscription windows and per-model costs,','read from the live Usage page.'],28)+
    text(88,777,['Project activity, across the week'],32,'#F3F5FD',500)+
    shot('project',88,824,636,187.2,24)+
    shot('usage',944,138,540,865.8,40)+
    shot('ball',1167,65,94,94,47)+
    text(947,1040,['Live app captures · cropped'],22,'#909CB8')),
};
for (const [name,svg] of Object.entries(figures)) {
  await sharp(Buffer.from(svg)).png().toFile(path.join(out, `${name}.png`));
  console.log(`${name}.png`);
}
await writeFile(path.join(out,'provenance.json'), JSON.stringify({
  captured_on:'2026-09-26',source:'Running Jarvis Companion, native app screenshots via Computer Use; live daemon on port 8006. No demo mode.',
  treatment:'Cropped and placed in editorial layouts. UI text and values are unchanged. Rounded crop frames are presentation framing, not new app UI. Conversation was exercised with two English test prompts.',
  privacy:'Source captures stay in ignored evidence/. Only bounded English crops are included; mail, older conversations, other projects and local paths are excluded.',
  crops,
},null,2)+'\n');
