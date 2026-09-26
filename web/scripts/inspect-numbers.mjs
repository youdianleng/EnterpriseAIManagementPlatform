/** Dump what the style guide actually renders for the numeric blocks. */

import { chromium } from "playwright";

const BASE = "http://localhost:3000";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

for (const locale of ["es", "en"]) {
  await page.goto(`${BASE}/${locale}/style-guide`, { waitUntil: "networkidle" });
  await page.waitForTimeout(400);
  // The heading ids sit on the h2, so read the section that labels them.
  const sectionText = async (headingId) =>
    (await page.locator(`section[aria-labelledby="${headingId}"]`).innerText()).replace(/\s+/g, " ");
  console.log(`--- ${locale} typography ---`);
  console.log(await sectionText("sg-typography"));
  console.log(`--- ${locale} table ---`);
  console.log(await sectionText("sg-table"));
  console.log();
}

await browser.close();
