# 40 — 草稿工具：产出待确认表单（不写库）

**What to build:** 员工说"帮我请下周三的假"，助手不是直接提交，而是**生成一张填好的请假表单**展示给员工看。助手在整个系统里根本没有写数据库的能力——这不是靠提示词约束，而是代码结构上不存在这样的工具。

**结构约束:** 落实 `docs/architecture/codebase-design.md` 约束 B 的第 1 层——`ai/**` 不得 import 任何写入型仓储，且 `domain/` 不得被 `ai/` 反向依赖（约束 A）。测试断言 AI 可触达的工具集合中不存在写库工具。

**Blocked by:** 39 — 只读工具（本人 + 经理）；16 — 审批引擎内核与状态机

**Status:** done

- [x] 提供草稿工具：请假申请草稿、补打卡草稿、工时表草稿
      — `test_the_draft_half_of_the_registry_is_design_6_2s_three_rows`、
      `test_each_tool_produces_a_filled_in_form`。§6.2 草稿半边的三行，`ToolKind.DRAFT`。
- [x] 草稿工具只**构造并返回**一张结构化表单，不产生任何数据库写入
      — `test_a_draft_tool_writes_nothing_at_all`（**schema 里每一张表**逐表计数，前后相等）、
      `test_a_refused_or_incomplete_draft_writes_nothing_either`。
- [x] 草稿内容经过校验：日期范围、额度是否足够、项目任务是否存在、格式是否合法；不合法时明确告知原因而不是生成一张注定失败的草稿
      — `test_each_refusal_is_the_submission_s_own_refusal` 一族六条 + `test_a_missing_field_is_named_rather_than_guessed`。
- [x] 草稿以持久化状态保存并关联到对话，刷新页面或重启服务后仍能找到未确认的草稿
      — `test_the_draft_survives_a_restart_and_is_read_back_through_the_conversation`。
- [x] 草稿有有效期（默认 24 小时），过期后标记为失效并要求重新生成
      — `test_the_default_lifetime_is_a_day_and_the_clock_is_the_database_s`、
      `test_a_lapsed_draft_is_marked_expired_when_it_is_read`、
      `test_a_draft_that_is_still_inside_its_day_is_proposed`。
- [x] 架构层面验证：AI 模块的依赖中**不存在**任何写入型仓储；有一条测试断言助手可调用的工具集合里没有任何写库工具
      — `test_the_tool_set_has_no_write_tool_and_the_agent_reaches_no_repository`（三条断言）+
      39 号工单的 AST 行走 `test_no_registered_tool_can_reach_a_write`（本工单的 `draft.py` 落在同一次行走里）。
- [x] 草稿内容在界面上以完整可编辑表单呈现，员工可以在提交前修改任何字段
      — `test_every_field_of_every_submission_is_a_field_of_its_form`（表单字段名 == 提交端点请求模型字段
      减去身份字段）+ `web/scripts/visual-check.mjs::checkDraftForm`（浏览器里逐字段数控件、逐个改值读回）。
      §8.1 的「Agent 草稿确认表单 = 40/41」这一行里，**本工单负责到"这是填好的、可编辑的、带有效期的表单"**；
      确认按钮是 41 号的，界面上的按钮因此是 disabled 并写明原因。

## 这对矛盾先解决，因为 checklist 里就有它

「草稿工具不产生任何数据库写入」与「草稿被持久化并关联到对话」同时成立，靠的是一条缝：

* **工具是纯的**：`api/app/ai/tools/draft.py` 只读 + 校验 + 返回 `PrefillForm`，从不打开写路径。
  纯到可以用"表计数"证明（下详）。
* **平台记录**：把行写进 `agent_actions` 的是**节点**（`agents/nodes.py::draft_tools_node`），经
  `app/domain/agent/service.py::AgentActionService.record_draft`，与 34 号工单的 `AnswerService` 写
  `rag_messages` 同一种分工——图负责转达，领域服务负责那一行。

所以"草稿工具没写"是关于**工具**的断言（计数所有表），"草稿能被找回来"是关于**平台**的断言（另两条测试）。

## 依赖可用性

无。本工单没有改 `api/pyproject.toml` / `api/uv.lock` / `web/package.json`：`langgraph` 与
`langgraph-checkpoint-postgres` 是 38 号工单装的，表单只用 React + 既有 UI 组件（`lib/ui/field.tsx`
的 `TextField`/`SelectField`/`TextAreaField`），没有新依赖、没有新镜像。

## 实现

### 文件

**新增（api）：**

- `app/domain/agent/` —— §3.6 的 `agent_actions` 作为一个领域模块：
  - `models.py`：`DraftStatus`（proposed/confirmed/rejected/expired）、`DraftEntity`、
    `FieldKind`、`FieldOption`、`PrefillField`、`PrefillForm`、`AgentAction`、`IDENTITY_FIELDS`。
  - `repository.py`：`PostgresAgentActionRepository`（`insert` 用 `now() + make_interval(hours => :ttl)`，
    `newest_for_conversation` 用 `expires_at <= now()` 判定是否过期，`mark_expired` 带 SQL 守卫）。
  - `service.py`：`AgentActionService.record_draft` / `latest_draft`，`service_for(session, ttl_hours=…)`。
- `app/models/agent_action.py` —— ORM 行（迁移 0027 的镜像；四种状态在这里写成字面量，
  与枚举的一致性由 `test_the_column_and_the_enum_agree` 钉住，因为**模型模块不能 import 领域包**
  ——`app.domain.agent` 会拉起 `answer.repository` → audit → `app.models`，形成循环导入，实测踩到过）。
- `alembic/versions/20261009_1000_agent_actions.py` —— 0027（接在 0026 之后，写前读过目录与 heads）。
- `app/ai/tools/draft.py` —— 三个草稿工具与 `DRAFT_TOOLS`。
- `tests/test_agent_draft_tools.py` —— 35 条。
- `tests/tools/seed_agent_draft.py` —— 给浏览器检查用的夹具（三个草稿 + 一个已过期草稿）。

**改写（api）：**

- `app/ai/tools/models.py` —— `ToolOutcome.INVALID`（第五个结局：**内容**被拒，带目录里的原因），
  `ALLOWED_PARAMETERS` 扩到 19 个名字（草稿字段），仍然没有任何能命名员工的参数。
- `app/ai/tools/registry.py` —— `REGISTRY` 合并 `DRAFT_TOOLS`（8 个工具）。
- `app/ai/tools/render.py` —— `ToolOutcome.INVALID` 的渲染（`needs_details` 会把缺的字段**标签**
  填进两种语言的句子里）、三个草稿句子的常量渲染器、`render_no_request()`。
- `app/ai/tools/services.py` —— 新增 `corrections()`、`projects()`，并把 `_attendance_collaborators` /
  `_approvals` 提成共用的装配（原来在 `leave()`/`timesheets()` 里各写了一遍）。
- `app/ai/agents/nodes.py` —— `draft_tools_node` 真正调用工具、校验、**记录**；`await_confirmation_node`
  只在有表单时 `interrupt()`。
- `app/ai/agents/state.py` —— `AgentState` 新增 `prefill_form` / `agent_action_id`（LangGraph 只保留
  声明过的 channel，没声明就是静默丢弃——实测踩到过 `KeyError: 'prefill_form'`）。
- `app/ai/agents/replies.py` —— 删掉 `NO_DRAFT_TOOL` 与占位 `CONFIRMATION_PENDING`，换成
  `confirmation_payload(draft, draft_id, expires_at)`。
- `app/api/v1/answer.py` + `schemas/answer.py` —— 会话读取多一个 `draft` 字段（**不新增路由**）。
- `app/config.py` —— `agent_draft_ttl_hours: int = 24`。
- `app/core/messages.py` —— `agent.draft.*` 双语共 26 条键（3 个句子 + 1 个"要我说清要哪个" +
  1 个"缺这些字段" + 3 个标题 + 14 个字段标签 + 2 个选项标签 + 2 条提示）。
- `tests/test_agent_graph.py`、`tests/test_agent_readonly_tools.py` —— 38/39 号工单里"草稿半边仍是占位"
  与"注册表只有五条只读"的断言按本工单改写（草稿分支现在真的产表单）。

**新增/改写（web）：**

- `web/app/[locale]/(app)/qa/draft-form.tsx` —— 表单卡片（逐 `kind` 画控件、facts 只读、过期态）。
- `web/app/[locale]/(app)/qa/qa-screen.tsx` —— 卡片画在对话流上方；**有草稿时不再画"先问一个问题"的空态**
  （第一版截图里两者同时出现，自相矛盾——看图后改的）。
- `web/lib/stores/qa-store.ts`、`web/lib/api/answers.ts` —— 草稿随会话读取一起持有 / 类型。
- `web/lib/i18n/{index,messages/es,messages/en}.ts` —— `qa.draft.*`（界面自己的状态文案：徽章、
  有效期句、过期句、禁用确认按钮的说明、facts 标签）。
- `web/lib/ui/field.tsx` —— 三个字段组件补 `disabled` 属性（`CONTROL_CLASSES` 里 `disabled:` 的样式
  从 03 号工单就在，缺的只是这个 prop）；过期草稿因此是**看得见但改不动**的。
- `web/scripts/visual-check.mjs` —— `checkDraftForm`（+ `draftConversations` / `openDraft`）。

### 校验用的是提交路径自己的规则（本工单的第二个重点）

工单要求"不合法时明确告知原因"，而"草稿的价值"在于**员工填好并确认的表单不会被事后拒绝**。
所以本工单没有写任何一条新的额度/窗口/锁检查，而是把三条写路径里的规则**提取出来给两个调用者共用**：

| 草稿工具 | 复用的校验 | 从哪来 |
|---|---|---|
| `draft_leave_request` | `LeaveService.check_request()` | 从 `draft()` 里原样提出：类型（存在/在用）、员工、窗口（起止顺序、最长天数、最远年份）、附件规则、工作日（排班）、重叠、额度（**提示性**那一遍）。`draft()` 现在是"`check_request()` + 三处写入"。`_require_affordable` 的顺序一字未动——两个错误同时存在时**报哪一个**是行为，不是重构。 |
| `draft_attendance_correction` | `CorrectionService.check_draft()` | 从 `draft()` 里提出：kind 必须是打卡、时刻必须带时区且已发生、原因必填、**当日+类型必须唯一指向一条打卡**（或一条都没有，那正是"忘了打卡"）。 |
| `draft_timesheet` | `TimesheetService.check_entry()` | 新方法，内部全是既有规则：`assert_monday`、`_require_day_in_week`、`_require_minutes`、`_require_employee`、`_require_room`、`_recordable`（项目任务的唯一裁决者），锁与窗口则共用 `_open_sheet`（从 `_editable` 里提出来的那两级拒绝阶梯）与 `_window_refusal`（从 `_require_open_week` 里提出来的**判定**那一半）。 |

**工时表这一处有一处有意的不同，写在票里因为它是一种取舍**：`_require_open_week` 在拒绝之前会把
"这一周已关闭"**写进** `timesheet_weeks_lock`（并留审计），`_editable` 会把引擎的答案**写回**过期的状态缓存；
草稿两者都不做——`check_entry` 用 `_engine_status` 在内存里读同一个答案，用 `_window_refusal` 抛同一个
目录化错误，一行都不写。理由：草稿不是一次写入尝试，让一张"有人在草稿里看到过表单"的记录去说
"这一周被关闭了"，是这套系统自己的审计在说谎。**周确实关了**：下一次真正的写入照样记录它。
`test_the_week_lock_refusal_does_not_lock_the_week` 两半都断言（草稿后锁表为 0，提交后为 1）。

**没有搬动任何规则**：三条 `check_*` 都还在各自模块里，本工单只是把私有方法提成公开方法并让写入路径回头调用它。
41 号工单确认时若要重新校验，应当调用的就是这三个方法。

### `PrefillForm` 的形状

```python
PrefillForm(
  tool="draft_leave_request",            # 注册表键
  entity="leave_request",                # 三类之一：leave_request / attendance_correction / timesheet_entry
  title_key="agent.draft.title.leave_request", title_es=…, title_en=…,
  submit_path="/api/v1/leave/requests",  # 确认后要 POST 到哪里（工时表这一条带 ?week=…，因为周是查询参数）
  fields=(PrefillField(                  # 提交将写入的每一个字段
      name="start_date",                 # ← 提交端点自己的字段名
      label_key="agent.draft.field.start_date", kind="date",
      label_es="Fecha de inicio", label_en="Start date",
      value="2026-09-28", required=True, options=(), hint_es=None, hint_en=None), …),
  facts={"business_days_count": 2, "working_days": [...], "allocations": [...]},  # 校验的答案，只读
)
```

* **表单里没有身份字段**：`IDENTITY_FIELDS = {"employee_id"}`，`RequestCreate`/`CorrectionCreate` 有它，
  表单没有，`ALLOWED_PARAMETERS` 里也没有。`test_every_field_of_every_submission_is_a_field_of_its_form`
  比较的就是"表单字段 == 请求模型字段 − 身份字段"，**三个实体各一条**。
* 一个字段的形状值得说明：补卡的 `corrected_at` 是 `kind="time"`（`16:10`），因为人是照着打卡记录读"几点"的，
  而提交要的是带时区的时刻——**由平台在确认时用 `business_date` + 这个时间合成**（在 Madrid 时区里），
  浏览器不参与时刻推导。字段名仍是提交的字段名，测试比的是名字。

### `agent_actions` 行

迁移 0027，§3.6 的列 + 两个实现自决列：

| 列 | 说明 |
|---|---|
| `conversation_id` / `user_id` | §3.6 的外键；`user_id` 只来自 `principal.user_id` |
| `thread_id` | LangGraph 线程（`runtime.execution_info.thread_id`），可空：没有 checkpointer 就没有线程，那是一种状态而不是缺值 |
| `tool_name` | 注册表键（节点从不写模型给的名字） |
| `tool_input` / `tool_output` | 结构化值；§10.1 的"绝不外发"，所以**只进这个表与 state**，绝不进节点记录 |
| `produced_prefill_form` | 表单对象（CHECK：必须是 JSON **object**，句子进不来） |
| `status` | proposed/confirmed/rejected/expired（CHECK），`proposed` 时 `confirmed_at`/`resulting_entity_id` 必须为空 |
| `created_at` / `expires_at` | 都由**数据库**产生；CHECK `expires_at > created_at` |
| `confirmed_at` / `resulting_entity_type` / `resulting_entity_id` | 41 号写；实体对"要么都有要么都没有" |

RLS 三条策略（读/写/改都是"这行是我的"，逐动词写开），`REVOKE DELETE`（D22 的审计表，请求角色不能删）。
**没有新增 GRANT**：0007 的 `ALTER DEFAULT PRIVILEGES` 已经覆盖（`test_permission_matrix.py` 对临时表断言过这一点）。

### 有效期：24 小时，时钟是数据库的

* 默认值 `AGENT_DRAFT_TTL_HOURS=24`（DESIGN §6.3 的「默认 24h」），可在环境里改，无需改代码。
* 写入：`INSERT … expires_at = now() + make_interval(hours => :ttl_hours)`——`ttl_hours` 以**数字**传进去，
  区间算术发生在有时钟的那一侧。
* 读取：`SELECT …, (status='proposed' AND expires_at <= now()) AS expired`；
  `AgentActionService.latest_draft` 看到已过期就 `UPDATE … SET status='expired' WHERE id=… AND status='proposed' AND expires_at <= now()`，
  然后返回 `expired`——**§6.3 要的是状态，不是读取时过滤**，而且那句 UPDATE 的守卫让"刚刚确认过的行"不可能被它改掉。
* 界面：卡片上写"可以确认到 X 日"，过期态写"已于 X 日过期"+ 一段说明（要求重新生成），字段变成只读，
  确认按钮不可用。**过期草稿的内容仍然显示**：它是"助手提议过什么"的证据。

### 路由：一条也没有新增

草稿随**会话读取**回来：`GET /api/v1/answers/conversations/{id}` 的响应多一个 `draft` 字段
（`PrefillFormRead` + `DraftRead`）。理由写在 `schemas/answer.py` 的 `ConversationRead` docstring 里：
草稿属于会话（§3.6 就用 `conversation_id` + `user_id` 给它定位），那条路由已经由 `session.read_own`
守卫并且带着 `WHERE user_id`，另开一条就是同一条所有权规则写两遍。
**因此 `tests/test_permission_matrix.py` 的 `HTTP_MATRIX` 与字面量 `81` 一个字都不用动。**

### 草稿分支怎么拿到"要起草什么"（以及本工单**没有**做的选择器）

节点和 39 号工单的只读分支用同一个缝：调用者（测试、41 号的请求、42 号的模型函数调用）在 state 里
给出 `tool` + `tool_arguments`；没有给，分支就**回答一个问题**（`agent.draft.no_request`："告诉我要起草哪一个、哪几天"），
并且**不暂停**——刚问完问题的分支去等确认，等的是一个它从没展示过的东西。

**为什么没有写词法选择器**：39 号工单的 `selection.py` 可以合理地推断"这个月"（一个默认周期），
而「帮我请下周三的假」里的"下周三"是**一个会出现在员工要确认的表单里的具体日期**——猜错不是把答案说偏，
是把一张写错的表放到人面前。所以日期由调用者给（今天：测试/41；42：模型函数调用）。
`test_the_draft_branch_refuses_a_name_that_is_not_a_draft` 钉住的是另一件事：**draft 分支只跑 draft 工具**，
一个注册过的只读名字在这里也是"没有这种草稿工具"，不会把查询结果塞进草稿的回复里。

### 约束 B 的第一层：三条断言，各自的证伪方向

`test_the_tool_set_has_no_write_tool_and_the_agent_reaches_no_repository`：

1. **工具集里没有写工具**：每个注册工具的 kind 都落在 `{read_only, draft}`，且 `ToolKind` 没有 `WRITE` 成员
   ——写工具是**描述不出来**的，不只是没注册；
2. **实现够不着写**：39 号工单的 AST 行走覆盖整个 `app/ai/tools` 包（现在包含 `draft.py`），
   本工单补的那句是"行走确实看到了 `draft.py`"——否则那条断言对草稿半边是空的；
3. **agent 包不持有写仓储**：`app/ai/agents/**` 里没有任何模块 import `app.repositories.*` 或 `app.models.*`。
   节点记录草稿是**通过领域服务**（`app/domain/agent/service.py`）做的，和 `answer_policy` 通过
   `AnswerService` 到达 `rag_messages` 是同一件事。工具包 import 仓储（它要装配领域服务），
   所以第一层可检验的形态是"没有写入被调用"，不是"没有仓储被提及"——`ai/tools/services.py` 的 docstring
   在 39 号工单里就写明了这一点。

**没有碰**：约束 B 的第 2、3 层（运行时只读上下文、只读数据库角色）是 52 号工单的（那张票正是为这个缺口开的）。
草稿路径的事务仍以请求角色 `eam_app` 打开；本工单不假装它已经只读。

## 纯不纯：把"没有写入"做成可证伪的

`test_a_draft_tool_writes_nothing_at_all` 的计数方式是**从目录里枚举**：

```sql
SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename
```

然后逐表 `count(*)`，调用三个工具（都用**合法**参数，因为"被拒所以没写"是无趣的），再数一遍，逐表相等。
从目录枚举而不是手写清单，是这条断言的要点：手写清单在"有人加了一张表并在里面写了一行"的那天仍然通过。
计数集合里显式断言包含 `agent_actions`、`rag_conversations`、`rag_messages`、`audit_log`
（平台写的那两张 + 这个系统里每次写入都留痕的那两张）——否则一条写入可能落在没人数的表里。

**正向对照**：同一套计数跑一次**确实会写**的路径（图的草稿分支，平台记录草稿），必须看到
`agent_actions +1`、`rag_conversations +1`，并且 `audit_log` **不变**（这一行的留痕就是 `agent_actions`，
再加一条 `audit_log` 等于替"有人做了这件事"背书）。没有这个对照，一个永远返回相同数字的计数器
会让上面那条断言无论如何都通过。

## 变更测试（mutation）

每条规则被打断一次，记录失败的测试名，然后还原；`git grep MUTATION-40 -- api/` 为空。

| 被破坏的规则 | 破坏方式 | 失败的测试 | 层数 |
|---|---|---|---|
| 工具是纯的 | `draft_leave_request` 在返回表单前调用 `LeaveService.draft()`（真的写一行请假申请） | `test_a_draft_tool_writes_nothing_at_all` | 1 |
| 请假额度 | `check_request` 里跳过 `_require_affordable` | `test_the_leave_balance_refusal_is_the_route_s`（草稿半边）**与** `test_a_draft_over_the_remaining_days_is_refused_while_it_is_still_a_draft`（25 号工单的提交路径） | 2 |
| 补卡目标唯一 | `check_draft` 里跳过 `_require_resolvable` | `test_the_correction_refusals_are_the_routes`（草稿）**与** `test_a_day_with_two_clock_outs_cannot_be_corrected`（24 号工单） | 2 |
| 八周窗口锁 | `_window_refusal` 恒返回 `None` | `test_the_week_lock_refusal_does_not_lock_the_week`（草稿）**与** `test_a_week_outside_the_window_is_refused_with_the_weeks_that_remain`、`test_outside_the_window_no_write_path_may_touch_the_week`（29 号工单） | 3 |
| 过期即失效 | `latest_draft` 观察到过期后仍返回 `proposed` | `test_a_lapsed_draft_is_marked_expired_when_it_is_read` | 1 |
| 有效期默认值 | `agent_draft_ttl_hours` 改成 1 | `test_the_default_lifetime_is_a_day_and_the_clock_is_the_database_s`、`test_a_draft_that_is_still_inside_its_day_is_proposed` | 2 |
| 注册表白名单 | `lookup` 的 `except KeyError` 回退到 `registered()[0]` | `test_the_draft_branch_refuses_a_name_that_is_not_a_draft`、`test_an_unregistered_name_is_refused_and_nothing_runs`（39 号工单） | 2 |
| 结构层：工具包 | 在 `draft.py` 里加一句 `await context.session.commit()` | `test_no_registered_tool_can_reach_a_write`（39 号工单的 AST 行走） | 1 |
| 结构层：agent 包 | `nodes.py` 里加 `from app.repositories.attendance import …` | `test_the_tool_set_has_no_write_tool_and_the_agent_reaches_no_repository` | 1 |
| 答案渲染的总性 | `_sentence` 改回 `template.format(**values)` | `test_a_refusal_sentence_this_module_does_not_own_still_renders`（`KeyError: 'days'`——目录里有一条带占位符的 `errors.*` 句子，草稿的拒绝话术是借来的） | 1 |
| 缺字段的文案总性 | 把 `agent.draft.field.week_start` 的键改名（标签与参数名脱钩） | `test_every_draft_parameter_has_a_label_its_answer_can_use`、`test_a_missing_week_is_asked_for_rather_than_crashing` | 2 |

**第 2、3、4 行是本工单最有价值的三条**：同一次破坏同时打红草稿半边的测试与**提交路径自己的**测试，
这就是"两个调用者共用一条规则"的证据，而不是"两条相似的规则恰好同时被改"。
（第 9 行还顺带记录了另一个真实层次：`services.py` 本来就可以 import 仓储，所以第一层的可检验形态只能是"没有写入被调用"。
第 10、11 行是本工单后期自己发现的两个真实缺陷，两个都在"没人测到的路径"上：一条目录句子带 `{days}` 占位符
而草稿的拒绝话术是借目录的键渲染的；`week_start` 是唯一没有标签键的参数，而"工时表草稿没给周"这条路径
本来会 `KeyError`。两条都是写完才发现的，也正是 mutation 这一步把它们逼出来的。）

## 验证

- `uvx ruff check app tests` 干净（`All checks passed!`）。
- `cd web && npx tsc --noEmit` 干净；`npx next build` 成功。
- 目标运行（本工单的 scratch 库 `eam_test_t40`、Redis 5 号库）：
  - `pytest tests/test_agent_draft_tools.py` → **35 passed**
  - `pytest tests/test_agent_graph.py tests/test_agent_readonly_tools.py` → **47 passed**
  - `pytest tests/test_leave.py tests/test_attendance_corrections.py tests/test_timesheets.py tests/test_timesheet_lock.py`
    → **132 passed**（校验提取没有改变任何既有行为）
  - `pytest tests/test_answer.py tests/test_errors.py` → **194 passed**
- 全量运行同一条命令（`env -u DOCUMENT_PARSE_RUNNER_ENABLED`，理由与 39 号工单相同）：
  `docker compose exec -T -e TEST_DATABASE_NAME=eam_test_t40 -e REDIS_URL=redis://redis:6379/5 api sh -lc 'cd /app && env -u DOCUMENT_PARSE_RUNNER_ENABLED python -m pytest -p no:warnings'`
  → **1459 passed**（38 号基线 1396；39 号 1423；本工单新增 35 条草稿测试 + 图测试净增 1 条）。
  本工单期间全量跑过三次（改动冻结前两次：1455、1458；最后一次 1459 是本节引用的数字，冻结树）。
- 浏览器（`EAM_USERNAME=devlead npm run visual`）：**ALL CHECKS PASSED**，其中本工单的断言 **50 条**
  （3 个草稿实体 × 12 + 过期态 5 + 320/768 两语言 8 + 夹具存在 1）。

### 看图后改的三处（以及一处开发库修复）

> `web/scripts/visual-check.mjs` 的既有传统：每个前端工单都会在 320/768/1280 两种语言下读图，
> 每次都找出 `tsc` 看不见的真实缺陷。本工单也是三处。

1. **草稿卡片下面同时出现"先问一个问题"的空态**（1280/768/320 三档都能看见）：
   夹具的草稿会话没有消息，于是 `qa-thread-empty` 与表单同屏，读起来是屏幕在自相矛盾。
   改成**有草稿时不画空态**（`!draft`）。
2. **过期草稿的字段仍然可以编辑**：`disabled={expired}` 传下去了，但 `lib/ui/field.tsx` 的三个组件
   根本没有这个 prop（`CONTROL_CLASSES` 里 `disabled:` 的样式从 03 号工单就在）。补上 prop 并接到
   控件上，过期卡片现在是"看得见、改不动"，检查里也断言"过期卡片的控件全部 disabled"。
3. **检查自己改的值进了截图**：先改字段再截图，于是留档的"助手提议的表单"其实是检查改过的版本。
   改成**先截图后编辑**，另外单独留一张 `*-edited-es.png` 作为"可编辑"的证据。
4. **（开发库修复，不是代码缺陷）** `qa: the cited original is still downloadable (404)`：
   开发库里 66 份文档有 63 份的**文件已经不在卷上**（容器重建过），检索于是引用了一份打不开的旧文档。
   删掉这些"没有文件的文档行"之后该断言变绿。这不是本工单引入的，但它是这条检查一直红着的原因，
   记在这里以免下一个工单再花时间查它。

### §8.2 自查（逐条，如实）

- 页面有明确主角 / 三档层级 / 网格对齐 / 主色 ≤2 / 状态不只靠颜色（"Caducado" 是文字 + 灰色徽章，
  过期还有图标化的 warning 块）：**通过**（截图）。
- 表单有 label、校验、错误提示与修正建议：**label 通过**（检查断言 0 个控件没有 label，
  两语言逐字段比较 API 发来的标签）；**校验**由服务端承担，界面上目前没有字段级错误态——
  因为字段级错误属于"确认时重新校验"，那是 41 号工单的（本工单如实记录）。
- 空/加载/错误状态：会话流的三个状态是 37 号工单的；本卡片新增两个状态（**待确认** / **已过期**），
  都画了、都截图了。
- 两种语言各检查一遍、320/768/1280 三档：**通过**（`qa-draft-<entity>-{es,en}.png`、
  `qa-draft-<entity>-768-es.png`、`qa-draft-{320,768}-{es,en}.png`、`qa-draft-expired-es.png`、
  `qa-draft-<entity>-edited-es.png`）。**英文与 320px 一起看是刻意的**：英文的标签最短、句子最长，
  两种语言在同一宽度下的断行位置不一样。
- 正文对比度、字号、间距取自 token、动效：沿用既有组件与 token，未新增颜色或魔法值。
- **移动端触控目标 ≥ 44px**：**如实报告**——320px 下卡片的四个控件实测 **39px**（`[note] draft 320px:
  control heights 39, 39, 39, 39 px`，检查断言的是 ≥32px 不塌陷）。39px 是既有 `TextField` 的高度，
  整站其他表单（打卡、补卡、请假）用的是同一个组件；本工单没有改它的尺寸（那会牵动所有屏幕），
  但这条清单项在本屏**没有**满足，记在这里。
  **后续（同一提交序列内）：** 这条已由父代理在共享组件上修掉——
  `web/lib/ui/field.tsx` 的 `CONTROL_CLASSES` 增加 `min-h-11`（44px），并写明理由：规则属于控件而不是
  某一屏，改在一处才不会让下一张表单重新引入。`npm run visual` 在改后仍然 ALL CHECKS PASSED。
  本工单的实现说明保留原始报告，因为那是当时的真实状态。
- 语义化 HTML / 单一 h1 / 键盘可达 / focus ring：检查脚本的通用部分（每档每语言）逐页跑过，**通过**。

## 客户端需要的形状（给 41 号工单与前端）

`GET /answers/conversations/{id}` 的 `draft` 字段：

```json
{"id": "…", "tool_name": "draft_leave_request", "status": "proposed|expired",
 "created_at": "…", "expires_at": "…",
 "prefill_form": {"tool": "…", "entity": "leave_request",
   "title_key": "agent.draft.title.leave_request", "title_es": "…", "title_en": "…",
   "submit_path": "/api/v1/leave/requests",
   "fields": [{"name": "leave_type", "label_key": "agent.draft.field.leave_type",
               "kind": "select", "label_es": "…", "label_en": "…", "value": "annual",
               "required": true,
               "options": [{"value": "annual", "label_es": "…", "label_en": "…"}],
               "hint_es": null, "hint_en": null}],
   "facts": {"business_days_count": 2, "working_days": ["…"], "allocations": [{"year": 2026, "days": 2}]}}}
```

* `status` 是**生效状态**：`proposed` 才提供确认；`expired` 表示 24 小时已过、要重新生成。
* `tool_answer`（state 里）的 `message_key` 取值：`agent.draft.leave_request`、`agent.draft.attendance_correction`、
  `agent.draft.timesheet`、`agent.draft.no_request`，以及**被拒时目录自己的键**
  （`agent.draft.needs_details` + `fields`，或 `errors.*` 的具体错误键 + `error_code` / `detail`）。
* `state` 里 41 号要读的键：`tool`、`tool_arguments`、`tool_result`、`tool_answer`、`tool_outcome`、
  `prefill_form`、`agent_action_id`、`pending_action`（`{status, tool, draft_id, expires_at}`）、`conversation_id`。

## 给 41 号工单的说明（形状变了的那些）

1. **`await_confirmation_node` 只在 `pending_action["status"] == "proposed"` 时 `interrupt()`**，
   其余情况返回 `{"confirmation": {"received": False, "value_type": "none", "interpreted": False}}` 并且**不暂停**。
   41 号接确认路由时，`paused` 的判定就是 `pending_action["status"]`。
2. **interrupt 载荷**是 `{"awaiting": "human_confirmation", "draft": <prefill_form>, "agent_action_id": …, "expires_at": …}`，
   没有别的字段；41 号加"确认/拒绝"语义时直接扩这个 dict（`replies.confirmation_payload`）。
3. **`AgentContext.session` 必须有值**：草稿路径要读（校验）也要写（`agent_actions`），
   所以接请求时要把**请求的 session** 传进 `AgentContext`，不要在工具里再开一个 session。
4. **事务边界**：`record_draft` 会 **commit**。请求的权限上下文是
   `set_config(..., is_local => true)`（事务级），所以 41 号在 `record_draft` 之后若要再读受 RLS 保护的表，
   需要重新 `apply_rls_context`（`tests/tools/seed_agent_draft.py` 就踩过这个坑，见它的 `_publish`）。
5. **确认后要重新校验**：调用本工单提取的三个方法——`LeaveService.check_request`、
   `CorrectionService.check_draft`、`TimesheetService.check_entry`——而不是新写一遍。
   补卡的 `corrected_at` 在表单里是 `HH:MM`，需要与 `business_date` 合成 Madrid 时刻（`app/domain/attendance/business_day.py::MADRID`）。
6. `agent_actions` 的三个结果列（`confirmed_at` / `resulting_entity_type` / `resulting_entity_id`）与
   `status` 的三种终态约束已经就位，41 号只写值。

## 仍然做不到 / 有意没做

- **没有词法选择器**（理由见上）：没有点名工具时，草稿分支回答"告诉我要起草哪一个"。
  「帮我请下周三的假」这类句子要等 42 号工单的模型函数调用。
- **没有确认/拒绝**：41 号工单。界面上的确认按钮是 disabled 的，并写明原因——比一个点了没反应的按钮诚实。
- **约束 B 的第 2、3 层**：52 号工单。本工单只把第 1 层做成测试，并**明确没有**声称另外两层。
- **被拒的草稿不留 `agent_actions` 行**：`agent_actions` 记录的是"助手**提议**了什么"，而一张注定失败的草稿
  什么都没提议；拒绝的理由在回复里（目录化的键与 detail）。若 41 号认为"试过但被拒"也要留痕，
  那需要第五个状态，属于那张票的决定。
- **没有草稿的批量列表接口**（"所有等我确认的草稿"）：本工单只有"这个会话的最新一张"。
  41 号的确认界面如果需要跨会话列表，那是它要加的一条路由 + `ix_agent_actions_pending` 这个索引（已经在迁移里备好）。
- **`facts` 不是动态 schema**：工具各自决定往里放什么，客户端只画它认识的那几个（`draft-form.tsx::factRows`），
  其余不显示——新增一个 fact 不会让界面漏出内部标识符，也不会自动出现。
- **同一会话的第二次点选不会重新读取草稿**：`qa-store.select()` 是 37 号工单的语义（"这份对话已经读过了就不再读"），
  所以"本客户端自己问出来的草稿"会经 `readInto` 立刻出现，而**别处**（另一个标签页、夹具）产生的草稿要等一次
  页面重载。清单要求的是"刷新页面后仍能找到"，这一点成立；把 `select` 改成"有 pending 草稿就重读"是可以在
  41 号工单顺手做的小改进（草稿会过期，重读才看得到 `expired`）。**没有做**，因为它会动 37 号工单的缓存语义。
