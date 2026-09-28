# Ticket 索引 — 企业内部 AI 管理系统

来源：`docs/DESIGN.md`（实现基线）。拆分方式：tracer bullet 纵向切片，每张切穿 schema → API → UI → 测试，完成即可独立演示。

**共 50 张。** 每张一个文件，编号即依赖顺序。

`01`–`47` 是从设计文档拆出来的原始集合；`48`、`49`、`50` 是后来补开的——设计文档里的三条明确决策
（compliance 只读查阅对话记录、每日备份、独立的 worker 服务）在拆分时没有落到任何票据上，按第 127 行的
口径（票据与设计文档冲突时以设计文档为准）补齐，而不是让它们静默缺失。三票的正文都写了为什么它们是
"补开"而不是"新增需求"。

## 第一刀在哪？

**前沿（无阻塞，可立刻开始）：`01`。**

`01` 完成后前沿变为 `02` 与 `03`（后端骨架与前端骨架可并行）。此后 `04` 依赖 `02`，是通往全部业务功能的唯一咽喉。

## 关键路径（26 张，决定项目最短工期）

```
01 → 02 → 04 → 05 → 06 → 07 → 08 → 09 → 10 → 11 → 12 → 13 → 14 → 15 → 16
   → 31 → 32 → 33 → 34 → 35 → 36 → 37 → 38 → 39 → 40 → 41 → 42
```

咽喉节点是 **`15`（权限矩阵测试）**——它同时是 M1 的出口和 M2/M3/M4/M5/M7 的入口。`15` 不通，五条线全部停摆。

## M0 — 地基（5 张，严格串行）

| # | Ticket | Blocked by |
|---|---|---|
| [01](01-monorepo-compose-skeleton.md) | Monorepo 与 Compose 骨架 | — |
| [02](02-backend-skeleton-error-codes-logging.md) | 后端骨架：统一错误码 + 结构化日志 + 健康检查 | 01 |
| [03](03-frontend-skeleton-i18n-design-system.md) | 前端骨架：语言路由 + 设计系统 | 01 |
| [04](04-alembic-pgvector-redis.md) | 数据库迁移、pgvector 与 Redis 接入 | 02 |
| [05](05-seed-data.md) | seed 数据脚本：组织与员工 | 04 |

## M1 — 身份与权限（10 张，严格串行）

| # | Ticket | Blocked by |
|---|---|---|
| [06](06-department-tree.md) | 部门树与组织架构管理 | 05 |
| [07](07-employee-profile-multi-assignment.md) | 员工档案与一人多职位 | 06 |
| [08](08-positions-and-roles.md) | 职位与角色 | 07 |
| [09](09-user-accounts-temp-password.md) | 用户账号与临时密码 | 08 |
| [10](10-login-session-forced-password-change.md) | 登录、会话与强制改密 | 09 |
| [11](11-authorization-kernel.md) | 授权内核：`can()` 与权限快照 | 10 |
| [12](12-clearance-and-access-decision.md) | 密级与文档访问判定 | 11 |
| [13](13-rls-restricted-db-role.md) | 行级安全策略与受限数据库角色 | 12 |
| [14](14-audit-log-compliance-query.md) | 审计日志与合规只读查询 | 13 |
| [15](15-permission-matrix-tests.md) | 权限矩阵与越权测试套件 | 14 |

## M2 — 审批与人事（5 张）

| # | Ticket | Blocked by |
|---|---|---|
| [16](16-approval-engine.md) | 审批引擎内核与状态机 | 15 |
| [17](17-personnel-change-requests.md) | 人员变动单：入转调离 | 16 |
| [18](18-termination-account-disable.md) | 离职生效与账号禁用 | 17 |
| [19](19-notification-center.md) | 通知中心与投递追踪 | 16 |
| [20](20-daily-digest-email.md) | 每日汇总邮件与 Mailpit | 19 |

## M3 — 考勤与请假（6 张）

| # | Ticket | Blocked by |
|---|---|---|
| [21](21-clock-events.md) | 打卡事件流与当日记录 | 15 |
| [22](22-work-schedule-holidays-expected-hours.md) | 工作日程、节假日表与月度应出勤 | 21 |
| [23](23-clockout-notification-anomalies.md) | 下班通知与缺卡异常检测 | 22, 19 |
| [24](24-attendance-correction-self-service.md) | 补打卡与员工自助查询 | 23, 16 |
| [25](25-annual-leave-and-requests.md) | 年假额度与请假申请 | 24 |
| [26](26-overtime-and-monthly-export.md) | 加班申请与月度导出 | 25 |

## M4 — 工时与项目（4 张）

| # | Ticket | Blocked by |
|---|---|---|
| [27](27-projects-and-tasks.md) | 项目与任务（含可计费标记） | 15 |
| [28](28-weekly-timesheet-entry.md) | 周工时表录入与提交 | 27 |
| [29](29-timesheet-approval-lock-supplementary.md) | 审批锁定、补充提交与周锁 | 28, 16 |
| [30](30-timesheet-reporting.md) | 工时报表与可计费汇总 | 29 |

## M5 — 知识库与 RAG（7 张）

| # | Ticket | Blocked by |
|---|---|---|
| [31](31-document-upload-async-parsing.md) | 文档上传与异步解析管道 | 15 |
| [32](32-chunking-and-embedding.md) | 分块与嵌入（父子分块） | 31 |
| [33](33-hybrid-retrieval-rrf-rerank.md) | 混合检索 + 融合排序 + 重排 | 32 |
| [34](34-streaming-answer-citations-refusal.md) | 流式回答、强制引用与拒答 | 33 |
| [35](35-retrieval-permission-filtering-tests.md) | 密级 × 部门检索过滤与越权测试 | 34, 12 |
| [36](36-personal-documents-visibility.md) | 个人文档与可见性 | 35 |
| [37](37-qa-interface-citation-links.md) | 问答界面与引用回链 | 36 |

## M6 — Agent（5 张）

| # | Ticket | Blocked by |
|---|---|---|
| [38](38-langgraph-orchestration-skeleton.md) | LangGraph 编排骨架 | 37 |
| [39](39-readonly-tools.md) | 只读工具（本人 + 经理） | 38 |
| [40](40-draft-tools-prefill-form.md) | 草稿工具：产出待确认表单（不写库） | 39, 16 |
| [41](41-human-confirmation-agent-audit.md) | 人审确认点与 agent_actions 审计 | 40 |
| [42](42-observability-redaction-model-fallback.md) | 可观测性脱敏与模型降级 | 41 |

## M7 — 薪酬、培训与合规（5 张）

| # | Ticket | Blocked by |
|---|---|---|
| [43](43-salary-records.md) | 薪酬档案 | 15 |
| [44](44-payslip-batch-upload-gap-list.md) | 财务批量上传工资单与缺失清单 | 43 |
| [45](45-payslip-self-service.md) | 员工工资单自助下载与可见份数 | 44 |
| [46](46-payslip-withdrawal-notification.md) | 工资单撤回与已下载者通知 | 45 |
| [47](47-training-export-retention.md) | 培训记录、数据导出与留存说明 | 18 |

## M8 — 补开的缺口（3 张）

拆分 `01`–`47` 时漏掉的三条设计决策。它们不属于新增需求：`docs/DESIGN.md` 早已写明，
只是没有票据承接（见 `48`、`49`、`50` 的正文）。

| # | Ticket | Blocked by |
|---|---|---|
| [48](48-compliance-conversation-review.md) | 对话记录的合规只读查阅（D18、§4 角色表、§5.3） | 34 |
| [49](49-daily-backups.md) | 每日数据库与文档卷备份，7 天保留，可关闭（D33） | 04 |
| [50](50-worker-service.md) | worker 服务：让计划任务有地方跑（§2 的 7 服务、§5.1） | 04, 31 |

## 并行泳道

`15` 通过之后，以下四条线互不依赖，可按任意顺序或并行推进：

| 泳道 | 门票 | 阻塞它的外部依赖 |
|---|---|---|
| A — 人事与审批 | `16 → 17 → 18`、`19 → 20` | 无 |
| B — 考勤与请假 | `21 → 22 → 23 → 24 → 25 → 26` | `19`（通知）、`16`（审批） |
| C — 工时与项目 | `27 → 28 → 29 → 30` | `16`（审批） |
| D — RAG 与 Agent | `31 → … → 42` | `12`（密级判定）、`16`（草稿提交） |
| E — 薪酬与合规 | `43 → 44 → 45 → 46`、`47` | `18`（离职，仅 `47`） |

**注意 B 线对 A 线的隐性依赖**：`23` 需要通知中心（`19`），`24` 需要审批引擎（`16`）。想早做考勤就必须先把 `16` 和 `19` 做掉。

## 使用方式

- 开工前读 `docs/DESIGN.md` 与 `docs/agents/domain.md`。
- 一次只做一张票。完成时把该文件里的 `Status: ready-for-agent` 改为 `Status: done`，文件保留。
- 若发现票据与设计文档冲突，**以设计文档为准**并修正票据。
- 新增需求必须改动 `docs/DESIGN.md`（含非目标清单）后再开票，不接受隐式追加。

## 前端落地口径（哪些票据落前端）

**唯一权威是 `docs/architecture/frontend-design-system.md` §8.1 的落地表**：`03`（token/组件/外壳）、
`19`/`20`（通知中心与状态）、`21`（打卡）、`24`（考勤自助与补卡）、`25`（请假）、`28`/`29`（工时网格）、
`31`（文档上传与进度）、`37`（问答与引用回链）、`40`/`41`（草稿确认表单）、`44`/`45`（工资单）。

按第 127 行的口径（票据与设计文档冲突时以设计文档为准），票据 checklist 里出现「界面 / 页面」
但**不在** §8.1 表里的行——`06` 的部门树展开收起、`07` 的员工详情页、`14` 的查询界面、
`17` 的状态展示、`22` 的节假日维护——验收以 **API 能力**为准（过滤、分页、状态投影、双语文案、
错误码），不为它们新建前端页面。理由是边界：§8.1 明确划分了哪些票据落前端，若票据措辞本身
就能新增页面，前端范围将没有边界（`docs/DESIGN.md` 的非目标清单也只列了后端能力）。

**补交记录（后端 done、前端按 §8.1 属于必交）**：`21`、`24`、`25` 三张票据的界面在票据完成时
没有随之落地，已在后续补做，见三张票据文件末尾的「前端实现」小节。`28`/`29` 的周网格与
`31` 的上传进度经逐条核对**已经实现**（复制上一周、服务端实时每日/本周合计、锁定与关闭提示、
补填窗口剩余周数、处理阶段进度与失败原因），无需补做。

补交不改变后端票据的 `done` 状态行：状态行记录的是该票据在其依赖链上的完成，前端补交记录在
同一文件里，便于追溯谁在什么时候补的。
