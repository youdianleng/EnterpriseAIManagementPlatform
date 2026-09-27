# 20 — 每日汇总邮件与 Mailpit

**What to build:** 每位经理每天早上收到**一封**邮件，列出其下属前一天的全部异常（缺卡、迟到、早退、未提交工时等）。开发与演示环境用 Mailpit 接住邮件，任何人都能在界面上看到实际发出的邮件内容。

**Blocked by:** 19 — 通知中心与投递追踪

**Status:** done

- [x] 邮件通过可配置的 SMTP 发送；开发与演示环境的配置指向本地邮件捕获服务，不产生真实外发
      — `api/app/mail.py`：`Mailer` 协议（`enabled` + `send`）、`SmtpMailer`（stdlib `smtplib`，Per-message 连接，STARTTLS 可选，
      用户名/密码必须成对），配置项 `MAIL_ENABLED` / `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` /
      `SMTP_STARTTLS`。默认**关**且指向 `mailpit:1025`；`docker-compose.yml` 新增 `mailpit` 服务（含 healthcheck、8025 UI）
      并把 api 的 `MAIL_ENABLED=true` 与 `SMTP_*` 指过去，README 与 `.env.example` 均已记录。
      测试：`test_the_declared_defaults_are_off_and_pointed_at_mailpit`、`test_the_envelope_is_a_text_part_and_an_html_part_in_that_order`、
      `test_a_disabled_sender_refuses_rather_than_trying`（主机名不可解析，若真去连会是 DNS 错误，收到的却是开关自己的原因）、
      `test_a_half_configured_relay_is_refused_rather_than_guessed_at`。
      **测试不碰任何 SMTP**：`MAIL_ENABLED=false` 时 `test_with_mail_disabled_nothing_is_attempted_and_the_rows_say_why`
      断言一封都没交给发信器、投递行仍是 `pending`/0 次，`error` 写明 `mail is disabled (MAIL_ENABLED=false)`。
- [x] 定时任务每天 08:00（马德里时间）为每位有下属的经理生成并发送一封汇总邮件
      — `python -m app.jobs.send_daily_digests [YYYY-MM-DD] [--retry]`，默认马德里昨天；生产用 cron
      （`CRON_TZ=Europe/Madrid` + `0 8 * * * …`，README「Mail」一节给出整行）。任务本身不睡眠、不内置调度器。
      路由与系统其余部分同规则（`attendance/notify.py` 的三段式 `COALESCE(覆盖通知人, 主职位经理, 部门经理)`，
      即 `ApprovalService._resolve_level_one` 的规则前面加上覆盖人）：
      `test_a_manager_gets_one_mail_with_the_days_anomalies_grouped_by_report`、
      `test_a_position_with_no_manager_falls_back_to_the_department_manager`、
      `test_the_assignments_notification_override_hears_about_the_report`。
- [x] 邮件内容按下属分组，逐条列出异常类型与日期，并提供跳转到系统的链接
      — 按 report 分组（每人一次名字，其下逐条 `类型 · 日期`），链接为 `{WEB_BASE_URL}/{语言}/notifications`。
      HTML 与纯文本同源渲染（`digest.render`），HTML 内联样式取自 `web/app/globals.css` 的设计 token 字面量，且用户数据一律转义。
      测试：`test_a_manager_gets_one_mail_with_the_days_anomalies_grouped_by_report`（两处均断言）、
      `test_the_plain_text_part_says_the_same_thing_without_markup`、`test_the_html_part_carries_the_grouped_names_the_link_and_no_stylesheet`
      （无 `<style>`/`<link>`）、`test_a_name_that_contains_markup_is_escaped`。
- [x] 没有异常的经理**不发送**邮件（避免每日噪音）
      — 服务里的一行：没有异常的通知行不被带上，因而没有收件人，也就无从撰写。测试
      `test_a_clean_day_gets_no_mail` 同时断言三件事：`mailer.messages == []`、`daily_digests` 无行、
      队列里的投递行仍是 `pending`/0 次且原因被改写为 `nothing to report…`。数据库层再加一道：
      `test_the_database_refuses_a_digest_with_nothing_to_report`（`ck_daily_digests_anomaly_count`）。
      已消除的异常同样不进邮件：`test_a_resolved_anomaly_is_not_reported`。
- [x] 同一天对同一经理只会发送一次；任务重复执行或服务重启不会重复发信（幂等）
      — `daily_digests` 唯一键 `(recipient_employee_id, digest_date)` + `sent_at`；重复执行读到 `sent_at` 即跳过，
      不重新撰写。测试：`test_a_second_run_of_the_same_day_sends_nothing`（第三次用新会话＝重启的容器）、
      `test_the_unique_key_is_the_idempotency`（唯一约束本身）、
      `test_the_command_runs_the_whole_pass_through_the_restricted_role`（第二次 `main()` 输出 `0 digests sent`）。
- [x] 邮件主题与正文支持西/英双语，语言取收件人的语言偏好
      — `digest.DIGEST_COPY` 一份目录两种语言（含五种异常类型的标签，测试遍历 `AnomalyType` 防漏）；语言取
      `users.locale`（票据 20 随迁移 0018 落地，DESIGN §10.4 指定这一列），为 NULL 时取 `DIGEST_DEFAULT_LANGUAGE`（默认 `es`）。
      主题只写日期，不含人名（`test_the_subject_names_the_digest_and_the_date_and_nothing_else`）。
      测试：`test_the_language_is_the_stored_preference_and_then_the_default`、`test_english_is_english_in_both_parts`、
      `test_the_mail_is_written_in_the_language_the_account_stored`（真实库里 `UPDATE users SET locale='en'` 后是英文邮件，
      未设置的那位收到西语）。
      **个人资料页尚未写 `users.locale`**：这就是留给它的机制，见下"实现注记"。
- [x] 发送失败会记录失败原因并有限重试；重试仍失败时在投递追踪中可见
      — 失败即写 `daily_digests.attempts += 1` 与 `error`（发信方原话），被带上的投递行转 `failed` 并记同一原因；
      `DIGEST_MAX_ATTEMPTS`（默认 3）用尽后不再尝试、留在 `failed`；`--retry` 在修好邮件服务器后把未发送行的重试预算清零。
      测试：`test_a_failure_is_recorded_retried_and_then_left_failed`（三次各记一次、第四次不再尝试、
      投递行为 `("failed", 3, REFUSAL, None)`）、`test_a_retry_after_the_relay_comes_back_still_sends`
      （重试成功且 `sent_at` 落库；已发送的行不会被再发一次）、
      `test_the_queued_email_rows_move_to_sent_with_their_attempt_count`（通道/状态/次数/原因/时间五项齐全）。
- [x] 汇总邮件的生成与发送有测试，可在不真等到 08:00 的情况下触发
      — 所有日期都是固定的周一/周二，没有一处依赖运行当天；命令接受日期参数。
      测试：`test_the_command_defaults_to_yesterday_in_madrid`、`test_the_command_reads_its_date_and_its_flag`、
      `test_the_command_runs_the_whole_pass_through_the_restricted_role`（以 `eam_app` 角色跑完整遍，退出码 0）、
      `test_the_command_says_so_when_mail_is_switched_off`（关闭邮件是决定而不是失败，仍退出 0）。
      另有 `api/tests/test_daily_digest.py` 33 例（真实 PostgreSQL，唯一替身是 `app/mail.py` 的传输）。
- [x] Mailpit 界面中能看到完整的邮件渲染结果（HTML 与纯文本两版）
      — 信封固定为 `multipart/alternative`，纯文本在前、HTML 在后（`test_the_envelope_is_a_text_part_and_an_html_part_in_that_order`）；
      并在运行中的栈上实测：`docker compose up -d mailpit` 后以 `MAIL_ENABLED=true … python -m app.jobs.send_daily_digests 2026-09-21`
      发出两封，`GET http://localhost:8025/api/v1/messages` 读到两封，`/api/v1/message/{id}/raw` 显示
      `multipart/alternative` + `charset=utf-8` 的 text/plain（8bit）与 text/html（quoted-printable）、
      主题按 RFC 2047 编码；正文含分组姓名、`Retraso · 21/09/2026`、跳转链接。第二次运行 Mailpit 仍为 2 封。

**Acceptance record**

- Backend: `api/tests/test_daily_digest.py`（33 例，真实 PostgreSQL；SMTP 传输是唯一的接缝，测试里是记录用的替身）。
  `docker compose exec -e TEST_DATABASE_NAME=eam_test_d -e REDIS_URL=redis://redis:6379/9 -T api python -m pytest tests/test_daily_digest.py` → 33 passed。
- 迁移 `0018`（`20261001_1100_daily_digests.py`）：`daily_digests` 表 + `users.locale`；原写为 0017，
  与票据 24 同窗口撞号后让位并接在它的 `0017` 之后（与 0015/0016 同样的处理）。
- 新增：`api/app/mail.py`、`api/app/domain/notification/digest.py`、`digest_repository.py`、`digest_service.py`、
  `api/app/repositories/digest.py`、`api/app/jobs/send_daily_digests.py`、`api/tests/test_daily_digest.py`。
  修改：`api/app/config.py`（邮件与汇总设置）、`api/app/models/notification.py`（`DailyDigest`）、
  `api/app/models/account.py`（`users.locale`）、`api/app/domain/notification/{__init__,models,service}.py`、
  `api/tests/support/platform.py`（清理表加 `daily_digests`）、`docker-compose.yml`、`.env.example`、`README.md`、
  `docs/DESIGN.md`（§3.7 实现注记）。**未新增端点、未新增审计动作**，故 `test_permission_matrix.py` 未因本票改动。
- 未改动：`api/app/domain/approval/`、`personnel/`、`timesheet/`、`project/`、`schedule/`、`attendance/`（只读）与 `web/`。

---

**实现注记（2026-10-01，agent）**

- **语言取哪儿：`users.locale`，本票顺手把它落地了。** DESIGN §10.4 说"用户可手动切换，选择持久化到用户偏好（写入
  `users.locale`）"，而在本票之前这一列并不存在。三种选择里（写死西语、按部门推、落列）选第三种：没有这一列，
  "取收件人的语言偏好"这句话就只能靠默认值兑现，而多语言目录就成了一段无法证明的代码。现在：`users.locale` 可为空，
  `CHECK (locale IS NULL OR locale IN ('es','en'))`；NULL＝从未选择过，按 `DIGEST_DEFAULT_LANGUAGE`（默认 `es`）书写。
  **留给个人资料页的就是这一列**——它写进去之后，同一个渲染器立刻按人的偏好发信，后端不必再改。
  库里存了非 `es`/`en` 的怪值不会让当天早上崩掉：`language_of` 一路回退到默认值。
- **一句话说清"谁收到什么"**：本票的收件人集合是两路的并集——(1) 当天**仍然成立**的异常按路由找到的收件人（经理或覆盖通知人），
  (2) 这些异常对应的队列里那封"本人补卡提醒"的收件人（即员工本人）。两路合成一个按收件人索引的 map，所以
  同一个人只收到**一封**（经理自己也有异常时：一封里含"你自己"和"你的团队"两段，见
  `test_a_manager_who_is_also_a_report_gets_one_mail_with_both_sections`）。这样做的理由不是好看：票据 23 的验收条件
  写的是"次日早上向员工本人发出补卡提醒（站内 + 汇总邮件中的个人部分）"，而 `DIGEST_CANDIDATE_TYPES` 是票据 23
  特地为本票留的取值集合。若只发经理，员工的提醒邮件行会永远停在 `pending`，投递表就开始说假话。
- **"干净的一天"是服务里的一行，也是数据库里的一条约束。** 队列里没有异常的通知行不被带上 → 没有收件人 → 无从撰写；
  即便有人绕过服务手写一行 `anomaly_count = 0`，PostgreSQL 也会拒绝。两处都在测试里钉住。
- **失败与重试的分工。** `daily_digests.attempts` 是"重试预算"，`notification_deliveries.attempts` 是"这封通知实际被尝试过几次"。
  两者在 `--retry` 之后会不同（预算清零、累计照旧），这是有意的：前者回答"还能不能再试"，后者回答"一共试过几次"。
- **`--retry` 够不到已经发出去的行**（`sent_at IS NOT NULL` 不在其列），因为比"没送到"更糟的是"送到两次"。
- **已知边界（写下来而不是假装没有）**：汇总按天取用队列，因此**某天的任务跑完之后**才产生的通知（例如那天之后补扫出来的异常）
  不会再被那天的邮件带上——幂等键决定了"一天一次"，这是它的另一面。重新扫描历史日期仍是可用的（异常与站内通知都会补上），
  只是邮件不再补发；需要时以 `--retry` 重跑该日，未发送的收件人会被重试。
- **未做**：未接 08:00 的调度器（README 给 cron 行，与 `apply_personnel_changes` 同一立场）；未把汇总邮件本身做成
  `notifications` 行（它是既有通知的邮件那一半，不是新事件）；未给邮件写审计记录（`daily_digests` 与投递行即是记录）。
