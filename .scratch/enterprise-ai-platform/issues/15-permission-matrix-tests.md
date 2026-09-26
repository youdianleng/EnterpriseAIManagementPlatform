# 15 — 权限矩阵与越权测试套件

**What to build:** 一套自动化测试，把"谁能看到什么"这件事变成可执行的断言，运行在真实数据库上（不用 mock）。它是身份与权限阶段的出口条件，也是本系统最重要的安全网——权限过滤恰恰是最容易漏、漏了又最难发现的地方。

**结构约束:** 依赖类别为"不可用本地替身替代"，因此**必须**运行在真实 PostgreSQL 上（`docs/architecture/codebase-design.md` §5）。测试应跨授权内核的**接口**断言（`can()` / `filter_for()`），而非实现细节，以便实现重构后测试无需修改。

**Blocked by:** 14 — 审计日志与合规只读查询

**Status:** done

**Verification (2026-09-26):**
- `pytest tests/test_permission_matrix.py` → 33 passed; whole suite → **503 passed**; `ruff check app tests` clean.
- `pytest tests/test_permission_matrix.py -k finance` → 3 selected, 3 passed: any scenario can be run alone.
- **Verified by mutation, because a suite that cannot fail is not a safety net.**
  (a) `can()` widened to allow `audit.read` for every role → **14 tests failed**, led by all seven
  kernel-matrix tests and the HTTP matrix for six roles (`expected 403, got 200 — {"items":[…`).
  (b) the withheld-fields read policy widened to `USING (TRUE)` in the migration *and* re-applied with
  `ALTER POLICY` (the database was already migrated, so editing the file alone would have changed
  nothing) → **4 tests failed**, led by `test_without_a_published_context_a_protected_table_returns_no_rows`
  (`no context published, yet employee_private returned 1 of the seeded row and 2 rows in total`).
  Both reverted byte-identically: file hashes match, `git diff` shows neither file, and `pg_policies.qual`
  reads back exactly as shipped.

- [x] 测试连接真实 PostgreSQL，不 mock 数据库与行级安全
- [x] 覆盖全部角色（系统管理员、人力资源、财务、IT 支持、合规、经理、普通员工）× 资源类型 × 密级的组合
- [x] 同时断言两层：应用层权限函数的判定结果，以及直连数据库时的行级安全拦截
- [x] 关键断言形式为：被拒绝时，该资源**在响应中完全不存在**，而不是返回 403 却泄漏了标题等字段
- [x] 覆盖代表性越权场景，至少包含：普通员工读他部门的中密级文档、中密级员工读财务部文档、经理读非下属的考勤、普通员工读他人薪资、非合规角色读审计日志
- [x] 覆盖权限缓存失效：改密级或改部门后，同一请求链路内权限立即变化
- [x] 测试数据由夹具按需构造，可单独运行某个场景，失败时输出足够定位的上下文
- [x] 该套件必须全绿才算完成本阶段，并且后续任何权限相关改动都要先让它跑通

**Five layers, one file.** They are five views of one rule, and a change to that rule has to satisfy all
of them: the generated kernel matrix (7 roles × 21 catalogued actions × 13 resource shapes = **1911
cases**, count asserted, plus a guard that the expectation actually discriminates between roles); the
row-level security layer over real `eam_app` connections; the HTTP matrix (7 roles × 12 endpoints); six
named escalation scenarios; and cache invalidation through the HTTP surface.

**The expectation is written in the test file, not imported from `RULES`.** A test that read the
catalogue would only prove the kernel agrees with itself. The readings that were needed to map DESIGN
§4.1's one-line-per-role onto twenty-one actions are recorded as `DESIGN_GRANTS` in the file rather than
left implicit — including the two that are genuinely interpretations: IT's "no business data" is read as
personnel *content* (its duties have no action of their own, so it holds nothing administrative,
fail-closed), and §4.2's last clause as hr/compliance only, not every privileged role.

**Documents, attendance and salary do not exist yet**, so three of the six scenarios assert the rule at
the layer that does exist — `can()` and the database predicate — and each names the ticket that supplies
the other half (31, 21, 43). Nothing in the file claims an end-to-end guarantee it cannot make. The
document half is nonetheless tested against a *real* policy: a `scratch_documents` table is created
inside a rolled-back transaction with `document_visibility_predicate` attached exactly as ticket 31's
migration will, and all 36 combinations of caller clearance, document clearance, department and ownership
are asserted over a real connection as `eam_app`.

**Coupling worth knowing about.** `test_the_design_table_describes_every_action_and_its_resource` fails
when a new action is added to the catalogue without a row here. That is deliberate — the alternative is a
silently incomplete matrix — but it means this file is edited whenever the catalogue grows.

**Process note.** The file landed in the previous commit (`f7afdb2`, ticket 08b) because that commit
staged the whole tree while this ticket was still being written, and the same sweep caught a concurrent
agent's half-finished frontend work. `scripts/commit-and-push.ps1` now takes `-Paths` so a commit can name
what it contains; the history was not rewritten, because rewriting a pushed branch to tidy a commit
message is a worse trade than a commit that contains two tickets' work.
