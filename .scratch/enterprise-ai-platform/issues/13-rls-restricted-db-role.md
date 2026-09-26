# 13 — 行级安全策略与受限数据库角色

**What to build:** 在应用层权限之外，数据库自己再拦一道：即使应用代码漏写了过滤条件，跨部门读取敏感表也会被数据库拒绝。应用连接使用的数据库账号**没有**修改或删除审计记录的权限，审计表在物理上不可篡改。

**Blocked by:** 12 — 密级与文档访问判定

**Status:** done

**Verification (2026-09-26):**
- `pytest` → **413 passed with the application connected as `eam_app`**, not the owner. That sentence is the
  ticket: the suite exercises the policies because the application's connection is subject to them.
- `tests/test_database_security.py` → 10 tests over a connection made with the runtime role:
  no context returns **0 rows**, a stranger returns 0, the person sees their own, personnel see all,
  `UPDATE audit_log` and `DELETE FROM audit_log` are refused by the database, `INSERT`/`SELECT` still
  work, the context does not survive its transaction, and the kernel's published context is the one the
  policy reads.
- `tools/probe_rls_cost.py` → measured cost, recorded in `docs/DESIGN.md` §10.6.

- [x] 对敏感表启用行级安全策略，策略依据当前会话中的应用上下文（当前员工、当前密级）判定
- [x] 每个事务开始设置应用上下文；上下文缺失时策略默认拒绝而不是放行
- [x] 迁移使用高权限角色执行，应用运行时使用受限角色，两者凭证分开配置
- [x] 应用运行时角色对审计表**只有插入与查询权限**，UPDATE 与 DELETE 被数据库拒绝
- [x] 有一条使用受限角色连接的测试，在**故意绕过应用层**的情况下直接查询，验证数据库层拦截生效
- [x] 行级安全对性能的影响被实测并记录（敏感表在 seed 数据规模下的查询耗时）
- [x] 策略定义纳入版本管理，与迁移一同演进，不允许手工在库上改策略

**Two connections, and the difference is the point.** `DATABASE_URL` owns the schema and runs
migrations; `APP_DATABASE_URL` serves requests and is subject to every policy. A table's owner is
exempt from its own row-level policies, so a suite — or a developer — connected as the owner would
exercise none of this and still look green. `Settings.enforces_database_security` reports which mode
is in force instead of pretending the difference does not exist, and the first test in the file
asserts the two roles really are different.

**Four defects found by doing it, none of which a code review would have caught:**

1. **`set_config` after a committed transaction reads back as `''`, not NULL.** A custom setting that
   has been written once keeps existing with an empty value for the next transaction on that pooled
   connection. Policies written as `COALESCE(current_setting(name, true), 'false')::boolean` therefore
   raised *"invalid input syntax for type boolean: \"\""* on the **second** request over a connection —
   which reads as a flaky test and is actually a rule with a hole in it. Both accessors now fold empty
   string into NULL (`app_setting`, `app_setting_array`), and every policy goes through them.
2. **An empty string is not an empty array.** `''::text[]` is rejected by PostgreSQL
   ("malformed array literal"), so the array comparisons needed `NULLIF(..., '')` before `'{}'`.
3. **`RETURNING` is subject to the select policy.** SQLAlchemy adds `RETURNING` to an insert to collect
   server defaults, so a writer who may not read what it just wrote got *"new row violates row-level
   security policy"* from a statement that had succeeded in every way that mattered.
   `employee_private` is now declared `implicit_returning=False`, and the write path never asks for the
   row back.
4. **An `UPDATE` reads the row it updates, so it needs the select policy too.** This is the one that
   changed the design. Keeping administrators out of the read clause made every administrative
   correction return "0 rows updated" and then collide on the insert — proved with `EXPLAIN`, not
   guessed. A row-level rule is coarser than a field-level one, so the policy says "the person,
   personnel, or an administrator" and the finer rule — an administrator may correct withheld fields
   and may not receive them — stays in the kernel and in `domain/employee/visibility.py`, where it is
   tested. `test_the_database_rule_is_coarser_than_the_product_rule` asserts exactly that, so the
   boundary is documented rather than discovered later.

**A savepoint is not needed and was not the cause** — worth recording because it looked like one
while diagnosing: `SET LOCAL` inside a savepoint survives `RELEASE`, and the failure reproduced in
plain `psql` without any savepoint at all.

**Default deny, and what "no context" means.** With nothing published, `app_setting` returns NULL, NULL
is not true, and the row is filtered out. A forgotten `apply_rls_context()` therefore returns **no
rows** rather than every row — the failure mode that leaks becomes the failure mode that empties a
list, which is visible immediately.

**`apply_rls_context` was broken and unused.** It called `session.execute` without awaiting it, so with
an `AsyncSession` it returned a coroutine that never ran; and nothing called it, which is why nobody
noticed. It is now awaited, called from `current_principal` in the same transaction as the request's
queries, and publishes the department and clearance *sets* as well — a policy cannot call
`Principal.covers_department`, and re-deriving the set in SQL would be a second implementation of the
rule the kernel owns. `test_the_context_published_by_the_kernel_is_what_the_policy_reads` runs the
kernel's writer into the database's reader, which is the only thing keeping two languages in step.

**Documents are where this gets teeth.** `document_visibility_predicate(owner, department, clearance)`
lands in the same migration, before the tables it will guard: ticket 31's `documents` and
`document_chunks` attach it rather than restating §4.2 from memory. `employee_private` is the table
that exists today, and it is genuinely sensitive — but the department × clearance rule needs documents
to mean anything.

**Convenience kept, on purpose:** probes and the seed connect as the owner. They set state up and
inspect it; a probe that had to work around the restricted role would be testing the workaround.
`tests/tools/support.py` says so where the connection is made.
