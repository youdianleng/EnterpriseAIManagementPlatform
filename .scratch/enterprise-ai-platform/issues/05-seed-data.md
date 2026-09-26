# 05 — seed 数据脚本：组织与员工

**What to build:** 一条命令把一套可信的演示数据灌进空库：一棵四层深的部门树、一批职位、约一百名员工（含多职位与兼职案例）、以及一个系统管理员账号。数据规模足以让权限过滤、分页和列表页在真实体量下被验证，而不是在三个人的假数据上"看起来能跑"。

**Blocked by:** 06 — 部门树与组织架构管理；07 — 员工档案与一人多职位；09 — 用户账号与临时密码

> **Dependency corrected during ticket 06.** This ticket was originally blocked only by 04, but its
> acceptance criteria require departments, employees and a login account, whose tables arrive with
> tickets 06, 07 and 09. Sequencing it first would have meant writing a seed script against tables
> that do not exist yet, so 06 was done before it and this edge was added.

**Status:** done

**Verification (2026-09-26):**
- `python -m app.seed` → 11 departments, 29 positions, 100 employees, 119 assignments, 4 logins.
- `python -m app.seed` a second time → 0 of everything; row counts identical. Asserted as a test,
  on what the *second* pass created rather than on totals, because a loader that deleted and
  recreated everything would also pass a totals check.
- `python -m app.seed --verify` → prints headcount per department and the position distribution,
  and exits non-zero when a promise is missing. Run against the dev database it reports
  `100 employees, 0 without a position, 18 holding more than one, 14 part-time,
  11 departments to depth 3, 4 logins` and `OK`.
- `pytest tests/test_seed.py` → 7 passed. The seed is exercised against the test database through
  the same `Seeder` the command uses.

- [x] 一条命令灌入数据，可重复执行且不产生重复记录（幂等）
- [x] 生成 6 个部门、四层嵌套，部门名与职位名为西班牙语
- [x] 生成约 100 名员工，字段仅限设计文档允许的范围（姓名、地址、照片、公司资料），**不含**身份证、银行卡、健康或生物特征数据
- [x] 数据中至少包含：一人多职位案例、兼职作息案例、每层部门都有负责人、若干员工不属于任何管理岗
- [x] 生成一个系统管理员账号用于首次登录
- [x] 有一条校验命令打印各部门人数与职位分布，用于人工确认数据合理

**Eleven departments, not six.** The ticket's floor is six with four levels; the tree here is
eleven across four levels (root → area → team → one team split again), because the depth rules,
the subtree queries and the department-union rule only see a difference at the fourth level, and
a dataset with three identical departments cannot show a paging bug.

**Idempotent by natural key, and self-healing.** Departments are matched by code, positions by
code, employees by email, accounts by username. Two things make that hold rather than merely
appear to: each person's data comes from a generator seeded by their serial number alone (a shared
generator drifts as soon as one person is skipped, and the next person gets a different email —
which is how an "idempotent" seed starts inserting duplicates on its second run), and positions are
decided by what the person already holds rather than by whether this run created them, so a
half-loaded database is completed rather than left half-loaded.

**Everything goes through the domain services**, so the seed cannot create a state the product
would refuse: the depth limit, clearance inheritance, one-account-per-person and the Argon2id hash
all apply. Two deliberate exceptions, both recorded where they are written: the department's
`manager_employee_id` is set by SQL (no service sets it yet), and the demo logins' roles are
granted by SQL because the endpoint that grants roles is ticket 08b.

**Part-time is `employee_assignments.is_part_time`.** The *schedule* tables that make a part-timer's
expected hours concrete arrive with ticket 22, so the seed marks the people and leaves the hours to
that ticket rather than inventing a column that will not match the one it lands on.

**Four demo logins, one-time passwords printed once:** `admin`, `rrhh`, `devlead`, `empleado`.
Creating accounts for all hundred would cost a hundred Argon2id hashes for no extra coverage, and
each of the four must still change its password at first sign-in — they are real accounts, not a
backdoor.

**The probes are destructive to this dataset.** They drive the running stack, which points at the
development database, and they wipe the tables they use. `python -m app.seed` puts it back; that is
noted in `tests/tools/support.py` where the wipe happens, so the next person does not discover it by
losing their demo data.
