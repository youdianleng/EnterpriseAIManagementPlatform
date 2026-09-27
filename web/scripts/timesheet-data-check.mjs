/**
 * End-to-end check of the weekly timesheet, against the real stack.
 *
 * `visual-check.mjs` asserts layout, accessibility and the 375px downgrade on an empty
 * week. This one drives the *product*: it signs in, makes sure there is a project with a
 * task and a schedule that expects eight hours a day, then fills a week through the grid
 * with the keyboard, watches the totals update, files the week, and checks that the
 * over-budget warning is on screen with every minute still there.
 *
 * It answers the ticket's two questions that only a browser can answer:
 *
 *   1. **Does the grid update live?** — the day total and the week total are read from
 *      the DOM after each write, not from the API's response.
 *   2. **Does a long day warn without losing anything?** — nine hours against eight
 *      expected, entered through the form, with the minutes read back out of the cell
 *      afterwards and the notice read off the page.
 *
 * It is idempotent in the sense that matters: it files whatever week it is pointed at,
 * and re-running it on the same week updates the entry rather than creating a second one
 * only if the week is still a draft — a filed week is refused, which the script reports
 * rather than hides.
 *
 * Usage: EAM_USERNAME=... EAM_PASSWORD=... node scripts/timesheet-data-check.mjs [baseUrl]
 * Screenshots land in .scratch/timesheet-flow/.
 */

import { mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const BASE = process.argv[2] ?? "http://localhost:3000";
const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, "..", "..", ".scratch", "timesheet-flow");
const API = process.env.EAM_API_URL ?? "http://localhost:8000";

const USERNAME = process.env.EAM_USERNAME;
const PASSWORD = process.env.EAM_PASSWORD;

/** Nine hours against an eight-hour expectation: the ticket's exact case. */
const OVER_MINUTES = 540;
const EXPECTED_MINUTES = 480;

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

/** The Monday of the week containing `date`, as `YYYY-MM-DD`, without a timezone. */
function mondayOf(date) {
  const copy = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  copy.setDate(copy.getDate() - ((copy.getDay() + 6) % 7));
  const month = `${copy.getMonth() + 1}`.padStart(2, "0");
  const day = `${copy.getDate()}`.padStart(2, "0");
  return `${copy.getFullYear()}-${month}-${day}`;
}

/** A week far enough back that nothing else in the product is looking at it. */
function targetWeek() {
  const monday = new Date();
  monday.setDate(monday.getDate() - 21);
  return mondayOf(monday);
}

async function main() {
  if (!USERNAME || !PASSWORD) {
    console.error("EAM_USERNAME and EAM_PASSWORD are required.");
    process.exit(2);
  }
  await mkdir(OUT, { recursive: true });

  const week = targetWeek();
  const day = week; // the Monday of that week
  console.log(`[note] filling the week of ${week} as ${USERNAME}`);

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    locale: "es-ES",
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => fail(`page error: ${error.message}`));

  // 1. Sign in through the form, so the session is the browser's own.
  await page.goto(`${BASE}/es/login`, { waitUntil: "networkidle" });
  await page.locator('input[autocomplete="username"]').fill(USERNAME);
  await page.locator('input[autocomplete="current-password"]').fill(PASSWORD);
  await page.getByRole("button", { name: /entrar/i }).click();
  await page.waitForURL((url) => !url.pathname.includes("/login"), { timeout: 20000 });
  ok("signed in through the form");

  // 2. The project the entry will be booked against, read through the endpoint the
  //    picker itself uses. The *schedule* is not read here: `schedule.manage` belongs to
  //    HR and administration, and an ordinary employee reading their own expectation is
  //    what the grid's header is for — asserted a few lines below.
  const session = await context.request;
  const { project, task } = await ensureProject(session, USERNAME);
  ok(`a bookable project and task exist (${project.code}/${task.code})`);

  // 3. The grid, at the width it is for.
  await page.goto(`${BASE}/es/timesheets?week=${week}`, { waitUntil: "networkidle" });
  const grid = page.locator('[data-testid="timesheet-grid"]');
  await grid.waitFor({ timeout: 15000 });

  const threshold = await grid
    .locator("thead th", { hasText: /previstas|Sin jornada/i })
    .count();
  expect(threshold === 7, `every column states its expectation (found ${threshold})`);
  const expectedShown = (await grid.locator("thead th").nth(0).innerText()).replace(/\s+/g, " ");
  expect(
    /8 h/.test(expectedShown),
    `Monday's expectation is on screen ("${expectedShown}")`,
  );

  const editable = await page.getByRole("button", { name: /^Añadir horas:/i }).count();
  expect(editable === 7, `an editable cell per day on a draft week (found ${editable})`);

  // 4. Fill Monday through the keyboard, with nine hours — the over-budget case.
  await fillCell(page, grid, 0, project, task, OVER_MINUTES);

  // The day's row is located by its date rather than by its index: a day with entries
  // occupies more rows than a day without, so "the first row" is Monday only until
  // somebody writes Tuesday.
  const monday = await dayTotal(grid, dayOfWeek(week, 0));
  expect(
    /Total del día: 9 h/.test(monday),
    `Monday kept every minute and its total updated live ("${monday}")`,
  );
  const weekTotalAfterMonday = await weekTotal(grid);
  expect(
    /9 h/.test(weekTotalAfterMonday),
    `the week total updated live ("${weekTotalAfterMonday}")`,
  );

  // The warning: present, in words, and it says the week can still be filed.
  const notice = (await page.getByText(/por encima de la jornada prevista/i).first().innerText())
    .replace(/\s+/g, " ");
  expect(notice.length > 0, `the over-budget notice is shown ("${notice.slice(0, 70)}…")`);
  expect(
    /puedes enviarla igualmente/i.test(notice),
    "the notice says the week can still be submitted",
  );
  expect(/1 h/.test(monday), `the day says how far over it is ("${monday}")`);
  await page.screenshot({ path: join(OUT, "1-grid-over-budget-es.png"), fullPage: true });

  // 5. A second day inside the expectation, so the warning is a fact about one day
  //    rather than about every day.
  await fillCell(page, grid, 1, project, task, EXPECTED_MINUTES);
  const tuesday = await dayTotal(grid, dayOfWeek(week, 1));
  expect(/Total del día: 8 h/.test(tuesday), `Tuesday totals 8 h ("${tuesday}")`);
  const afterTuesday = await weekTotal(grid);
  expect(/17 h/.test(afterTuesday), `the week totals 17 h ("${afterTuesday}")`);
  expect(
    /Total del día: 9 h/.test(await dayTotal(grid, dayOfWeek(week, 0))),
    "Monday still reads 9 h after Tuesday was written",
  );

  const stillOver = await page.getByText(/por encima de la jornada prevista/i).count();
  expect(stillOver > 0, "the warning survives the second write");

  // 6. File the week, from the grid's own button.
  const submit = page.getByRole("button", { name: /^Enviar a revisión$/i });
  await submit.click();
  await page.waitForFunction(
    () => /\bPendiente de aprobación\b/.test(document.body.innerText),
    undefined,
    { timeout: 20000 },
  );
  ok("the week was filed and the status says it is waiting for approval");

  const lockedCells = await page.getByRole("button", { name: /^Añadir horas:/i }).count();
  expect(lockedCells === 0, `a filed week offers no editable cell (found ${lockedCells})`);
  const lockedHint = await page.getByText(/ya está enviada y no se puede editar/i).count();
  expect(lockedHint > 0, "a filed week says why it cannot be edited");
  const warningAfterFiling = await page.getByText(/por encima de la jornada prevista/i).count();
  expect(warningAfterFiling > 0, "the warning is still on screen after filing");

  // The history is server-rendered and arrives with the refresh the write triggered, so
  // it is waited for rather than sampled: reading it too early tests the network.
  await page.waitForFunction(
    () => /Ronda/.test(document.querySelector("#timesheet-history-heading")?.parentElement?.innerText ?? ""),
    undefined,
    { timeout: 20000 },
  );
  const history = (
    await page.locator("#timesheet-history-heading").locator("..").innerText()
  ).replace(/\s+/g, " ");
  expect(/Ronda/.test(history), `the submission history is rendered ("${history.slice(0, 90)}")`);
  await page.screenshot({ path: join(OUT, "2-grid-filed-es.png"), fullPage: true });

  // 7. English, the same screen.
  await page.goto(`${BASE}/en/timesheets?week=${week}`, { waitUntil: "networkidle" });
  const english = (await page.locator("main").innerText()).replace(/\s+/g, " ");
  expect(/Waiting for approval/.test(english), "the English screen names the status");
  expect(
    /Some days are over the expected hours/.test(english),
    "the English screen shows the same warning",
  );
  expect(!/Total de la semana/.test(english), "no Spanish leaked into the English screen");
  await page.screenshot({ path: join(OUT, "3-grid-filed-en.png"), fullPage: true });

  await context.close();
  await browser.close();

  console.log();
  if (failures.length > 0) {
    console.log(`${failures.length} check(s) failed`);
    process.exit(1);
  }
  console.log(`TIMESHEET FLOW PASSED — screenshots in ${OUT}`);
}

/** Open the day's editor with the keyboard, choose the target, type the minutes, save. */
async function fillCell(page, grid, dayIndex, project, task, minutes) {
  const cell = grid.getByRole("button", { name: /^Añadir horas:/i }).nth(dayIndex);
  await cell.focus();
  await page.keyboard.press("Enter");

  const editor = page.locator("form[id^='entry-form-']").first();
  await editor.waitFor({ timeout: 10000 });

  await editor.locator("select").nth(0).selectOption({ label: `${project.code} · ${project.name_es}` });
  await editor.locator("select").nth(1).selectOption({ label: `${task.code} · ${task.name_es}` });
  await editor.locator('input[type="number"]').fill(String(minutes));

  const totalAfterSave = new Promise((resolve) =>
    page.waitForFunction(
      (value) => document.body.innerText.includes(value),
      `${Math.trunc(minutes / 60)} h`,
      { timeout: 20000 },
    ).then(resolve),
  );
  await page.keyboard.press("Enter");
  await editor.waitFor({ state: "detached", timeout: 20000 });
  await totalAfterSave;
  ok(`wrote ${minutes} minutes on day ${dayIndex + 1} through the keyboard`);
}

async function weekTotal(grid) {
  return (await grid.locator("tfoot").innerText()).replace(/\s+/g, " ");
}

/**
 * One day's total line, addressed by its date.
 *
 * The grid marks each day's total row with `data-day-total`, so this does not depend on
 * how many rows a day happens to occupy — which changes the moment somebody writes an
 * entry, and is exactly the kind of index that makes a check pass for the wrong reason.
 */
async function dayTotal(grid, date) {
  return (await grid.locator(`tr[data-day-total="${date}"]`).first().innerText()).replace(
    /\s+/g,
    " ",
  );
}

/** The `YYYY-MM-DD` of a day inside the week, without a timezone in the way. */
function dayOfWeek(week, offset) {
  const [year, month, day] = week.split("-").map(Number);
  const date = new Date(year, month - 1, day + offset);
  const paddedMonth = `${date.getMonth() + 1}`.padStart(2, "0");
  const paddedDay = `${date.getDate()}`.padStart(2, "0");
  return `${date.getFullYear()}-${paddedMonth}-${paddedDay}`;
}

/** A project in the caller's own reach, with an active task, offered by the picker. */
async function ensureProject(session, username) {
  const listed = await session.get(`${API}/api/v1/projects/selectable?limit=200`);
  const items = listed.ok() ? (await listed.json()).items : [];
  const usable = items.find((row) => row.status === "active");
  if (usable) {
    const detail = await session.get(`${API}/api/v1/projects/${usable.id}`);
    const tasks = (await detail.json()).tasks.filter((task) => task.is_active);
    if (tasks.length > 0) return { project: usable, task: tasks[0] };
  }
  throw new Error(
    `no active project with an active task is bookable by ${username}; ` +
      "create one (as a project manager) before running this check",
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
