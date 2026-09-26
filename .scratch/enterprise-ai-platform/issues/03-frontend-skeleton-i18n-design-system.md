# 03 — 前端骨架：语言路由 + 设计系统

**What to build:** 前端能按浏览器语言自动决定进入西班牙语还是英语页面，地址栏体现语言前缀，页面上的语言切换按钮能即时切换并**记住用户的选择**。同时确立可复用的设计系统，让后续所有页面不必重复发明样式。

**结构约束:** token、双语布局、组件六态、无障碍底线一律遵循 `docs/architecture/frontend-design-system.md`（§2 token、§3 文本膨胀、§5 无障碍）。本票是该规范的主要落地场所。

**Blocked by:** 01 — Monorepo 与 Compose 骨架

**Status:** done

**Verification (2026-09-25):**
- `npx tsc --noEmit` clean. Both locale files are typed against one `Dictionary`, so a key present in Spanish and missing in English is a compile error rather than a blank string.
- `npm run visual` (Playwright, `web/scripts/visual-check.mjs`) → all checks passed across 320/768/1280 × es/en × home/style-guide: exactly one h1 per page, no skipped heading levels, no page-level horizontal overflow, every control has an accessible name.
- Text expansion: `es` is 0.7% taller than `en` at 1280px, well inside the 25% tolerance.
- Number formatting: `es` renders `11,5` / `1.111,5` and `en` renders `11.5` / `1,111.5`, consistently in both the typography samples and the table.
- Element-level screenshots written to `.scratch/visual/` for both languages; reviewed by eye.
- Production image builds (`--target prod`) and serves `/`, `/es`, `/en`, `/es/style-guide`; `/` 307s into a locale; the dev-only indicator is absent.

**Defect found and fixed:** the style guide mixed decimal separators — the table showed `37,5` while the typography block showed `1.5`, because `toLocaleString(undefined)` follows the *browser* while the sample values were hardcoded in English. Added `web/lib/format` (locale-aware number, date and duration formatting) and a rule in the design system: formatting must take the interface locale explicitly. For an HR system displaying hours and money, an inconsistent separator is a data-reading hazard.

**Note:** the dev-mode Next.js indicator ("1 Issue") appears in the screenshots; it is absent from production builds and does not affect the assertions.

- [x] 访问根路径时根据浏览器的语言偏好重定向到 `/es` 或 `/en`，无法判断时默认 `/es`
- [x] 语言切换即时生效、不需整页刷新，且刷新后仍保持所选语言
- [x] 语言偏好持久化，并预留登录后以用户档案里的偏好覆盖浏览器推断的能力
- [x] 文案全部来自语言字典文件，页面代码中不出现硬编码的用户可见字符串
- [x] 设计系统提供基调（颜色、字体、间距）与基础组件（按钮、输入框、表格、表单容器、对话框、提示条），均有西/英双语文案与键盘可达性
- [x] 有一个风格指南页面把所有基础组件展示一遍，便于后续开发对照

**Amendment (2026-09-26, ticket 10's screens).** The language preference is persisted as a cookie and
the *signed-in override* is still pending: `users.locale` does not exist yet, so a person's stored
preference cannot win over the browser's. §10.4 says it will, and it belongs with the profile screen
rather than here.

**Amendment (2026-09-26, auth screens).** This ticket's milestone work — the sign-in and
forced-change screens — is delivered, together with the signed-in shell the rest of the product will
grow inside:

- `web/lib/api/auth.ts` (+ `session-server.ts`, `auth-error-text.ts`) — a typed auth client whose
  errors carry the catalogue `code`, `message_key` and `detail`, so the UI renders *its own* wording
  in the reader's language rather than the API's Spanish sentence. Two pieces of backend coupling live
  there rather than in each screen: the `message_key` → catalogue mapping, and the parsing of the one
  free-text `detail` field (`policy violations: …`, `locked out; N seconds remaining`).
- `/[locale]/login` and `/[locale]/change-password` under an `(auth)` route group; `(app)` holds the
  signed-in shell. The gate is in `(app)/layout.tsx`, decided on the server from `/auth/session`: no
  session → sign-in, `must_change_password` → the change screen and nowhere else. A page added later
  cannot forget it, which is the same reasoning the API's own gate uses.
- The password rule is rendered from `/auth/password-policy`, never restated, and each broken rule gets
  its own message.

Evidence: `npx tsc --noEmit` clean; `node scripts/auth-flow-check.mjs` → 24/24 assertions against the
running stack (wrong password → catalogued message; flagged account → change screen; `/es` and
`/es/style-guide` both bounce back there; a weak password names four separate rules and the raw
`policy violations:` string never reaches the person; a valid change lands on the home page showing the
name; sign-out really removes the cookie); `node scripts/visual-check.mjs` → ALL CHECKS PASSED with the
Spanish/English expansion ratio at 1.019 against the 1.25 limit. Nine screenshots inspected.

**Two defects found while doing it.** Server Components could not reach the API inside Docker at all —
`NEXT_PUBLIC_API_URL` is host-facing, so `localhost` inside the web container is the container itself.
Pages rendered, so nothing had exercised it until a Server Component fetched; `API_INTERNAL_URL` now
carries the compose service name while the browser keeps the public URL. And `SessionRead` had lost
`employee_full_name` when it was split out of the account schema, which forced the shell into a second
request for the name; the field is back and the shell is one request.

