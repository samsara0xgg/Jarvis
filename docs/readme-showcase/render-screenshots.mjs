// Pure screenshot composition. No promotional copy or generated UI.
// Uses local native captures; output is for review, not automatic publication.
import {createRequire} from 'node:module';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const require=createRequire(import.meta.url);
const sharp=require(process.env.SHARP_MODULE || 'sharp');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const source=path.join(root,'desktop/resonance/evidence/readme-live');
const out=path.join(source,'dashboard-and-notch.png');
// Keep native pixels at one consistent scale. White is the capture's own backdrop.
const panel = async name => sharp(path.join(source,`${name}.png`)).extract({left:300,top:72,width:680,height:1340}).png().toBuffer();
const notch=await sharp(path.join(source,'notch-current.jpg')).extract({left:330,top:0,width:674,height:80}).png().toBuffer();
await sharp({create:{width:2120,height:1556,channels:3,background:'#ffffff'}}).composite([
  {input:notch,left:723,top:36},
  {input:await panel('home'),left:24,top:168},
  {input:await panel('usage'),left:720,top:168},
  {input:await panel('conversation'),left:1416,top:168},
]).png().toFile(out);
console.log(out);
