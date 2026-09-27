# 17 — 人员变动单：入转调离

**What to build:** 人力资源用一张统一的"人员变动单"处理转岗、晋升、调薪、离职：填写变更内容与生效日期，走两级审批，**审批通过后并不立刻生效，而是等到生效日当天自动落库**。这样未来的转岗可以提前审批好而不会提前泄漏。

**Blocked by:** 16 — 审批引擎内核与状态机

**Status:** done

**Verification (2026-09-26):**
- `pytest tests/test_personnel_changes.py` → 49 passed; whole suite green; `ruff check app tests` clean.
- Migration `0010` applied to both databases.
- **Verified by mutation.** (a) Removing the due-date filter from `lock_next_due` failed 3 tests
  (`test_the_job_waits_for_the_effective_date` first). (b) Removing the `applied_at IS NULL` guard
  failed 2 (`test_running_the_job_twice_applies_nothing_the_second_time`,
  `test_two_workers_at_once_apply_each_change_exactly_once`). Both reverted.

- [x] 单据类型覆盖：入职、转岗、晋升、调薪、离职，各自带生效日期与字段变更明细
- [x] 变更明细以结构化形式存储（字段名 → 变更前后值），不是一段自由文本
- [x] 审批通过后单据进入"已批准待生效"，字段**不立即变更**
- [x] 定时任务在生效日当天执行落库；补跑机制保证服务停机后重启仍会执行未落库的到期单据
- [x] 生效时逐项写入实际的员工、职位分配、薪酬记录，并留审计（含变更前后值）
- [x] 未生效的单据可在生效日前撤销；已生效的单据不可撤销，只能新建一张反向变动单
- [x] 一张单据的多个字段变更要么全部生效要么全部不生效（事务性），不存在半生效状态
- [x] 界面能区分为四种状态：草稿、审批中、已批准待生效、已生效

**Approval is not application, and the test reads the rows.** Approving a transfer changes the
employee and assignment rows in no way at all — asserted by reading both before and after — and the
change sits in `approved_pending` until its effective date. A join is the sharpest case: the employee
row is only written when the change applies, so an approved hire cannot appear in the directory early.

**The job is idempotent, catch-up capable, and safe under two workers.** After a week of downtime it
applies the week in effective-date order; a second run applies nothing. `SELECT ... FOR UPDATE SKIP
LOCKED` is what makes two concurrent appliers safe, proved with two appliers over three due changes.
Each change applies in one transaction: a test makes the second operation of a change fail after the
first has been written and flushed, and asserts the employee the change had already created is gone.

**The status column records what this module did; the engine's answer is read from the engine.**
`draft → pending → applied/cancelled` is the row's own story; `approved` and `rejected` come from
`state_of` on every read. Mirroring them would be a second version of the truth. The API exposes both
plus the computed `state` the interface needs, computed in SQL for lists and in Python for detail,
with `test_the_query_state_and_the_pure_state_agree` running both over one corpus — two expressions of
one rule drift, and this is the test that says so.

**Salary without inventing ticket 43's table.** The agreed figures live in the change's payload and are
stamped into `applied_values` when the change takes effect, with the audit carrying before and after.
`test_a_salary_change_is_applied_to_the_change_itself` also asserts `to_regclass('salary_records') IS
NULL`, so nobody helpfully adds the table here.

**The scheduler is a command, with an optional thin runner.** `python -m app.jobs.apply_personnel_changes`
is the supported path and exits 0 either way — a change that cannot be applied is logged, left for the
next pass, and does not make an operator's cron cry wolf every fifteen minutes. The in-process loop is
off behind `PERSONNEL_APPLY_RUNNER_ENABLED` and a test pins the default.

**Two gaps closed in this ticket.** Approvers had no endpoint at all: the two-level flow existed only
in tests and jobs, so a manager could not approve anything through the product.
`POST /personnel-changes/{id}/decide` now exists, and the guard is "signed in" rather than a role
because *who approves this request* is the engine's answer — a role check here would be a second,
weaker copy of that rule. And the personnel callers now go through `ApprovalNotifier`, so filing tells
the next approver and a decision tells the requester; until then no production path raised an approval
notification, which ticket 19 could not fix from its own side.

**Interpretations recorded rather than hidden.** A promotion may not change department — the ticket
describes it as a position change, and a cross-department move is a transfer, so a promotion that would
move somebody is refused with a message saying to raise a transfer. And a withdrawn or rejected change
can be re-filed (only a rejection is final at the engine), while a *cancelled* change stays cancelled
and its replacement is a new document.

**Left open:** the refusal for cancelling an applied change names the counter-change as the alternative,
but no endpoint creates one yet; and a change's audit record is written at application, not at filing,
so "who asked for this" is on the row (`created_by_employee_id`) rather than in the audit log.
