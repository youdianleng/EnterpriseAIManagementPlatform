# 12 — 密级与文档访问判定

**What to build:** 文档访问由**两个条件同时成立**才放行：文档密级不高于用户密级，且文档所属部门在用户可访问的部门范围内。两者只要有一个不满足就不放行。这条规则后续会同时驱动文件列表和 AI 检索，所以必须只有一处实现。

**结构约束:** 本票**不新增模块**——它是授权内核中的一个 action，不是独立的"文档访问判定"模块（否则立刻产生两套密级规则，见 `docs/architecture/codebase-design.md` §3）。

**Blocked by:** 11 — 授权内核：`can()` 与权限快照

**Status:** done

**Verification (2026-09-26):**
- `pytest` → 395 passed; `ruff check app tests` clean.
- `tests/test_document_access.py` generates **4 536** combinations (role × user clearance × document clearance × department relation × company/personal × owner × shared) and compares `can()` against the rule written out from `docs/DESIGN.md` §4.2. The expectation is written from the design text, not from the kernel, so it is not the kernel agreeing with itself.
- `probe_accounts.py` drives clearance inheritance over real HTTP: a newly created account for someone assigned to a `high` department comes back `clearance_level: "high"`, and the row agrees.

- [x] 密级为 low / medium / high 三档，文档与用户各持一个
- [x] 判定规则实现为：`密级(文档) ≤ 密级(用户)` **且** `部门(文档) ∈ 用户可访问部门（含子孙）`
- [x] 显式共享给他人的文档**不能突破密级上限**——低密级用户即便被点名共享，仍看不到高密级文档
- [x] 例外角色（人力资源、合规）可跨部门读取公司文档，但该例外在代码中显式列出，不通过"跳过硬编码"实现
- [x] 新建员工时其默认密级继承主职位所属部门的默认密级
- [x] 存在一个角色 × 部门 × 密级的三维单测矩阵，覆盖全部组合，且矩阵在测试中代码生成而非手写枚举
- [x] 判定逻辑在本阶段被 AI 检索复用前就已单独可测（不依赖任何文档解析或向量能力）

**The rule has one home.** `_can_read_document()` in `domain/access/kernel.py` is the
only implementation, and it is a pure function of `Principal` and `Resource`, so
the whole matrix runs without a database. Documents are decided *before* the
generic department-and-clearance path rather than by it: that path allows a
resource with no department and no clearance, which for a document means somebody
else's private upload. The four clauses are:

1. `owner_employee_id == me` — unconditional, as §4.2 writes it. Hiding someone's
   own upload from them is not a security property.
2. `is_company_kb AND clearance_ok AND dept_ok`.
3. `explicit_grant AND clearance_ok` — the share clause. Being named is not a
   clearance; this is the ticket's explicit requirement.
4. `is_company_kb AND has(hr|compliance) AND clearance_ok`.

**Every clause is evaluated before answering**, because they are alternatives: a
low-clearance HR member is refused by the share clause and allowed by the
exception clause, and short-circuiting on the first refusal would get that
backwards.

**Clearance now has two sources, higher wins** (D12, §10.5): the stored
`users.clearance_level`, initialised from the primary position's department when
the account is created, and what the person's departments grant, read fresh on
every snapshot. `highest_clearance()` is the single place that combines them. Both
are in the cache key, so a raise takes effect on the next request rather than
after the TTL.

**Interpretation recorded, not hidden.** §4.2's last clause is written as an
unconditional `OR user.has_role('hr') OR user.has_role('compliance')`, which would
also lift the *clearance* ceiling for those two roles. This ticket states the
exception as cross-department ("例外角色…可跨部门读取公司文档") and states separately
that sharing cannot break the ceiling, so the exception is implemented as
cross-department with the ceiling kept. `test_the_exception_roles_are_still_under_
the_ceiling` pins that reading. The alternative — an unconditional clause — would
mean HR and compliance are the only roles that can read above their own clearance;
that is a policy question, and it is flagged rather than assumed.

**Two behaviour changes to ticket 11's kernel, both deliberate:**

1. `admin` is **not** an exception role for documents. It configures clearances;
   that is not a reason to read above them. §4.2 names hr and compliance only.
2. `finance` is **not** an exception role either. `is_privileged` (hr, finance,
   compliance) is about withheld *employee* fields — payroll — and the two sets are
   not the same. `DOCUMENT_CROSS_DEPARTMENT_ROLES` is now an explicit list in
   `permissions.py` rather than a reused flag.

**Open items, deliberately not invented here:**
- `department_clearances` (DESIGN §5, "部门可授予的最高密级") is a *different*
  column from `departments.clearance_level` ("该部门文档的默认密级"). Only the
  latter exists, and it currently serves both purposes. They must be separated
  when clearance administration is built (ticket 08b or the ticket that grants
  clearance), not before — a table nobody writes is a table nobody tests.
- Because the effective clearance is the *higher* of the two sources, lowering a
  person's stored clearance below what their department grants has no effect.
  §10.5 says an administrator may "提权或降权" individually; under D12's max rule a
  downgrade means changing the department or ending the assignment. This is the
  design's own tension and is left as written.
- Nothing yet offers an endpoint to raise one person's clearance; the column is
  set at creation and by SQL. The administrative surface belongs with the audit
  requirement that "密级变更" is a recorded event (DESIGN §6), which is ticket 14's
  neighbour.
