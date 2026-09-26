# Domain docs

**Layout:** single-context.

## Files

- `docs/DESIGN.md` — the implementation baseline: decisions (D1–D35), data model, permission model, RAG pipeline, Agent graph, milestones, risk register. **Authoritative.** If code and this doc disagree, this doc wins until it is deliberately amended.
- `docs/architecture/codebase-design.md` — **where code goes.** Directory layout, the deep-module interfaces (`can()` / `filter_for()`, employee directory, approval engine, attendance events, ingest pipeline), real-vs-hypothetical seams, and the three blocking structural constraints (A: `domain/` never imports `ai/`; B: AI holds no write repository, enforced at three layers; C: query permission conditions are unbypassable).
- `docs/architecture/frontend-design-system.md` — **how the UI is built.** Design tokens, the Spanish/English text-expansion rules, WCAG AA baseline, dense-data-entry rules for the four error-prone screens, responsive boundaries.
- `docs/agents/issue-tracker.md` — where tickets live and how to read them.
- `CONTEXT.md` — (not yet created) the project's domain glossary. Create it when the first ambiguous term needs pinning down.
- `docs/adr/` — (not yet created) architecture decision records. Create the directory when the first ADR is needed.

## Consumer rules

1. **Read `docs/DESIGN.md` before implementing anything.** It records 48 settled decisions; re-litigating them in code wastes a context window. Then read the matching section of `docs/architecture/codebase-design.md` for structure and `docs/architecture/frontend-design-system.md` for UI work.
2. **Commands run inside the containers**, not on the host:

   ```bash
   docker compose exec -T api python -m pytest        # integration tests (real Postgres + Redis)
   docker compose exec -T api sh -c "uvx ruff check app tests"
   docker compose exec -T web npx tsc --noEmit
   cd web && node scripts/visual-check.mjs            # Playwright; runs on the host
   ```

   The `tools/probe_*.py` scripts under `api/tests/tools/` are acceptance probes: each prints `[ok]`/`[FAIL]` per check and exits non-zero on failure. Run the relevant one when closing a ticket.
3. **Use the project's vocabulary exactly.** Non-negotiable terms:
   - `business_date` — the Madrid-local calendar day an attendance event belongs to. Never derive it from a UTC timestamp at read time.
   - `clearance_level` — `low` / `medium` / `high`. Document classification, and a user attribute.
   - `PrefillForm` — the Agent's only output for state-changing intent. Never a DB write.
   - `initiated_by=agent` — the audit marker distinguishing an Agent-originated request from a self-service one.
   - Correction chain — attendance and timesheet corrections append new events; they never overwrite.
   - `is_company_kb` — separates company knowledge from personal uploads.
3. **Respect the non-goals list** (`DESIGN.md` §8.3). Payroll calculation, shift scheduling, GPS/biometric attendance, OCR, external SSO, microservices, multi-tenancy are all deliberately out of scope. Adding one requires editing the design doc first, not sneaking it into a ticket.
4. **When a new decision is needed**, record it as an ADR in `docs/adr/` and add a row to the decision table in `docs/DESIGN.md`. Do not leave it implicit in code.
5. **The permission model is the highest-risk area.** Any change touching `can()`, RLS policies, or RAG retrieval filtering must extend the access-control test suite in the same change.
