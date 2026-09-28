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
const PATHS = [
  "",
  "/style-guide",
  "/notifications",
  "/clock",
  "/attendance",
  "/leave",
  "/timesheets",
  "/documents",
  "/qa",
  "/login",
];
const VIEWPORTS = [
  { name: "320", width: 320, height: 720 },
  // 375 as well as 320, because the ticket names 375 explicitly and the timesheet grid
  // is the one screen whose behaviour *changes* at that width rather than merely
  // reflowing: below 768 it is replaced by the "please use a desktop" panel.
  { name: "375", width: 375, height: 812 },
  { name: "768", width: 768, height: 900 },
  { name: "1280", width: 1280, height: 900 },
];

/**
 * The width at which the timesheet grid stops being replaced by the notice.
 *
 * Mirrors the breakpoint in `docs/architecture/frontend-design-system.md` §7 and the
 * `matchMedia` query in `timesheet-screen.tsx`. Named here rather than left implicit so
 * a disagreement between the three is a failing check rather than a quiet one.
 */
const GRID_BREAKPOINT = 768;

/**
 * The week `scripts/timesheet-data-check.mjs` fills, as a `YYYY-MM-DD` Monday.
 *
 * Three weeks back, computed from the components of a local date so the browser's
 * timezone cannot move the day — the same rule `lib/api/timesheets.ts` follows, and for
 * the same reason: a wrong Monday is a 422 from the API.
 */
function fixtureWeek() {
  const today = new Date();
  const monday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 21);
  monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
  const month = `${monday.getMonth() + 1}`.padStart(2, "0");
  const day = `${monday.getDate()}`.padStart(2, "0");
  return `${monday.getFullYear()}-${month}-${day}`;
}

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

  // 5. A `headers` attribute has to point at an id that exists.
  //
  // A table that names its header cells and gets the ids wrong is worse than one that
  // does not name them: a screen reader announces nothing where a label was promised.
  // This is invisible on screen, which is exactly why it is checked.
  const danglingHeaders = await page.evaluate(() => {
    const broken = [];
    for (const cell of document.querySelectorAll("[headers]")) {
      for (const id of (cell.getAttribute("headers") ?? "").split(/\s+/).filter(Boolean)) {
        if (document.getElementById(id) === null) {
          broken.push(`${cell.tagName.toLowerCase()} headers="${id}" has no matching id`);
        }
      }
    }
    return broken;
  });
  if (danglingHeaders.length > 0) {
    fail(`${label}: table cells point at headers that do not exist: ${danglingHeaders.join(" | ")}`);
  }

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
    // `url`, not `domain`. A cookie stored with `domain: "localhost"` is in the
    // jar and is never sent — the signed-in pages then rendered the sign-in
    // screen while every check still passed, because a sign-in screen satisfies
    // "one h1, no overflow, every control named". Host-only is what the API
    // itself sets, so this is also the closer reproduction of a real browser.
    url: BASE,
    httpOnly: true,
    sameSite: "Lax",
  };
}

/**
 * The weekly timesheet grid (ticket 28).
 *
 * `PATHS` already covers its headings, overflow and accessible names in both languages
 * at every width, and the loop above asserts the 375px gate. What is asserted here is
 * what the screen is *for*, and each one is a rule from the design system rather than a
 * preference:
 *
 *   - seven columns, Monday to Sunday (§6.1: a week is seven days);
 *   - a day total and a week total on screen (§6.1: live totals);
 *   - tabular numerals on the numbers (§2.3: `7,5` and `11,5` must align by place value,
 *     which is the difference between reading 11,5 and 1,5);
 *   - a cell that Enter opens and Escape closes with the focus handed back (§6.1:
 *     "filling a week with a mouse is torture" — so the grid must be reachable by Tab
 *     and operable by Enter, and the focus must not be lost when the editor closes);
 *   - and, in Spanish, an over-budget notice that is present *without* the minutes
 *     changing, on a week that genuinely has one. The warning-not-truncation rule is
 *     asserted against the API in `tests/test_timesheets.py`; what is checked here is
 *     that the screen shows it. `scripts/timesheet-data-check.mjs` fills the week this
 *     reads — nine hours against eight expected — so the notice is read off a week that
 *     has one rather than waited for.
 *
 * **Two weeks, on purpose.** The structure and the keyboard belong to a week somebody can
 * still write in, and the current week is empty and editable. The warning belongs to a
 * week that has a long day, which is the filed fixture week — a filed week has no cells
 * to Tab into, so one week cannot answer both questions.
 */
async function checkTimesheets(browser, sessionCookie) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addCookies([sessionCookie]);
  const page = await context.newPage();

  // Addressed by a data attribute rather than by a caption or a section: the
  // submission-history table is nested inside the same section, and the caption is in
  // Spanish, which the English half of this function is not.
  const grid = page.locator('[data-testid="timesheet-grid"]');

  // --- the empty, editable week: structure and keyboard (§6.1) ------------------
  await page.goto(`${BASE}/es/timesheets`, { waitUntil: "networkidle" });
  // The grid is a client component, so it is waited for rather than sampled: on a
  // loaded machine the first commit after `networkidle` can still be the shell, and a
  // column count taken then would describe a page that had not drawn the week.
  await grid.waitFor({ state: "visible", timeout: 30000 });

  const columns = await grid.locator("thead th").count();
  expect(columns === 7, `timesheets: seven day columns, Monday to Sunday (found ${columns})`);

  const dayNames = await grid.locator("thead th").allInnerTexts();
  expect(
    /lunes/i.test(dayNames[0]) && /domingo/i.test(dayNames[6]),
    `timesheets: the columns run Monday to Sunday (${dayNames.map((t) => t.split("\n")[0]).join(", ")})`,
  );

  // Live totals: a day total on every day row, and the week's total in the footer.
  const dayTotals = await grid.locator("tbody tr td:first-child", { hasText: /Total del día/ }).count();
  expect(dayTotals === 7, `timesheets: every day shows its own total (found ${dayTotals})`);
  const footer = (await grid.locator("tfoot").innerText()).replace(/\s+/g, " ");
  expect(/Total de la semana/i.test(footer), `timesheets: the week total is shown ("${footer}")`);

  // Tabular numerals: the totals column must not use proportional digits (§2.3).
  const totalsAreTabular = await grid
    .locator("tbody td:first-child .tabular")
    .evaluateAll((elements) => elements.length > 0 && elements.every((el) => el !== null));
  expect(totalsAreTabular, "timesheets: day totals use tabular numerals");

  // Keyboard: Tab reaches an "add hours" cell, Enter opens its editor, Escape closes it
  // and hands the focus back to the cell it came from.
  const firstCell = grid.getByRole("button", { name: /^Añadir horas:/i }).first();
  await firstCell.focus();
  const focusedBefore = await page.evaluate(() => document.activeElement?.getAttribute("aria-label") ?? "");
  expect(
    /^Añadir horas:/i.test(focusedBefore),
    `timesheets: a cell is focusable by keyboard ("${focusedBefore}")`,
  );

  await page.keyboard.press("Enter");
  const editor = page.locator("form[id^='entry-form-']").first();
  await editor.waitFor({ timeout: 5000 });
  ok("timesheets: Enter on a cell opens its editor");

  // The editor takes the focus itself. Without that, removing the focused cell sends the
  // focus to <body>, Escape reaches nothing and the next Tab restarts at the top of the
  // page — the defect this assertion exists for.
  const focusedField = await page.evaluate(() => {
    const element = document.activeElement;
    return element ? `${element.tagName}:${element.getAttribute("type") ?? ""}` : "none";
  });
  expect(focusedField !== "BODY:", `timesheets: the editor takes the focus (${focusedField})`);

  const labelledFields = await editor.locator("select, input").count();
  const labelled = await editor.locator("label").count();
  expect(
    labelled >= labelledFields,
    `timesheets: every editor field carries a label (${labelled} labels for ${labelledFields} fields)`,
  );

  await page.keyboard.press("Escape");
  await editor.waitFor({ state: "detached", timeout: 5000 });
  const focusedAfter = await page.evaluate(
    () => document.activeElement?.getAttribute("aria-label") ?? "",
  );
  expect(
    /^Añadir horas:/i.test(focusedAfter),
    `timesheets: Escape returns the focus to the cell ("${focusedAfter}")`,
  );

  await page.screenshot({ path: join(OUT, "timesheets-grid-es.png"), fullPage: true });

  // --- the week with a long day: the warning, and the minutes intact -------------
  const week = fixtureWeek();
  await page.goto(`${BASE}/es/timesheets?week=${week}`, { waitUntil: "networkidle" });

  const warning = page.getByText(/por encima de la jornada prevista/i).first();
  // Waited for, not sampled: this notice is rendered by the client component, and the
  // week it belongs to is the one `timesheet-data-check.mjs` fills.
  const noticed = await warning
    .waitFor({ state: "visible", timeout: 30000 })
    .then(() => true)
    .catch(() => false);
  expect(
    noticed && (await warning.count()) > 0,
    `timesheets: the over-budget notice is shown for the week of ${week}`,
  );
  if ((await warning.count()) > 0) {
    const notice = (await warning.innerText()).replace(/\s+/g, " ");
    expect(
      /puedes enviarla igualmente/i.test(notice),
      `timesheets: the notice says the week can still be submitted ("${notice}")`,
    );
    // Nothing was truncated: the nine hours are still on the day, with the excess named
    // beside them — which is the difference between warning and truncating.
    const monday = (
      await grid.locator(`tr[data-day-total="${week}"]`).first().innerText()
    ).replace(/\s+/g, " ");
    expect(
      /Total del día: 9 h/.test(monday),
      `timesheets: the long day kept every minute ("${monday}")`,
    );
    expect(
      /Por encima de lo previsto: 1 h/.test(monday),
      `timesheets: and says by how much ("${monday}")`,
    );
    ok("timesheets: the warning warns and the totals are intact");
  }
  await page.screenshot({ path: join(OUT, "timesheets-over-budget-es.png"), fullPage: true });

  // The narrow widths: the notice, and no grid at all.
  for (const width of [320, 375]) {
    await page.setViewportSize({ width, height: 812 });
    await page.goto(`${BASE}/es/timesheets?week=${week}`, { waitUntil: "networkidle" });
    // Which of the two the screen shows is decided by an effect after the first
    // commit, so counting straight after the navigation can describe a page that has
    // not measured its viewport yet — the grid *and* the notice are both absent for
    // that pass. Waiting for whichever one is going to appear is what makes the two
    // counts below describe the rendered screen.
    await page
      .locator('#timesheet-desktop-only, [data-testid="timesheet-grid"]')
      .first()
      .waitFor({ state: "visible", timeout: 20000 });
    const notice = page.locator("#timesheet-desktop-only");
    expect((await notice.count()) === 1, `timesheets ${width}px: the desktop notice is shown`);
    expect(
      (await grid.count()) === 0,
      `timesheets ${width}px: the grid is not squeezed onto the screen`,
    );
    const text = (await notice.innerText()).replace(/\s+/g, " ");
    expect(
      /ordenador/i.test(text),
      `timesheets ${width}px: the notice says what to do instead ("${text.slice(0, 80)}…")`,
    );
    await page.screenshot({ path: join(OUT, `timesheets-${width}-es.png`), fullPage: true });
  }

  // English, at the width the grid is for: the same screen, the other language.
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(`${BASE}/en/timesheets?week=${week}`, { waitUntil: "networkidle" });
  const english = (await grid.innerText()).replace(/\s+/g, " ");
  expect(/Week total/i.test(english), "timesheets en: the totals are in English");
  expect(
    !/Total de la semana/i.test(english),
    "timesheets en: no Spanish leaked into the English screen",
  );
  expect(
    /Monday/i.test(english) && /Sunday/i.test(english),
    "timesheets en: the columns are named in English",
  );
  await page.screenshot({ path: join(OUT, "timesheets-grid-en.png"), fullPage: true });

  await context.close();
}

/**
 * The lock and the supplementary submission (ticket 29).
 *
 * Three questions only a browser can answer, and each is a line of the ticket:
 *
 *   1. **Does a locked week say so?** The week `seed_timesheet_demo.py` approves is
 *      read here, and the notice has to be on screen in words — locked, and how many
 *      weeks of correction are left.
 *   2. **Can the correction be opened from the screen, and does the grid show what it
 *      did?** The form is filled in the browser, and the result is read off the DOM:
 *      an adjustment line marked as one, and a day that now reads
 *      `registrado − ajustado = neto`. The API's own arithmetic is asserted in
 *      `tests/test_timesheet_lock.py`; what is checked here is that a person can see it.
 *   3. **Is the correction offered only inside the window?** A week fourteen weeks back
 *      is outside it: the screen says the window has closed, offers no correction, and
 *      the API refuses a write there with the catalogued, bilingual key — the global
 *      week lock, exercised through the session the browser is signed in with.
 *
 * The fixture week is the one the seed fills, four weeks back. Running this twice is
 * safe: the second run finds a correction already in flight and asserts *that* state
 * instead, which is the other half of the feature.
 */
async function checkLockAndCorrection(browser, sessionCookie, session) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addCookies([sessionCookie]);
  const page = await context.newPage();

  const lockedWeek = weeksBack(4);
  const closedWeek = weeksBack(14);
  const grid = page.locator('[data-testid="timesheet-grid"]');
  const notice = page.locator('[data-testid="timesheet-lock-notice"]');

  await page.goto(`${BASE}/es/timesheets?week=${lockedWeek}`, { waitUntil: "networkidle" });

  // Waited for rather than counted straight away: the grid is a client component, so
  // the first sample after `networkidle` can be the empty shell, and every `count()`
  // below would then describe a page that had not drawn the week yet.
  await notice.first().waitFor({ state: "visible", timeout: 20000 });
  expect((await notice.count()) === 1, "timesheets: the locked week says it is locked");
  const lockText = (await notice.innerText()).replace(/\s+/g, " ");
  expect(
    /bloqueada/i.test(lockText),
    `timesheets: the lock is stated in words ("${lockText.slice(0, 90)}")`,
  );
  expect(
    /semanas más/i.test(lockText),
    `timesheets: the notice names the weeks left to correct it ("${lockText.slice(0, 120)}")`,
  );
  // The locked original's rows are readable and not editable. Which is asserted by
  // *state* rather than absolutely: once a correction is open it is a draft the
  // employee may add to, and the cells then belong to the correction rather than to
  // the week that was signed.
  const correct = page.getByRole("button", { name: /^Corregir esta semana$/i });
  const canCorrect = (await correct.count()) === 1;
  if (canCorrect) {
    const cells = await page.getByRole("button", { name: /^Añadir horas:/i }).count();
    expect(
      cells === 0,
      `timesheets: a locked week with no correction in flight offers no editable cell (${cells})`,
    );
  } else {
    const inFlightHint = (await page.locator("main").innerText()).replace(/\s+/g, " ");
    expect(
      /Corrección en curso/i.test(inFlightHint),
      `timesheets: a correction already in flight is shown ("${inFlightHint.slice(0, 120)}")`,
    );
  }
  await page.screenshot({ path: join(OUT, "timesheets-locked-es.png"), fullPage: true });

  if (canCorrect) {
    await correct.click();
    const form = page.locator("form[aria-labelledby='timesheet-supplement-heading']");
    await form.waitFor({ timeout: 10000 });
    ok("timesheets: a locked week inside the window offers the correction");

    const rows = form.locator('input[type="number"]');
    const rowCount = await rows.count();
    expect(rowCount >= 2, `timesheets: the correction lists the week's entries (${rowCount})`);

    // An empty submission is refused locally, with the reason on screen.
    await form.getByRole("button", { name: /^Enviar la corrección$/i }).click();
    await page.waitForTimeout(150);
    const problem = (await form.innerText()).replace(/\s+/g, " ");
    expect(
      /Cambia al menos una entrada/i.test(problem),
      `timesheets: a correction that changes nothing is refused on screen ("${problem.slice(0, 90)}")`,
    );

    // One real correction: the first entry's minutes change, and the request goes out.
    await rows.first().fill("300");
    await form.getByRole("button", { name: /^Enviar la corrección$/i }).click();
    await page.waitForFunction(() => /Corrección abierta/.test(document.body.innerText), undefined, {
      timeout: 20000,
    });
    ok("timesheets: the correction was opened and the screen said so");
  }

  // What the correction left on screen: an adjustment line, the original untouched,
  // and a day that reads what its net was reached from.
  const adjustments = page.locator('[data-entry-type="reversal"]');
  const adjustmentCount = await adjustments.count();
  expect(
    adjustmentCount >= 1,
    `timesheets: the correction is drawn as an adjustment line (${adjustmentCount})`,
  );
  if (adjustmentCount > 0) {
    const adjustment = (await adjustments.first().innerText()).replace(/\s+/g, " ");
    expect(
      /Línea de ajuste/i.test(adjustment),
      `timesheets: the adjustment is a word as well as a colour ("${adjustment}")`,
    );
    expect(
      /-8 h/.test(adjustment),
      `timesheets: the adjustment carries the original minutes negated ("${adjustment}")`,
    );
    const monday = (
      await grid.locator(`tr[data-day-total="${lockedWeek}"]`).first().innerText()
    ).replace(/\s+/g, " ");
    expect(
      /Total del día: 5 h/.test(monday),
      `timesheets: the day nets 8 h − 8 h + 5 h to 5 h ("${monday}")`,
    );
    expect(
      /Registrado: 13 h − Ajustado: 8 h/.test(monday),
      `timesheets: and says what the net was reached from ("${monday}")`,
    );
    const netTable = page.locator('[data-testid="timesheet-task-net"]');
    expect(
      (await netTable.count()) === 1,
      "timesheets: the net per task is shown once something has been adjusted",
    );
    const sheets = (await page.locator("#timesheet-sheets-heading").locator("..").innerText())
      .replace(/\s+/g, " ");
    expect(
      /Corrección/.test(sheets) && /Semana original/.test(sheets),
      `timesheets: the week lists both sheets and what the correction adjusts ("${sheets.slice(0, 120)}")`,
    );
  }
  await page.screenshot({ path: join(OUT, "timesheets-supplement-es.png"), fullPage: true });

  // English, the same screen: the lock and the adjustment in the other language.
  await page.goto(`${BASE}/en/timesheets?week=${lockedWeek}`, { waitUntil: "networkidle" });
  const englishLock = (await notice.innerText()).replace(/\s+/g, " ");
  expect(/Locked/i.test(englishLock), "timesheets en: the lock is stated in English");
  expect(
    /Adjustment line/.test(await grid.innerText()),
    "timesheets en: the adjustment line is named in English",
  );
  await page.screenshot({ path: join(OUT, "timesheets-locked-en.png"), fullPage: true });

  // --- outside the window: closed to every write ------------------------------
  await page.goto(`${BASE}/es/timesheets?week=${closedWeek}`, { waitUntil: "networkidle" });
  const closedText = (await notice.innerText()).replace(/\s+/g, " ");
  expect(
    /Fuera de plazo/i.test(closedText),
    `timesheets: a week outside the window says so ("${closedText.slice(0, 100)}")`,
  );
  expect(
    (await page.getByRole("button", { name: /^Corregir esta semana$/i }).count()) === 0,
    "timesheets: no correction is offered for a week outside the window",
  );
  expect(
    (await page.getByRole("button", { name: /^Añadir horas:/i }).count()) === 0,
    "timesheets: nor is a cell whose write the API would refuse",
  );
  await page.screenshot({ path: join(OUT, "timesheets-window-closed-es.png"), fullPage: true });

  // The same refusal from the API, through the session the browser is signed in with:
  // the key is the catalogue's, so the sentence is the reader's language and not the
  // server's, and the detail states the weeks that remain.
  const refused = await session.post(`${API}/api/v1/timesheets/submit?week=${closedWeek}`);
  expect(
    refused.status() === 409,
    `timesheets: the API refuses a write in a closed week (${refused.status()})`,
  );
  const envelope = await refused.json();
  expect(
    envelope.error?.message_key === "errors.timesheet_week_closed",
    `timesheets: the refusal is the catalogued window key ("${envelope.error?.message_key}")`,
  );
  expect(
    /0 of 8 weeks remain/.test(envelope.error?.detail ?? ""),
    `timesheets: the refusal names the weeks left ("${envelope.error?.detail}")`,
  );
  ok("timesheets: the global week lock refuses a write, with the bilingual key");

  await context.close();
}

/**
 * The clock (ticket 21).
 *
 * `PATHS` already covers its headings, overflow, control names and both languages at
 * every width. What is asserted here is the ticket's own line — *the employee's state is
 * visible in the interface, live: working and counting, clocked out, or not clocked in
 * yet* — and each assertion is a rule from the design system rather than a preference:
 *
 *   - the state is a **word beside an icon**, not a colour (§5), and it is the API's own
 *     derived status rather than something the screen worked out (`data-day-status`);
 *   - while a shift is open the elapsed time **ticks** — not "is displayed", which a
 *     frozen string would also satisfy;
 *   - clocking out goes through a real `<dialog>` whose **Cancel** leaves the day
 *     untouched, because a punch cannot be edited afterwards;
 *   - a success is confirmed in words (§6.2: the reader closes the page immediately, so
 *     the state change and the notice have to be there before they do);
 *   - the punches list names its source, so a made-up punch is never mistaken for one
 *     somebody clocked;
 *   - and the primary target is at least 44px tall at 320px (§5, touch).
 *
 * **Rerun-safe on purpose.** The clock is always *today*, so the first run drives
 * `absent → working → ok` and a second run finds the day already closed. Rather than
 * failing on its own second run, the sequence is entered when a clock-in button is
 * offered and the closed-day invariants are asserted when it is not — and the run says
 * which of the two it did.
 */
async function checkClock(browser, sessionCookie) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addCookies([sessionCookie]);
  const page = await context.newPage();

  const state = page.locator('[data-testid="clock-state"]');
  const timer = page.locator('[data-testid="clock-elapsed"]');
  const punches = page.locator('[data-testid="clock-punches"]');

  await page.goto(`${BASE}/es/clock`, { waitUntil: "networkidle" });
  await state.waitFor({ state: "visible", timeout: 30000 });

  // 1. A designed state, in words, from the API's own derivation.
  const status = await state.getAttribute("data-day-status");
  expect(
    ["working", "ok", "absent", "missing_out", "incomplete", "holiday", "non_working"].includes(
      status ?? "",
    ),
    `clock: the day reports one of the derived statuses (${status})`,
  );
  const stateText = (await state.innerText()).replace(/\s+/g, " ");
  // The words are the product's own (`dict.dayStatus.label`): `absent` is the API's
  // status for a day with no punches, and the screen states it as `notStarted`. This
  // expectation asserted "Todavía no has fichado", a sentence that exists nowhere in the
  // product, so on a Madrid day with no punches it failed against correct copy.
  const stateWords = {
    absent: /Sin fichajes/,
    working: /Jornada abierta/,
    ok: /Jornada cerrada/,
  };
  expect(
    stateWords[status]?.test(stateText) ?? true,
    `clock: the state is stated in words for "${status}" ("${stateText.slice(0, 90)}")`,
  );
  // The badge is icon + word: the icon is decorative, so the word is what carries it.
  expect(
    (await state.locator("svg").count()) >= 1,
    "clock: the state badge carries an icon as well as its word",
  );

  const clockIn = page.getByRole("button", { name: /^Fichar entrada$/i });
  const clockOut = page.getByRole("button", { name: /^Fichar salida$/i });

  if ((await clockIn.count()) > 0) {
    // The empty state is a designed state, not a blank table (§4.3) — but only a day with
    // nothing on it has one. A closed day also offers "clock in" (a second shift is real),
    // so the empty-state wording is asserted against the table's absence instead of the
    // button's presence.
    if ((await punches.count()) === 0) {
      expect(
        /Todavía no hay fichajes hoy/.test(
          (await page.locator("main").innerText()).replace(/\s+/g, " "),
        ),
        "clock: a day with no punches says so and says what to do",
      );
    }

    await clockIn.click();
    await page.waitForFunction(
      () => document.querySelector('[data-testid="clock-state"]')?.dataset.dayStatus === "working",
      undefined,
      { timeout: 20000 },
    );
    ok("clock: clocking in moves the screen to the open-shift state");

    // §6.2: the reader closes the page immediately, so the confirmation has to be there.
    // Scoped to `main`, because the shell's unread badge is a `role="status"` too and its
    // number would otherwise be read as the confirmation.
    const notice = (
      await page.locator('main [role="status"]').first().innerText()
    ).replace(/\s+/g, " ");
    expect(
      /Entrada fichada a las/i.test(notice),
      `clock: clocking in is confirmed in words ("${notice.slice(0, 80)}")`,
    );
  } else {
    console.log(
      `[note] clock: the day is already "${status}" — the punch sequence is skipped (it is always today)`,
    );
  }

  // 2. While the shift is open the elapsed time has to be *moving*.
  if ((await page.locator('[data-testid="clock-state"][data-day-status="working"]').count()) > 0) {
    await timer.waitFor({ state: "visible", timeout: 10000 });
    const first = await timer.innerText();
    const ticked = await page
      .waitForFunction(
        (previous) => {
          const element = document.querySelector('[data-testid="clock-elapsed"]');
          return element !== null && element.textContent.trim() !== previous;
        },
        first,
        { timeout: 6000 },
      )
      .then(() => true)
      .catch(() => false);
    expect(ticked, `clock: the open shift's timer ticks (was "${first}")`);
    expect(
      /^\d+:\d{2}:\d{2}$/.test((await timer.innerText()).trim()),
      `clock: the timer is H:MM:SS, not a decimal ("${await timer.innerText()}")`,
    );
    await page.screenshot({ path: join(OUT, "clock-working-es.png"), fullPage: true });

    // 3. Clocking out is confirmed, and cancelling it changes nothing.
    await clockOut.click();
    const dialog = page.locator("dialog[open]");
    await dialog.waitFor({ state: "visible", timeout: 5000 });
    const dialogText = (await dialog.innerText()).replace(/\s+/g, " ");
    expect(
      /Se registrará tu salida a las/i.test(dialogText),
      `clock: the confirmation names the time it will record ("${dialogText.slice(0, 100)}")`,
    );
    expect(
      (await dialog.getAttribute("aria-labelledby")) !== null,
      "clock: the confirmation dialog is labelled",
    );
    await page.screenshot({ path: join(OUT, "clock-confirm-es.png") });
    await page.getByRole("button", { name: /^Cancelar$/ }).click();
    await dialog.waitFor({ state: "detached", timeout: 5000 });
    expect(
      (await page.locator('[data-testid="clock-state"][data-day-status="working"]').count()) === 1,
      "clock: cancelling the confirmation leaves the day open",
    );

    await clockOut.click();
    await page.locator("dialog[open]").waitFor({ state: "visible", timeout: 5000 });
    await page.getByRole("button", { name: /^Sí, fichar la salida$/ }).click();
    await page.waitForFunction(
      () => document.querySelector('[data-testid="clock-state"]')?.dataset.dayStatus === "ok",
      undefined,
      { timeout: 20000 },
    );
    ok("clock: clocking out closes the day");
  }

  // 4. The closed day: both punches on screen, with their source named.
  const closed = (await page.locator('[data-testid="clock-state"]').getAttribute("data-day-status")) === "ok";
  if (closed && (await punches.count()) > 0) {
    const rows = await punches.locator("tbody tr").count();
    expect(rows >= 2, `clock: a closed day lists its entry and exit (${rows} rows)`);
    const listed = (await punches.innerText()).replace(/\s+/g, " ");
    expect(/Fichado en la web/.test(listed), `clock: the source of a punch is named ("${listed.slice(0, 90)}")`);
    expect(
      /Salida/.test(listed) && /Entrada/.test(listed),
      "clock: the kind of each punch is named",
    );
    const worked = (await page.locator('[data-testid="clock-worked"]').innerText()).trim();
    expect(
      /\d+ h/.test(worked) || /\d+ min/.test(worked),
      `clock: the day's worked time is a duration, not a decimal ("${worked}")`,
    );
  }
  await page.screenshot({ path: join(OUT, "clock-closed-es.png"), fullPage: true });

  // 5. English: the same screen in the other language.
  await page.goto(`${BASE}/en/clock`, { waitUntil: "networkidle" });
  await state.waitFor({ state: "visible", timeout: 20000 });
  const english = (await page.locator("main").innerText()).replace(/\s+/g, " ");
  expect(
    /(Shift open|Shift closed|You have not clocked in yet)/.test(english),
    "clock en: the state is stated in English",
  );
  expect(
    !/Jornada (abierta|cerrada)/.test(english),
    "clock en: no Spanish leaked into the English screen",
  );
  await page.screenshot({ path: join(OUT, "clock-en.png"), fullPage: true });

  // 6. 320px: the primary target is still a touch target (§5), and nothing overflows.
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto(`${BASE}/es/clock`, { waitUntil: "networkidle" });
  const target = page.locator('[data-testid="clock-state"] a, [data-testid="clock-state"] button').first();
  if ((await target.count()) > 0) {
    const box = await target.boundingBox();
    expect(
      box !== null && box.height >= 44,
      `clock 320px: the primary target is at least 44px tall (${box ? Math.round(box.height) : "none"})`,
    );
  }
  await page.screenshot({ path: join(OUT, "clock-320-es.png"), fullPage: true });

  await context.close();
}

/**
 * The attendance record and the correction flow (ticket 24).
 *
 * `PATHS` already covers the screen's headings, overflow, control names and both languages
 * at every width. What is asserted here is the ticket's own lines:
 *
 *   - **the month is complete** — every day of the month is a row, the days nobody worked
 *     included, because a record that listed only the days somebody punched would answer a
 *     different question;
 *   - **a flagged day says what was flagged** — the status word is the API's derivation, and
 *     the anomaly names beside it come from the day's own record;
 *   - **the chain of same-day corrections is visible, each link naming what it supersedes**
 *     — this is the checklist's explicit line and the reason the panel draws an ordered
 *     evolution rather than one "current value";
 *   - **the correction form is a form** — labelled fields, a refusal that names the missing
 *     piece without costing a round trip, and a filing whose outcome is reported on screen
 *     whether it is accepted or refused;
 *   - and **the record can leave the building**, as the ticket's export line asks.
 *
 * Rerun-safe: a day that already has a correction in flight is refused by the API with its
 * own bilingual code, and the check asserts the refusal is rendered rather than failing on
 * correct behaviour — the same convention `checkDocuments` uses for a duplicate upload.
 */
async function checkAttendance(browser, sessionCookie, session) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addCookies([sessionCookie]);
  const page = await context.newPage();

  const table = page.locator('[data-testid="attendance-month-table"]');
  const panel = page.locator("#attendance-day");
  const chain = page.locator('[data-testid="attendance-chain"]');

  await page.goto(`${BASE}/es/attendance`, { waitUntil: "networkidle" });
  await table.waitFor({ state: "visible", timeout: 30000 });

  // 1. The month is complete: one row per calendar day.
  const rows = await table.locator("tbody tr").count();
  const monthLabel = (await page.locator('[data-testid="attendance-month"]').innerText()).trim();
  expect(rows >= 28 && rows <= 31, `attendance: every day of the month is a row (${rows} for "${monthLabel}")`);

  const statuses = await table.locator("tbody tr td:nth-child(2)").allInnerTexts();
  expect(
    statuses.length === rows && statuses.every((text) => text.trim().length > 0),
    "attendance: every day states its state in words",
  );
  const monthText = (await table.innerText()).replace(/\s+/g, " ");
  expect(
    /(Jornada cerrada|Sin fichajes|Día no laborable)/.test(monthText),
    `attendance: the month shows worked and non-working days ("${monthText.slice(0, 120)}")`,
  );

  // 2. A flagged day carries the anomaly that was found on it, not just its status word.
  //
  // The markers are fetched a round trip *after* the month's first paint (the API serves
  // anomalies one day at a time), so this waits for one rather than sampling the page the
  // moment it settles — otherwise the check reports "nothing was flagged" for a month that
  // was still loading its flags.
  const flagged = page.locator('[data-testid="attendance-day-flags"]');
  const anyFlagged = await flagged
    .first()
    .waitFor({ state: "visible", timeout: 15000 })
    .then(() => true)
    .catch(() => false);
  if (anyFlagged) {
    const text = (await flagged.first().innerText()).replace(/\s+/g, " ");
    expect(text.length > 0, `attendance: a flagged day names its anomaly ("${text}")`);
    const row = flagged.first().locator("xpath=ancestor::tr");
    expect(
      /Falta la salida|Fichaje incompleto/.test(await row.innerText()),
      "attendance: and the row's state agrees that something was flagged",
    );
  } else {
    console.log("[note] attendance: no day carries an anomaly — the flag checks are skipped");
  }

  // 3. The chain: a corrected punch shows its original, its corrections and the value in
  //    force, each correction naming the link it replaced. Found through the API rather
  //    than by guessing which day the fixture happened to correct.
  const applied = await session.get(`${API}/api/v1/attendance/corrections?state=applied&limit=1`);
  const appliedBody = applied.ok() ? await applied.json() : { items: [] };
  const correctedDay = appliedBody.items?.[0]?.business_date;
  if (correctedDay) {
    await page.goto(`${BASE}/es/attendance?month=${correctedDay.slice(0, 7)}&day=${correctedDay}`, {
      waitUntil: "networkidle",
    });
    await chain.first().waitFor({ state: "visible", timeout: 20000 });
    // Every chain in the panel, not just the first: the day's clock-in has no correction and
    // the clock-out does, so reading `.first()` would describe the wrong punch.
    const flat = (await chain.allInnerTexts()).join(" ").replace(/\s+/g, " ");
    expect(/Original/.test(flat), `attendance: the chain shows the original row ("${flat.slice(0, 120)}")`);
    expect(/Corrección/.test(flat), "attendance: the chain shows the correction that restated it");
    expect(
      /Sustituye a/.test(flat),
      `attendance: each correction names what it supersedes ("${flat.slice(0, 160)}")`,
    );
    const effective = (
      await page.locator('[data-testid="attendance-punch-effective"]').first().innerText()
    ).trim();
    expect(
      /Valor vigente:/.test(effective),
      `attendance: the value in force is stated ("${effective}")`,
    );
    expect(
      (await page.locator('[data-testid="attendance-day-anomalies"]').count()) > 0,
      "attendance: the corrected day still lists the anomaly it resolved",
    );
    await page.screenshot({ path: join(OUT, "attendance-chain-es.png"), fullPage: true });
  } else {
    console.log("[note] attendance: no applied correction exists — the chain checks are skipped");
  }

  // 4. The form: labelled fields, a local refusal, and a filing that reports its outcome.
  await page.goto(`${BASE}/es/attendance`, { waitUntil: "networkidle" });
  const correctionForm = page.locator("section:has(#attendance-correction-heading) form").first();
  await correctionForm.waitFor({ state: "visible", timeout: 20000 });

  const dateField = correctionForm.locator('input[type="date"]');
  const timeField = correctionForm.locator('input[type="time"]');
  expect((await dateField.count()) === 1, "attendance: the form has a date field");
  expect((await timeField.count()) === 1, "attendance: the form has a time field");
  expect(
    (await correctionForm.locator("select").count()) === 1,
    "attendance: the form has a kind selector",
  );
  expect(
    (await correctionForm.locator("textarea").count()) === 1,
    "attendance: the form has a reason textarea",
  );
  expect(
    (await correctionForm.locator("label").count()) >= 4,
    "attendance: every form control carries a label",
  );
  expect(
    (await dateField.getAttribute("max")) !== null,
    "attendance: the date field states the bound the API enforces",
  );

  // An empty reason must be refused on screen, with the remedy, and without a round trip.
  await correctionForm.locator("textarea").fill("");
  await correctionForm.getByRole("button", { name: /^Enviar la solicitud$/ }).click();
  await page.waitForTimeout(150);
  const refusedLocally = (await correctionForm.innerText()).replace(/\s+/g, " ");
  expect(
    /Explica por qué hay que corregir/i.test(refusedLocally),
    `attendance: an empty reason is refused on screen ("${refusedLocally.slice(-140)}")`,
  );

  // A real filing. Either it is accepted — and the list gains a document waiting for a
  // decision — or the API refuses it because one is already open for that day, which the
  // screen has to render as a sentence in the reader's language.
  await correctionForm.locator("textarea").fill("Verificacion automatica de la interfaz.");
  await correctionForm.getByRole("button", { name: /^Enviar la solicitud$/ }).click();
  // Scoped to `main`: the shell's unread badge carries `role="status"` as well.
  const outcome = await Promise.race([
    page
      .locator('main [role="status"]')
      .first()
      .waitFor({ state: "visible", timeout: 20000 })
      .then(() => "status"),
    page
      .locator('main [role="alert"]')
      .first()
      .waitFor({ state: "visible", timeout: 20000 })
      .then(() => "alert"),
  ]).catch(() => "none");
  expect(outcome !== "none", "attendance: filing a correction reports its outcome on screen");
  if (outcome === "status") {
    const notice = (
      await page.locator('main [role="status"]').first().innerText()
    ).replace(/\s+/g, " ");
    expect(
      /Solicitud enviada/i.test(notice),
      `attendance: an accepted filing says so ("${notice.slice(0, 90)}")`,
    );
    const list = page.locator('[data-testid="attendance-corrections"] > li');
    await page.waitForFunction(
      () => document.querySelectorAll('[data-testid="attendance-corrections"] > li').length > 0,
      undefined,
      { timeout: 20000 },
    );
    ok(`attendance: the filed request appears in the list (${await list.count()})`);
  } else {
    const alert = (
      await page.locator('main [role="alert"]').first().innerText()
    ).replace(/\s+/g, " ");
    expect(
      alert.length > 20 && !/errors\./.test(alert),
      `attendance: a refused filing is a sentence, not a code ("${alert.slice(0, 140)}")`,
    );
  }
  await page.screenshot({ path: join(OUT, "attendance-form-es.png"), fullPage: true });

  // 5. The export: the record leaves the building as the file the API renders.
  const exportLink = page.locator('a[href*="/attendance/export"]').first();
  expect((await exportLink.count()) === 1, "attendance: the month can be exported");
  if ((await exportLink.count()) === 1) {
    const href = (await exportLink.getAttribute("href")) ?? "";
    const from = /from_date=(\d{4}-\d{2}-\d{2})/.exec(href)?.[1];
    const to = /to_date=(\d{4}-\d{2}-\d{2})/.exec(href)?.[1];
    expect(
      Boolean(from && to && from.endsWith("-01")),
      `attendance: the export covers the month on screen ("${href}")`,
    );
    const file = await session.get(`${API}/api/v1/attendance/export?from_date=${from}&to_date=${to}`);
    expect(file.ok(), `attendance: the export endpoint answers (${file.status()})`);
    const csv = await file.text();
    expect(
      /empleado|employee/.test(csv.split("\n")[0] ?? ""),
      "attendance: the file has the documented bilingual header",
    );
  }

  // 6. English: the same screen in the other language.
  await page.goto(`${BASE}/en/attendance`, { waitUntil: "networkidle" });
  await table.waitFor({ state: "visible", timeout: 20000 });
  const english = (await page.locator("main").innerText()).replace(/\s+/g, " ");
  expect(
    /(Shift closed|No punches|Non-working day)/.test(english),
    "attendance en: the states are in English",
  );
  expect(
    !/Jornada cerrada|Sin fichajes|Día no laborable/.test(english),
    "attendance en: no Spanish leaked into the English screen",
  );
  expect(
    /Request a correction/.test(english),
    "attendance en: the correction form is in English",
  );
  await page.screenshot({ path: join(OUT, "attendance-en.png"), fullPage: true });

  // 7. Narrow: the table drops the minutes column rather than scrolling sideways or
  //    squeezing a state badge onto two lines (§3.1, §7).
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto(`${BASE}/es/attendance`, { waitUntil: "networkidle" });
  await table.waitFor({ state: "visible", timeout: 20000 });
  const visibleColumns = await table.locator("thead th").evaluateAll(
    (cells) => cells.filter((cell) => cell.offsetParent !== null).length,
  );
  expect(
    visibleColumns === 2,
    `attendance 320px: the day and its state are shown, the minutes are dropped (${visibleColumns} columns)`,
  );
  const wrappedBadges = await table
    .locator("tbody tr td:nth-child(2) span")
    .evaluateAll((badges) => badges.filter((badge) => badge.getClientRects().length > 1).length);
  expect(
    wrappedBadges === 0,
    `attendance 320px: no state badge is broken over two lines (${wrappedBadges} wrapped)`,
  );
  await page.screenshot({ path: join(OUT, "attendance-320-es.png"), fullPage: true });

  await context.close();
}

/**
 * Leave: the balances, the request form and the calendar (ticket 25).
 *
 * `PATHS` already covers the screen's headings, overflow, control names and both languages at
 * every width. What is asserted here is the ticket's own lines:
 *
 *   - **the allowance and what is left, per type** — the four figures the API keeps, with the
 *     configured allowance travelling beside them so "22 of what" is answerable;
 *   - **the working-day count is the API's** — the form drafts first and shows the number the
 *     server computed for the range; the check never does the arithmetic itself, it compares
 *     what is on screen with what the API answered for the same dates;
 *   - **the request's status and the decision** — a filed request shows its state in words and
 *     the engine's decisions, level by level, with the approver's comment;
 *   - **the calendar shows the approved absences**, weekends included;
 *   - and **there is no free-text field**, which is the design's rule rather than an omission:
 *     the request body refuses one, so the screen must not offer one.
 *
 * Rerun-safe: the filing is attempted on a range the fixture has not used, and a refusal —
 * which is what an overlapping range produces — is asserted as a rendered sentence rather
 * than allowed to fail the run.
 */
async function checkLeave(browser, sessionCookie, session) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addCookies([sessionCookie]);
  const page = await context.newPage();

  const balances = page.locator('[data-testid="leave-balances"]');
  const form = page.locator("section:has(#leave-request-heading) form").first();
  const requests = page.locator('[data-testid="leave-requests"]');

  await page.goto(`${BASE}/es/leave`, { waitUntil: "networkidle" });
  await balances.waitFor({ state: "visible", timeout: 30000 });

  // 1. The allowance, and what is left of it.
  const balanceText = (await balances.innerText()).replace(/\s+/g, " ");
  for (const label of ["Reconocidos", "Disfrutados", "En trámite", "Disponibles"]) {
    expect(balanceText.includes(label), `leave: the balance shows "${label}" ("${balanceText.slice(0, 90)}")`);
  }
  const allowance = (await page.locator("main h1").locator("xpath=../p[2]").innerText()).replace(
    /\s+/g,
    " ",
  );
  expect(
    /Cupo anual/.test(allowance),
    `leave: the configured allowance is stated ("${allowance}")`,
  );
  expect(
    /Cupo anual/.test(allowance) && /\d/.test(allowance),
    "leave: and it carries the number the API configured",
  );

  // 2. The form asks for a type and two dates, and offers no free text on the request itself.
  expect(
    (await form.locator("select").count()) === 1,
    "leave: the form offers the leave-type catalogue",
  );
  expect(
    (await form.locator('input[type="date"]').count()) === 2,
    "leave: the form asks for a start and an end date",
  );
  expect(
    (await form.locator("textarea").count()) === 0,
    "leave: the form offers no note field, because the API refuses one",
  );
  expect(
    (await form.locator("label").count()) >= 3,
    "leave: every form control carries a label",
  );

  // An empty submission is refused on screen, with the remedy.
  await form.getByRole("button", { name: /^Calcular los días$/ }).click();
  await page.waitForTimeout(150);
  expect(
    /Elige el tipo de permiso/i.test((await form.innerText()).replace(/\s+/g, " ")),
    "leave: an empty form is refused on screen",
  );

  // 3. The draft: the working-day count is the API's, and the screen shows it before filing.
  //
  // A range the fixture has not used, so the draft succeeds on a first run; a second run of
  // this script is refused as an overlap, which is asserted below rather than treated as a
  // failure.
  const stamp = new Date();
  const start = isoDay(new Date(stamp.getFullYear(), stamp.getMonth() + 3, 9));
  const end = isoDay(new Date(stamp.getFullYear(), stamp.getMonth() + 3, 11));
  await form.locator("select").selectOption("annual");
  await form.locator('input[type="date"]').first().fill(start);
  await form.locator('input[type="date"]').nth(1).fill(end);
  await form.getByRole("button", { name: /^Calcular los días$/ }).click();

  const draft = page.locator('[data-testid="leave-draft"]');
  const drafted = await draft
    .waitFor({ state: "visible", timeout: 20000 })
    .then(() => true)
    .catch(() => false);

  if (drafted) {
    const shown = (await draft.innerText()).replace(/\s+/g, " ");
    const computed = Number(/Días laborables de ese periodo: (\d+)/.exec(shown)?.[1] ?? NaN);
    expect(
      Number.isFinite(computed),
      `leave: the API's working-day count is shown ("${shown.slice(0, 90)}")`,
    );
    await page.screenshot({ path: join(OUT, "leave-draft-es.png"), fullPage: true });

    // Filing it: the state moves, and the list says so.
    await form.getByRole("button", { name: /^Enviar a aprobación$/ }).click();
    await page.waitForFunction(() => /Solicitud enviada/.test(document.body.innerText), undefined, {
      timeout: 20000,
    });
    ok("leave: the request was filed and the screen confirmed it");

    // The same question asked of the API about the document that now exists: the number on
    // screen has to be the server's, not a count this check — or the browser — worked out.
    const listed = await session.get(`${API}/api/v1/leave/requests?limit=20`);
    if (listed.ok()) {
      const body = await listed.json();
      const filed = (body.items ?? []).find((item) => item.start_date === start);
      if (filed) {
        expect(
          filed.business_days_count === computed,
          `leave: the count on screen is the API's (${computed} shown, ${filed.business_days_count} stored)`,
        );
      } else {
        console.log("[note] leave: the filed request is not in the list yet — count unchecked");
      }
    }
  } else {
    // A refusal is a designed state here too: an overlapping request is refused by the API,
    // and what the reader gets has to be a sentence about the range rather than a code.
    const refused = (await form.innerText()).replace(/\s+/g, " ");
    expect(
      refused.length > 40 && !/errors\.|ERR_/.test(refused),
      `leave: a refused range is explained in words ("${refused.slice(-140)}")`,
    );
    console.log("[note] leave: the draft was refused (an overlapping request exists) — filing skipped");
  }

  // 4. The list: states in words, the API's day count, and the engine's decisions.
  await page.goto(`${BASE}/es/leave`, { waitUntil: "networkidle" });
  // Direct children only: each request holds a nested list of the engine's decisions, and a
  // bare `li` would count those as requests.
  const requestRows = requests.locator("> li");
  if ((await requests.count()) > 0) {
    const states = await requestRows.evaluateAll((rows) =>
      rows.map((row) => row.getAttribute("data-leave-state") ?? ""),
    );
    expect(
      states.length > 0 && states.every((state) => state.length > 0),
      `leave: every request carries its state (${states.join(", ")})`,
    );
    const listText = (await requests.innerText()).replace(/\s+/g, " ");
    expect(
      /Días laborables: \d+/.test(listText),
      `leave: the list shows the API's day count ("${listText.slice(0, 120)}")`,
    );
    expect(
      /(Borrador sin enviar|Pendiente de aprobación|Aprobada|Rechazada|Retirada)/.test(listText),
      "leave: the states are words, not colours",
    );
    // The decision, with the level it was taken at and the approver's own words. The
    // decisions are read after the first paint (the list endpoint does not carry them), so
    // this waits for them rather than sampling the page the instant it settles.
    const decided = await page
      .waitForFunction(() => /Nivel \d · ronda \d/.test(document.body.innerText), undefined, {
        timeout: 20000,
      })
      .then(() => true)
      .catch(() => false);
    if (decided) {
      const filed = requestRows.filter({ hasText: /Nivel \d/ }).first();
      const decisions = (await filed.innerText()).replace(/\s+/g, " ");
      expect(
        /Nivel \d · ronda \d/.test(decisions) && /(Aprobada|Rechazada|Devuelta)/.test(decisions),
        `leave: a filed request states the decision and its level ("${decisions.slice(0, 160)}")`,
      );
    } else {
      console.log("[note] leave: no request has been decided — the decision checks are skipped");
    }
    await page.screenshot({ path: join(OUT, "leave-requests-es.png"), fullPage: true });
  } else {
    console.log("[note] leave: no leave requests exist — the list checks are skipped");
  }

  // 5. The calendar: the approved absence, on every calendar day it covers.
  const approved = await session.get(`${API}/api/v1/leave/requests?limit=20`);
  const approvedBody = approved.ok() ? await approved.json() : { items: [] };
  const away = (approvedBody.items ?? []).find((item) => item.state === "approved");
  if (away) {
    await page.goto(`${BASE}/es/leave?month=${away.start_date.slice(0, 7)}`, {
      waitUntil: "networkidle",
    });
    const calendar = page.locator('[data-testid="leave-calendar"]');
    await calendar.waitFor({ state: "visible", timeout: 20000 });
    const marked = await calendar.locator('[data-on-leave="true"]').count();
    expect(
      marked >= away.business_days_count,
      `leave: the calendar marks the approved absence on every day it covers (${marked} marked for ${away.business_days_count} working days)`,
    );
    const monthLabel = (await page.locator('[data-testid="leave-calendar-month"]').innerText()).trim();
    expect(
      !/\d{2}\/\d{2}\/\d{4}/.test(monthLabel),
      `leave: the calendar names the month rather than a date ("${monthLabel}")`,
    );
    const summary = (
      await page.locator('[data-testid="leave-calendar-summary"]').innerText()
    ).replace(/\s+/g, " ");
    expect(
      !/no tienes ninguna ausencia/i.test(summary),
      `leave: the month's absences are summarised in words ("${summary}")`,
    );
    await page.screenshot({ path: join(OUT, "leave-calendar-es.png"), fullPage: true });
  } else {
    console.log("[note] leave: no approved leave exists — the calendar checks are skipped");
  }

  // 6. English: the same screen in the other language.
  await page.goto(`${BASE}/en/leave`, { waitUntil: "networkidle" });
  await balances.waitFor({ state: "visible", timeout: 20000 });
  const english = (await page.locator("main").innerText()).replace(/\s+/g, " ");
  expect(/Available/.test(english), "leave en: the balance labels are in English");
  expect(!/Disponibles|En trámite/.test(english), "leave en: no Spanish leaked into the English screen");
  expect(/Request leave/.test(english), "leave en: the form is in English");
  await page.screenshot({ path: join(OUT, "leave-en.png"), fullPage: true });

  await context.close();
}

/** A `YYYY-MM-DD` string for a local date, so a fixture range cannot move by a timezone. */
function isoDay(date) {
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

/** The Monday of the week `value` falls in, as `YYYY-MM-DD`, without a timezone. */
function mondayOf(value) {
  const copy = new Date(value.getFullYear(), value.getMonth(), value.getDate());
  copy.setDate(copy.getDate() - ((copy.getDay() + 6) % 7));
  const month = `${copy.getMonth() + 1}`.padStart(2, "0");
  const day = `${copy.getDate()}`.padStart(2, "0");
  return `${copy.getFullYear()}-${month}-${day}`;
}

/** The Monday `weeks` weeks before this one: the fixture weeks the seed writes. */
function weeksBack(weeks) {
  const today = new Date();
  return mondayOf(new Date(today.getFullYear(), today.getMonth(), today.getDate() - weeks * 7));
}

async function main() {
  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch();

  // Credentials are optional: the public screens are checked either way, and the
  // signed-in ones cannot be reached without them.
  const username = process.env.EAM_USERNAME;
  const password = process.env.EAM_PASSWORD;
  let sessionCookie = null;
  const request = await playwright_request.newContext();
  if (username && password) {
    try {
      sessionCookie = await signIn(request, username, password);
      if (sessionCookie) ok(`signed in as ${username} through the API`);
    } catch (error) {
      console.error(error);
      await request.dispose();
      await browser.close();
      process.exit(1);
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

        // A signed-in page that quietly rendered the sign-in screen passes every
        // check above — one h1, no overflow, every control named — so the session
        // is asserted here rather than assumed. Without this, a cookie the browser
        // refuses to send turns the whole signed-in half into a no-op that reports
        // success.
        if (sessionCookie && path !== "/login") {
          const signedIn = await page.locator('header nav a[href$="/notifications"]').count();
          expect(
            signedIn > 0 && !page.url().includes("/login"),
            `${label}: the shell rendered signed in (cookie accepted)`,
          );
        }

        // The timesheet grid's own gate (design system §7): below the breakpoint the
        // grid must be *absent* and the notice present. Asserted at every width rather
        // than only the narrow ones, because "the grid is missing on a desktop" and
        // "the grid is squeezed onto a phone" are both failures of the same rule.
        if (sessionCookie && path === "/timesheets") {
          const grid = await page.locator('[data-testid="timesheet-grid"]').count();
          const notice = await page.locator("#timesheet-desktop-only").count();
          const expectedGrid = viewport.width >= GRID_BREAKPOINT ? 1 : 0;
          expect(
            grid === expectedGrid,
            `${label}: the grid is ${expectedGrid ? "rendered" : "replaced by the notice"}`,
          );
          expect(
            notice === (expectedGrid ? 0 : 1),
            `${label}: the "use a desktop" notice is ${expectedGrid ? "hidden" : "shown"}`,
          );
        }

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
    await checkNotifications(browser, sessionCookie);
    await checkClock(browser, sessionCookie);
    await checkAttendance(browser, sessionCookie, request);
    await checkLeave(browser, sessionCookie, request);
    await checkTimesheets(browser, sessionCookie);
    await checkLockAndCorrection(browser, sessionCookie, request);
    await checkDocuments(browser, sessionCookie, request);
    await checkQa(browser, sessionCookie, request);
  } else {
    console.log("[note] signed-in checks skipped (no usable credentials)");
  }

  await browser.close();
  // The request context outlives the credential check because the documents half
  // uploads a fixture through it.
  await request.dispose();

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
 * The notification centre.
 *
 * `PATHS` already covers its layout, headings, overflow and accessible names in
 * both languages at all three widths. What is asserted here is what the screen is
 * *for*: a badge whose accessible name carries the count, rows that are list
 * items, and marking read being a real button whose effect is visible in words —
 * plus the text-expansion ratio, because this screen's Spanish copy is longer than
 * its English copy and a reflow would only show up in one of them.
 */
async function checkNotifications(browser, sessionCookie) {
  // Narrow on purpose: this is where the longer Spanish copy wraps and the
  // expansion rule has something to measure. At 1280 every row is one line in
  // both languages and the ratio is 1.000 whatever the wording does.
  const context = await browser.newContext({ viewport: { width: 320, height: 720 } });
  await context.addCookies([sessionCookie]);
  const page = await context.newPage();

  const heights = {};
  for (const locale of LOCALES) {
    await page.goto(`${BASE}/${locale}/notifications`, { waitUntil: "networkidle" });
    heights[locale] = await page.evaluate(() => document.documentElement.scrollHeight);
  }
  const growth = heights.es / heights.en;
  console.log(
    `notifications heights: es=${heights.es} en=${heights.en} ratio=${growth.toFixed(3)}`,
  );
  if (growth > 1.25) {
    fail(`the notification centre is ${((growth - 1) * 100).toFixed(1)}% taller in Spanish (>25%)`);
  } else {
    ok(`notifications: text expansion within tolerance (${((growth - 1) * 100).toFixed(1)}% taller)`);
  }

  await page.goto(`${BASE}/es/notifications`, { waitUntil: "networkidle" });

  const items = page.locator("main ul > li");
  const rows = await items.count();
  if (rows === 0) {
    console.log("[note] nothing in the notification centre — row checks skipped");
  } else {
    ok(`notifications: ${rows} notification(s) rendered as list items`);
  }

  const badge = page.locator('header a[href$="/notifications"] [role="status"]');
  const rowsWithButton = await items.evaluateAll((elements) =>
    elements.findIndex((element) => element.querySelector("button") !== null),
  );

  if (rowsWithButton === -1) {
    console.log("[note] nothing unread — the badge and the read action cannot be exercised");
  } else {
    const label = (await badge.first().getAttribute("aria-label")) ?? "";
    const before = Number(/(\d+)/.exec(label)?.[1] ?? NaN);
    expect(
      Number.isFinite(before),
      `notifications: the badge's accessible name carries the count ("${label}")`,
    );

    // By index, not "the first row that has a button": once this row is read it
    // stops matching that filter and the locator would quietly move to the next
    // unread row, so every assertion below would describe a different row than
    // the one that was clicked.
    const row = items.nth(rowsWithButton);
    await row.getByRole("button", { name: /marcar como leída/i }).click();

    // What the write has to be visible as: this row read, in words, with its
    // action gone. Waiting on the DOM rather than on a timeout is what makes the
    // check about the screen instead of about the machine's speed.
    await page.waitForFunction(
      (index) => {
        const element = document.querySelectorAll("main ul > li")[index];
        if (!element) return false;
        const text = element.textContent ?? "";
        return element.querySelector("button") === null && text.includes("Leída");
      },
      rowsWithButton,
      { timeout: 10000 },
    );

    const text = (await row.innerText()).replace(/\s+/g, " ");
    expect(/\bLeída\b/.test(text), `notifications: the row now says it is read (${text})`);
    expect(!/Marcar como leída/.test(text), "notifications: the action is gone once it is read");

    // The badge is rendered by the shell on the server, so this also proves the
    // refresh landed. Reading it after the wait, not before.
    await page.waitForFunction(
      (expected) => {
        const element = document.querySelector(
          'header a[href$="/notifications"] [role="status"]',
        );
        const found = /(\d+)/.exec(element?.getAttribute("aria-label") ?? "");
        return (found ? Number(found[1]) : 0) === expected;
      },
      before - 1,
      { timeout: 10000 },
    );
    ok(`notifications: the badge drops from ${before} to ${before - 1} when one is read`);
    await page.screenshot({ path: join(OUT, "notifications-read-one-es.png"), fullPage: true });
  }

  await context.close();
}

/**
 * The documents screen (ticket 31).
 *
 * `PATHS` already covers its headings, overflow and accessible names in both languages
 * at every width. What is asserted here is what the screen is *for*, and each one is a
 * line of the ticket rather than a preference:
 *
 *   - **a status and a progress for every document** — the upload answers immediately
 *     with the document in `processing`, so the screen has to say so, and the bar has
 *     to be a real `<progress>` with a text label beside it rather than a coloured
 *     rectangle;
 *   - **the failure reason** — a document that produced no text shows *why*, in the
 *     pipeline's own words (`no text extracted; upload a text version`), which is the
 *     ticket's acceptance criterion for a scanned file and the one thing a reader has
 *     to be able to act on;
 *   - **a labelled upload control** — a file input with a `<label for>`, and a form
 *     that refuses an empty submit locally instead of costing a round trip;
 *   - and **both languages**, because every string on this screen comes from the
 *     catalogue and a missing key would render as an empty box rather than as a
 *     failure.
 *
 * The fixtures are uploaded through the API here rather than committed anywhere: a
 * real text file that the pipeline will take to `ready`, and a genuinely text-free PDF
 * — produced by the same library the server reads it with — which the pipeline must
 * refuse. That is what makes the failure assertion a claim about the product rather
 * than about a mocked status.
 */
async function checkDocuments(browser, sessionCookie, request) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.addCookies([sessionCookie]);
  const page = await context.newPage();

  const list = page.locator('[data-testid="document-list"]');
  const upload = page.locator('input[type="file"]');

  await page.goto(`${BASE}/es/documents`, { waitUntil: "networkidle" });

  // --- the upload control -------------------------------------------------
  expect((await upload.count()) === 1, "documents: there is one file input");
  const fileControl = upload.first();
  expect(
    (await fileControl.getAttribute("aria-describedby")) !== null,
    "documents: the file input is described by its hint",
  );
  const labelled = await page.evaluate(() => {
    const input = document.querySelector('input[type="file"]');
    if (!input) return false;
    if (input.id && document.querySelector(`label[for="${input.id}"]`)) return true;
    return input.closest("label") !== null;
  });
  expect(labelled, "documents: the file input carries a label");
  const accepted = await fileControl.getAttribute("accept");
  expect(
    /\.pdf/.test(accepted ?? "") && /\.docx/.test(accepted ?? ""),
    `documents: the file input names the accepted formats ("${accepted}")`,
  );

  // An empty submit is refused locally, with a message tied to a field.
  await page.getByRole("button", { name: /^subir$|^upload$/i }).first().click();
  await page.waitForTimeout(150);
  const described = await page.locator('[aria-invalid="true"][aria-describedby]').count();
  expect(described >= 1, "documents: an empty submit is refused with a described field");

  // --- a real upload, through the API ------------------------------------
  //
  // The bytes carry the timestamp, so **a second run of this script is a fresh upload
  // rather than a duplicate**. That matters here for a reason worth stating: the
  // product recognises the same content hash and answers 409, which is the behaviour
  // `tests/test_documents.py` pins — and a check that failed on its own second run
  // would be reporting that correct behaviour as a defect. The titles carry the stamp
  // too, so the rows this run created are recognisable in the screenshots.
  const stamp = Date.now();
  const textTitle = `Politica de vacaciones ${stamp}`;
  const scanTitle = `Escaneado ${stamp}`;
  const uploaded = await apiUpload(request, {
    filename: "politica.txt",
    contentType: "text/plain",
    body: `Politica de vacaciones: veintitres dias laborables. Referencia ${stamp}.\n\nAnexo I: permisos.`,
    title: textTitle,
  });
  expect(uploaded.ok(), `documents: the text fixture uploaded (${uploaded.status()})`);
  const scan = await apiUpload(request, {
    filename: "escaneado.pdf",
    contentType: "application/pdf",
    body: scannedPdf(stamp),
    title: scanTitle,
  });
  expect(scan.ok(), `documents: the scanned fixture uploaded (${scan.status()})`);

  // The pipeline is a separate process, so this waits for the row to leave
  // `processing` rather than for a fixed delay: the whole point of the screen is that
  // the status moves, and a timed wait would be a race on a loaded machine.
  await waitForParsed(request, uploaded, 40);
  await waitForParsed(request, scan, 40);

  await page.reload({ waitUntil: "networkidle" });

  const rows = await list.locator("li").count();
  expect(rows >= 2, `documents: the list shows the uploaded documents (found ${rows})`);

  // Every row states its status in words and draws a real progress element.
  const statuses = await page.locator('[data-testid="document-status"]').allInnerTexts();
  expect(
    statuses.length >= 2 && statuses.every((text) => text.trim().length > 0),
    `documents: every document states its status in words (${statuses.join(", ")})`,
  );
  const progress = await list.locator("progress").count();
  expect(progress >= 2, `documents: every document draws its progress (found ${progress})`);
  const progressLabelled = await list
    .locator("progress")
    .evaluateAll((elements) =>
      elements.every((element) => (element.getAttribute("aria-label") ?? "").trim().length > 0),
    );
  expect(progressLabelled, "documents: every progress element has an accessible name");

  // The scanned file failed, and the screen says why — the ticket's own sentence.
  const failure = page.locator('[data-testid="document-failure"]').first();
  expect(
    (await failure.count()) > 0,
    "documents: a document that produced no text shows its failure reason",
  );
  if ((await failure.count()) > 0) {
    const reason = (await failure.innerText()).replace(/\s+/g, " ");
    expect(
      /no text extracted; upload a text version/i.test(reason),
      `documents: the reason is the ticket's sentence ("${reason}")`,
    );
  }

  // The ready one is downloadable, and the link points at the content endpoint.
  const download = list.locator('a[href*="/content"]').first();
  expect((await download.count()) > 0, "documents: a document links to its original");
  if ((await download.count()) > 0) {
    const href = (await download.getAttribute("href")) ?? "";
    expect(
      /\/api\/v1\/documents\/[0-9a-f-]{36}\/content$/.test(href),
      `documents: the download points at the permission-guarded endpoint ("${href}")`,
    );
  }

  await page.screenshot({ path: join(OUT, "documents-es.png"), fullPage: true });

  // --- English, same screen, other language ------------------------------
  await page.goto(`${BASE}/en/documents`, { waitUntil: "networkidle" });
  const english = (await list.innerText()).replace(/\s+/g, " ");
  expect(/Ready/i.test(english) || /Processing failed/i.test(english),
    "documents en: the statuses are in English");
  expect(!/Listo/.test(english), "documents en: no Spanish leaked into the English screen");
  expect(
    (await page.getByRole("button", { name: /^upload$/i }).count()) === 1,
    "documents en: the upload control is in English",
  );
  await page.screenshot({ path: join(OUT, "documents-en.png"), fullPage: true });

  // --- narrow, where Spanish copy wraps ----------------------------------
  await page.setViewportSize({ width: 320, height: 720 });
  await page.goto(`${BASE}/es/documents`, { waitUntil: "networkidle" });
  await page.screenshot({ path: join(OUT, "documents-320-es.png"), fullPage: true });

  await context.close();
}

/**
 * One upload through the API, with the session the browser is using.
 *
 * `fetch` with a `FormData` body from Node would need a `File`; Playwright's request
 * context takes `multipart` directly, which is the same wire shape the browser sends.
 */
function apiUpload(request, { filename, contentType, body, title }) {
  return request.post(`${API}/api/v1/documents`, {
    multipart: {
      file: { name: filename, mimeType: contentType, buffer: Buffer.from(body) },
      title,
      clearance_level: "low",
    },
  });
}

/**
 * Wait until the pipeline has finished with a document.
 *
 * Reads the row rather than a queue: `status` leaves `processing` when the parsing job
 * has written its outcome, so this is the same signal the screen polls on.
 */
async function waitForParsed(request, response, attempts) {
  const { id } = await response.json();
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const read = await request.get(`${API}/api/v1/documents/${id}`);
    if (read.ok()) {
      const document = await read.json();
      if (document.status !== "processing") return document;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return null;
}

/**
 * A PDF with a page and no text at all: the scanned file the ticket refuses.
 *
 * Written by hand rather than committed as a binary, and produced the same way a
 * scanner's output looks to a text extractor — a page whose content stream draws
 * nothing. The server reads it with `pypdf`, which finds no characters, so this is the
 * case rather than a stand-in for it.
 *
 * The `stamp` goes into the document's metadata rather than into a content stream:
 * every run therefore has different bytes — which is what keeps a repeat run a fresh
 * upload rather than a duplicate — while the *pages* stay text-free, which is the
 * property under test. Putting the stamp in the content would make the file parse.
 */
function scannedPdf(stamp) {
  const info = `<< /Producer (visual-check ${stamp}) >>`;
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << >> /Contents 4 0 R >>",
    "<< /Length 0 >>\nstream\n\nendstream",
    info,
  ];
  let body = "%PDF-1.4\n";
  const offsets = [];
  objects.forEach((object, index) => {
    offsets.push(body.length);
    body += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = body.length;
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets) {
    body += `${`${offset}`.padStart(10, "0")} 00000 n \n`;
  }
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R /Info 5 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return body;
}

/**
 * The Q&A screen (ticket 37).
 *
 * `PATHS` already covers its headings, overflow, accessible names and both languages at
 * every width. What is asserted here is what the ticket's checklist lines are about, and
 * each one needs a browser rather than a unit test:
 *
 *   - **the answer streams in**, observed as *several committed states* of the answer text
 *     rather than as a final string. A renderer that buffered the whole answer and drew it
 *     once would satisfy "the text is on screen" and fail this;
 *   - **the model's text is text, not markup** — the fixture document contains
 *     `<img src=x onerror=alert(1)>`, the fake model quotes it verbatim into the answer, and
 *     what is asserted is that it arrives as characters, that no `img` element exists inside
 *     the answer, and that no dialog fired;
 *   - **a citation badge opens its passage**, and "open the original" points at the
 *     document's content route with the page anchor the citation carries;
 *   - **a refusal is rendered distinctly and is not an error** (design system §4.4): its own
 *     block, the catalogue's sentence, and no `role="alert"` inside it;
 *   - **the scope banner** (§5.2/Q29) appears on an answer that quotes a personal upload and
 *     is absent from a refusal;
 *   - **the conversation list** can be renamed and deleted, and a deleted conversation
 *     leaves the list at once;
 *   - **a language switch mid-stream does not disturb the answer**: the question is sent,
 *     the response is deliberately held for a moment, the interface is switched to English
 *     while the request is still in flight, and the answer has to arrive anyway — with no
 *     second request. That is the check the whole module-scope store exists for.
 *
 * The fixture is uploaded through the API, like the documents check's: a real PDF whose
 * page text the pipeline extracts, so the citation's page is a fact the parse recorded
 * rather than a number this script invented. Async-safe to re-run — the title carries a
 * timestamp.
 */
async function checkQa(browser, sessionCookie, request) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    locale: "es-ES",
  });
  await context.addCookies([sessionCookie]);
  const page = await context.newPage();

  // Every question that reaches the API, in order. "The language switch did not re-ask"
  // is a claim about *requests*, and only the network can answer it.
  const asks = [];
  page.on("request", (sent) => {
    if (sent.method() === "POST" && sent.url().endsWith("/api/v1/answers")) asks.push(sent.url());
  });
  // A payload that escapes into HTML runs script or opens a dialog; both are observable,
  // and neither can be caught by looking at the text afterwards.
  let dialogs = 0;
  page.on("dialog", async (dialog) => {
    dialogs += 1;
    await dialog.dismiss().catch(() => {});
  });
  const scriptErrors = [];
  page.on("pageerror", (error) => scriptErrors.push(String(error)));

  // --- the stream instrument, installed before the first navigation --------------
  //
  // **The answer's bytes are delivered in small pieces, and that is the instrument rather
  // than a convenience.** On loopback the whole stream arrives in one TCP read, React
  // commits it once, and no sampling of the DOM can tell a streaming client from a
  // buffering one: the first version of this check observed "2 committed steps" on one run
  // and "1" on the next, which is a measurement of the socket rather than of the screen.
  // Chromium's `Network.emulateNetworkConditions` was tried next and has the same flaw — it
  // delivers in token-bucket bursts, so the deltas still arrived in one read, and it
  // throttles the RSC payload of the following `router.refresh()` too.
  //
  // So the *response body* is wrapped instead: every 8 bytes, 3 ms apart, which is what a
  // slow connection looks like to the code under test and is entirely under this script's
  // control. The application is untouched — it reads a `ReadableStream` either way — and
  // what is recorded is every distinct state a *frame* put on screen. Measured: nine
  // states, each a strict prefix of the last.
  //
  // **`addInitScript` runs on the *next* navigation**, so this is installed here, before
  // the first `page.goto`, and not beside the sampling below — an earlier version installed
  // it after the page had loaded, which meant it was never applied at all and the assertion
  // was measuring the socket again.
  //
  // It wraps **one request**, the streamed question, and nothing else: `POST` is what makes
  // the stream the stream, so the conversation reads, the rename and the delete — which
  // share the path prefix — are left at full speed, exactly as the rest of this check
  // expects them.
  await page.addInitScript(() => {
    const original = window.fetch;
    window.fetch = async (input, init) => {
      const response = await original(input, init);
      const url = typeof input === "string" ? input : (input?.url ?? "");
      const method = (init?.method ?? "GET").toUpperCase();
      if (method !== "POST" || !url.endsWith("/api/v1/answers") || !response.body) {
        return response;
      }
      const reader = response.body.getReader();
      let pending = null;
      const stream = new ReadableStream({
        async pull(controller) {
          if (pending === null) {
            const { done, value } = await reader.read();
            if (done) {
              controller.close();
              return;
            }
            pending = value;
          }
          controller.enqueue(pending.slice(0, 8));
          pending = pending.slice(8);
          if (pending.length === 0) pending = null;
          await new Promise((resolve) => setTimeout(resolve, 3));
        },
      });
      return new Response(stream, { status: response.status, headers: response.headers });
    };
  });

  // --- the fixture: a PDF with the payload in its first sentence ------------------
  //
  // The fake chat model answers by quoting the *first sentence* of the best passage, so the
  // payload has to be inside that sentence or it never reaches the answer — and the document
  // has to share terms with the question, or retrieval refuses instead of answering.
  const stamp = Date.now();
  const question = "¿Cuántos días dura el permiso por matrimonio?";
  const payload = "<img src=x onerror=alert(1)>";
  const uploaded = await apiUpload(request, {
    filename: "politica-matrimonio.pdf",
    contentType: "application/pdf",
    body: textPdf(
      stamp,
      `El permiso por matrimonio dura quince dias laborables ${payload} y la politica de ` +
        "personal lo regula en el convenio colectivo de la empresa.",
      "Anexo: el resto de permisos se solicitan por escrito con quince dias de antelacion.",
    ),
    title: `Politica de matrimonio ${stamp}`,
  });
  expect(uploaded.ok(), `qa: the fixture document uploaded (${uploaded.status()})`);
  const parsed = await waitForParsed(request, uploaded, 60);
  expect(
    parsed !== null && parsed.status === "ready",
    `qa: the fixture parsed (${parsed ? parsed.status : "timed out"})`,
  );

  await page.goto(`${BASE}/es/qa`, { waitUntil: "networkidle" });
  const composer = page.locator("#qa-question");
  await composer.waitFor({ state: "visible", timeout: 30000 });

  // A known starting point. Conversations accumulate across runs — the delete assertion
  // below counts rows, and "N − 1" is only a meaningful claim when N is the number this run
  // created. Clearing them through the API also means the first list assertion describes a
  // sidebar with exactly one conversation in it rather than whatever earlier runs left.
  const existing = await request.get(`${API}/api/v1/answers/conversations?limit=200`);
  const leftovers = existing.ok() ? ((await existing.json()).items ?? []) : [];
  for (const stale of leftovers) {
    await request.delete(`${API}/api/v1/answers/conversations/${stale.id}`);
  }
  if (leftovers.length > 0) {
    ok(`qa: cleared ${leftovers.length} conversation(s) left by earlier runs`);
  }
  await page.reload({ waitUntil: "networkidle" });
  await composer.waitFor({ state: "visible", timeout: 30000 });

  // §5.1's 界面上明确告知该期限: the retention is on the screen, not only in the design.
  const retention = (await page.locator('[data-testid="qa-retention"]').innerText()).replace(
    /\s+/g,
    " ",
  );
  expect(
    /90 días/.test(retention),
    `qa: the 90-day retention is stated on screen ("${retention}")`,
  );

  // The empty thread is a designed state (§4.3), not a blank column.
  const emptyThread = page.locator('[data-testid="qa-thread-empty"]');
  if ((await emptyThread.count()) > 0) {
    ok("qa: a new conversation says what to do first");
  }

  // --- one answer, watched as it arrives ----------------------------------------
  //
  // The sampler records every distinct state of the answer text that a *frame* put on
  // screen. The instrument that makes those states exist is the response-body wrapper
  // installed before the first navigation — see the note there for why neither loopback nor
  // Chromium's throttling can show them.
  await page.evaluate(() => {
    window.__qaGrowth = [];
    window.__qaSampling = true;
    const sample = () => {
      if (!window.__qaSampling) return;
      const element = document.querySelector('[data-testid="qa-answer-text"]');
      if (element) {
        const text = element.innerText;
        if (text && text !== window.__qaGrowth[window.__qaGrowth.length - 1]) {
          window.__qaGrowth.push(text);
        }
      }
      requestAnimationFrame(sample);
    };
    requestAnimationFrame(sample);
  });

  const askButton = page.getByRole("button", { name: /^Preguntar$/ });
  await composer.fill(question);
  await askButton.click();

  const answer = page.locator('[data-testid="qa-answer"][data-answer-phase="complete"]').first();
  await answer.waitFor({ state: "visible", timeout: 60000 });
  const growth = await page.evaluate(() => {
    window.__qaSampling = false;
    return window.__qaGrowth ?? [];
  });

  const answerText = (await page.locator('[data-testid="qa-answer-text"]').first().innerText()).replace(
    /\s+/g,
    " ",
  );
  expect(
    /matrimonio/i.test(answerText),
    `qa: the answer quotes the fixture passage ("${answerText.slice(0, 120)}")`,
  );

  const finished = growth[growth.length - 1] ?? "";
  const incremental = growth.filter((value) => value.length < finished.length);
  const prefixes = growth.every(
    (value, index) => index === 0 || value.startsWith(growth[index - 1]),
  );
  console.log(`[note] qa: ${growth.length} rendered state(s) of the answer were observed`);
  expect(
    prefixes,
    "qa: the answer's rendered states are successive prefixes of one another",
  );
  expect(
    incremental.length >= 1,
    `qa: the answer was rendered incrementally (${incremental.length} partial state(s) of ` +
      `${growth.length} seen, over a throttled connection)`,
  );
  expect(
    finished.replace(/\s+/g, " ").includes(payload),
    "qa: the observed states do not end at the finished answer",
  );

  // --- the payload arrived as text ---------------------------------------------
  expect(
    answerText.includes(payload),
    `qa: the passage's markup is not on screen as text ("${answerText.slice(0, 200)}")`,
  );
  expect(
    (await page.locator('[data-testid="qa-answer-text"] img').count()) === 0,
    "qa: an <img> from the corpus was rendered as an element",
  );
  expect(
    (await page.locator('[data-testid="qa-answer-text"] script').count()) === 0,
    "qa: a <script> from the corpus was rendered as an element",
  );
  expect(dialogs === 0, `qa: the answer opened ${dialogs} dialog(s)`);
  expect(scriptErrors.length === 0, `qa: script errors: ${scriptErrors.join(" | ")}`);

  // --- the scope banner --------------------------------------------------------
  const banner = page.locator('[data-testid="qa-scope-banner"]');
  expect(
    (await banner.count()) === 1,
    "qa: an answer quoting a personal document carries the scope banner",
  );
  if ((await banner.count()) === 1) {
    const text = (await banner.innerText()).replace(/\s+/g, " ");
    expect(
      /documento personal/i.test(text) && /base de conocimiento de la empresa/i.test(text),
      `qa: the banner is the server's own sentence in the reader's language ("${text}")`,
    );
  }
  await page.screenshot({ path: join(OUT, "qa-answer-es.png"), fullPage: true });

  // --- the citation badge and its panel ----------------------------------------
  const badge = page.locator('[data-testid="qa-citation-badge"]').first();
  expect((await badge.count()) > 0, "qa: the answer carries a clickable citation badge");
  if ((await badge.count()) > 0) {
    const label = (await badge.getAttribute("aria-label")) ?? "";
    expect(/cita/i.test(label), `qa: the badge has an accessible name ("${label}")`);

    await badge.click();
    const panel = page.locator('[data-testid="qa-citation-panel"]');
    await panel.waitFor({ state: "visible", timeout: 10000 });
    const panelText = (await panel.innerText()).replace(/\s+/g, " ");
    expect(
      /matrimonio/i.test(panelText),
      `qa: the panel shows the quoted passage ("${panelText.slice(0, 140)}")`,
    );
    expect(
      (await panel.getAttribute("aria-labelledby")) !== null,
      "qa: the panel is a labelled region",
    );

    const original = (await page.locator('[data-testid="qa-open-original"]').getAttribute("href")) ?? "";
    expect(
      /\/api\/v1\/documents\/[0-9a-f-]{36}\/content#page=\d+$/.test(original),
      `qa: "open the original" points at the cited page ("${original}")`,
    );
    await page.screenshot({ path: join(OUT, "qa-citation-panel-es.png"), fullPage: true });

    // The link is the same route the citation was retrieved through, and the session can
    // still open it — a citation that cannot be opened is not a citation.
    const download = await request.get(original.split("#")[0]);
    expect(download.ok(), `qa: the cited original is still downloadable (${download.status()})`);
  }

  // --- rename ------------------------------------------------------------------
  const rows = page.locator('[data-testid="qa-conversation"]');
  // **Waited for, not counted straight away.** The sidebar is the server's copy and the
  // question's conversation reaches it one `router.refresh()` later — a round trip. A count
  // taken before it lands describes a sidebar the reader has already stopped seeing, and the
  // count is the baseline the delete assertion below is measured against.
  await rows.first().waitFor({ state: "visible", timeout: 30000 });
  const before = await rows.count();
  expect(before >= 1, `qa: the question created a conversation in the list (${before})`);

  const renamed = `Vacaciones y permisos ${stamp}`;
  await rows.first().getByRole("button", { name: /^Renombrar/ }).click();
  await page.getByLabel("Nombre de la conversación").fill(renamed);
  await page.getByRole("button", { name: /^Guardar$/ }).click();
  await page.waitForFunction(
    (title) => document.body.innerText.includes(title),
    renamed,
    { timeout: 20000 },
  );
  // The rename must not move the conversation, and the row it renamed must be the one the
  // next step deletes: "the list is ordered by use" is a rule of the list endpoint, and a
  // rename that reordered it would be a list that reorders itself.
  const top = (await rows.first().innerText()).split("\n")[0].trim();
  expect(
    top === renamed,
    `qa: the renamed conversation stayed at the top of the list ("${top}")`,
  );
  ok("qa: a conversation can be renamed from the list");

  // --- delete ------------------------------------------------------------------
  //
  // Addressed by the row's own conversation id rather than by "the list is one shorter":
  // a count is a claim about everything else the list happens to hold, and a row that
  // leaves and a row that never arrives look identical through it.
  const doomed = rows.first();
  const doomedId = await doomed.getAttribute("data-conversation-id");
  const doomedTitle = (await doomed.innerText()).split("\n")[0].trim();
  await doomed.getByRole("button", { name: /^Eliminar/ }).click();
  const dialog = page.locator("dialog[open]");
  await dialog.waitFor({ state: "visible", timeout: 5000 });
  const dialogText = (await dialog.innerText()).replace(/\s+/g, " ");
  expect(
    /90 días/.test(dialogText),
    `qa: the confirmation says what "deleted" means today ("${dialogText.slice(0, 160)}")`,
  );
  expect(
    dialogText.includes(doomedTitle),
    `qa: the confirmation names the conversation it will delete ("${doomedTitle}" vs "${dialogText.slice(0, 120)}")`,
  );
  await page.screenshot({ path: join(OUT, "qa-delete-confirm-es.png") });
  await page.getByRole("button", { name: /^Sí, eliminar$/ }).click();
  await page.waitForFunction(
    (id) =>
      document.querySelector(
        `[data-testid="qa-conversation"][data-conversation-id="${id}"]`,
      ) === null,
    doomedId,
    { timeout: 20000 },
  );
  expect(
    (await rows.count()) === before - 1,
    `qa: a deleted conversation leaves the caller's list immediately (${before} → ${await rows.count()})`,
  );

  // --- the refusal, as a normal state (design system §4.4) ----------------------
  //
  // A question the corpus shares no term with. Written in Chinese for the reason
  // `tests/test_answer.py` records: the deterministic embedder scores a Spanish question
  // about an uncovered topic by lexical coincidence, and one draft of that test passed the
  // threshold by accident. The Chinese question is reliably "no basis" — and it is one of
  // §5.2's three input languages, so it is a real question rather than a contrivance.
  await composer.fill("公司年会抽奖的奖品清单是什么？");
  await askButton.click();
  const refusal = page.locator('[data-testid="qa-refusal"]').first();
  const refused = await refusal
    .waitFor({ state: "visible", timeout: 30000 })
    .then(() => true)
    .catch(() => false);
  expect(refused, "qa: a question with no basis renders the refusal block");
  if (refused) {
    const text = (await refusal.innerText()).replace(/\s+/g, " ");
    expect(
      /Sin base en la base de conocimiento/.test(text),
      `qa: the refusal has its own heading ("${text.slice(0, 100)}")`,
    );
    expect(
      /No he encontrado base en la base de conocimiento de la empresa/.test(text),
      "qa: the refusal renders the catalogue's sentence, in the reader's language",
    );
    expect(
      (await refusal.locator('[role="alert"]').count()) === 0,
      "qa: the refusal is announced as an answer, not as an error",
    );
    expect(
      (await page.locator('[data-testid="qa-refusal"] [data-testid="qa-scope-banner"]').count()) === 0,
      "qa: a refusal carries no scope banner, because it quotes nothing",
    );
    expect(
      (await page
        .locator('[data-testid="qa-answer"][data-answer-phase="refused"]')
        .count()) >= 1,
      "qa: the refusal is its own phase, not a styled ordinary answer",
    );
    // The bilingual constant is not printed: one language, one sentence.
    expect(
      !/I found no basis in the company knowledge base/.test(text),
      "qa: the bilingual refusal text was printed to a reader of one language",
    );
    await page.screenshot({ path: join(OUT, "qa-refusal-es.png"), fullPage: true });
  }

  // --- switching the language while the answer is still coming -------------------
  //
  // The request is held before it goes out, so the switch genuinely happens *before* the
  // answer exists. If the language switch were still a document navigation, the `fetch`
  // would be aborted with the page and no answer would ever arrive; if the answer lived in
  // component state, the new page would have thrown it away. Either failure is visible here.
  await page.route("**/api/v1/answers", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.continue();
  });

  await page.locator('[data-testid="qa-new-conversation"]').click();
  await composer.fill(question);
  const inFlight = page.waitForRequest(
    (sent) => sent.method() === "POST" && sent.url().endsWith("/api/v1/answers"),
    { timeout: 10000 },
  );
  await askButton.click();
  await inFlight;
  const asksBefore = asks.length;

  await page.locator('a[hreflang="en"]').click();
  await page.waitForURL(/\/en\/qa/, { timeout: 20000 });
  ok("qa: the language switch happened while the question was in flight");

  // Waited for until *complete*, not until visible: the answer is delivered in pieces (see
  // the instrument above), so "there is text on screen" is true while it is still arriving.
  // What this section asserts is that the answer finishes after the switch — which a fetch
  // aborted by a document navigation never does.
  const switched = page
    .locator('[data-testid="qa-answer"][data-answer-phase="complete"]')
    .first();
  const arrived = await switched
    .waitFor({ state: "visible", timeout: 30000 })
    .then(() => true)
    .catch(() => false);
  expect(arrived, "qa: the answer arrived after the language switch");
  if (arrived) {
    const text = (
      await switched.locator('[data-testid="qa-answer-text"]').first().innerText()
    ).replace(/\s+/g, " ");
    expect(
      /matrimonio/i.test(text),
      `qa: the answer survived the switch intact ("${text.slice(0, 120)}")`,
    );
  }
  expect(
    asks.length === asksBefore,
    `qa: switching the language re-asked the question (${asksBefore} → ${asks.length} requests)`,
  );
  const englishShell = (await page.locator('[data-testid="qa-conversation-list"]').innerText()).replace(
    /\s+/g,
    " ",
  );
  expect(
    /Vacaciones y permisos|Politica de matrimonio/.test(englishShell) === false ||
      /matrimonio/i.test(englishShell),
    "qa en: the conversation list is the other language's screen",
  );
  expect(
    /Your conversations/.test(await page.locator("main").innerText()),
    "qa en: the sidebar heading is in English after the switch",
  );
  await page.screenshot({ path: join(OUT, "qa-answer-en.png"), fullPage: true });
  await page.unroute("**/api/v1/answers");

  // --- the narrow widths, with a real answer on screen --------------------------
  for (const width of [320, 768]) {
    await page.setViewportSize({ width, height: width === 320 ? 720 : 900 });
    await page.goto(`${BASE}/es/qa`, { waitUntil: "networkidle" });
    await page.screenshot({ path: join(OUT, `qa-${width}-es.png`), fullPage: true });
  }

  await context.close();
}

/**
 * A PDF whose pages carry `Tj` text, built the long way round.
 *
 * The same structure `tests/support/documents.py::pdf_bytes` uses, and for the same reason:
 * the fixture has to be a file the pipeline parses into *paged* chunks, so the citation's
 * page — and therefore the `#page=N` the "open the original" link carries — is a fact the
 * parse recorded rather than a number this script invented. `scannedPdf` below is its
 * text-free twin.
 *
 * ASCII only on purpose: the font dictionary declares Helvetica with no encoding, so a byte
 * outside ASCII reads back as mojibake. Spanish bodies belong in the `.txt` and `.md`
 * fixtures, which carry UTF-8 properly; what this builder is for is the *page number*.
 *
 * **`stamp` goes into the PDF's `/Info` dictionary and nowhere else.** The product
 * recognises identical bytes by content hash and answers 409 — correct behaviour, and the
 * reason a second run of this script must upload *different* bytes rather than the same
 * file twice. Putting the stamp in the metadata keeps every page's text identical, so the
 * parsed chunks, the answer and the citation the assertions describe are the same on every
 * run. The first version stamped only the document's *title*, which the hash ignores: the
 * second run of this check got a 409 and asserted against the previous run's fixture.
 */
function textPdf(stamp, ...pages) {
  const objects = [];
  const kids = pages.map((_, index) => `${4 + index * 2} 0 R`).join(" ");
  objects.push("<< /Type /Catalog /Pages 2 0 R >>");
  objects.push(`<< /Type /Pages /Kids [${kids}] /Count ${pages.length} >>`);
  objects.push("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>");
  pages.forEach((page, index) => {
    const content = `BT /F1 12 Tf 72 720 Td (${page}) Tj ET`;
    objects.push(
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] ` +
        `/Resources << /Font << /F1 3 0 R >> >> /Contents ${5 + index * 2} 0 R >>`,
    );
    objects.push(
      `<< /Length ${Buffer.byteLength(content)} >>\nstream\n${content}\nendstream`,
    );
  });
  const info = objects.length + 1;
  objects.push(`<< /Producer (visual-check ${stamp}) >>`);

  let out = "%PDF-1.4\n";
  const offsets = [];
  objects.forEach((body, index) => {
    offsets.push(out.length);
    out += `${index + 1} 0 obj\n${body}\nendobj\n`;
  });
  const startXref = out.length;
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets) {
    out += `${`${offset}`.padStart(10, "0")} 00000 n \n`;
  }
  out +=
    `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R /Info ${info} 0 R >>\n` +
    `startxref\n${startXref}\n%%EOF\n`;
  return out;
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
