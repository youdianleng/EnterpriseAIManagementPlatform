# 16 — 审批引擎内核与状态机

**What to build:** 一个所有单据共用的审批引擎：任何东西提交后都走直属经理 → 人力资源两级。经理可以通过、驳回或退回补正；退回后申请人可修改再提交，且此前的决定记录全部保留。引擎不关心单据内容，只负责流转。

**结构约束:** 接口固定为 `submit` / `decide` / `withdraw` / `state_of` 四个操作（`docs/architecture/codebase-design.md` §2.3）。**"生效"不属于引擎**——引擎只回答"批没批"，生效由各领域模块自己的定时任务读取"已批准且到期"的单据执行。把生效逻辑塞进引擎会让引擎必须理解每种单据的字段语义，深度立刻崩塌。

**Blocked by:** 15 — 权限矩阵与越权测试套件

**Status:** done

**Verification (2026-09-26):**
- `pytest tests/test_domain_approval.py` → 41 passed; whole suite → **555 passed**; `ruff check app tests` clean.
- Migration `0009` applied to both databases.
- **Verified by mutation.** (a) Allowing self-approval failed 3 tests
  (`test_self_approval_skips_the_first_level`, `test_an_hr_requester_cannot_decide_their_own_second_level`,
  `test_a_skipped_level_is_recorded_in_the_audit_log`). (b) Letting a returned request keep its decided
  state failed 2 (`test_a_return_at_the_first_level_reopens_the_round`, `…_at_the_second_level…`).
  Both reverted with the service file's hash restored byte-for-byte.

- [x] 状态机固定为：草稿 → 待一级（直属经理）→ 待二级（人力资源）→ 已通过；另有已驳回与已撤销两个终态
- [x] 支持三种决定：通过、驳回、退回补正；退回补正使单据回到草稿且历史决定保留可查
- [x] 审批上级取自申请人**主职位**对应的审批上级；未配置时回退到该部门负责人
- [x] 第一级审批人与第二级审批人为同一人时，不得自我审批同一张单据（自动跳过或要求他人代审，行为需明确且被测）
- [x] 申请人可在进入第二级之前撤销单据
- [x] 每次决定记录审批人、决定、意见、时间；记录为追加式，不可修改
- [x] 引擎以统一的实体类型 + 实体 ID 关联任意业务单据，新增单据类型不需要改动引擎（用一张演示用的假单据类型验证这一点）
- [x] 全部状态流转路径有测试覆盖，含退回后重新提交、驳回后不可再提交、越权审批被拒

**Five tables' worth of behaviour in one module with four operations.** The public surface is pinned by a
test that would fail on a fifth (`vars(ApprovalService)` public names == the four), because
`codebase-design.md` §2.3 fixes that interface and the temptation to add `apply_due_requests` is exactly
what the document warns about. The module docstring states why effective dating is absent, with the
reason: an engine that understood effective dates would have to understand each document's fields.

**Two structural decisions worth naming.**

1. **One open request per entity is a partial unique index**, not a service check. Two administrators
   submitting at the same instant cannot both win, and the test proves it by inserting directly as the
   restricted role.
2. **`approval_decisions` is append-only at the database level**, and its foreign key is `RESTRICT`
   rather than `CASCADE`. A cascade runs with the *owner's* privileges, so `CASCADE` would walk straight
   past the revoked DELETE and take the decision history with a deleted request — the guarantee would hold
   for the application's role and fail for anyone with owner credentials.

**Self-approval is skipped, and the reason is recorded.** A department head filing their own request has
themselves as the resolved first approver; the step is written as `skipped` with a reason, a decision row
carries `SELF_APPROVAL_REASON`, and the request goes straight to HR. That is reachable in production
because a department's manager pointer has no "not yourself" check — which is why the behaviour is
explicit and tested rather than assumed.

**Level 2 is "any HR other than the requester".** A first-level approver who also holds `hr` is therefore
not excluded from the second level. The stricter reading — excluding the first approver — would strand a
request whose manager is the only HR person, so the looser rule is the one implemented; it is recorded
here because it is a reading, not a deduction.

**Three gaps found while building it, and closed in the same ticket:**

1. **The fallback approver was unreachable from the product.** `departments.manager_employee_id` is the
   documented first-level fallback, and no endpoint or service could write it — it existed only in seed
   data and raw SQL, so in a real deployment the fallback was dead. `PUT /departments/{id}/manager` now
   sets and clears it, refusing somebody with no active position in the department
   (`ERR_ORG_008`), and audited with both sides.
2. **Filing and withdrawing left no audit record.** The catalogue had `approval.decided` and nothing for
   the other two acts, so a withdrawal was visible only as a status on a row, with no record of who took
   it back. `approval.submitted` and `approval.withdrawn` now exist and are written in the same
   transaction as the change.
3. **`state_of` returns the latest request**, which is provably the open one whenever one exists, since a
   new request can only be created while nothing is open.

**Still open, recorded rather than fixed:** `codebase-design.md` §2.2 declares `manager_of(employee_id,
on_date)` and `primary_assignment(employee_id, on_date)` as part of the employee directory's interface,
and the module implements neither — so the approval route rule reads assignments through its own
repository, and ticket 19's notification routing would be the second copy. The interface should gain
those two methods before that happens.
