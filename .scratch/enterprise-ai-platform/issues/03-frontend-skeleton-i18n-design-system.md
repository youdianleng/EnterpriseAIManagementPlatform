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

- [ ] 访问根路径时根据浏览器的语言偏好重定向到 `/es` 或 `/en`，无法判断时默认 `/es`
- [ ] 语言切换即时生效、不需整页刷新，且刷新后仍保持所选语言
- [ ] 语言偏好持久化，并预留登录后以用户档案里的偏好覆盖浏览器推断的能力
- [ ] 文案全部来自语言字典文件，页面代码中不出现硬编码的用户可见字符串
- [ ] 设计系统提供基调（颜色、字体、间距）与基础组件（按钮、输入框、表格、表单容器、对话框、提示条），均有西/英双语文案与键盘可达性
- [ ] 有一个风格指南页面把所有基础组件展示一遍，便于后续开发对照
