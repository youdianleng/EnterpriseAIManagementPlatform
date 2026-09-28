# Enterprise AI Management Platform

Internal platform for a single organisation: organisation and employee records,
attendance, project timesheets, payroll documents, training, and a
permission-aware RAG knowledge assistant.

## Documentation

| Doc | Purpose |
|---|---|
| `docs/DESIGN.md` | Implementation baseline: 48 settled decisions, data model, permission model, RAG pipeline, Agent design, milestones |
| `docs/architecture/codebase-design.md` | Where code goes: module layout, deep-module interfaces, structural constraints |
| `docs/architecture/frontend-design-system.md` | Design tokens, bilingual layout rules, accessibility baseline |
| `docs/agents/domain.md` | How the docs above fit together, for whoever (or whatever) works here next |
| `.scratch/enterprise-ai-platform/issues/INDEX.md` | Ticket index: critical path and parallel lanes |

## Where the project stands

Built and verified, newest first: hybrid retrieval (both legs in one statement,
reciprocal rank fusion, a reranker behind a seam, an explicit "no basis" answer
and an authorised debug view), the hours report with billable and non-billable
told apart, the weekly timesheet with its approval lock and supplementary
submissions, documents from upload through parsing, chunking and embeddings,
leave and annual allowance, overtime with its monthly export, attendance
corrections, the daily digest, projects and tasks, work schedules with expected
hours and holidays, clock events and anomaly detection, the approval engine,
personnel change requests, termination, the notification centre, and — the
foundation everything else asks — the authorization kernel with its clearance
and document rules, row-level security under a restricted database role, the
audit trail with a compliance read surface, role administration, accounts and
sessions. On the interface: sign-in, forced change, the signed-in shell,
notifications, timesheets and documents.

The permission matrix (5187 generated cases over 76 routes, plus the database
layer that asserts what PostgreSQL itself refuses) is the safety net the rest of
the work runs against.

Next, in order: the answer pipeline with its mandatory citations and its refusal
to answer without retrieved evidence, the retrieval escalation suite, personal
documents and their visibility, the question-and-answer interface, then the
agent (intent routing, read-only tools, draft tools with human confirmation, and
redacted observability with provider fallback), and finally payroll, payslips and
the data export. The ticket index is the authority; every ticket's file records
what was verified and what was left open.


## Requirements

- Docker Desktop (Linux containers) with Compose v2
- ~4 GB free disk for images

## Run

```bash
cp .env.example .env      # optional; every value has a working default
docker compose up --build
```

| Service | URL |
|---|---|
| Web | http://localhost:3000 |
| API | http://localhost:8000 |
| API docs (OpenAPI) | http://localhost:8000/docs |
| API health | http://localhost:8000/health |
| Mailpit (caught mail) | http://localhost:8025 |

Opening the web app prints the backend's answer on the landing page, which is
the end-to-end proof that web, api, postgres and redis are wired together.

## Mail: caught locally, never sent out

Every message the API sends goes to **Mailpit**, which accepts anything on port
1025 and delivers it nowhere: the rendered message is read at
http://localhost:8025. Development and demo therefore have no route to a real
inbox, which is the point — a digest that escapes the network is a real person's
morning. Nothing in the test suite reaches a mail server at all: the transport
(`api/app/mail.py`) is a seam with a recording double behind it.

The one mail this system sends today is the **daily digest**
(`docs/DESIGN.md` §7.1): one message per recipient about the previous day's
attendance anomalies, grouped by report, with a link into the platform.

```bash
# One pass, for a date you choose — no need to wait for 08:00.
docker compose exec -T api python -m app.jobs.send_daily_digests 2026-09-21
docker compose exec -T api python -m app.jobs.send_daily_digests            # Madrid yesterday
docker compose exec -T api python -m app.jobs.send_daily_digests --retry    # after fixing a relay
```

Production runs the same command from cron at 08:00 Madrid, which is where the
timezone belongs — the job takes the date it is about and knows nothing about
when it was started:

```cron
CRON_TZ=Europe/Madrid
0 8 * * * docker compose exec -T api python -m app.jobs.send_daily_digests
```

Two properties make that safe to install: a day is mailed **once** per recipient
(`daily_digests` is unique on recipient and date, so a re-run or a restarted
container is a no-op), and **a clean day is not mailed at all** — a digest exists
only where there is something to report. A message that fails is retried a bounded
number of times and then left `failed` with the sender's reason on the delivery
row, which is where "I never got it" is answered.

## First run: demo data and a login

The platform starts empty. One command loads a demo organisation — eleven
departments across four levels, a hundred people, and four logins whose one-time
passwords are printed once:

```bash
docker compose exec -T api python -m app.seed            # load (safe to re-run)
docker compose exec -T api python -m app.seed --verify   # report what is there
```

Sign in at http://localhost:3000 with any of the printed usernames (`admin`,
`rrhh`, `devlead`, `empleado`). Every one of them must change its password at
first sign-in — they are real accounts, not a backdoor, so the forced-change
screen appears before anything else.

## Permissions, in one place

Every "may this person do this to this thing" question goes through
`api/app/domain/access/kernel.py`: `can()`, `filter_for()` and
`apply_rls_context()`. Endpoints ask the kernel through a dependency; they never
test a role themselves. The same rule is enforced a second time by PostgreSQL
row-level policies, as a role that cannot rewrite the audit trail.

## Tests and probes

```bash
docker compose exec -T api python -m pytest                        # the suite (~18 min)
docker compose exec -T api sh -c "uvx ruff check app tests"        # lint
docker compose exec -T api python /app/tests/tools/probe_auth.py   # one probe
cd web && npx tsc --noEmit && node scripts/visual-check.mjs        # frontend
```

`node scripts/visual-check.mjs` drives a real browser against the running stack and
signs in as a demo account (`EAM_USERNAME`/`EAM_PASSWORD`), so it needs that account
to have data. The fixtures that give it some live in `scripts/demo/`:

```powershell
& .\scripts\demo\seed-screens.ps1     # clock, attendance and leave, for devlead by default
```

They set a known password on the four demo accounts and write a month of punches, a
correction chain and leave requests in every state. That is the opposite of what
`api/app/seed.py` does — it issues one-time passwords so none is ever stored — and it is
deliberate: a password printed once cannot be typed into an automated run, and the four
demo accounts are the only ones affected.

**Do not add `-q` to the pytest command.** `api/pyproject.toml` already sets
`addopts = "-q"`, and a second one makes pytest quiet enough to drop its summary
line — the run then prints nothing but dots and an exit code, so "did it pass, and
how many" becomes unanswerable. It takes arguments that *replace* nothing: add
`-p no:warnings` or a path, not another `-q`.

**Never kill a run half-way.** An interrupted pytest leaves its backend connections
open, and the next run against the same database deadlocks with `40P01` errors that
look like a code fault. Recover by terminating them and dropping the scratch
database:

```bash
docker compose exec -T postgres psql -U eam -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity \
   WHERE datname='eam_test_x' AND pid <> pg_backend_pid();"
docker compose exec -T postgres psql -U eam -d postgres -c "DROP DATABASE eam_test_x;"
```

**Two runs at once need two databases and two Redis databases.** The name comes from
`TEST_DATABASE_NAME` (default `eam_test`); the fixture creates it, and migrations
must be applied to it separately:

```bash
docker compose exec -T -e TEST_DATABASE_NAME=eam_test_x -e REDIS_URL=redis://redis:6379/5 \
  api python -m pytest
```

Two kinds of check. **Pytest** drives the application in-process against a
separate test database, and is the suite that must stay green. **Probes** under
`api/tests/tools/` drive the *running* stack over a socket, which is what catches
a deployment that differs from the code — with the caveat that they wipe the
development database, so re-run the seed afterwards.

## Common commands

```bash
docker compose ps                  # service health
docker compose logs -f api         # follow one service
docker compose down                # stop, keep volumes
docker compose down -v             # stop and wipe the database
docker compose build --no-cache    # rebuild images
```

The web container keeps `node_modules` and the Next.js build cache in named
volumes, so host-side `npm install` is never needed and the bind mount stays
fast.

## Layout

```
api/       FastAPI backend (domain logic lives under app/domain)
web/       Next.js frontend
docs/      Design baseline and architecture rules
.scratch/  Local ticket tracker
```

## Conventions

- Code comments are written in English and kept short; they explain *why*, not *what*.
- UI strings are never hardcoded — Spanish and English dictionaries live in `web/lib/i18n`.
- `domain/` must not import `api/`, `workers/` or `ai/` (enforced by a test).
