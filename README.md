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

Built and verified, newest first: the organisation tree, employee records with
multiple assignments, the position catalogue, accounts with one-time passwords,
sessions with the forced password change, the authorization kernel with its
clearance and document rules, row-level security under a restricted database
role, the audit trail with a compliance read surface, role administration, and —
on the interface — sign-in, forced change and the signed-in shell. The
permission matrix (1911 generated cases plus the database layer) is the safety
net the rest of the work runs against.

Next, in order: personnel change requests, notifications, attendance and leave,
timesheets, the document pipeline and retrieval, the agent, payroll. The ticket
index is the authority; every ticket's file records what was verified and what
was left open.


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

Opening the web app prints the backend's answer on the landing page, which is
the end-to-end proof that web, api, postgres and redis are wired together.

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
docker compose exec -T api python -m pytest -q                      # the suite
docker compose exec -T api sh -c "uvx ruff check app tests"         # lint
docker compose exec -T api python /app/tests/tools/probe_auth.py    # one probe
cd web && npx tsc --noEmit && node scripts/visual-check.mjs         # frontend
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
