# 11 — 授权内核：`can()` 与权限快照

**What to build:** 全系统只有**一个**判定"这个人能不能对这个资源做这件事"的地方。每个请求开始时把用户的角色、密级、可访问部门算成一个权限快照并缓存；任何受保护接口都必须经过它。越权访问返回 403 并留痕，而不是静默返回空列表。

**结构约束:** 接口形状固定为 `can()` / `filter_for()` / `apply_rls_context()` 三者，见 `docs/architecture/codebase-design.md` §2.1。`filter_for()` 返回**数据描述**而非查询构造器；`FilterSpec` 无公开构造器，只能由本模块产出（"忘记过滤"在类型层面不可能）。`Decision` 必须携带命中规则的理由，供审计日志使用。

**Blocked by:** 10 — 登录、会话与强制改密

**Status:** done

**Verification (2026-09-26):**
- `pytest` → 373 passed; `ruff check app tests` clean.
- `tools/probe_access.py`, `tools/probe_role_constraint.py` → all checks passed over real HTTP.
- The kernel itself is DB-free: 57 unit tests in `test_access_kernel.py` cover the role × action matrix, the refusal reasons, and the three resource conditions. The 14 HTTP tests in `test_access_api.py` prove the endpoints are actually wired to it — the part a unit test cannot see.

- [x] 存在唯一入口 `can(user, action, resource)`，业务代码中不存在任何自写的权限分支
- [x] 权限快照包含：角色集合、密级、可访问部门集合（所有在职职位的部门**并集**，含各自子孙部门）、主职位、管理岗标识
- [x] 权限快照缓存在 Redis，键与 TTL 明确；缓存未命中时从数据库重建
- [x] 角色、密级、职位分配、主职位、部门结构发生变更时，**由写操作主动清除**相关用户的快照，不依赖 TTL 过期
- [x] 缓存的主动失效有集成测试：改密级后，同一个请求链路内立即生效（不等 TTL）
- [x] 所有受保护接口通过统一依赖注入获得权限上下文；未受保护的接口必须显式标注为公开
- [x] 越权返回 403 且响应体使用统一错误码；被拒绝的访问写入审计日志

**How invalidation works.** The cache key is
`perm:user:{user_id}:{session_epoch}.{roles}.{assignment_version}.{structure_version}`.
Every input to a snapshot is *in* the key, so a changed input produces a different
key and the old entry is never read again rather than being deleted and rebuilt —
which cannot fail half-way the way a delete-then-rebuild can. TTL (300s) is a
backstop for a missed invalidation, not the mechanism. `assignment_version` is a
`hashtext` over the active assignment rows (department, primary flag, approver,
department path, managerial flag) rather than a counter, because a counter has to
be incremented by every writer and the one that forgets is the bug. Department
edits — including a clearance change, which is where clearance is derived from —
arrive through the organisation module's structure stamp.

**Two defects found while testing this, both fixed:**

1. **Roles were missing from the cache key.** Granting a role would have kept
   serving the previous snapshot until the TTL expired — discovered before
   ticket 08b could depend on it. Roles are read from the row the builder already
   loads, so they cost nothing to include.
2. **`"high"` clearance resolved to `"low"`.** `_clearance_for` ranked departments
   with `MIN(...)` (0 = high) and then mapped the rank back through
   `int(level or 2)`. Since `0 or 2` is `2` in Python, the *most* permissive rank
   fell through to the least permissive name: everyone whose clearance came from a
   high-clearance department was handed "low". It fails closed, so nothing leaked,
   but the rule was doing the opposite of what it says. The rank→name mapping is
   now an explicit tuple and `test_a_clearance_change_takes_effect_at_once` walks
   low → medium → high so neither end can be off by one again. The dead
   re-implementation of clearance inside `assignment_facts` (a second copy of the
   same rule, never read) was deleted with it.

**One endpoint removed.** Ticket 09 had exposed self-service password change at
`POST /accounts/{id}/change-password`, which duplicated the rule ticket 10 owns:
same verification, same policy check, different guard (no session at all), and a
second audit action name. Two paths to one write drift apart, so the account-scoped
route, its service method and its schema are gone; `probe_accounts.py` now asserts
the route is a 404 so it cannot creep back. The three tests that covered it were
already covered on the real route in `test_auth_api.py`.

**Audit scope note:** a refusal is recorded with the action attempted, the rule
that fired (`reason`), the endpoint, and the client address. `app/audit.py` carries
the note that append-only is not yet enforced at the database level; ticket 13 is
where that stops depending on application code.
