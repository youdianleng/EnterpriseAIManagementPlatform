/**
 * Visual and layout verification.
 *
 * Two things this proves that no unit test can:
 *   1. Spanish text expansion does not clip or overflow (design system §3.1).
 *   2. Every screen behaves at 320 / 768 / 1280 (design system §7).
 *
 * Usage: node scripts/visual-check.mjs [baseUrl]
 * Screenshots land in .scratch/visual/ for human review.
 */

import { mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const BASE = process.argv[2] ?? "http://localhost:3000";
const HERE = dirname(fileURLToPath(import.meta.url));
// Repo root, not the web app: screenshots are review artefacts, not build output.
const OUT = join(HERE, "..", "..", ".scratch", "visual");

const LOCALES = ["es", "en"];
const PATHS = ["", "/style-guide"];
const VIEWPORTS = [
  { name: "320", width: 320, height: 720 },
  { name: "768", width: 768, height: 900 },
  { name: "1280", width: 1280, height: 900 },
];

/** Everything the shell and the style guide must expose. */
const REQUIRED_SELECTORS = [
  "header a[href]",
  "main h1",
  "[aria-label]",
];

const failures = [];

function fail(message) {
  failures.push(message);
  console.log(`[FAIL] ${message}`);
}

function ok(message) {
  console.log(`[ok  ] ${message}`);
}

async function checkPage(page, url, label) {
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForTimeout(150);

  // 1. Exactly one h1, and no skipped heading levels.
  const headingCounts = await page.evaluate(() => {
    const levels = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")].map((h) =>
      Number(h.tagName[1]),
    );
    let previous = 0;
    let skipped = false;
    for (const level of levels) {
      if (previous && level > previous + 1) skipped = true;
      previous = level;
    }
    return { h1: levels.filter((l) => l === 1).length, skipped, total: levels.length };
  });
  if (headingCounts.h1 !== 1) fail(`${label}: expected exactly 1 h1, found ${headingCounts.h1}`);
  else ok(`${label}: exactly one h1`);
  if (headingCounts.skipped) fail(`${label}: heading levels skip a level`);

  // 2. No page-level horizontal overflow: content must not push the viewport.
  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  if (overflow.scrollWidth > overflow.clientWidth + 1) {
    fail(
      `${label}: page scrolls horizontally (${overflow.scrollWidth} > ${overflow.clientWidth})`,
    );
  } else {
    ok(`${label}: no horizontal page overflow`);
  }

  // 3. Required landmarks exist.
  for (const selector of REQUIRED_SELECTORS) {
    const count = await page.locator(selector).count();
    if (count === 0) fail(`${label}: missing ${selector}`);
  }

  // 4. Every interactive control is reachable and has an accessible name.
  const unnamed = await page.evaluate(() =>
    [...document.querySelectorAll("button, a[href], input, select")]
      .filter((element) => {
        const name =
          element.getAttribute("aria-label") ??
          element.textContent?.trim() ??
          element.getAttribute("placeholder") ??
          "";
        const labelled =
          element.id && document.querySelector(`label[for="${element.id}"]`) !== null;
        return name.length === 0 && !labelled;
      })
      .map((element) => element.outerHTML.slice(0, 80)),
  );
  if (unnamed.length > 0) fail(`${label}: controls without an accessible name: ${unnamed.join(" | ")}`);
  else ok(`${label}: all controls have an accessible name`);

  return headingCounts;
}

async function main() {
  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch();

  for (const viewport of VIEWPORTS) {
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      // Pin the language so a host preference cannot change the outcome.
      locale: "es-ES",
    });
    const page = await context.newPage();

    for (const locale of LOCALES) {
      for (const path of PATHS) {
        const url = `${BASE}/${locale}${path}`;
        const label = `${viewport.name}px ${locale}${path || "/"}`;
        await checkPage(page, url, label);

        const file = join(OUT, `${viewport.name}-${locale}${path.replace("/", "-") || "-home"}.png`);
        await page.screenshot({ path: file, fullPage: true });
      }
    }

    await context.close();
  }

  // Text expansion: the Spanish page must not be dramatically taller than the
  // English one, which would mean the layout is reflowing badly.
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const heights = {};
  for (const locale of LOCALES) {
    await page.goto(`${BASE}/${locale}/style-guide`, { waitUntil: "networkidle" });
    heights[locale] = await page.evaluate(() => document.documentElement.scrollHeight);
  }
  const growth = heights.es / heights.en;
  console.log(`heights: es=${heights.es} en=${heights.en} ratio=${growth.toFixed(3)}`);
  if (growth > 1.25) {
    fail(`Spanish layout is ${((growth - 1) * 100).toFixed(1)}% taller than English (>25%)`);
  } else {
    ok(`text expansion within tolerance (${((growth - 1) * 100).toFixed(1)}% taller)`);
  }

  // Locale-aware number formatting.
  //
  // The failure this guards against: `toLocaleString()` without a locale follows
  // the browser, so a page can show "11,5" in one block and "11.5" in another.
  // For hours and money that is a data-reading hazard, so the two languages must
  // differ in exactly the expected way and each page must be internally consistent.
  const expectations = [
    { locale: "es", present: ["11,5"], absent: ["11.5", "1,111.5"] },
    { locale: "en", present: ["11.5"], absent: ["11,5", "1.111,5"] },
  ];
  for (const expectation of expectations) {
    await page.goto(`${BASE}/${expectation.locale}/style-guide`, { waitUntil: "networkidle" });
    // Read the hydrated DOM, not the SSR response: the table is a client
    // component, so its formatted numbers only exist after hydration.
    // Note the heading id belongs to the h2, so select the section that labels it.
    const sectionText = async (headingId) =>
      (await page.locator(`section[aria-labelledby="${headingId}"]`).innerText()).replace(
        /\s+/g,
        " ",
      );
    const text = await sectionText("sg-typography");
    const tableText = await sectionText("sg-table");
    for (const needle of expectation.present) {
      const inTypography = text.includes(needle);
      const inTable = tableText.includes(needle);
      if (!inTypography && !inTable) {
        fail(`${expectation.locale}: expected "${needle}" in the numeric samples`);
      }
    }
    for (const needle of expectation.absent) {
      if (text.includes(needle) || tableText.includes(needle)) {
        fail(`${expectation.locale}: found "${needle}", which belongs to the other locale`);
      }
    }
    // Internal consistency: both blocks must use the same separator.
    const separator = expectation.locale === "es" ? "," : ".";
    const other = expectation.locale === "es" ? "." : ",";
    const decimalInTypography = /\d+([.,])\d/.exec(text)?.[1];
    const decimalInTable = /\d+([.,])\d/.exec(tableText)?.[1];
    if (decimalInTypography && decimalInTypography !== separator) {
      fail(`${expectation.locale}: typography block used "${decimalInTypography}"`);
    }
    if (decimalInTable && decimalInTable !== separator) {
      fail(`${expectation.locale}: table used "${decimalInTable}"`);
    }
    if (decimalInTypography && decimalInTable && decimalInTypography !== decimalInTable) {
      fail(
        `${expectation.locale}: the page mixes "${decimalInTypography}" and "${decimalInTable}"`,
      );
    }
    ok(`${expectation.locale}: numbers formatted consistently (${other} absent as separator)`);
  }

  // Full-resolution crops: a downscaled full-page shot cannot show whether a
  // swatch is the right lightness or a label has the right weight.
  await page.goto(`${BASE}/es/style-guide`, { waitUntil: "networkidle" });
  const details = [
    ["sg-colours", "detail-colours-es"],
    ["sg-typography", "detail-typography-es"],
    ["sg-buttons", "detail-buttons-es"],
    ["sg-forms", "detail-forms-es"],
    ["sg-table", "detail-table-es"],
  ];
  for (const [id, name] of details) {
    const section = page.locator(`#${id}`).locator("..");
    if ((await section.count()) > 0) {
      await section.first().screenshot({ path: join(OUT, `${name}.png`) });
    }
  }
  ok(`element crops written for ${details.length} sections`);

  await browser.close();

  console.log();
  if (failures.length > 0) {
    console.log(`${failures.length} check(s) failed`);
    process.exit(1);
  }
  console.log(`ALL CHECKS PASSED — screenshots in ${OUT}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
