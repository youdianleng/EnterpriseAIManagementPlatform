# 24 — 补打卡与员工自助查询

**What to build:** 员工为自己漏掉的打卡发起补卡申请，写好时间与原因，走两级审批。审批通过后系统**追加**一条更正事件，原来那条错误的记录**原样保留**，形成可追溯的更正链。同时员工能自助查到自己的历史考勤——这是西班牙工时记录义务里"员工本人可查阅"的落地。

**Blocked by:** 23 — 下班通知与缺卡异常检测；16 — 审批引擎内核与状态机

**Status:** done

- [x] 补卡申请含：目标业务日期、补的类型（上班 / 下班）、补的时间、原因
      — `test_a_correction_request_carries_the_four_facts`（四件事都落库、都进审计）、
      `test_a_request_with_no_reason_or_no_timezone_is_refused`、
      `test_a_correction_of_a_day_that_has_not_happened_is_refused`（未发生的一天、未来的时刻都拒绝）。
      文档表 `attendance_corrections`（迁移 0017）的 `business_date`、`kind`、`corrected_at`、`reason`。
- [x] 走两级审批（直属经理 → 人力资源），复用统一审批引擎
      — `test_both_levels_are_needed_and_the_approval_appends`（一级通过后原始行分毫未动、当天快照仍是旧值，
      二级通过才追加）、`test_only_the_resolved_approver_decides`（越权与本人审批都由引擎拒绝）、
      `test_a_returned_correction_can_be_corrected_and_filed_again`（退回可改、可重提，第一轮的退回记录仍在）、
      `test_a_rejection_is_final_for_the_document_and_a_new_one_replaces_it`。
      状态机全部来自 `ApprovalNotifier`／`ApprovalService`，本模块没有第二个状态机。
- [x] 审批通过后追加一条类型为"更正"的事件，并指向被更正的原始事件；**原始事件不被修改或删除**
      — `test_both_levels_are_needed_and_the_approval_appends`（追加行 `event_type='correction'`、
      `correction_of_event_id` 指向原件，原件九列逐字节不变）、`test_a_second_correction_chains_onto_the_correction`
      （第二条更正指向第一条更正，而不是原件）。
- [x] 从事件流重新推导当天的工时与异常状态；被消除的异常自动标记为已解决
      — `test_a_missing_clock_out_is_made_up_and_the_anomaly_is_resolved`（漏掉的下班卡由审批**补上**一条
      `source='correction'` 的打卡，当天变 `ok`，`missing_clock_out` 由那次事件标记为已解决，之后再扫描不复活）、
      `test_a_correction_that_leaves_the_day_late_resolves_nothing`（改完仍然迟到的，一行都不标记为已解决）。
      调用的是票据 23 的 `AnomalyService.resolve_for_correction`。
- [x] 同日多次更正形成链式记录，界面上能看出完整演变过程
      — `test_a_second_correction_chains_onto_the_correction`；读接口 `GET /attendance/punches` 返回
      `punches[].corrections[]`（按写入顺序，原件在前）与 `effective_at`（当天真正读的那个值）。
- [x] 员工可以查看自己任意历史日期的打卡明细与推导结果，可翻阅到至少四年前
      — `test_an_employee_reads_their_own_punches_and_derived_day`、
      `test_a_date_four_years_ago_is_answerable_and_nothing_truncates_before_it`（四年前与六年前都答得出，
      证明查询没有悄悄的下界；唯一的界是一个请求的宽度 `MAX_RANGE_DAYS = 1461`）。
- [x] 员工只能看自己的考勤；经理看下属、人力资源看全员需按权限放行，越权返回 403
      — 新增目录条目 `attendance.read_report`（经理，仅限直属下属）、`attendance.read_all`（HR，全员）、
      `attendance.correction_own`（自助，self-only）、`attendance.correction_any`（HR 事后修正他人）；
      `test_a_manager_reads_a_report_and_not_a_colleague`（同部门的非下属同事也算越权）、
      `test_hr_reads_anybody_and_an_employee_reads_only_their_own`、
      `test_the_kernel_refuses_a_manager_somebody_who_does_not_report_to_them`（内核维度，带控制组）；
      `api/tests/test_permission_matrix.py` 已补四行与字面量基数 `7 * 39 * 13`。
- [x] 考勤记录支持导出为便于财务或劳动监察阅读的格式
      — `GET /attendance/export`（CSV，列集见下）、
      `test_the_export_lists_every_day_with_a_total_and_the_documented_columns`、
      `test_a_date_four_years_ago_is_answerable_and_nothing_truncates_before_it`（超过 1461 天直接 422
      `ERR_ATT_006`，不做整库转储）。
- [x] 人力资源事后修正也走同一条更正链（保留原值与更正原因），不提供任何原地覆盖的入口
      — `test_an_hr_correction_takes_the_same_chain`（HR 指定他人、同一条链、追加行记下是谁提的）、
      `test_a_request_about_somebody_else_is_hrs_and_refused_for_everybody_else`、
      `test_a_punch_can_only_change_through_an_approved_correction`（运行时角色对 `attendance_events` 只有
      SELECT/INSERT，直接 `UPDATE` 被 PostgreSQL 拒绝；打卡接口既改不了已有行也造不出第二条）、
      `test_the_correction_document_cannot_be_deleted_by_the_runtime_role`。

---

**导出列集（`app/domain/attendance/records.py` 的 `EXPORT_COLUMNS`；双语表头，值不分语言）**

| 列 | 内容 |
|---|---|
| `empleado / employee` | `"姓, 名"`，与西班牙官方名册同序（含逗号，由 CSV writer 加引号） |
| `empleado_id / employee_id` | 员工 UUID |
| `fecha / date` | 马德里业务日（ISO） |
| `estado / status` | 派生状态：`ok` / `working` / `missing_out` / `incomplete` / `absent` / `holiday` / `non_working` |
| `primera_entrada / first_in` | 当天第一个上班卡，马德里本地 ISO（带偏移） |
| `ultima_salida / last_out` | 当天最后一个下班卡，同上 |
| `minutos_trabajados / worked_minutes` | 已闭合区间的分钟数 |
| `minutos_esperados / expected_minutes` | 排班期望分钟；没有排班时留空（空 ≠ 0） |
| `minutos_extra / overtime_minutes` | 加班分钟；未知时留空 |

- 一行一天，**含没有任何打卡的日子**（`absent` / `non_working` / `holiday` 各占一行），按日期升序；
  最后一行以 `TOTAL` 开头（`fecha` 列写 `起..止`，三个分钟列写区间合计）。
- 日期是马德里业务日，时刻是带偏移的马德里本地时间，分钟就是分钟；文件名
  `attendance-<employee_id>-<from>-<to>.csv`，`text/csv; charset=utf-8`。
- **故意不含**：工号与薪资（属 `employee_private` 的受限字段——经理导出会静默为空，一列会说谎）、
  异常标记（那是"判断"，有自己的读取与时间戳，同一段历史在不同日子导出会不同）、
  更正链（链在按天的明细读里看，文件只陈述每一天最终是什么）。
- 范围上限即四年保留窗口（1461 天）：超宽直接拒绝，而不是吐一份整库转储。

---

**实现记录（2026-10-01，agent）**

- **迁移 0017** `attendance_corrections`（接在 0016 之后；0018 是票据 20 的日报邮件，链接在本条之后）。
  表里**没有 status 列**：可见状态由三个已存储的事实推导——是否立项（`approval_request_id`）、引擎的
  请求状态、是否已追加（`applied_at` / `applied_event_id`，两者一起写一起空，由约束钉住）。
  列表把同一规则写成 SQL `CASE`（`app/repositories/attendance.py` 的 `STATE_OF_ROW`），
  `test_the_query_state_and_the_pure_state_agree` 用一份覆盖全部六态的语料把两种写法对齐。
  运行时角色保留 UPDATE、收回 DELETE（与 `attendance_events` / `attendance_daily` / `attendance_anomalies` 同理）。
- **链条的"哪一条是当前值"只有一处规则**：`derivation.chain_tip`（原先 `_corrected_instant` 的内部走法，
  现在公开），当天算术与追加目标都用它，界面与数字不可能各说各话。
- **追加的那一行**：有原件时是 `event_type='correction'` 指向链条末端；原件不存在（漏打卡）时是
  `source='correction'`、`event_type=clock_in/clock_out` 的补卡。两种都经由
  `AttendanceRepository.append_event`，没有第二条写入路径。
- **审计**：`attendance_correction.requested`（立项）、引擎自己的 `approval.submitted` /
  `approval.decided`（`entity_type='attendance_correction'`，同一个 `entity_id`）、
  `attendance_correction.applied`（追加）。没有第二份"谁批准了"的副本——那会是一份迟早与引擎不一致的副本。
  测试：`test_every_state_change_is_audited`。
- **通知**：全部来自被包起来的引擎（`ApprovalNotifier`），本模块一条都不发；
  测试 `test_the_outcome_notification_is_the_engines` 断言申请人只收到引擎的 `APPROVAL_APPROVED`，
  且二级审批人先收到"轮到你"。
- **数据库层不新增行级策略**（0012 把这个决定留给本票）：要表达的规则一半是 `app.current_employee_id`，
  另一半是上下级关系，而策略只能以调用者上下文去读 `employee_assignments`，结果是"什么都看不到、全部拒绝"。
  403 由内核按快照（`reports_employee_ids`）在查询之前判定；数据库这一层的贡献仍是**任何角色都不能改一条打卡**。
- **已知边界**：一天有两个班次（两个 `clock_out`）时，"那天的下班卡"无法指明是哪一条，流程**拒绝**
  （`ERR_ATT_010`）而不是猜——`test_a_day_with_two_clock_outs_cannot_be_corrected`。
- **未覆盖**：没有为补卡申请写定时补偿任务（`CorrectionService.apply_approved` 是幂等的追加入口，
  审批路径已经调用它；把它接到调度器属于作业层，和 `apply_personnel_changes` 放在一起更合适）。
  "员工撤回申请"的接口没有做：申请一旦提出，出口是审批人的退回或拒绝，被拒后另立新文档——
  引擎的 `withdraw` 仍可用，只是本表层没有暴露它。

