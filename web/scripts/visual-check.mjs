/**
 * Visual and layout verification.
 *
 * Three things this proves that no unit test can:
 *   1. Spanish text expansion does not clip or overflow (design system §3.1).
 *   2. Every screen behaves at 320 / 768 / 1280 (design system §7).
 *   3. The sign-in screen renders what the API refused, in the reader's own
 *      language, including the remaining lockout time (ticket 10).
 *
 * Signed-in pages need a session. Pass one with
 * `EAM_USERNAME=... EAM_PASSWORD=... node scripts/visual-check.mjs`; without it
 * those pages are skipped with a note instead of failing.
 *
 * Usage: node scripts/visual-check.mjs [baseUrl]
 * Screenshots land in .scratch/visual/ for human review.
 */

import { mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium, request as playwright_request } from "playwright";

const BASE = process.argv[2] ?? "http://localhost:3000";
const HERE = dirname(fileURLToPath(import.meta.url));
// Repo root, not the web app: screenshots are review artefacts, not build output.
const OUT = join(HERE, "..", "..", ".scratch", "visual");
const API = process.env.EAM_API_URL ?? "http://localhost:8000";

const LOCALES = ["es", "en"];
const PATHS = ["", "/style-guide", "/login"];
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

function expect(condition, message) {
  if (condition) ok(message);
  else fail(message);
}

/**
 * Read an element's text once it has stopped changing.
 *
 * Sampling an alert the moment it appears is a race: React commits the first
 * paint and the formatted detail can land in a later pass, so a single read
 * reports a message the user never sees and a check that fails on a loaded
 * machine. Polling until the text is stable measures what is rendered, not when
 * the assertion happened to run.
 */
async function settledText(locator, timeoutMs = 4000) {
  const deadline = Date.now() + timeoutMs;
  let previous = "";
  while (Date.now() < deadline) {
    const current = await locator.innerText();
    if (current && current === previous) return current;
    previous = current;
    await locator.page().waitForTimeout(100);
  }
  return previous;
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

/**
 * The sign-in form itself: field count, autofill tokens, and the local
 * empty-submit refusal, which must not cost a round trip.
 */
async function checkLoginForm(page, label) {
  const path = join(OUT, `${label}.png`);
  await page.screenshot({ path, fullPage: true });

  const inputs = page.locator("form input");
  expect((await inputs.count()) === 2, `${label}: sign-in form has two fields`);
  expect(
    (await page.locator('input[autocomplete="username"]').count()) === 1,
    `${label}: username field is autofillable`,
  );
  expect(
    (await page.locator('input[autocomplete="current-password"]').count()) === 1,
    `${label}: password field is autofillable`,
  );

  // Empty submit must be refused locally, with the message tied to the field.
  await page.getByRole("button", { name: /entrar|sign in/i }).click();
  await page.waitForTimeout(100);
  const described = await page
    .locator('input[aria-invalid="true"][aria-describedby]')
    .count();
  expect(described === 2, `${label}: both empty fields are marked invalid and described`);
  const stillOnLogin = page.url().includes("/login");
  expect(stillOnLogin, `${label}: empty submit does not leave the sign-in screen`);
}

/**
 * Signs in through the API and returns the session cookie.
 *
 * Playwright's request context has its own cookie jar, so the cookie has to be
 * lifted out and handed to the browser context explicitly. Returns null when no
 * credentials were supplied — the authenticated pages are then skipped with a
 * note rather than silently passing.
 */
async function signIn(request, username, password) {
  const response = await request.post(`${API}/api/v1/auth/login`, {
    data: { username, password },
  });
  if (!response.ok()) {
    throw new Error(`sign-in failed: ${response.status()} ${await response.text()}`);
  }
  const session = await response.json();
  if (session.must_change_password) {
    // An account in the forced-change state is refused everywhere else, so it
    // cannot produce the signed-in screenshots. Say so instead of failing on
    // every page with a redirect.
    console.log(`[note] ${username} must change its password — signed-in pages skipped`);
    return null;
  }
  const { cookies } = await request.storageState();
  const cookie = cookies.find((entry) => entry.name === "eam_session");
  if (!cookie) throw new Error("sign-in succeeded but no session cookie was set");
  return {
    name: "eam_session",
    value: cookie.value,
    domain: new URL(BASE).hostname,
    path: "/",
    httpOnly: true,
    sameSite: "Lax",
  };
}

async function main() {
  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch();

  // Credentials are optional: the public screens are checked either way, and the
  // signed-in ones cannot be reached without them.
  const username = process.env.EAM_USERNAME;
  const password = process.env.EAM_PASSWORD;
  let sessionCookie = null;
  if (username && password) {
    const request = await playwright_request.newContext();
    try {
      sessionCookie = await signIn(request, username, password);
      if (sessionCookie) ok(`signed in as ${username} through the API`);
    } finally {
      await request.dispose();
    }
  } else {
    console.log("[note] EAM_USERNAME/EAM_PASSWORD not set — signed-in pages skipped");
  }

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
        const slug = `${viewport.name}-${locale}${path.replace("/", "-") || "-home"}`;

        // The signed-in pages need a session, which is the same cookie the
        // browser and the Server Components read.
        if (sessionCookie) await context.addCookies([sessionCookie]);
        else await context.clearCookies();

        await checkPage(page, url, label);
        // The sign-in screen gets its own screenshot once the form has been
        // exercised, in `checkLoginForm`.
        if (path !== "/login") {
          await page.screenshot({ path: join(OUT, `${slug}.png`), fullPage: true });
        }
      }

      if (viewport.name === "1280") {
        // No session cookie at all: the shell must not render, and the sign-in
        // screen is where a signed-in page has to land.
        await context.clearCookies();
        await page.goto(`${BASE}/${locale}`, { waitUntil: "networkidle" });
        expect(
          page.url().includes("/login"),
          `${viewport.name}px ${locale}: a signed-in page without a session lands on sign-in`,
        );
      }
    }

    await context.close();
  }

  // Sign-in refusal paths, which need the form to be driven.
  for (const viewport of VIEWPORTS) {
    const context = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      locale: "es-ES",
    });
    const page = await context.newPage();

    for (const locale of LOCALES) {
      const loginLabel = `${viewport.name}px-${locale}-login`;
      await checkPage(page, `${BASE}/${locale}/login`, loginLabel);
      await checkLoginForm(page, loginLabel);
    }

    await context.close();
  }

  // The sign-in screen must say what the API said, in the reader's language:
  // its own catalogued message for the code, not the API's sentence.
  await checkRefusals(browser);

  // Everything past here reads the style guide, which now sits behind the
  // signed-in shell like every other page of the product.
  if (sessionCookie) {
    await checkStyleGuide(browser, sessionCookie);
  } else {
    console.log("[note] signed-in checks skipped (no usable credentials)");
  }

  await browser.close();

  console.log();
  if (failures.length > 0) {
    console.log(`${failures.length} check(s) failed`);
    process.exit(1);
  }
  console.log(`ALL CHECKS PASSED — screenshots in ${OUT}`);
}

/** Text expansion, locale-aware numbers and the component crops. */
async function checkStyleGuide(browser, sessionCookie) {
  // Text expansion: the Spanish page must not be dramatically taller than the
  // English one, which would mean the layout is reflowing badly.
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addCookies([sessionCookie]);
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
    ["sg-states", "detail-states-es"],
    ["sg-dialog", "detail-dialog-es"],
  ];
  for (const [id, name] of details) {
    const section = page.locator(`#${id}`).locator("..");
    if ((await section.count()) > 0) {
      await section.first().screenshot({ path: join(OUT, `${name}.png`) });
    }
  }
  ok(`element crops written for ${details.length} sections`);
  await context.close();
}

/**
 * What a refusal looks like on screen.
 *
 * The API's real envelopes are replayed here — wrong password, lockout — because
 * the question is whether the *screen* renders the catalogued message and the
 * remaining lockout time, not whether the API refuses. The real end-to-end path
 * is driven separately by `scripts/auth-flow-check.mjs`, with a live account.
 */
async function checkRefusals(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();

  const scenarios = [
    {
      name: "login-invalid",
      locale: "es",
      envelope: {
        status: 401,
        code: "ERR_ACC_007",
        message_key: "errors.account_invalid_credentials",
        message: "Usuario o contraseña incorrectos.",
        detail: "password mismatch",
      },
      expect: /Usuario o contraseña incorrectos/,
      expectNot: /Incorrect username or password/,
    },
    {
      name: "login-locked",
      locale: "es",
      envelope: {
        status: 423,
        code: "ERR_AUTH_003",
        message_key: "errors.account_locked",
        message: "La cuenta está bloqueada temporalmente.",
        detail: "locked out; 899 seconds remaining",
      },
      expect: /15 min/,
      expectNot: /seconds remaining/,
    },
    {
      name: "login-invalid-en",
      locale: "en",
      envelope: {
        status: 401,
        code: "ERR_ACC_007",
        message_key: "errors.account_invalid_credentials",
        message: "Usuario o contraseña incorrectos.",
        detail: "password mismatch",
      },
      // English UI, English wording — even though the API's sentence is Spanish.
      expect: /Incorrect username or password/,
      expectNot: /Usuario o contraseña/,
    },
  ];

  for (const scenario of scenarios) {
    await page.route(`${API}/api/v1/auth/login`, (route) =>
      route.fulfill({
        status: scenario.envelope.status,
        contentType: "application/json",
        body: JSON.stringify({
          error: { request_id: null, timestamp: "2026-09-26T00:00:00Z", ...scenario.envelope },
        }),
      }),
    );

    await page.goto(`${BASE}/${scenario.locale}/login`, { waitUntil: "networkidle" });
    await page.locator('input[autocomplete="username"]').fill("empleado");
    await page.locator('input[autocomplete="current-password"]').fill("Wrong!Passw0rd");
    await page.getByRole("button", { name: /entrar|sign in/i }).click();
    await page.locator('[role="alert"]').first().waitFor({ timeout: 5000 });

    const alert = await settledText(page.locator('[role="alert"]').first());
    expect(scenario.expect.test(alert), `${scenario.name}: says "${scenario.expect}"`);
    expect(!scenario.expectNot.test(alert), `${scenario.name}: does not leak the API's wording`);
    expect(
      (await page.locator('input[aria-invalid="true"]').count()) === 0,
      `${scenario.name}: a refused sign-in does not mark the fields as invalid`,
    );
    await page.screenshot({ path: join(OUT, `refusal-${scenario.name}.png`), fullPage: true });
    await page.unroute(`${API}/api/v1/auth/login`);
  }

  await context.close();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
