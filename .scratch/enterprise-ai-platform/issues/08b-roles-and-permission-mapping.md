# 08b — 角色授予与权限映射

**What to build:** 管理员能把系统角色授予用户：系统管理员、人力资源、财务、IT 支持、合规、经理。角色决定一个人能做什么；一个用户可以同时持有多个角色，权限按**并集**计算。角色与权限的映射以数据表达，便于审计；经理角色由职位的管理岗标记推导或由管理员显式授予。

**Blocked by:** 09 — 用户账号与临时密码（角色挂在用户上，用户表由 09 建立）；08 — 职位目录（管理岗标记是经理角色的推导来源）

**Status:** done

**Origin:** 从票据 08 拆出。08 的原始阻塞边只写了 07，但"角色授予用户"必须有 users 表，而 users 表由 09 建立——这是拆分时漏掉的依赖。职位目录部分不依赖 users，已在 08 完成并验收。

**Verification (2026-09-26):**
- `pytest tests/test_roles_api.py` → 21 passed; `ruff check app tests` clean.
- `GET /roles` publishes 7 roles and 78 role-permission rows; `test_the_published_catalogue_matches_the_code`
  compares the table against `RULES` in **both** directions, so neither side can have rows the other lacks.
- A grant and a revocation each take effect on the very next request, asserted through the API rather than
  through the cache: grant `hr` and the withheld employee fields appear; revoke it and an administrator-only
  endpoint answers 403.

- [x] 系统角色集合固定为：系统管理员、人力资源、财务、IT 支持、合规、经理
- [x] 经理角色由职位的管理岗标记自动推导，或由管理员显式授予
- [x] 一个用户可同时持有多个角色；权限按并集计算
- [x] 只有系统管理员能授予或撤销角色
- [x] 每次角色变更都写入审计日志，含变更前后快照
- [x] 角色与权限的映射关系以数据表达（可查询的表），而非散落在代码分支中
- [x] 不存在"超级用户绕过一切检查"的隐藏路径：管理员同样受权限函数约束
- [x] 撤销角色后，该用户的权限快照立即失效，而不是等 TTL 过期
- [x] 有测试覆盖"撤销后再访问"的即时生效

**The mapping is a projection, not a second source of truth.** Migration 0008 creates `roles` and
`role_permissions`, and `domain/access/catalogue.py` rewrites them from `RULES` at application start.
Two alternatives were rejected on purpose: deciding permissions *from* the tables would put a query on
every request and let a row edit change an authorisation rule without review; publishing nothing would
leave the auditor reading Python. So the code decides, the tables describe, and
`test_the_published_catalogue_matches_the_code` is what holds them together — drift is a failing test
rather than a system quietly documenting a rule it does not apply. The runtime role has SELECT and
nothing else on both tables.

`ROLE_MANAGE` is a **separate action from `ACCOUNT_MANAGE`**: creating a login and deciding what that
login may do are not the same authority, and collapsing them would mean an administrator who may create
accounts implicitly may create administrators.

**Two rules that exist because their absence is found at the worst moment.** An unknown role is refused
with `ERR_ACC_010` naming it, rather than stored and ignored. And the **last active administrator cannot
lose the role** (`ERR_ACC_011`, 409): removing it leaves a system nobody can administer, and the person
doing it is the one least able to notice. `test_the_last_administrator_keeps_the_role` also asserts the
flip side — with a second administrator, the same request succeeds, which is what "last" means.

**The manager role is derived, and the test says so.** A managerial position adds `manager` to the
permission snapshot without anybody granting it; `test_a_managerial_position_confers_the_manager_role`
asserts both halves: the role appears in the principal, and the stored role set is untouched.

**Revocation is immediate twice over.** The permission snapshot's cache key carries the role set, so a
stale entry can never be read again; `set_roles` also drops the cached snapshot, because a guarantee
should not depend on somebody remembering how a key is built.
