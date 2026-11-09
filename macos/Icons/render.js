// Renders the 13 committed PNGs from the SVG masters in the folder given as the first argument,
// and writes them into that folder. The build renders nothing: it reads the PNGs. See README.md.
//
//     NODE_PATH=<a folder outside the checkout>/node_modules node macos/Icons/render.js macos/Icons
//
// The app icon: app-N.png, N pixels square, from app.svg, except app-16.png, which comes from the
// hand-hinted app-16.svg. The menu bar glyphs: <name>Template.png at 18 pixels and
// <name>Template@2x.png at 36, black on transparent.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const dir = path.resolve(process.argv[2]);
const jobs = [];
for (const size of [16, 32, 64, 128, 256, 512, 1024]) {
  jobs.push({ src: size === 16 ? 'app-16.svg' : 'app.svg', size, out: `app-${size}.png` });
}
for (const [src, name] of [
  ['menubar.svg', 'MenuBarTemplate'],
  ['menubar-paused.svg', 'MenuBarPausedTemplate'],
  ['menubar-attention.svg', 'MenuBarAttentionTemplate'],
]) {
  jobs.push({ src, size: 18, out: `${name}.png` });
  jobs.push({ src, size: 36, out: `${name}@2x.png` });
}

(async () => {
  const browser = await chromium.launch();
  for (const job of jobs) {
    const page = await browser.newPage({
      viewport: { width: job.size, height: job.size },
      deviceScaleFactor: 1,
    });
    // The master's own width and height give way to the size of this render.
    let svg = fs.readFileSync(path.join(dir, job.src), 'utf8');
    svg = svg
      .replace(/<svg([^>]*?)width="\d+" height="\d+"/, '<svg$1')
      .replace('<svg', `<svg width="${job.size}" height="${job.size}" style="display:block"`);
    await page.setContent(
      `<!doctype html><html><body style="margin:0;background:transparent">${svg}</body></html>`
    );
    await page.screenshot({
      path: path.join(dir, job.out),
      omitBackground: true,
      clip: { x: 0, y: 0, width: job.size, height: job.size },
    });
    await page.close();
  }
  await browser.close();
  console.log('rendered', jobs.length);
})();
