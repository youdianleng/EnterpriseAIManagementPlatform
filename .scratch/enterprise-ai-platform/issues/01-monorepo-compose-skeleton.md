# 01 — Monorepo 与 Compose 骨架

**What to build:** 一条 `docker compose up` 命令把整套系统拉起来并能访问。仓库形成前后端分明的结构，四个服务（api / web / postgres / redis）互相能通信且都有健康检查；浏览器打开首页能看到一个由后端接口提供数据的页面，证明整条链路是通的。

**结构约束:** 目录结构按 `docs/architecture/codebase-design.md` §1 落位（按业务域切分，不按技术层切分）；`domain/` 不得 import `api/`、`workers/` 或 `ai/`。

**Blocked by:** None — can start immediately.

**Status:** done

**Verification (2026-09-25):**
- `docker compose up --build` starts all four services; every one reports `(healthy)`.
- API: `/health` → `{"status":"ok",...}`, `/ready` → both dependencies `ok`, `/api/v1/info` → application facts.
- Web: `/` → 307 into a locale; `/es` and `/en` → 200; the landing page renders the backend section.
- Locale middleware probe (`api/tests/tools/probe_locale.py`), 8/8 cases: honours `Accept-Language`, falls back to `es`, and a stored cookie outranks the browser preference.
- `docker compose exec api python -m pytest` → 6 passed.
- CORS preflight from `http://localhost:3000` allowed; `X-Request-ID` echoed and present in structured logs.

**Note:** postgres:18+ images store data under a major-version subdirectory, so the volume mounts `/var/lib/postgresql`, not `/var/lib/postgresql/data`.

- [ ] 仓库结构为单仓多应用，后端与前端各自独立依赖管理
- [ ] `docker compose up` 一条命令启动 api / web / postgres / redis 四个服务，无手工前置步骤
- [ ] 浏览器访问前端地址能看到页面，且页面上的数据来自后端接口（不是硬编码）
- [ ] 每个服务都定义了健康检查，`docker compose ps` 能反映真实健康状态
- [ ] README 记录了启动、停止、查看日志的命令
