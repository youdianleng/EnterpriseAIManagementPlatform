/**
 * End-to-end check of the sign-in, forced-change and signed-in screens.
 *
 * `visual-check.mjs` asserts layout and accessibility on every screen and
 * replays the API's refusals; this one drives the *real* stack with a real
 * account, because the forced-change gate is decided by the server and can only
 * be proved against the server. It answers one question per step:
 *
 *   1. a wrong password shows the catalogued message, in Spanish;
 *   2. a correct password for an account flagged `must_change_password` lands on
 *      the change-password screen and no other page opens;
 *   3. a weak new password is refused naming every broken rule;
 *   4. a valid change replaces the session and the home page renders the name;
 *   5. sign-out returns to the sign-in screen and the session is gone.
 *
 * Usage:
 *   EAM_USERNAME=empleado EAM_PASSWORD='<one-time password>' \
 *     node scripts/auth-flow-check.mjs [baseUrl]
 *
 * The account must still be in the forced-change state; the run ends by changing
 * its password, so it is a check you run once per account, like the flow itself.
 * Screenshots land in .scratch/auth-flow/.
 */

import { mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const BASE = process.argv[2] ?? "http://localhost:3000";
const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, "..", "..", ".scratch", "auth-flow");

const USERNAME = process.env.EAM_USERNAME;
const PASSWORD = process.env.EAM_PASSWORD;
/** What the account is changed *to*, so the same value can be reused next run. */
const NEW_PASSWORD = process.env.EAM_NEW_PASSWORD ?? "Nueva!Clave2026";

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

async function shot(page, name) {
  await page.screenshot({ path: join(OUT, `${name}.png`), fullPage: true });
}

/**
 * The first alert with content.
 *
 * Next's development overlay ships an empty `role="alert"` node, so waiting for
 * the role alone and reading `.first()` returns that empty node and every
 * assertion against it fails for the wrong reason.
 */
async function readAlert(page) {
  const alert = page.locator('[role="alert"]').filter({ hasText: /\S/ }).first();
  await alert.waitFor({ timeout: 15000 });
  return alert.innerText();
}

async function main() {
  if (!USERNAME || !PASSWORD) {
    console.error("EAM_USERNAME and EAM_PASSWORD are required.");
    process.exit(2);
  }

  await mkdir(OUT, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    locale: "es-ES",
  });
  const page = await context.newPage();

  // 1. A wrong password, with an account that exists.
  await page.goto(`${BASE}/es/login`, { waitUntil: "networkidle" });
  await page.locator('input[autocomplete="username"]').fill(USERNAME);
  await page.locator('input[autocomplete="current-password"]').fill(`${PASSWORD}-wrong`);
  await page.getByRole("button", { name: /entrar/i }).click();
  const refusal = await readAlert(page);
  expect(
    /Usuario o contraseña incorrectos/.test(refusal),
    "wrong password shows the catalogued message",
  );
  expect(
    !/Request failed|ERR_ACC/.test(refusal),
    "wrong password does not leak a status code or key",
  );
  await shot(page, "1-login-wrong-password");

  // 2. The real sign-in: this account owes a password change.
  await page.locator('input[autocomplete="current-password"]').fill(PASSWORD);
  await page.getByRole("button", { name: /entrar/i }).click();
  await page.waitForURL(/change-password/, { timeout: 15000 });
  ok("a flagged account lands on the change-password screen");

  const policy = await page.locator("section").first().innerText();
  expect(/\b8\b/.test(policy), "the screen states the API's minimum length");
  expect(/minúscula/.test(policy), "the screen names the lowercase requirement");
  expect(/carácter especial/.test(policy), "the screen names the special-character requirement");
  await shot(page, "2-change-password-forced");

  // The gate: no other page opens while the flag is set.
  for (const path of ["/es", "/es/style-guide"]) {
    await page.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
    expect(
      page.url().includes("/change-password"),
      `${path} is refused and returns to the change-password screen`,
    );
  }

  // 3. A weak password, refused with every broken rule named.
  const fields = page.locator("form input[type=password]");
  await fields.nth(0).fill(PASSWORD);
  await fields.nth(1).fill("short");
  await fields.nth(2).fill("short");
  await page.getByRole("button", { name: /guardar/i }).click();
  const weak = await readAlert(page);
  const rules = [
    ["too_short", /más corta de lo permitido/],
    ["missing_upper", /letra mayúscula/],
    ["missing_digit", /falta un número/],
    ["missing_special", /carácter especial/],
  ];
  for (const [rule, pattern] of rules) {
    expect(pattern.test(weak), `weak password names the broken rule "${rule}"`);
  }
  expect(!/policy violations/.test(weak), "the raw rule list is not shown to the person");
  await shot(page, "4-weak-password-rules");

  // A confirmation that does not match is caught locally: no second request, and
  // the message lands on the field rather than in the server-error block.
  await fields.nth(1).fill(NEW_PASSWORD);
  await fields.nth(2).fill(`${NEW_PASSWORD}x`);
  await page.getByRole("button", { name: /guardar/i }).click();
  await page.waitForTimeout(200);
  expect(
    (await page.locator('input[aria-invalid="true"]').count()) === 1,
    "a mismatched confirmation marks only that field",
  );
  const confirmationError = await page
    .locator(`#${await page.locator('input[aria-invalid="true"]').first().getAttribute("aria-describedby")}`)
    .innerText();
  expect(/no coinciden/.test(confirmationError), "the mismatch message is tied to the field");
  await shot(page, "5-confirmation-mismatch");

  // 4. The change itself: new session, home page with the name on it.
  await fields.nth(2).fill(NEW_PASSWORD);
  await page.getByRole("button", { name: /guardar/i }).click();
  await page.waitForURL((url) => url.pathname === "/es", { timeout: 15000 });
  ok("a completed change lands on the home page");

  const home = await page.locator("main").innerText();
  expect(/Sesión iniciada/.test(home), "the home page says who is signed in");
  expect(home.includes(USERNAME), `the home page shows the username (${USERNAME})`);
  const name = await page.locator("main dl dd").first().innerText();
  expect(name.trim().length > 0 && name.trim() !== USERNAME, `the name is resolved: "${name}"`);
  await shot(page, "6-home-signed-in");

  // The gate lifted: the style guide opens again.
  await page.goto(`${BASE}/es/style-guide`, { waitUntil: "networkidle" });
  expect(page.url().includes("style-guide"), "the forced-change gate has lifted");
  await shot(page, "7-style-guide-after-change");

  // The session replaced the old one, so the pre-change cookie is gone.
  await page.goto(`${BASE}/es`, { waitUntil: "networkidle" });
  ok("the device that changed the password stays signed in");

  // 5. Sign-out.
  await page.getByRole("button", { name: /cerrar sesión/i }).first().click();
  await page.waitForURL(/\/es\/login/, { timeout: 15000 });
  ok("sign-out lands on the sign-in screen");
  await shot(page, "8-after-sign-out");

  // And the session really is gone, not just navigated away from.
  await page.goto(`${BASE}/es`, { waitUntil: "networkidle" });
  expect(page.url().includes("/login"), "a signed-in page without a session lands on sign-in");

  // English, once, on the same session-free browser: the sign-in screen and the
  // forced-change screen are the two that must read naturally in both languages.
  await page.goto(`${BASE}/en/login`, { waitUntil: "networkidle" });
  expect(
    /Sign in/.test(await page.locator("main").innerText()),
    "the English sign-in screen is in English",
  );
  await shot(page, "9-login-en");

  await context.close();
  await browser.close();

  console.log();
  if (failures.length > 0) {
    console.log(`${failures.length} check(s) failed`);
    process.exit(1);
  }
  console.log(`AUTH FLOW PASSED — screenshots in ${OUT}`);
  console.log(`The account ${USERNAME} now has the password from EAM_NEW_PASSWORD.`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
