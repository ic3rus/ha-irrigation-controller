// Screenshot the visual harness: every [data-theme] [data-scenario] block of
// visual/harness.html into visual/out/<theme>-<scenario>.png (Story 4.5).
//
// A dev script, not a CI gate: the pictures are looked at, not diffed. Run
// `npm run build` first — the harness loads the committed bundle. Playwright
// and its Chromium come with `@vitest/browser-playwright`, the same browser
// vitest renders in (`npx playwright install chromium` if it is missing).

import { mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { chromium } from "playwright";

const here = dirname(fileURLToPath(import.meta.url));
const harness = pathToFileURL(join(here, "harness.html")).href;
const out = join(here, "out");

await mkdir(out, { recursive: true });

const browser = await chromium.launch({
  // The harness loads the bundle as a module script from file://, which
  // Chromium otherwise blocks as a cross-origin request from an opaque origin.
  args: ["--allow-file-access-from-files"],
});
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 }, deviceScaleFactor: 2 });
  const problems = [];
  page.on("pageerror", (error) => problems.push(`page error: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") {
      problems.push(`console error: ${message.text()}`);
    }
  });

  await page.goto(harness);
  try {
    await page.waitForSelector("body[data-ready]", { timeout: 15000 });
    const ready = await page.getAttribute("body", "data-ready");
    if (ready !== "true") {
      problems.push("not every card drew a document (body[data-ready] is not true)");
    }
  } catch (error) {
    // The harness never signalled: the page and console errors above say why.
    problems.push(`harness not ready: ${error instanceof Error ? error.message : String(error)}`);
  }
  // Let the running scenario's first 1 s tick (and its 1.1 s fill transition) land.
  await page.waitForTimeout(1500);

  const blocks = page.locator("[data-theme] [data-scenario]");
  const count = await blocks.count();
  const written = [];
  for (let index = 0; index < count; index += 1) {
    const block = blocks.nth(index);
    const scenario = await block.getAttribute("data-scenario");
    const theme = await block.locator("xpath=ancestor::*[@data-theme]").getAttribute("data-theme");
    const file = join(out, `${theme}-${scenario}.png`);
    await block.screenshot({ path: file });
    written.push(file);
  }

  for (const file of written) {
    console.log(file);
  }
  if (problems.length > 0) {
    console.error(problems.join("\n"));
    process.exitCode = 1;
  } else if (written.length !== 8) {
    console.error(`expected 8 snapshots (4 scenarios x 2 themes), got ${written.length}`);
    process.exitCode = 1;
  } else {
    console.log(`${written.length} snapshots written to ${out}`);
  }
} finally {
  await browser.close();
}
