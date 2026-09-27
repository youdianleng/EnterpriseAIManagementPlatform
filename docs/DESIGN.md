# 企业内部 AI 管理系统 — 设计总结（待确认）

> 状态：**设计确认稿**。本文档在获得确认前不产生任何实现代码。
> 本文档是四轮追问（Q1–Q48）的结果汇总，每个决定都标注了来源问题编号。
> 事实类结论（西班牙/欧盟法规）已核实并附来源；决策类结论来自用户回答或用户明确接受的推荐。

---

## 0. 一页纸概览

| 维度 | 结论 |
|---|---|
| 系统性质 | 单企业私有系统，单租户（Q1） |
| 语言 | 界面西班牙语为主、英语为辅；文档引用保留原文不翻译（Q2、补充 3） |
| 法域 | 西班牙 / 欧盟（GDPR、RD-ley 8/2019、EU AI Act） |
| 规模基线 | ~100 员工、峰值 50 并发会话、≤10,000 份文档、部门 4 层 |
| 定位 | **自我技术项目**——主动砍掉法务级复杂度，保留架构正确性与演示完整性 |
| 后端 | Python + FastAPI + PostgreSQL(pgvector) + Redis |
| 前端 | React + Next.js + TypeScript + Zustand + Tailwind CSS |
| AI | OpenAI 为主，多供应商自动降级；LangChain + LangGraph 编排；RAG 流式输出 |
| 部署 | 单机 Docker Compose（7 个服务），HTTP 局域网，无 HTTPS |
| 测试 | pytest + docker compose 测试库 + Alembic + seed 数据 + Playwright E2E |

### 核心业务模块（7 个）

1. **组织与人事**：部门树、职位、员工档案、一人多职位、入转调离
2. **考勤**：Web 打卡、异常检测、补打卡、年假 30 自然日、加班累计导出
3. **工时表**：项目 → 任务两级、可计费标记、按周提交锁定、8 周补填窗口
4. **审批引擎**：固定两级（直属经理 → HR），被请假/补打卡/工时/人员变动共用
5. **薪酬档案**：薪酬记录 + 工资单 PDF（财务上传、员工可见最近 3 份）
6. **培训**：课程、报名、完成记录（轻量）
7. **RAG 知识库 + Agent**：三级密级文档问答、只读工具、写操作产出待确认草稿

---

## 1. 全局决策表

| # | 决策 | 结论 | 来源 |
|---|---|---|---|
| D1 | 租户模型 | 单企业单租户；字段保留 `org_unit` 但**不引入** `tenant_id` | Q1 |
| D2 | 语言 | 西语主 / 英语辅；i18n 字典驱动，错误码双语 | Q2 |
| D3 | 法域 | 西班牙 + 欧盟 | Q2 |
| D4 | 规模 | 100 员工 / 50 并发 / 1 万文档 | Q3、Q45 |
| D5 | 考勤方式 | Web 点击上下班；**不做** GPS、**不做**生物识别 | Q11 |
| D6 | 作息 | 部门级配置 + 员工级覆盖；支持兼职；节假日表由 HR 维护（可导入，不硬编码） | Q22 |
| D7 | 年假 | **30 自然日**，按工作日粒度申请，额度可配置（`annual_leave_days=30`） | Q24 |
| D8 | 工时 | 项目→任务两级、billable 由项目配置、周一起算、周锁定、8 周补填窗口 | Q15、Q30 |
| D9 | 薪酬深度 | **只存档 + PDF 自助下载；明确不计算工资单**（非目标） | Q13 |
| D10 | 审批 | **固定两级**（直属经理 → HR），但引擎内部做成可配置内核 | Q14 |
| D11 | 密级 | low / medium / high，**双条件**：`clearance(doc) ≤ clearance(user)` **且** `dept(doc) ⊆ depts(user)` | Q6、Q28 |
| D12 | 权限来源 | 多职位**并集**（部门并集、密级取最高）；主职位决定审批上级 | Q43 |
| D13 | 登录 | 账号密码 + Redis 服务端会话；admin 建号 → SHA-256 临时密码 → 首登强制改 | Q8、Q26 |
| D14 | 密码策略 | 至少 8 位、含大小写 + 数字 + 特殊符号；忘记密码由 IT/admin 重置 | Q26 |
| D15 | 个人信息边界 | **只存**姓名、地址、照片、公司资料；不存身份证/银行卡/健康/生物特征 | Q9 |
| D16 | 集成 | **零外部系统集成**（无 SSO、无 HR 系统、无 IM） | Q10 |
| D17 | 模型 | OpenAI 主，DeepSeek / Claude 备；Provider 抽象 + 自动降级 | Q17、Q45 |
| D18 | 对话留存 | 90 天自动清除；**仅 compliance 角色可查**；用户可自行删除 | Q18 |
| D19 | 引用 | 回答强制带引用（文件名 + 页码 + 原文片段） | Q38 |
| D20 | 检索为空 | **明确拒答**，绝不用模型自身知识兜底 | Q29、Q38 |
| D21 | 个人文档 | 默认仅自己可见；可公开给本部门；公开后仍受密级约束；**不进公司检索池** | Q29、Q44 |
| D22 | Agent 写操作 | **永不直接写库**，只产出 `PrefillForm` 草稿，员工显式确认后由本人名义提交 | Q40 |
| D23 | Agent 硬禁止 | 查他人薪资/考勤、生成绩效或晋升建议、直接改库 | Q39 |
| D24 | 审计日志 | 追加式表 + 禁用 UPDATE/DELETE；保留 **4 年**；与运行日志物理分离 | Q32、Q47 |
| D25 | 更正模型 | 考勤/工时采用**更正链**（保留原值 + 更正原因），不原地覆盖 | Q32 |
| D26 | 通知 | 通知中心 + 每日汇总邮件（Mailpit）；下班打卡后通知直属经理 | Q23、Q41 |
| D27 | 工资单 | 财务按月份批量上传；员工可见最近 N 份（默认 3，可配置）；可撤回并通知已下载者 | Q31、Q42 |
| D28 | 数据权利 | 实现数据导出（JSON + PDF 打包）；界面明示留存期与删除限制；文档维护 RoPA | Q33 |
| D29 | 缓存 | Redis 存会话/队列/限流 + 组织树/节假日/权限快照；**权限缓存由写操作主动失效** | Q46 |
| D30 | 可观测性 | 结构化 JSON 日志 + request_id；统一错误码双语；**不引入 Prometheus/Grafana** | Q47 |
| D31 | 时区 | 存 UTC，展示层转 `Europe/Madrid`；考勤"业务日"按马德里时间切分 | 补充 1 |
| D32 | 数据库防线 | 应用层过滤 + **PostgreSQL Row-Level Security 兜底** | 补充 5 |
| D33 | 部署 | Docker Compose 7 服务，局域网 HTTP，**保留每日 pg_dump** | Q20、Q48 |
| D34 | 编排 | LangChain + LangGraph（`interrupt` + Postgres checkpointer 实现人审点） | 补充 6 |
| D35 | 测试 | pytest + 真实 Postgres + Alembic + seed + Playwright 主干流程 | Q21、Q34 |

---

## 2. 系统架构

### 2.1 服务拓扑（Docker Compose）

```
                    ┌─────────────────────────────────────┐
  浏览器 ──HTTP──▶  │  web (Next.js 15, port 3000)        │
                    └──────────────┬──────────────────────┘
                                   │ /api/v1  (OpenAPI → TS 类型生成)
                    ┌──────────────▼──────────────────────┐
                    │  api (FastAPI + uvicorn)            │
                    │  · 认证/会话   · 权限判定            │
                    │  · 业务领域     · RAG 流式 (SSE)     │
                    │  · LangGraph 编排入口                │
                    └──┬────────┬────────┬────────┬───────┘
                       │        │        │        │
        ┌──────────────▼─┐ ┌────▼────┐ ┌─▼──────┐ ┌▼──────────────┐
        │ postgres       │ │ redis   │ │ worker │ │ mailpit (dev) │
        │ + pgvector     │ │ 会话    │ │ 解析   │ │ SMTP 捕获     │
        │ + RLS          │ │ 缓存    │ │ 邮件   │ └───────────────┘
        │ + 审计表       │ │ 队列    │ │ 定时   │
        │ + LangGraph    │ │ 限流    │ │ 摘要   │
        │   checkpoints  │ └─────────┘ └────────┘
        └────────────────┘
                                   │
                        外部 LLM API（OpenAI / DeepSeek / Claude）
```

**服务清单（7 个）**：`web`、`api`、`worker`、`postgres`、`redis`、`mailpit`（dev only）、`caddy`（可选，仅有域名时启用）。

**为什么 worker 独立**：文档解析（PDF/DOCX/XLSX）、嵌入生成、每日汇总邮件、月度应出勤快照、PersonnelChange 生效落库——这些都不能占用 HTTP 请求周期（Q45）。

### 2.2 后端分层

```
app/
  api/v1/          路由层：只做参数校验 + 权限依赖注入 + 调 service
  core/            配置、错误码、日志、安全（密码/会话）、时区
  domain/          纯业务逻辑，不依赖 FastAPI（可单测）
    org/ employee/ attendance/ timesheet/ approval/
    payroll/ training/ document/ notification/ audit/
  ai/              LLM Provider 抽象、LangGraph 图、工具注册表
    providers/     openai.py deepseek.py anthropic.py  (统一接口 + 降级链)
    agents/        graphs/（RAG 图、只读查询图、草稿生成图）
    tools/         只读工具 + 草稿工具（无写库工具）
    rag/           解析、分块、嵌入、混合检索、RRF、rerank、生成
  repositories/    SQLAlchemy 查询；RLS 上下文设置
  workers/         arq/Celery 任务（解析、邮件、摘要、快照）
  models/          SQLAlchemy ORM
alembic/           迁移
tests/             unit / integration（真实 Postgres）/ e2e（Playwright）
```

**关键原则**：`domain/` 不 import FastAPI、不 import LangChain。AI 只能通过调用 `domain/` 的**只读服务**或**草稿构造器**来影响系统——这在代码结构层面就保证了 D22（Agent 永不写库）。

---

## 3. 数据模型

### 3.1 组织与人事（12 张表）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `departments` | `id`, `code`, `name_es`, `name_en`, `parent_id`, `path`(ltree), `depth`, `clearance_level`, `manager_employee_id`, `cost_center`, `is_active` | 树用 `ltree` 做祖先查询；`clearance_level` 是该部门文档的默认密级 |
| `job_positions` | `id`, `code`, `title_es`, `title_en`, `department_id`, `is_managerial`, `is_active` | 职位模板 |
| `employees` | `id`, `employee_no`, `first_name`, `last_name`, `preferred_name`, `email`, `photo_path`, `address_line`, `city`, `postal_code`, `country`, `hire_date`, `termination_date`, `status`(active/leave/terminated), `birth_date`, `emergency_contact`(JSONB) | Q9 允许的字段**仅此**；无身份证/银行卡/健康字段 |
| `employee_assignments` | `id`, `employee_id`, `department_id`, `job_position_id`, `is_primary`, `manager_employee_id`, `notification_override_employee_id`, `start_date`, `end_date` | **一人多职位的核心表**；`is_primary` 默认第一个，仅 admin 可改（Q43） |
| `users` | `id`, `employee_id`(unique), `username`, `password_hash`, `must_change_password`, `roles`(JSONB), `clearance_level`, `is_active`, `session_epoch`, `last_login_at`, `password_changed_at` | 账号与员工**一对一**。角色为 `roles` JSONB 数组（见下方实现注记），临时密码为 Argon2id（见 §10.2） |
| `roles` / `user_roles` / `role_permissions` | — | 角色权限矩阵的可数据化部分 |
| `work_schedules` | `id`, `department_id`(nullable), `name`, `weekly_hours`, `is_default` | 部门级作息（Q22） |
| `work_schedule_days` | `schedule_id`, `weekday`, `expected_minutes`, `start_time`, `end_time`, `break_minutes` | 周一至周四 8h、周五 intensivo 6h 之类 |
| `employee_schedule_overrides` | `employee_id`, `schedule_id`, `effective_from`, `effective_to`, `reason` | 员工级覆盖（含兼职 jornada parcial） |
| `holidays` | `id`, `date`, `name_es`, `scope`(national/regional/local), `region_code`, `year` | **可导入的数据表，不硬编码**（Q22） |
| `personnel_changes` | `id`, `employee_id`, `change_type`(join/transfer/promotion/termination/salary), `effective_date`, `payload`(JSONB 字段变更明细), `approval_request_id`, `status`(draft/pending/approved/applied/cancelled), `applied_at` | 入转调离统一单据（Q25） |

**实现注记（2026-09-26）：** `users` 的角色由六个 `is_*` 布尔列改为 `roles` JSONB 数组，值域由数据库触发器约束在固定集合内（迁移 `0005`），`clearance_level` 与 `session_epoch` 同批落地（迁移 `0006`）。理由：角色集合固定且很小，但每加一个角色就要加一列、改一次判定；数组让"这个人有哪些角色"是一次读取，而不是六次。若角色将来获得属性或范围，这里改成关联表，判定内核只改一处。

### 3.2 考勤与请假（7 张表）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `attendance_events` | `id`, `employee_id`, `event_type`(clock_in/clock_out/correction), `occurred_at`(UTC), `business_date`(Madrid), `source`(web/correction), `ip_address`, `created_by`, `correction_of_event_id`, `reason` | **追加式事件流**，永不 UPDATE（D25） |
| `attendance_daily` | `id`, `employee_id`, `business_date`, `first_in`, `last_out`, `worked_minutes`, `expected_minutes`, `overtime_minutes`, `status`(ok/missing_out/late/absent/holiday/leave), `is_locked`, `snapshot_schedule_id` | 由事件推导的快照表，加速查询；**快照当时作息**以便 4 年追溯 |
| `attendance_anomalies` | `id`, `employee_id`, `business_date`, `type`(missing_clock_out/missing_clock_in/late/early_leave), `detected_at`, `notified_at`, `resolved_by_event_id` | Q23 的异常检测落库 |
| `leave_types` | `id`, `code`, `name_es`, `name_en`, `is_paid`, `requires_attachment`, `counts_against_annual`, `is_active` | 病假/事假/产假等 |
| `leave_balances` | `id`, `employee_id`, `year`, `leave_type_id`, `entitled_days`(默认 30 自然日), `used_days`, `pending_days`, `carried_over_days` | 额度参数化（D7） |
| `leave_requests` | `id`, `employee_id`, `leave_type_id`, `start_date`, `end_date`, `business_days_count`, `reason`, `status`, `approval_request_id`, `attachment_path` | 按工作日粒度 |
| `overtime_records` | `id`, `employee_id`, `business_date`, `minutes`, `reason`, `pre_approved_by`, `approval_request_id`, `month_bucket`(YYYY-MM), `exported_at` | 月底导出给财务（Q12） |

> **实现注记（票据 22）：** 五处与上表不同，均为有意为之。**作息挂在部门上、以部门为粒度定位日历，地区则落在 `departments.region_code`（ISO 3166-2，如 `ES-MD`）**：节假日按"国籍 + 工作地"生效，所以某人的地区来自**当日生效的主职位所属部门**——调岗当天就换成新自治区的日历，而兼职覆盖不会改变它（覆盖改的是工时，不是工作地点）。人身上不加地区字段：那会是一份和办公地点迟早对不上的手工副本。**`work_schedules` 增 `code` 与 `is_active`，`work_schedule_days` 每周几一行**；`expected_minutes` 由数据库 CHECK 校验（上班日必须等于 `end_time - start_time - break_minutes`，休息日三者皆空），**不是**由窗口推导——四年后读者应直接读到当时的数字，而一段对不上的窗口是数据错误，不该被静默推导掉；`weekly_hours` 相反，是服务按天求和写回的派生值，一份和写两遍必然对不上。**覆盖带生效期且不允许重叠**（`btree_gist` 排他约束），否则"三月按什么算"会有两个答案。**新增 `expected_hours_snapshots`：每人每月一条追加式记录（`REVOKE UPDATE, DELETE`），`inputs` JSONB 冻结当日逐日的作息 id、窗口、分钟数、来源与所引用的节假日行**——月度应出勤的月度级快照，与 `attendance_daily` 的逐日快照互补；重算只追加新 revision，旧 revision 永远可读。**`attendance_daily.status` 增加 `holiday` 与 `non_working`**：票据 21 的五个值只覆盖"从打卡能看出来的事"，而零工时的日子不是缺勤——`late` 仍留给票据 23（它需要阈值，不只是作息）。

### 3.3 项目与工时（6 张表）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `projects` | `id`, `code`, `name`, `client_name`, `department_id`, `manager_employee_id`, `is_billable_default`, `status`, `start_date`, `end_date` | billable 由项目配置（Q30） |
| `project_tasks` | `id`, `project_id`, `code`, `name`, `is_billable`, `is_active` | 二级任务 |
| `timesheets` | `id`, `employee_id`, `week_start_date`(周一), `status`(draft/submitted/approved/rejected/locked), `submitted_at`, `approved_by`, `total_minutes`, `is_supplementary`, `supersedes_timesheet_id` | 一人一周一条 |
| `time_entries` | `id`, `timesheet_id`, `employee_id`, `work_date`, `project_id`, `task_id`, `minutes`, `is_billable`, `note`, `entry_type`(normal/reversal) | **每人每天每任务**的小时数（Q30） |
| `timesheet_weeks_lock` | `week_start_date`, `locked_at`, `locked_by` | 全局周锁定 |
| `supplementary_windows` | `employee_id`, `week_start_date`, `allowed_until` | 8 周补填窗口校验（Q30） |

### 3.4 审批引擎（4 张表）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `approval_flows` | `id`, `code`, `name`, `entity_type`(leave/attendance_correction/timesheet/personnel_change), `is_active` | 每种业务一条流程定义 |
| `approval_flow_steps` | `flow_id`, `step_order`, `approver_type`(direct_manager/hr/it/finance), `is_required`, `allow_delegation` | **固定两级**：step 1 直属经理、step 2 HR（Q14） |
| `approval_requests` | `id`, `flow_id`, `entity_type`, `entity_id`, `requester_employee_id`, `status`(pending/approved/rejected/cancelled), `current_step`, `initiated_by`(user/agent), `confirmed_by_user_id`, `submitted_at`, `decided_at` | `initiated_by=agent` 是 D22 的审计落点 |
| `approval_decisions` | `id`, `request_id`, `step_order`, `approver_employee_id`, `decision`(approve/reject/return), `comment`, `decided_at`, `delegated_from` | 追加式，不可改 |

**状态机（所有单据共用）**：

```
draft ──submit──▶ pending(step1) ──approve──▶ pending(step2) ──approve──▶ approved ──▶ applied(生效日)
                       │                              │
                       └──reject──▶ rejected ◀────────┘
                       └──return──▶ draft（退回补正，保留历史决定）
```

### 3.5 薪酬与培训（5 张表）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `salary_records` | `id`, `employee_id`, `effective_from`, `effective_to`, `base_salary`, `currency`, `pay_frequency`, `components`(JSONB 津贴明细), `change_reason`, `created_by` | 薪酬**档案**，不做计算（D9） |
| `payslips` | `id`, `employee_id`, `period`(YYYY-MM), `file_path`, `file_size`, `checksum_sha256`, `uploaded_by`, `upload_batch_id`, `status`(published/withdrawn), `withdrawn_at`, `withdraw_reason`, `download_count` | 财务按月份批量上传（Q31） |
| `payslip_batches` | `id`, `period`, `uploaded_by`, `total_count`, `success_count`, `missing_employee_ids`(JSONB) | 上传清单 + 缺失清单 |
| `training_courses` | `id`, `code`, `title_es`, `title_en`, `description`, `is_mandatory`, `duration_hours`, `validity_months`, `is_active` | 培训目录 |
| `training_enrollments` | `id`, `employee_id`, `course_id`, `status`(enrolled/in_progress/completed/failed/expired), `enrolled_at`, `completed_at`, `score`, `certificate_path`, `expires_at` | 完成记录与证书有效期 |

### 3.6 文档 / RAG / Agent（10 张表）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `documents` | `id`, `title`, `owner_employee_id`(nullable=公司文档), `department_id`(nullable), `clearance_level`(low/medium/high), `visibility`(private/department/company), `is_company_kb`(bool), `category`, `tags`(JSONB), `current_version_id`, `status`(processing/ready/failed/archived), `language` | Q6/Q28/Q29/Q44 的落点 |
| `document_versions` | `id`, `document_id`, `version_no`, `original_path`, `mime_type`, `file_size`, `checksum_sha256`, `page_count`, `text_extracted_at`, `chunking_version`, `embedding_model`, `embedding_dim`, `uploaded_by`, `is_current` | **保留原始文件**，引用回链的锚点（Q36） |
| `department_clearances` | `department_id`, `granted_clearance_level` | 部门可授予的最高密级 |
| `document_permissions` | `document_id`, `subject_type`(employee/department/role), `subject_id`, `permission`(read) | 显式共享（覆盖默认规则，但**不能突破密级上限**） |
| `document_chunks` | `id`, `version_id`, `document_id`, `parent_chunk_id`, `chunk_index`, `content`, `token_count`, `page_from`, `page_to`, `heading_path`, `embedding`(vector), `tsv`(tsvector) | **父子分块**（D-Q37） |
| `knowledge_bases` | `id`, `code`, `name`, `description`, `is_company_kb` | 首期只有两个：`company` 与 `personal`（后者按 owner 过滤） |
| `rag_conversations` | `id`, `user_id`, `title`, `created_at`, `last_message_at`, `expires_at`(=created+90d), `deleted_by_user` | Q18 的 90 天留存 |
| `rag_messages` | `id`, `conversation_id`, `role`(user/assistant/tool), `content`, `citations`(JSONB), `model_used`, `provider_used`, `token_in`, `token_out`, `latency_ms`, `retrieval_debug`(JSONB: 命中的 chunk id 与分数), `is_refusal` | 引用与检索可追溯 |
| `agent_actions` | `id`, `conversation_id`, `user_id`, `thread_id`(LangGraph), `tool_name`, `tool_input`(JSONB), `tool_output`(JSONB), `produced_prefill_form`(JSONB), `status`(proposed/confirmed/rejected/expired), `confirmed_at`, `resulting_entity_type`, `resulting_entity_id` | D22 的核心审计表 |
| `agent_checkpoints` | — | LangGraph Postgres checkpointer 的表（由库自动管理） |

### 3.7 通知与审计（6 张表）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `notifications` | `id`, `recipient_user_id`, `type`, `title_key`, `payload`(JSONB), `entity_type`, `entity_id`, `is_read`, `created_at`, `expires_at` | 通知中心 |
| `notification_deliveries` | `id`, `notification_id`, `channel`(inapp/email), `status`(pending/sent/failed), `attempts`, `error`, `sent_at` | 投递追踪 |
| `daily_digests` | `id`, `manager_employee_id`, `digest_date`, `payload`(JSONB), `sent_at`, `anomaly_count` | 每日汇总邮件去重（Q41） |
| `audit_log` | `id`(bigserial), `occurred_at`, `actor_user_id`, `actor_role_snapshot`, `action`, `entity_type`, `entity_id`, `before`(JSONB), `after`(JSONB), `reason`, `request_id`, `ip_address`, `user_agent`, `initiated_by`(user/agent/system) | **追加式；REVOKE UPDATE/DELETE；保留 4 年**（D24、Q32） |
| `data_export_jobs` | `id`, `employee_id`, `requested_by`, `status`, `file_path`, `expires_at`, `completed_at` | GDPR 数据导出（Q33） |
| `retention_policies` | `id`, `data_category`, `retention_months`, `legal_basis`, `notes` | RoPA 的数据化表达（Q33） |

> **实现注记（票据 19）：** 三处与上表不同，均为有意为之。**收件人是 `recipient_employee_id` 而非 `recipient_user_id`**：通知发给**人**，而人未必有账号（员工与账号一对一，但不是每个员工都有账号），且权限快照里携带的正是 `employee_id`。**已读落为 `read_at` 时间戳而非 `is_read` 布尔**：审查要问的是"什么时候看到的"，一列即可回答，而"未读"就是 `read_at IS NULL`；时间戳只写一次，重复标记不改写首次阅读时间。**新增 `dedupe_key`，`(recipient_employee_id, dedupe_key)` 唯一**：同一事件对同一收件人只落一条，幂等由数据库唯一索引保证——应用层"先查后写"正是并发下会漏的那种写法；被抑制的第二次尝试写入审计（`notification.duplicate_suppressed`），否则"通知器跑了两次"和"通知器根本没跑"从外部看完全一样。另外两条由数据库约束表达：`title_key` 必须匹配点分小写键、`payload` 必须是 JSON 对象，因此"在记录里存拼接好的句子"会被 PostgreSQL 拒绝而不是靠评审发现；`expires_at` 只是读取过滤，过期通知不出现在列表与未读数中，行保留。邮件投递行在 mailer 落地（票据 20）之前为 `pending`，并在 `error` 写明"未尝试"，以免与"尝试过但失败"混淆。

**审计必须覆盖的动作**（Q32）：登录/登出/失败登录、权限与角色变更、密级变更、文档可见性变更、薪酬访问与工资单下载、审批决定、Agent 发起的操作、数据导出、工资单撤回、考勤/工时更正。

> **实现注记（票据 14）：** 动作目录在 `api/app/audit.py` 的 `AuditAction`，已落地的动作写入数据库；尚未实现的模块（文档、薪酬、审批、Agent、导出）先把常量登记在此，避免各处在用到时才临场拼写、拼成不同名字。记录入口只有 `record()` 一个，调用方只描述"变了什么"——操作者、IP、客户端由 `bind_actor()` 在解析出 principal 时写入请求上下文，因此给一处新写入加审计是一行代码，而不是把 actor 参数穿过三层。
>
> **两种保留期是不同的东西**：审计记录存数据库、保留 4 年（`AUDIT_RETENTION_DAYS=1460`，对应西班牙工时记录的 4 年义务）；运行日志写 stdout、保留 14 天（`LOG_RETENTION_DAYS=14`）。两者既不共用存储也不共用保留策略——随运行日志一起过期的"审计"不是审计。

---

## 4. 权限模型

### 4.1 角色

| 角色 | 范围 |
|---|---|
| `admin` | 系统管理；建号、改主职位、部门与密级配置；**看不到工资单内容**（职责分离） |
| `hr` | 员工档案全量、考勤/请假/工时全量、入转调离审批第 2 级、文档公司级管理 |
| `finance` | 薪酬档案、工资单上传与撤回、加班月度导出；**看不到员工对话** |
| `it` | 账号重置、会话强制登出；**看不到任何业务数据** |
| `compliance` | 只读审计日志、只读对话记录（留痕）、RoPA；**不能改业务数据** |
| `manager`（叠加在员工之上） | 查看**直属下属**的考勤、工时、请假；审批第 1 级 |
| `employee` | 本人全部数据 + 本部门通讯录 + 受密级约束的文档问答 |

### 4.2 文档访问判定（D11）

```
allow(user, doc) =
    doc.owner_employee_id == user.employee_id                  # 自己的文档
 OR (doc.is_company_kb AND clearance_ok AND dept_ok)
 OR explicit_grant(user, doc)                                  # 显式共享
 OR user.has_role('hr') OR user.has_role('compliance')          # 例外角色（仅公司文档）

clearance_ok = rank(doc.clearance_level) <= rank(user.clearance_level)
dept_ok      = doc.department_id ∈ descendants(user_departments)   # 含子部门
user_departments = 所有在职职位的部门（并集，含其子树）
user.clearance_level = max(所有在职职位部门的可授予密级) 与 用户显式密级 取高
```

**显式共享不能突破密级上限**——个人公开文档同样受 `clearance_ok` 约束（Q44）。

### 4.3 RAG 检索的权限下推

检索 SQL 的 `WHERE` 子句**必须**包含上述判定，作为**检索前过滤**（pre-filter），而不是检索后再筛：

```sql
-- 概念示意（实际由 SQLAlchemy + RLS 双重实现）
SELECT c.id, c.content, hybrid_score(c.embedding, c.tsv, :q, :vec) AS score
FROM document_chunks c
JOIN documents d ON d.id = c.document_id
WHERE c.version_id = d.current_version_id
  AND d.status = 'ready'
  AND (
        d.owner_employee_id = :me
     OR d.document_id IN (SELECT document_id FROM allowed_documents_for(:me))
      )
ORDER BY score DESC
LIMIT 20;
```

两层防御：**应用层 SQL 过滤 + PostgreSQL RLS 策略**（D32）。RLS 通过 `SET LOCAL app.current_employee_id` / `app.current_clearance` 在每个事务内生效。

### 4.4 缓存失效清单（D29）

| 缓存键 | TTL | 主动失效触发点 |
|---|---|---|
| `perm:user:{id}` | 5 min | 改角色、改密级、改部门、改职位、离职生效 |
| `org:tree` | 1 h | 部门增删改、员工调动生效 |
| `holidays:{year}` | 24 h | HR 修改节假日表 |
| `session:{sid}` | 8 h 滑动 | 登出、改密、admin 强制登出 |
| `schedule:emp:{id}` | 1 h | 作息覆盖变更、部门作息变更 |

**权限缓存的失效必须由写操作显式触发**，不能只靠 TTL。

---

## 5. RAG 管道

### 5.1 入库（异步，worker 执行，前端显示进度）

```
上传 → 落盘原始文件 + documents/document_versions 记录(status=processing)
     → 队列任务：
        1. 文本抽取：PyMuPDF(PDF, 带页码) / python-docx / openpyxl / 纯文本
        2. 结构感知切分：按 heading_path 先切章节
        3. 父块（~1500 token）+ 子块（~400 token，重叠 15%）
        4. 嵌入：text-embedding-3-large（provider 抽象，可切 bge-m3）
        5. 写入 document_chunks（embedding + tsv 同时生成）
        6. status=ready，通知上传者
     失败 → status=failed + 原因（明确拒绝扫描件：提示"未提取到文本，请上传文字版"）
```

**嵌入维度在 Alembic 迁移里定死**（pgvector 列维度不可改），并记录在设计文档中以免后期返工。若使用 `text-embedding-3-large` 的 3072 维，将 HNSW 索引建在 `halfvec` 上（pgvector ≥ 0.7）以节省内存与加速；否则降到 1536 维。

### 5.2 查询（流式 SSE）

```
用户提问（西/英/中）
  → 权限上下文快照（部门并集 + 密级 + 个人文档开关）
  → 查询改写（可选：LangGraph 节点，做指代消解/关键词扩展）
  → 混合检索：pgvector 向量 top 20 ∥ Postgres 全文检索 top 20
  → RRF 融合 → rerank → top 5（父块内容 + 子块定位）
  → 阈值判定：最高分 < threshold → 直接返回"知识库中未找到依据"（D20，不调用生成模型）
  → 生成（流式）：强制引用 [文件名 p.12]
  → 落库 rag_messages（含 citations + retrieval_debug + model_used/provider_used）
```

**回答规则**：
- 引用格式统一为 `《文件名》第 N 页`，前端可点击回链到原文（PDF 定位到页）。
- 若命中的是个人文档，回答顶部追加标记：**"以下内容来自个人文档（非公司知识库）"**（Q29）。
- 检索不到依据时**明确拒答**，不得使用模型自身知识作答（D20）。
- 回答语言跟随提问语言；引用原文不翻译（补充 3）。

### 5.3 Provider 抽象与降级链（D17）

```python
# 统一接口
class LLMProvider(Protocol):
    async def stream_chat(messages, tools=None) -> AsyncIterator[Chunk]: ...
    async def embed(texts) -> list[list[float]]: ...
    name: str

# 降级链（按配置顺序尝试；仅"错误/超时"触发降级）
CHAT_CHAIN   = [openai:gpt-4o, deepseek:deepseek-chat, anthropic:claude-*]
EMBED_CHAIN  = [openai:text-embedding-3-large]   # 嵌入维度固定，降级仅限同维度
```

**降级只在技术失败时发生**（超时、429、5xx、连接错误）。**绝不因为"回答质量下降"而自动切换模型**——那会让审计日志里的 `model_used` 失去解释力。每次降级都记入 `rag_messages.provider_used` 与结构化日志。

**嵌入模型的降级约束**：不同模型的向量维度不同，**不可跨维度降级**。若必须切换嵌入模型，需走"重新嵌入整个知识库"的迁移任务——这是设计上唯一的硬耦合点，需在文档中显著标注。

---

## 6. Agent 设计（LangGraph）

### 6.1 图结构

```
                    ┌──────────────┐
  用户消息 ────────▶│  classify    │  意图分类：闲聊 / 制度问答 / 只读查询 / 待办操作
                    └──────┬───────┘
             ┌─────────────┼──────────────┬────────────────┐
             ▼             ▼              ▼                ▼
        ┌────────┐   ┌──────────┐   ┌───────────┐   ┌──────────────┐
        │ refuse │   │ RAG 图   │   │ 只读工具  │   │ 草稿生成     │
        │ 硬禁止 │   │ (第5节)  │   │ 调用      │   │ 工具调用     │
        └────────┘   └──────────┘   └───────────┘   └──────┬───────┘
                                                           │
                                              ┌────────────▼─────────────┐
                                              │ interrupt(): 等待人工确认 │
                                              │ 展示完整 PrefillForm      │
                                              │ (LangGraph checkpointer   │
                                              │  持久化，可跨请求恢复)     │
                                              └────────────┬─────────────┘
                                                   confirm │ reject
                                        ┌──────────────────┴──────────────┐
                                        ▼                                 ▼
                              ┌──────────────────┐              ┌────────────────┐
                              │ 以员工本人名义    │              │ 丢弃草稿       │
                              │ 提交 → 两级审批   │              │ 记录拒绝       │
                              │ initiated_by=agent│             └────────────────┘
                              └──────────────────┘
```

**LangGraph 的价值点**：`interrupt()` + Postgres checkpointer 让"等待员工确认"成为一个**可持久化、可跨 HTTP 请求恢复**的状态，而不是把表单塞进前端内存。这让"用户关掉浏览器第二天回来确认"也能正常工作。

### 6.2 工具注册表（白名单，无写库工具）

| 工具 | 类型 | 权限约束 |
|---|---|---|
| `get_my_attendance(range)` | 只读 | 仅本人 |
| `get_my_leave_balance(year)` | 只读 | 仅本人 |
| `get_my_timesheets(status)` | 只读 | 仅本人 |
| `get_colleague_contact(name)` | 只读 | 仅姓名/职位/邮箱/照片（Q27） |
| `get_team_attendance_summary(range)` | 只读 | 仅 manager 角色的直属下属 |
| `search_policy(question)` | 只读 | 走 RAG 权限过滤 |
| `draft_leave_request(...)` | 草稿 | 产出 PrefillForm，不写库 |
| `draft_attendance_correction(...)` | 草稿 | 产出 PrefillForm，不写库 |
| `draft_timesheet(...)` | 草稿 | 产出 PrefillForm，不写库 |

**硬禁止（代码级拒绝，且在系统提示中明确声明）**：查询他人薪资、查询他人考勤、生成绩效评价/晋升/解雇建议、任何直接写库操作、上传/删除文档（Q39、D23）。

「就业场景的 AI」在欧盟 AI 法案 [Annex III](https://www.lexology.com/library/detail.aspx?g=19b69b8c-4616-47f1-b1fd-a4c77cb790c0) 中属高风险用途（招聘、晋升、解雇、任务分配）。本设计**主动排除**这些能力，因此系统定位为"内部知识助手 + 只读查询 + 草稿助理"，不构成高风险 AI 系统。

### 6.3 人工审查点的实现要点（D22、Q40）

1. `PrefillForm` 必须是**完整、可编辑的表单**，展示所有将写入的字段值。
2. "确认提交"必须是**显式按钮点击**，且需要重新校验权限与会话；**聊天里回一句"好的"不算确认**。
3. 提交后 `approval_requests.requester_employee_id = 员工本人`，`initiated_by = 'agent'`，`confirmed_by_user_id = 确认人`。
4. `agent_actions` 表全程留痕：工具入参、出参、生成的草稿、确认状态、最终实体 ID。
5. 草稿有**过期时间**（默认 24h），过期后 `status=expired`，需重新生成。

### 6.4 Agent 可观测性（见 §10 的待决问题）

---

## 7. 关键业务流程

### 7.1 打卡与异常（Q11、Q23）

```
员工点击"上班" → 写 attendance_events(clock_in, UTC + business_date(Madrid))
员工点击"下班" → 写 attendance_events(clock_out)
                → 触发通知：直属经理（或 employee_assignments 上的覆盖通知人）
                → 通知中心即时 + 进入次日汇总邮件队列
夜间定时任务（worker）：
  扫描当日 attendance_daily：
    缺 clock_out → 生成 attendance_anomalies(missing_clock_out)
    缺 clock_in  → missing_clock_in
    未达应出勤   → 标记（不自动扣年假）
  次日 08:00 → 汇总邮件给经理 + 站内提醒员工补卡
```

**补打卡**：员工提交 `attendance_correction` → 两级审批 → 通过后写一条 `attendance_events(event_type=correction, correction_of_event_id=...)`，**原事件保留不动**（D25），`attendance_daily` 重新推导。

### 7.2 请假（Q12、Q24）

```
员工按工作日粒度选日期 → 系统计算 business_days（排除周末+节假日表）
                      → 校验 leave_balances（默认 30 自然日额度，参数化）
                      → 提交 → 两级审批（直属经理 → HR）
                      → 通过后：pending_days 转 used_days，写入考勤日历为 leave
提前撤销：仅允许在开始日期之前撤回，撤回后释放额度并留审计
```

### 7.3 加班（Q12）

```
加班**事前申请** → 两级审批 → 通过后写入 overtime_records
实际打卡时长与申请时长不一致 → 取较小值并标记待 HR 确认
月底：worker 汇总 month_bucket 内的 overtime_records
     → 生成导出文件（XLSX/CSV）供财务下载
     → 记录 exported_at（同一批次重复导出会留痕，不阻止）
```
系统**只累计与导出，不计算加班费**——加班费计算属于财务职责（Q12）。

### 7.4 工时表（Q15、Q30）

```
周一~周日为一周期；员工在 timesheet 中按"每人每天每任务"填分钟数
提交 → 两级审批 → 通过后 timesheet.status=locked
周锁定：timesheet_weeks_lock 记录已锁周
补充提交：对上锁周发起 → 生成 is_supplementary=true 的新 timesheet
        + 对原条目生成 entry_type=reversal 的冲销 entry（负值）
        + 新条目正数 → 净额正确，原记录永久保留
补填窗口：仅允许补最近 8 周（supplementary_windows 校验）
billable：由项目/任务配置决定，员工不可自行修改
```

### 7.5 工资单（Q31、Q42）

```
财务选择月份 → 批量上传 PDF（文件名含 employee_no 或选择员工）
            → 系统生成 payslip_batches + 发放清单 + **缺失清单**
            → status=published，通知员工
员工：可见最近 N 份（N 可配置，默认 3），可下载（download_count + 审计留痕）
撤回：财务撤回 → status=withdrawn → 员工列表立即消失，
     但显示占位"该月工资单已撤回，请联系财务"
     → 给所有 download_count>0 的员工发消息
```
**不允许**在界面上暗示"你还有更早的工资单但看不到"（Q42）。

### 7.6 入转调离（Q25）

```
HR 创建 personnel_changes 单据（类型 + 生效日期 + 字段变更明细 payload）
  → 两级审批
  → 审批通过后，worker 在**生效日当天**执行落库（不是审批通过即刻）
  → 离职：生效日禁用账号（users.is_active=false）+ 清 Redis 会话
         employee.status=terminated
         考勤/工时/工资单记录**保留不动**（法定追溯期）
```

### 7.7 GDPR 数据导出（Q33）

```
员工在个人中心点击"导出我的数据"
  → 生成异步任务：JSON（结构化数据）+ PDF（可读摘要）打包为 ZIP
  → 落盘 + data_export_jobs 记录
  → 通知员工下载链接（有效期 7 天）
界面同时展示：各类数据的留存期与"为何不能删除"的说明（retention_policies）
```

---

## 8. 安全与合规

### 8.1 已核实的法定义务

| 义务 | 依据 | 设计落点 |
|---|---|---|
| 逐日工时记录，保存 4 年，员工/工会/劳动监察可查阅 | 西班牙 [RD-ley 8/2019](https://ejaso.com/conocimiento/el-registro-de-jornada-laboral-tras-cinco-anos-de-su-implantacion) | `attendance_events` 追加式 + `attendance_daily` 快照 + 员工自助查询页 |
| 工时记录须可证明"当时如何计算" | 同上（追溯举证） | `attendance_daily.snapshot_schedule_id` 记录当时作息 |
| 特殊类别个人数据（健康）需明确合法性基础 | [AEPD FAQ](https://www.aepd.es/preguntas-frecuentes/2-tus-obligaciones-como-responsable-del-tratamiento/5-bases-legitimadoras-del-tratamiento/FAQ-0215-cuales-son-las-bases-de-legitimacion-para-el-tratamiento-de-las-categorias-especiales-de-datos) | 病假**只记录 leave_type**，不存诊断信息；附件加密存储且仅 HR 可见 |
| 就业场景 AI 属高风险，需透明度与日志 | [EU AI Act Annex III](https://www.lexology.com/library/detail.aspx?g=19b69b8c-4616-47f1-b1fd-a4c77cb790c0) | 系统**主动排除**招聘/晋升/解雇/任务分配类 AI 能力 |
| 年假下限 30 自然日（其中至少 22 工作日） | [ET art.38 参考](https://dogv.gva.es/datos/2022/03/11/pdf/2022_1704.pdf) | `annual_leave_days=30` 默认值 |
| 生成式 AI 服务备案（**仅面向公众时适用**） | [暂行办法](https://tech.ifeng.com/c/8RO3vRNWecQ) | 本系统为内部系统 + 私有部署，不面向公众，**不适用**；语料安全与内容标识要求仍遵循 [国标要求](http://www.chinaeic.net/xxgk/bzgf/zqyj/202405/W020240528353748437484.pdf) 的精神（引用可溯） |

### 8.2 安全控制清单

| 层面 | 控制 |
|---|---|
| 认证 | 密码 Argon2id 哈希；首登强制改密；密码策略（大小写+数字+特殊符号，≥8）；失败登录限流（Redis，5 次/15 分钟锁定）；会话存 Redis（服务端可强制登出） |
| 授权 | 应用层 SQL 过滤 + **PostgreSQL RLS** 双保险；权限判定集中在一个 `can(user, action, resource)` 函数，禁止散落各处 |
| 传输 | 局域网 HTTP（Q48 明确接受）；上传文件类型与大小白名单；文件名规范化防路径穿越 |
| 存储 | 原始文档与 PDF 存卷（非数据库）；敏感附件（病假证明）单独目录 + 仅 HR 角色可经 API 读取；数据库不存生物特征/身份证/银行卡 |
| 审计 | 追加式 `audit_log`，数据库层 `REVOKE UPDATE, DELETE`；应用连接使用**受限数据库角色**（无 DELETE 权限） |
| AI | 权限下推检索；检索为空拒答；无写库工具；提示注入防护（文档内容一律视为数据，不作为指令执行）；输出带引用 |
| 依赖 | 锁定版本（`uv.lock` / `pnpm-lock.yaml`）；镜像固定 digest |

### 8.3 明确列为非目标（避免范围蔓延）

- ❌ 西班牙工资单计算（社保基数、IRPF 预扣）
- ❌ 招聘 / 绩效 / 晋升 / 解雇相关的 AI 能力
- ❌ 排班（轮班）引擎
- ❌ GPS 定位打卡、人脸/指纹识别
- ❌ 与外部系统（SSO、HR、IM、OA）的集成
- ❌ 微服务、Kubernetes、多租户
- ❌ OCR 扫描件支持
- ❌ 原生移动 App（PWA 先行）

---

## 9. 非功能需求

| 项 | 目标 |
|---|---|
| 并发 | 峰值 50 并发会话，其中约 5 个同时进行 AI 流式请求 |
| 响应 | 普通 API p95 < 300ms；RAG 首 token < 2.5s；打卡 < 200ms |
| 可用性 | 单机部署，无 HA 要求；`/health` 与 `/ready` 端点 |
| 时区 | 存 UTC，展示 `Europe/Madrid`；考勤业务日按马德里时间切分 |
| i18n | 界面西/英双语；错误码双语；文档引用不翻译 |
| 日志 | **审计日志**（4 年，不可变）与**运行日志**（结构化 JSON，14 天滚动）物理分离存储 |
| 错误处理 | 统一错误码 `ERR_<DOMAIN>_<NNN>`；前端按码显示双语提示；AI 失败显示明确错误 + 重试按钮，**不静默降级为无检索回答** |
| 文件上传 | 异步解析 + 进度条（SSE 或轮询）；单文件上限 50MB |
| 缓存 | 见 §4.4；权限缓存写操作主动失效 |
| 备份 | 每日 `pg_dump` + 文档卷 rsync，保留 7 天，可通过环境变量关闭 |

---

## 10. 已确认的落地细节

### 10.1 LangSmith 与数据出境的冲突 → **已确认选项 (A)**

**事实**：LangSmith 是托管 SaaS（有 [US/EU region](https://docs.langchain.com/langsmith/regions-faq)），自托管版需要 [license key / 商务授权](https://support.langchain.com/articles/7011309930-how-do-i-obtain-a-self-hosted-langsmith-license-key)。而 Q18/Q33 已定：**员工对话内容只有 compliance 可查**。

**三个选项**：
- **(A) LangSmith Cloud 但做 PII 脱敏**：trace 里只发 `tool_name`、耗时、token 数、错误类型、节点流转；**对话文本与检索内容不发**。成本零，保留 LangGraph 调试能力。
- **(B) 自托管 LangSmith**：需 license，且要再加 4–5 个容器，对单机项目偏重。
- **(C) 改用 Langfuse**（可完全自托管、开源）：多 2 个容器（langfuse + 其 Postgres），但数据不出内网。

**决定：采用 (A) LangSmith Cloud + PII 脱敏过滤器。** 脱敏做成 provider 层的 trace 过滤器（一个函数，默认开启）：

```python
# ai/observability/trace_filter.py —— 唯一允许向 LangSmith 发送 trace 的出口
ALLOWED_TRACE_FIELDS = {"tool_name", "node_name", "latency_ms", "token_in",
                        "token_out", "error_type", "provider_used", "model_used",
                        "retrieval_hit_count", "is_refusal"}
FORBIDDEN = {"content", "messages", "prompt", "completion", "query",
             "chunk_text", "citations", "tool_input", "tool_output"}
```

**绝不发送**：对话文本、提示词、补全内容、检索到的文档片段、工具的入参与出参、引用内容。**只发送**：节点流转、工具名、耗时、token 计数、错误类型、命中文档**数量**（非内容）。该过滤器必须有单元测试，断言禁止字段不出现在序列化结果中。未来若需完整 trace，切换至选项 (C) Langfuse 自托管。

### 10.2 LangGraph checkpoint 存储位置 → **已确认：同库 Postgres**

用 `langgraph-checkpoint-postgres` 存在**同一个 Postgres** 的独立 schema（`langgraph`）中，**不引入 Redis checkpointer**。理由：Redis 用于缓存与会话（可丢），而 Agent 的 interrupt 状态**不可丢**（丢了员工确认到一半的草稿就没了）。

### 10.3 嵌入维度与向量索引 → **已确认：1536 维（实测后修正）**

**原文的前提是错的**，实施时实测纠正如下。

原推荐理由是"3072 维在 1 万文档规模下收益很小但索引内存翻倍，需用 halfvec 才划算"。实测发现这不是"划不划算"的问题：

| 方案 | 表大小 | 建索引 | 查询 p50 |
|---|---|---|---|
| `vector(1536)` + HNSW | 95.5 MB | 0.9 s | 0.27 ms |
| `halfvec(1536)` + HNSW | 77.7 MB | 1.1 s | 0.27 ms |
| `halfvec(3072)` + HNSW | 152.6 MB | 1.8 s | 0.31 ms |
| `vector(3072)` + HNSW | **无法建索引** | — | — |

**真正的硬约束**：pgvector 的 HNSW 索引对 `vector` 类型上限 2000 维、对 `halfvec` 上限 4000 维。因此 `vector(3072)` **根本建不了 HNSW 索引**——这不是性能取舍，是功能不可用。

**最终决定**：`vector(1536)` + HNSW + `vector_cosine_ops`，通过 `text-embedding-3-large` 的 `dimensions=1536` 参数降维输出。选择理由从"省内存"变为"用普通 `vector` 类型即可存储 API 原样返回的值，无需每次写入做半精度转换"。

**实现位置**：维度常量定义在 `api/app/core/constants.py`（`EMBEDDING_DIMENSIONS`），迁移中以字面量写入 DDL（迁移必须描述它当时实际应用的 schema），并由 `api/tests/test_database.py` 中的测试断言两者一致——因为 pgvector 的列维度是 schema 的一部分，常量与数据库不一致只会在运行时插入失败时才暴露。

**实测脚本保留在仓库中**：`api/tests/tools/probe_vector_dimensions.py`，可随时重跑复核。

### 10.4 界面默认语言 → **已确认：跟随浏览器**
默认读取 `Accept-Language` 决定西语/英语，用户可手动切换，选择持久化到用户偏好（写入 `users.locale`，登录后以用户偏好覆盖浏览器推断）。

### 10.5 新员工密级默认值 → **已确认：继承主职位部门**

新建员工时 `users.clearance_level` 初始值 = 主职位所属部门的 `clearance_level`。HR 只需在部门上配置一次，避免逐人手工作业出错。**admin 之后仍可单独提权或降权**（降权不追溯已授权的会话，但下一次权限判定立即生效，因为权限快照缓存在写操作时主动失效）。

> **实现注记（票据 12）：** 生效密级取"部门授予"与"用户显式值"两者的**较高者**（D12）。因此把显式值降到低于部门授予值时**不产生降权效果**——降权需要改部门或结束该职位。这也意味着 §10.5 中"admin 可单独降权"在当前规则下只对高于部门授予的部分成立。

### 10.6 行级安全的实测开销 → **实测：可忽略（票据 13）**

应用以受限角色 `eam_app` 连接，敏感表启用 RLS。开销实测（`tools/probe_rls_cost.py`，同一会话内对比；连跑两次的结果一并列出，因为差异本身就是结论的一部分）：

| 场景 | 规模 | 无策略 | 有策略 | 差值 |
|---|---|---|---|---|
| `count(*)` 顺序扫描 | 50 000 行 | 1.976 ms | 2.148 ms | +0.172 ms |
| 同上，重跑一次 | 50 000 行 | 2.211 ms | 2.134 ms | −0.077 ms（噪声内） |
| 单条语句扫描 | 100 行（`employee_private`） | 6.2 µs | 45.8 µs | +39.6 µs |
| 单条语句扫描 | 0 行 | 3.0 µs | 4.9 µs | +1.9 µs |

**读法**：5 万行量级上差值在噪声范围内（策略里的 `current_setting` 一个扫描只求值一次，逐行只剩比较）；空表上剩余的是**每条语句**约 2 µs 的装配开销；100 行时为 40 µs 量级，即每行约 0.4 µs。相对一次请求里的 Argon2id 校验（数十毫秒）与网络往返，这些都小到不影响设计取舍。

**真正的理由不是性能而是失败模式**：应用漏写过滤条件时，数据库返回 0 行而不是全部行。票据 35 会在文档规模上重测，那里的过滤是下推进 SQL 的，届时一并复核。

**实测脚本**：`api/tests/tools/probe_rls_cost.py`，可随时重跑复核。

---

## 11. 交付里程碑与验收标准

**原则**：每个里程碑都能独立部署演示（Q35）。

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| **M0 地基** | Compose 骨架、Alembic、CI 式本地测试脚本、seed 数据、i18n 框架、错误码、结构化日志、健康检查 | `docker compose up` 后 `/health` 通；`pytest` 全绿；seed 出 6 部门/100 员工/4 层树 |
| **M1 身份与权限** | 登录/会话/强制改密/admin 建号、部门树管理、职位、员工档案、一人多职位、密级与角色、**权限判定函数 + RLS**、审计日志框架 | 权限矩阵集成测试全绿；越权访问返回 403 且入审计；改密级后 5 分钟内权限缓存已失效 |
| **M2 人事与审批** | 入转调离单据、两级审批引擎、生效日定时落库、离职禁用账号、数据导出 | 审批状态机全路径测试（含退回/驳回/委派）；生效日当天字段才变更；离职后登录被拒 |
| **M3 考勤与请假** | Web 打卡、异常检测、补打卡、年假额度（30 自然日）、加班累计导出、员工自助查询页 | 缺卡次日生成异常并提醒；补打卡走两级审批且原事件保留；月度应出勤快照可复算；4 年数据可查 |
| **M4 工时与项目** | 项目/任务、周工时表、提交锁定、补充提交冲销、8 周窗口、billable | 上锁周不可直接改；补充提交后净额正确且原记录可查；超窗补填被拒 |
| **M5 知识库与 RAG** | 上传/异步解析/分块/嵌入、混合检索+RRF+rerank、流式回答、强制引用与回链、拒答、密级×部门过滤、个人文档 | **权限泄漏测试**：low 用户问 high 文档内容必须拒答；无依据时拒答；引用页码正确；个人文档不进入他人检索结果 |
| **M6 Agent** | LangGraph 图、只读工具、草稿工具 + interrupt 人审、`agent_actions` 审计、模型降级链、流式 | Agent 无法写库（代码级验证）；草稿须显式确认才提交；`initiated_by=agent` 可追溯；主模型故障时自动降级并记录 provider |
| **M7 薪酬、培训与合规收尾** | 薪酬档案、财务批量上传工资单、可见份数限制、撤回通知、培训记录、RoPA 文档、留存策略页 | 员工只见最近 N 份；撤回后已下载者收到消息；数据导出 ZIP 内容完整；RoPA 与数据字典与实现一致 |

**M6 的最小只读版可提前到 M3 之后**（只读工具零风险），但草稿与人审必须等 M2 的审批引擎成熟（Q35）。

---

## 12. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| 嵌入维度写死后难改 | 换嵌入模型需全量重嵌 | 迁移脚本中定死 1536；文档显著标注；保留 `chunking_version` 与 `embedding_model` 字段以便灰度重嵌 |
| 权限过滤在 SQL 层漏写 | 数据泄漏（最严重） | 集中式 `can()` + RLS 兜底 + 专门的越权集成测试矩阵 |
| 考勤"业务日"与 UTC 混淆 | 跨月/跨日记录错位 | 所有考勤写入必须同时写 `business_date`；领域层禁止直接使用 `occurred_at` 做日期聚合 |
| 审计表被应用误删 | 合规失效 | 应用数据库角色 `REVOKE DELETE, UPDATE ON audit_log` |
| 模型供应商涨价/停服 | AI 功能不可用 | Provider 抽象 + 降级链；嵌入模型的切换路径预先设计 |
| 范围蔓延（工资计算、排班、绩效） | 项目失控 | §8.3 非目标清单作为变更基线；新增需求必须显式替换而非追加 |
| LangSmith 泄漏对话内容 | GDPR 违规 | §10.1 的 trace 脱敏过滤器（默认开启） |

---

## 13. 设计状态

**设计树已闭合**：Q1–Q48 共 48 个问题全部结算，5 个收尾细节（§10.1–§10.5）已确认。

- 本文档为**实现基线**。新增需求必须显式修改本文档（含 §8.3 非目标清单），不接受"隐式追加"。
- 实现开始前需用户明确放行。
- 实现过程中若发现本文档与代码冲突，**以本文档为准**，并同步修正文档。
