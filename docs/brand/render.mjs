// 重新產生網站圖示與分享預覽圖（設計素材有改時才用；網站與每日排程不需要）。
// 需要 Playwright：NODE_PATH="$(npm root -g)" node docs/brand/render.mjs
// 圖示原稿是 web/icons/mark.svg；分享預覽圖的文字與版面在 docs/brand/og.html。
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
const { chromium } = createRequire(import.meta.url)("playwright");
const dir = path.dirname(fileURLToPath(import.meta.url)), web = path.resolve(dir, "../../web");
const proxy = process.env.HTTPS_PROXY ? { server: process.env.HTTPS_PROXY } : undefined;
const browser = await chromium.launch({ proxy });
// [原稿, 寬, 高, 輸出, 背景透明]
const jobs = [["og.html", 1200, 630, "og.png", false], ["icon.html", 192, 192, "icons/icon-192.png", true],
  ["icon.html", 512, 512, "icons/icon-512.png", true], ["icon.html", 180, 180, "icons/apple-touch-icon.png", true],
  ["maskable.html", 512, 512, "icons/icon-maskable-512.png", false]];
for (const [file, width, height, out, transparent] of jobs) {
  const page = await browser.newPage({ viewport: { width, height } });
  await page.goto(`file://${dir}/${file}`, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: path.join(web, out), omitBackground: transparent });
  await page.close();
}
await browser.close();
console.log("已更新 web/og.png 與 web/icons/");
