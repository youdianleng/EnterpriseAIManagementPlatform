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
| `.scratch/enterprise-ai-platform/issues/INDEX.md` | Ticket index: critical path and parallel lanes |

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
