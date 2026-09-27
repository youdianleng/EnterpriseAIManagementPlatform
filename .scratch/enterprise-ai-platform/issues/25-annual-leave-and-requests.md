# 25 — 年假额度与请假申请

**What to build:** 员工按工作日粒度申请休假，系统自动扣除周末与节假日，从年假额度里扣。额度默认 30 个自然日（可配置），额度不足时无法提交。请假同样走两级审批。

**Blocked by:** 24 — 补打卡与员工自助查询

**Status:** done

- [x] 年假额度以系统参数提供，默认为 30 自然日；修改参数不需要改代码
      — 参数是 `Settings.annual_leave_days`（默认 30），D7 的 `annual_leave_days=30`。
      `test_a_balance_reports_the_configured_allowance`（额度出现在报告额度的接口里，
      首次需要时落成 `leave_balances.entitled_days` 并记一条 `grant` 流水）、
      `test_changing_the_parameter_changes_the_allowance_with_no_code_change`
      （以 25 构造服务 → 库里就是 25）。选设置而不是表：这是公司级的**一个政策数字**，
      `ANNUAL_LEAVE_DAYS=25` 改环境即可；表需要接口、权限、审计与界面，而"某个人被授予多少"
      本来就是 `leave_balances.entitled_days`（HR 可逐人调整）。
- [x] 休假类型可配置（年假、病假、事假、产假等），各类型可分别设置是否带薪、是否需要附件、是否占用年假额度
      — `leave_types`（迁移 0019 播种 4 条：`annual` 年假/带薪/不占额度外、
      `sick` 病假（IT）/带薪/需附件/不占额度、`personal` 事假/不带薪/不占额度、
      `parental` 产假（nacimiento y cuidado de menor）/带薪/不占额度——西班牙 2021 年起父母同权，
      故只建一个类型而不是产假/陪产假两个）。
      `test_the_starter_catalogue_is_usable_without_anybody_writing_sql`、
      `test_hr_retires_a_type_and_nothing_new_may_be_filed_under_it`（新增/停用类型，
      停用后不可再申请）。目录里刻意没有 `reason` 之类的自由文本字段（见第 9 条）。
- [x] 申请按起止日期提交，系统按工作日粒度计算实际扣减天数，自动排除周末与节假日表命中的日期
      — 工作日由 **schedule 模块**回答（`DayExpectation.is_working_day`），本模块不另造日历。
      `test_a_request_for_friday_to_monday_deducts_two_days`（周五→周一 = 2）、
      `test_a_holiday_inside_the_range_is_not_deducted`（2026-03-18..03-24，含 3/19 节假日
      与周末 = 4；同月无节假日的一周 = 5）、
      `test_a_whole_month_is_worth_twenty_two_working_days_and_one_holiday_less`
      （2026 年 3 月手工数得 22 个工作日，含一个节假日 = 21）、
      `test_a_range_with_no_working_day_is_refused_rather_than_costed_at_zero`（422，不是 0 天）。
- [x] 申请时校验额度：超出剩余额度的申请无法提交，并明确显示剩余天数
      — 草稿阶段先做一次只读校验（礼貌），提交时在额度行 `FOR UPDATE` 锁下再判一次
      （真正的决定），数据库 `ck_leave_balances_within_allowance` 是第三层。
      `test_exactly_the_remaining_days_is_accepted_and_one_more_is_refused`（剩 6 天：
      5 天接受、第 6 天正好接受、第 7 天 409 且 detail 报出四个数字 `6 entitled + 0 carried
      over - 0 used - 6 pending` 与 `remaining 0`）、
      `test_a_draft_over_the_remaining_days_is_refused_while_it_is_still_a_draft`、
      `test_the_balance_constraint_refuses_more_than_the_year_holds`（绕过服务也写不进去）。
- [x] 待审批中的天数计入"占用中"，审批驳回或撤销后释放
      — 提交即 `pending`，通过转 `used`，驳回/撤销释放；每一步都是 `leave_balance_entries`
      的一行（追加式，`REVOKE UPDATE, DELETE`），行上带该次变动之后的四个数字。
      `test_filing_reserves_the_days_and_an_approval_spends_them`（一级通过时仍是 pending：
      二级未决）、`test_a_rejection_releases_the_reserved_days`、
      `test_a_withdrawal_releases_the_days_the_same_way`、
      `test_an_approved_leave_withdrawn_before_it_starts_gives_the_days_back`（`refund`，
      与 `release` 分开的一种流水）、
      `test_the_settle_sweep_finishes_a_settlement_the_engine_made_alone`（引擎自己提交决定、
      本模块尚未结算的窗口由 `settle_decided` 补上，跑第二遍不动任何数字）。
- [x] 审批通过后，这些日期在考勤日历上显示为休假，且不产生缺卡异常
      — `GET /api/v1/leave/calendar` 是日历叠加层；异常侧实现票据 23 的 `LeaveLookup`
      （`LeaveCalendar`，一次索引查询），接线两处：夜间扫描 `jobs/scan_attendance_anomalies.anomaly_service`
      与更正流 `api/v1/attendance._corrections` 里构造 `AnomalyService` 的那一处；扫描前先
      `settle_decided()`，否则"引擎已批准但尚未结算"的一天会被判成缺勤。
      `test_an_approved_leave_day_produces_no_anomaly_and_the_same_day_without_it_does`
      （同一周一：休假者无异常，同部门无假者 `no_punches`）、
      `test_the_attendance_calendar_shows_the_approved_days_and_not_the_pending_ones`
      （待审的不显示，通过的显示且包含中间的周末）、
      `test_a_correction_on_a_day_of_approved_leave_is_examined_against_the_leave`
      （更正流复检同一天：没有离接缝就长出一条 `missing_clock_in`）。
- [x] 员工只能在休假开始日之前撤销；已开始的休假不能撤销（需改由人力资源走更正流程）
      — 未决的走引擎撤销，已批准的由本模块取消（引擎一旦持有批准就不再受理撤销），两者都要求
      `madrid_today(now) < start_date`。
      `test_a_pending_request_may_be_withdrawn_at_any_time_before_it_starts`（几个月后与
      下周都可以；撤销过的再撤是 409）、
      `test_a_leave_that_has_begun_cannot_be_withdrawn_and_the_refusal_names_hr`
      （待审与已批准两种状态都拒，detail 里点名 `attendance correction flow`）。
- [x] 跨年休假按规则拆分到两个年度的额度（规则需明确定义并测试）
      — 规则：以年界切开，**每一年的工作日从那一年的额度里扣**，不做跨年借用、不按月折算；
      第二年度没有额度行时按参数就地生成（记 `grant`），**不自动结转**
      （`carried_over_days = 0`，结转只能由人写入）。
      `test_a_request_over_the_new_year_is_charged_to_both_years`（2026-12-28..2027-01-05，
      元旦为节假日：4 + 2 = 6，`allocations` 报出 `2026:4 / 2027:2`，2027 行新建且
      `carried_over_days = 0`）、
      `test_a_cross_year_request_is_refused_when_the_second_year_cannot_cover_it`
      （409，detail 以 `2027 annual: 2 working day(s) requested` 开头并给出 `remaining 1`；
      第一年也一分未扣——整个提交回滚）。
- [x] 病假这类特殊类别的休假只记录类型与日期，**不记录诊断或医疗详情**；如需附件，附件单独加密存储且仅人力资源可读
      — `leave_requests` 上**没有** `reason`/`note`/任何自由文本列，唯一的文本列是
      `attachment_reference`，且被 CHECK 约束成"存储键的形状"（含空格或重音就被拒）；
      请求体是 `StrictModel`，多传 `reason` 是 422；文件字节不在本表——**票据 31**（文档上传）
      的存储负责，在此之前该列可空、不透明，只有持 `leave.attachment_read`（仅 HR）的调用者
      能在响应里看到它，其他人只看到 `has_attachment` 与 `attachment_readable_by: ["hr"]`。
      `test_a_sick_leave_records_the_type_the_dates_and_a_reference_only`（缺附件 422；
      本人读不到键、HR 读得到）、
      `test_a_reference_that_is_not_a_storage_key_is_refused`（服务 422 且不回显内容 +
      数据库 `ck_leave_requests_attachment_reference`）、
      `test_the_request_schema_holds_no_free_text_and_no_medical_column`（查
      `information_schema`：`leave_requests` 只有 `attachment_reference` 一个文本列，
      且没有名字像 reason/diagnos/medic/symptom 的列）、
      `test_a_request_payload_refuses_a_reason_field`、
      `test_permission_matrix.py::test_a_manager_cannot_read_a_non_reports_leave_nor_the_note_behind_it`
      （经理可批休假、读不到诊断附件）。
- [x] 员工可查询自己的额度、已用、占用中与剩余，并看到额度计算的历史记录
      — `GET /leave/balances`（含 `history`：每条流水的类型、天数与之后的四个数字）、
      `GET /leave/requests`、经理看下属（`leave.read_report`）、HR 看全员
      （`leave.read_all`，`?everyone=true` 与按人查询），HR 可调额度（`leave.balance_manage`）。
      `test_an_employee_reads_their_own_balances_with_the_history_that_produced_them`、
      `test_a_manager_reads_a_reports_leave_and_is_refused_a_colleagues`、
      `test_hr_reads_everybody_and_nobody_else_reads_the_company`、
      `test_nobody_files_leave_on_somebody_elses_behalf`、
      `test_hr_carries_days_over_and_the_history_says_so`、
      `test_the_runtime_role_cannot_rewrite_a_balance_history`；
      动作目录与 `7 * 47 * 13` 字面计数在 `test_permission_matrix.py`（8 个新动作：
      `leave.type_read`、`leave.type_manage`、`leave.read_own`、`leave.request_own`、
      `leave.read_report`、`leave.read_all`、`leave.attachment_read`、`leave.balance_manage`）。

**实现注记（与 `docs/DESIGN.md` §3.2 的差异）：** `leave_requests` 去掉 `reason`、不存
`status` 列（由引擎状态与四个时间戳推导），`attachment_path` 改名 `attachment_reference`；
新增追加式 `leave_balance_entries`（`REVOKE UPDATE, DELETE`，按 `seq` 排序——`created_at`
是事务开始时间，一个请求写的几行会共享它）。`docs/DESIGN.md` 表格下方已补一段
"实现注记（票据 25）"说明这三处。
