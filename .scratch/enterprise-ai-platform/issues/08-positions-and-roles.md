# 08 — 职位与角色

**What to build:** 人力资源能维护职位目录（每个职位挂在某个部门下，并标记是否为管理岗），管理员能把系统角色授予用户。角色决定一个人在系统里能做什么：系统管理、人力资源、财务、IT 支持、合规、经理。

**Blocked by:** 07 — 员工档案与一人多职位

> **Split during ticket 08.** The ticket has two halves with different
> prerequisites. The positions catalogue needs only departments, positions and
> assignments, all of which exist after ticket 07. The roles half needs a users
> table, which ticket 09 creates — a dependency the original blocking edge did
> not record. The catalogue was built and verified here; the role half moved to
> ticket 08b, immediately after ticket 09.

**Status:** positions catalogue done; role assignment deferred to 08b

**Verification — positions catalogue (2026-09-26):**
- `pytest` → 223 passed (32 new). `ruff check app tests` clean.
- `tools/probe_positions.py` → all 20 checks passed over real HTTP: a position carries its department; a code is unique per department but reusable across departments; an unknown or inactive department is refused; the catalogue reports live and total assignment counts; a position in use cannot be deleted and the refusal names deactivation; deactivating it keeps existing assignments resolving while a new assignment is still refused by the employee path; an unknown field is refused rather than dropped.
- Every probe now cleans up after itself, verified by asserting the database holds zero rows after the full probe suite.

- [x] 职位有唯一编码、西/英双语名称、所属部门、是否管理岗、是否启用
- [x] 员工只能被分配到启用中的职位
- [ ] 系统角色集合固定为：系统管理员、人力资源、财务、IT 支持、合规、经理 → **08b**
- [ ] 一个用户可同时持有多个角色；权限按并集计算 → **08b**
- [ ] 只有系统管理员能授予或撤销角色，且每次变更都写入审计日志（含变更前后快照） → **08b**
- [ ] 角色与权限的映射关系以数据表达而非散落在代码分支中 → **08b**
- [ ] 不存在"超级用户绕过一切检查"的隐藏路径 → **08b**

**Defect found and fixed while verifying:** the service counted only *active* assignments when deciding whether a position could be deleted, but `employee_assignments.job_position_id` is `RESTRICT`. A position referenced only by history passed the check and then failed inside the database, turning a clear 409 into a 500. Deletion now counts every reference and reports both figures, while the catalogue still shows the active count separately because that is what an operator is looking for.

**Position model note:** `job_positions` and its migration were created in ticket 07, because multi-position assignments need the table. This ticket adds the catalogue rules and endpoints.
