// Installs the official logo in one place and derives every icon from it.
//
//   npm run brand -- path/to/official-logo.png   # install a new logo (PNG, SVG, JPEG or WebP)
//   npm run brand                                # regenerate icons from the current logo
//
// The logo file is copied unchanged to dist/assets/logo.<ext>; the path is updated in brand.js
// and index.html. Icons keep the logo's aspect ratio (fitted, never stretched or cropped):
// favicon 32 px, apple-touch-icon 180 px, PWA icons 192/512 px and a 512 px maskable icon with
// the logo inside the safe zone. Needs Playwright with Chromium (pre-installed or PLAYWRIGHT_PATH).
import {createRequire} from 'node:module';
import {copyFileSync, readFileSync, writeFileSync, readdirSync, unlinkSync, existsSync} from 'node:fs';
import path from 'node:path';

const dist = path.resolve('dist');
const assets = path.join(dist, 'assets');
const brandFile = path.join(dist, 'brand.js');
const indexFile = path.join(dist, 'index.html');
const types = {'.png': 'image/png', '.svg': 'image/svg+xml', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'};

let logoRel = /LOGO_SRC = '([^']+)'/.exec(readFileSync(brandFile, 'utf8'))[1];
const input = process.argv[2];
if (input) {
  const ext = path.extname(input).toLowerCase();
  if (!types[ext]) throw new Error(`Unsupported logo type ${ext}. Use PNG, SVG, JPEG or WebP.`);
  for (const f of readdirSync(assets)) if (/^logo\.[a-z]+$/.test(f)) unlinkSync(path.join(assets, f));
  copyFileSync(input, path.join(assets, 'logo' + (ext === '.jpeg' ? '.jpg' : ext)));
  const next = 'assets/logo' + (ext === '.jpeg' ? '.jpg' : ext);
  writeFileSync(brandFile, readFileSync(brandFile, 'utf8').replace(/LOGO_SRC = '[^']+'/, `LOGO_SRC = '${next}'`));
  const html = readFileSync(indexFile, 'utf8')
    .replaceAll(logoRel, next)
    .replace(/(<link rel="icon" href="assets\/logo\.[a-z]+" type=")[^"]+"/, `$1${types[ext === '.jpeg' ? '.jpg' : ext]}"`);
  writeFileSync(indexFile, html);
  logoRel = next;
  console.log(`Installed ${input} as dist/${next}.`);
}
const logoPath = path.join(dist, logoRel);
if (!existsSync(logoPath)) throw new Error(`Logo not found at ${logoPath}`);

let pw;
try { pw = (await import('playwright')).chromium; } catch { pw = createRequire(import.meta.url)(process.env.PLAYWRIGHT_PATH || 'playwright').chromium; }
const browser = await pw.launch();
const page = await browser.newPage();
const dataUrl = `data:${types[path.extname(logoPath).toLowerCase()]};base64,${readFileSync(logoPath).toString('base64')}`;
const specs = [
  {file: 'icon-32.png', size: 32, pad: 0, bg: null},
  {file: 'apple-touch-icon.png', size: 180, pad: 0.1, bg: '#FFFFFF'}, // iOS fills transparency with black
  {file: 'icon-192.png', size: 192, pad: 0.04, bg: null},
  {file: 'icon-512.png', size: 512, pad: 0.04, bg: null},
  {file: 'icon-maskable-512.png', size: 512, pad: 0.2, bg: '#FFFFFF'}  // logo kept inside the 80% safe zone
];
const out = await page.evaluate(async ({dataUrl, specs}) => {
  const img = new Image();
  img.src = dataUrl;
  await img.decode();
  const w = img.naturalWidth || 512, h = img.naturalHeight || 512;
  return specs.map(({file, size, pad, bg}) => {
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const ctx = c.getContext('2d');
    ctx.imageSmoothingQuality = 'high';
    if (bg) { ctx.fillStyle = bg; ctx.fillRect(0, 0, size, size); }
    const box = size * (1 - pad * 2);
    const scale = Math.min(box / w, box / h);
    const dw = w * scale, dh = h * scale;
    ctx.drawImage(img, (size - dw) / 2, (size - dh) / 2, dw, dh);
    return {file, data: c.toDataURL('image/png').split(',')[1]};
  });
}, {dataUrl, specs});
for (const {file, data} of out) writeFileSync(path.join(assets, file), Buffer.from(data, 'base64'));
await browser.close();
console.log(`Generated ${out.map(o => o.file).join(', ')} from dist/${logoRel}.`);
