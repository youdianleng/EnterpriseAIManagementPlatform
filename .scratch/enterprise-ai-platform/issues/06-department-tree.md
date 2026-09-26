# 06 — 部门树与组织架构管理

**What to build:** 人力资源角色能在界面上创建、重命名、移动部门，看到一棵四层深的组织树。移动一个部门时，它下面的所有子部门跟着一起移动，且任意层级的"本部门及所有下级"查询都正确——这是后续所有权限判定的地基。

**Blocked by:** 05 — seed 数据脚本：组织与员工

**Status:** done

**Verification (2026-09-26):**
- `pytest` → 93 passed (48 new). `ruff check app tests` clean.
- `tools/probe_departments.py` → all 16 checks passed over real HTTP: a four-level nested tree is accepted and a fifth level refused with `ERR_ORG_007`; the materialised path is built from the parent chain; a subtree query returns only that branch; a move rewrites the moved node *and* its grandchildren in one statement with depths recomputed; moving into a descendant returns `ERR_ORG_006`; deleting a parent returns `ERR_ORG_004` while deleting a leaf returns 204.
- Ancestry uses ltree `<@`, so "this department and all descendants" is one indexed comparison. `ix_departments_path` backs it.
- Writes require a structure role and deny by default; a request without roles gets `ERR_AUTH_002`.
- Code uniqueness is a partial unique index on `(parent_id, code) WHERE is_active`, so a code belongs to a position in the tree rather than being globally reserved.
- Writes bump the `org:tree:version` stamp in Redis instead of waiting for a TTL.

**Five defects found and fixed while verifying:**
1. `to_label` used a lookaround regex that kept matching inside its own replacement, so `r_and_d` grew an underscore on every pass. Replaced with a character scan.
2. The in-memory substitute rebuilt descendant paths as `f"{new_path}{suffix}"`, dropping the separator, while the SQL uses `subpath()` correctly. A substitute diverging from the real implementation is exactly what a substitute can do wrong.
3. `subtree_height` counted from the root instead of from the node, so every move looked one level deeper than it was and legitimate moves were rejected.
4. The depth guard used `>=` where it had to be `>`.
5. The engine and Redis clients cached by `lru_cache` are bound to the event loop of the first test that used them, so a later test in a different loop failed `/ready` with 503. Fixed by clearing those caches around each test — the same loop-affinity bug found in ticket 04, this time inside the application.

**Depth limit reconciled with the source.** `MAX_DEPTH = 4` (depths 0..4) follows the "4 层嵌套" wording in tickets 22 and 43. Ticket 03's "部门 4 层" records the scale baseline, not the tree depth. An earlier pass conflated the two and used 3, which the probe caught by accepting a fifth level.

- [ ] 部门有唯一编码、西/英双语文名、上级部门、层级深度、默认密级与成本中心字段
- [ ] 支持四层以上嵌套的创建与展示，界面能展开/收起
- [ ] 移动部门后，其整棵子树一并移动，深度字段被正确重算
- [ ] 查询"某部门及全部下级"能在一次查询内返回正确集合（用物化路径实现，禁止递归查询逐层拉取）
- [ ] 禁止把部门移动到自己的子孙之下，该操作被拒绝并给出可读的西/英提示
- [ ] 删除有在职员工或有子部门的部门被拒绝
- [ ] seed 数据中的 6 部门四层树在界面上完整呈现
