# 23 — 下班通知与缺卡异常检测

**What to build:** 员工点下班后，其直属经理收到一条通知（可以是员工档案上单独指定的通知人）。系统每天夜里检查谁没打下班卡或没打上班卡，次日早上提醒本人补卡，并把异常列进经理的每日汇总邮件。

**Blocked by:** 22 — 工作日程、节假日表与月度应出勤；19 — 通知中心与投递追踪

**Status:** ready-for-agent

- [x] 员工完成下班打卡后，通知发往其**主职位**对应的经理；若员工档案上指定了覆盖通知人，则发给覆盖对象
      — `test_clocking_out_notifies_the_manager_of_the_primary_position`、
      `test_the_assignments_notification_override_wins_over_the_manager`、
      `test_a_position_with_no_manager_falls_back_to_the_department_manager`（职位没写经理时回落到部门经理，
      与审批路由同一条回落）、`test_a_department_manager_clocking_out_is_not_notified_about_themselves`
      （不通知本人）、`test_a_department_with_no_manager_notifies_nobody_and_still_punches`
      （无人可通知不是错误，打卡照常落库）、`test_a_replayed_clock_out_notifies_the_manager_once`（重放只通知一次）。
      规则在 `api/app/domain/attendance/notify.py`：覆盖人 > 主职位经理 > 部门经理；查询在
      `PostgresAttendanceRepository.notification_route` / `department_manager`，与审批引擎读主职位的方式一致。
- [x] 通知同时进入站内通知中心与次日汇总邮件的候选集
      — `test_the_clock_endpoint_notifies_the_manager`（走真实 `POST /attendance/clock` 与
      `GET /notifications`）、`test_the_clock_out_notification_is_a_candidate_for_the_digest`
      （站内即时 `sent`，邮件停在 `pending` 并写明原因）。
      候选集是 `notification.models.DIGEST_CANDIDATE_TYPES`（票据 20 的 08:00 汇总任务按类型选取）。
- [x] 夜间定时任务扫描当天记录，产出缺失下班卡、缺失上班卡、迟到、早退、以及未记录任何打卡的异常
      — 纯判定：`test_a_day_nobody_punched_is_one_anomaly_and_not_two`、
      `test_an_open_shift_is_a_missing_clock_out`、`test_a_clock_out_with_no_shift_to_close_is_a_missing_clock_in`、
      `test_late_and_early_are_measured_against_the_window_with_the_tolerance`；真实库一遍扫：
      `test_the_nightly_scan_records_what_each_day_is_missing`。命令为
      `python -m app.jobs.scan_attendance_anomalies [YYYY-MM-DD]`（默认马德里昨天）。
- [x] 异常记录含类型、业务日期、检测时间、通知时间、以及被哪次补卡事件消除
      — 迁移 `0016` 的 `attendance_anomalies`：`type`、`business_date`、`detected_at`、`notified_at`、
      `resolved_by_event_id`（指向 `attendance_events`，RESTRICT）。`detected_at` 取自任务自己的时钟
      （`test_the_nightly_scan_records_what_each_day_is_missing` 钉死），`notified_at` 见
      `test_the_morning_reminder_tells_the_employee_and_stamps_the_row`，`resolved_by_event_id` 见
      `test_a_correction_resolves_the_anomaly_the_day_no_longer_shows`。
- [x] 次日早上向员工本人发出补卡提醒（站内 + 汇总邮件中的个人部分）
      — `test_the_morning_reminder_tells_the_employee_and_stamps_the_row`、
      `test_the_job_runs_both_phases_and_says_what_it_did`。提醒在夜间那一遍里即时进站内中心并即时成为
      汇总候选（邮件那一半由票据 20 的 08:00 任务取走），一条通知对应一行异常（一个实体、一个去重键、
      一个 `notified_at`）。
- [x] 已请假的日期不产生缺卡异常；节假日不产生异常
      — `test_leave_suppresses_a_day_for_one_person_and_not_their_colleague`、
      `test_a_holiday_and_a_rest_day_produce_no_anomalies`、纯判定
      `test_a_holiday_a_rest_day_and_leave_all_produce_nothing`。
- [x] 异常在其对应的补卡被审批通过后自动标记为已解决，而不是靠人工关闭
      — `test_a_correction_resolves_the_anomaly_the_day_no_longer_shows`（当天已不再呈现的异常由那次事件消除，
      同时记录更正新造的异常）、`test_a_correction_that_leaves_the_day_late_resolves_nothing`
      （更正后仍然迟到的不许标记为已解决）。规则即列本身：`resolved_by_event_id` 非空就是已解决；
      入口是 `AnomalyService.resolve_for_correction(employee_id, date, event_id)`，**票据 24 在审批通过、
      追加补卡事件并重算当天之后调用它**（此处不建审批流程）。
- [x] 检测任务幂等：同一天重复执行不会产生重复异常
      — `test_running_the_scan_twice_creates_anomalies_once`（第一遍建、第二遍不建且报告说明）、
      `test_the_anomaly_table_refuses_a_second_row_for_the_same_day_and_kind`（唯一约束本身）。
- [x] 有测试可注入任意日期与打卡数据来验证各类异常的判定
      — `test_the_scan_answers_for_whichever_date_it_is_given`（同一人周一干净、周二迟到，两遍互不影响）、
      `test_a_day_nobody_was_due_leaves_the_scan_nothing_to_examine`（`examined` 说明扫了谁）；
      纯判定用例全部用手工构造的事件与日期，不依赖跑测试的那一天。

---

**实现注记（2026-09-30，agent）**

- **请假接缝留给票据 25。** 扫描通过 `AnomalyRepository` / `LeaveLookup.is_on_leave(employee_id, date)`
  询问当天是否休假，当前实现在 `anomaly_repository.py` 里叫 `AssumeNoLeave`（恒为 `False`）。
  票据 25 用真实查询替换它并改一处装配即可，判定规则一行都不用动。`employees.status = on_leave`
  不参与判定：休假是"某人在某一天"的事实，只有这个接缝能回答。
- **迟到/早退的阈值归票据 23**（票据 22 的注记如此交代）：`PUNCH_TOLERANCE = 5 分钟`，双向同一个常量；
  窗口取票据 22 的 `DayExpectation.schedule_day`，用马德里墙钟分钟比较。归属到别的马德里日的那张卡
  （跨午夜的 `clock_out`）不拿当天的窗口去量它。
- **身份与幂等来自唯一约束** `uq_attendance_anomalies_day_type (employee_id, business_date, type)`：
  异常是"这一天如此"的属性，不是"有人发现了一次"的事件，所以一行就是全部答案，重复执行由
  `ON CONFLICT DO NOTHING` 兜住。运行时角色对这张表被收回 `DELETE`（可改不可删，与 `attendance_daily` 同）。
- **一处已知边界：** 若某类型已被某次补卡消除、之后又被另一次更正弄成同样的问题，那一行仍保持
  "已解决"并记着当初消除它的事件（唯一键不允许同一天同类型第二行）；当天自身的 `attendance_daily`
  快照仍然如实反映问题。
- **未新增端点、未新增动作目录条目**，因此 `api/tests/test_permission_matrix.py` 未改动：票据没有要求
  异常查询接口，员工与管理者的"看到"分别是站内通知中心与票据 20 的汇总邮件；异常读取留在领域层
  （`AnomalyService.day_anomalies`），供票据 24 的补卡页与后续报表使用。
- **迁移号**：0016，接在票据 28 的 0015（`20260930_1500_timesheets.py`）之后。
