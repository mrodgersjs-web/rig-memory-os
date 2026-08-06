import { chromium, devices } from 'playwright';
import path from 'node:path';
import fs from 'node:fs';

const BASE = 'http://127.0.0.1:4173';
const OUT = path.join(import.meta.dirname ?? '.', 'store-assets');
fs.mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();

async function shot(viewport, label) {
  const ctx = await browser.newContext({ viewport });
  const page = await ctx.newPage();

  await page.goto(BASE);
  await page.waitForSelector('#title:not(.hidden)');
  await page.screenshot({ path: `${OUT}/${label}-1-title.png` });

  await page.click('text=Begin the Night');
  await page.waitForSelector('#game:not(.hidden)');
  await page.waitForSelector('.seat');
  await page.screenshot({ path: `${OUT}/${label}-2-game.png` });

  await page.goto(`${BASE}/privacy.html`);
  await page.waitForSelector('h1');
  await page.screenshot({ path: `${OUT}/${label}-3-privacy.png` });

  await ctx.close();
}

// Google Play feature graphic: full HD landscape (1280×720 recommended; store accepts ≥1024×500)
await shot({ width: 1280, height: 720 }, 'play');

// Apple: max iPhone Pro viewport
await shot({ width: 1290, height: 2796 }, 'apple');

// Apple: 12.9" iPad Pro
await shot({ width: 2048, height: 2732 }, 'ipad');

await browser.close();
console.log('Done. Files:');
fs.readdirSync(OUT).forEach(f => console.log('  ', f));
