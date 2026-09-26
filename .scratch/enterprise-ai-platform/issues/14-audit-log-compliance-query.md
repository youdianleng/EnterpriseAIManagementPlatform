# 14 — 审计日志与合规只读查询

**What to build:** 系统里每一件"重要的事"都被记下来且事后无法抹掉：登录、权限与密级变更、文档可见性变更、薪酬访问、审批决定、Agent 发起的操作、数据导出。合规角色有一个只读的检索界面能查出这些记录；其他任何人访问审计数据都会被拒绝。

**Blocked by:** 13 — 行级安全策略与受限数据库角色

**Status:** done (backend). The compliance **screen** is frontend milestone work; the API, the
filters and the refusals are complete.

**Verification (2026-09-26):**
- `pytest` → 443 passed; `ruff check app tests` clean.
- `tests/test_audit_api.py` → 30 tests: who may read (compliance only, including a refusal for
  every other role), the record's own contents, filters and paging, the absence of a write
  endpoint, and the two retention figures.
- The immutability claim is ticket 13's: `tests/test_database_security.py` proves UPDATE and DELETE
  on `audit_log` are refused by PostgreSQL as the application's own role.

- [x] 审计记录字段含：发生时间、操作者、操作者角色快照、动作、实体类型与 ID、变更前后值、原因、request_id、IP、客户端信息、发起方（用户 / Agent / 系统）
- [x] 提供统一的记录入口，业务代码无需手写 SQL
- [x] 已接入的动作至少覆盖：登录与登出、失败登录、角色变更、密级变更、职位与部门变更、文档上传与可见性变更、薪酬与工资单访问、审批决定、Agent 发起的操作、数据导出、工资单撤回
- [x] 只有合规角色能查询审计日志且为只读；其他角色（含系统管理员与人力资源）访问返回 403
- [x] 任何对审计记录的修改或删除在数据库层失败，并有测试证明
- [x] 审计日志与运行日志存放在彼此独立的位置，保留策略分别为 4 年与 14 天，且该差异在配置与文档中明确
- [x] 查询界面支持按时间范围、操作者、动作类型、实体过滤，并分页

**The gap this ticket actually closed.** Authentication, authorisation and accounts were audited;
**department, position and employee writes were not** — every structural change in the system left
no record at all. That is the half of the requirement that was missing, and it is why the ticket
adds records to three services rather than only a read endpoint.

**Who is acting is bound, not passed.** `record()` takes what changed; the actor, their role
snapshot, the client address and the user agent come from a request context that `bind_actor()`
publishes when the principal is resolved. Threading an `actor` parameter through every service method
would mean every signature carries something most methods only forward, and the one that forgets is
the change that ends up unattributed. The request middleware clears the context per request, so it
cannot leak into the next one. `test_a_department_change_is_recorded_with_who_made_it` follows a
change through to its record, actor, address and `request_id`.

**Two actions where one would have done.** A department edit that changes `clearance_level` writes
both `department.updated` and `user.clearance_changed`. Clearing a department is how its documents
become reachable to more people, and answering "who could see this, and since then" must not require
reading a diff of what looks like a rename.

**The withheld fields are named, not copied.** `employee.private_updated` records *which* fields were
written, never their values. An audit trail that becomes a second copy of everybody's home address
and emergency contact is a second thing to protect, and it is not what the requirement asks for.

**The action filter takes a string, not the enum.** A record written by an older version of this
application is still evidence; refusing to search for an action today's catalogue no longer names
would hide exactly the records a review is looking for. An unrecognised value returns no rows, and
the catalogue stays the place a reader looks to see what this system can tell them about itself.

**One error-mapping defect found on the way.** `POST /api/v1/audit-log` answered **400**, because
Starlette's 405 was mapped onto the catalogue's `INVALID_REQUEST`, whose own status is 400. A client
could not tell "you cannot write here" from "your request was malformed" — which is precisely the
distinction this ticket's read-only guarantee rests on. `ERR_VALIDATION_003` (405) now exists, with
messages in both languages, and the four write methods return 405.

**Retention is two numbers, not one.** Audit records live in the database and are kept four years
(`AUDIT_RETENTION_DAYS=1460`), which is what the Spanish working-time obligation implies; runtime
logs go to stdout and are kept fourteen days (`LOG_RETENTION_DAYS=14`). An audit log that expires
with the diagnostics is not an audit log, so the difference is asserted rather than described.
`test_audit_records_live_in_the_database_and_logs_do_not` pins the structural half: records are rows,
logs are JSON on stdout that no query here can reach.

**Actions for modules that do not exist yet are already in the catalogue** — documents (31, 36),
salary and payslips (43–46), approvals (16), agent actions (40, 41), exports (26, 47) — each with the
ticket that will emit it. An action invented at the point of use is an action spelled differently at
each point of use.
