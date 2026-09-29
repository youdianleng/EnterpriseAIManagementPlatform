# 41 — 人审确认点与 agent_actions 审计

**What to build:** 员工看到助手填好的表单，点"确认提交"之后，单据才真正进入审批流。**提交人记录为员工本人**，但审计日志里同时标明这次提交是助手发起的、由谁确认的。聊天里回一句"好的"**不算**确认。

**Blocked by:** 40 — 草稿工具：产出待确认表单（不写库）

**Status:** done

- [x] 只有显式点击"确认提交"按钮才会真正创建单据；聊天中的语言回复不触发任何写入
      — `test_a_chat_reply_that_sounds_like_consent_writes_nothing_at_all`（三条确认语气的消息走进**对话路径**，
      逐表计数不变、`agent_actions` 仍是 `proposed`）、
      `test_the_chat_path_has_no_way_to_reach_a_confirmation`（结构性：`ai/agents/**` 里没有任何模块 import
      `domain/agent/confirmation`；`await_confirmation_node` 只记录答案的**类型**）、
      `web/scripts/visual-check.mjs::checkDraftDecisions`（浏览器里把"sí, confirma, adelante"打进输入框，
      再读会话确认草稿仍 `proposed`、请假单数量不变）。
- [x] 点击确认时会重新校验会话有效性与当前权限，权限已变更时拒绝并提示重新生成
      — `test_a_withdrawn_permission_refuses_the_confirmation_and_creates_nothing`（账号被停用 → `resolve_principal`
      返回空 → 401，草稿原封不动、无单据）、
      `test_a_permission_that_no_longer_permits_is_refused_by_the_action_it_names`（内核在确认时刻被重新问一次，
      拒绝里点名 `leave.request_own`）、`test_a_rule_that_moved_refuses_the_confirmation_and_keeps_the_draft_proposed`
      （额度被 HR 改成 0 → 复用提交路径自己的 `ERR_LVE_009`，草稿仍是 `proposed`）、
      `test_a_confirmation_without_a_session_never_reaches_the_handler`（无 cookie → 401，草稿不动）。
      **"变更"衡量的是什么，见下面单独一节。**
- [x] 确认提交后，单据的申请人/提交人记录为**员工本人**，走既有的两级审批流，不跳过任何一级
      — `test_the_submitted_document_is_the_employee_s_own_and_walks_both_levels`（`leave_requests.employee_id`
      == 员工本人、`approval_requests.initiated_by='agent'`、`confirmed_by_user_id`==点击者的 user id、
      状态停在 `pending_first`、第一关是经理且可被经理真的批掉、批完进第二关）、
      `test_a_timesheet_draft_confirms_into_the_week_s_own_request`（工时那一个落到**周**的审批请求上）。
- [x] 审批链路上看到的信息与员工手工提交的单据完全一致，审批人无法也无需要区分来源
      — `test_a_confirmed_draft_and_a_hand_filed_request_are_the_same_shape`（同一经理读两份单据，
      响应键集合逐键相等、`employee_id`/`leave_type`/`business_days_count`/`state`/附件四列相等、
      审批块形状相等，**唯一差别是透明度标注**）。
- [x] 审计中记录：发起方为助手、确认人、确认时间、使用的工具与入参、生成的草稿内容、最终产生的单据 ID
      — `test_the_audit_row_carries_the_tool_its_input_the_form_and_the_entity`（逐列对照，
      并断言"存的是**提议时**的草稿"而单据里是**确认时**的值——员工改过的那个字段两者不同）。
- [x] 每笔助手发起的操作在助手操作记录表中全程留痕：工具名、入参、出参、草稿、确认状态、最终实体
      — `test_the_row_is_the_record_from_proposal_to_submission`（同一行、点击前后各读一次，
      id/工具/入参/草稿不变，状态从 `proposed` 到 `confirmed` 并带上最终实体）、
      `test_the_column_and_the_enum_agree`（40 号工单的，仍在跑）。
- [x] 员工拒绝草稿时同样留痕（状态为已拒绝），不产生任何单据
      — `test_a_rejection_is_recorded_and_creates_nothing`（`agent_actions` 之外的**每一张表**逐表计数不变、
      行状态为 `rejected`、`confirmed_at` 有时间、`resulting_entity_*` 为空、再次拒绝被 `ERR_AGT_002` 拒）。
- [x] 单据详情页对审批人可见"由助手起草、本人确认"的标注（透明度要求）
      — `test_the_approver_s_request_detail_carries_the_transparency_annotation`（请假/补卡/工时三处
      `ApprovalRead` 都从引擎自己的行上读出 `initiated_by` 与 `confirmed_by_user_id`；手工提交的那份是
      `user` + null）。
- [x] 有端到端测试：从对话到单据生成的完整链路，断言中间过程中数据库无任何写入
      — `test_from_conversation_to_document_and_the_middle_wrote_nothing`（对话产草稿 → 等确认期间计数不变
      （含"员工在浏览器里改了字段"这一步）→ 点击只多出 1 张请假单 + 1 个审批请求 + 1 个审批步骤 →
      单据是本人的、审批从第一关开始、经理与 HR 各批一次后 `approved`、经理收到通知）。

**另加三条本工单自己的断言**（都是被 mutation 逼出来的，见下）：
`test_a_second_confirmation_of_the_same_draft_creates_nothing_further`、
`test_the_audit_row_cannot_be_re_decided_even_without_the_service`、
`test_two_clicks_at_once_produce_one_document`、
`test_the_revalidation_is_the_submission_s_own_rule`（结构性：确认模块的可执行语句里没有第二条规则）。

## "权限已变更"衡量的是什么（这是本工单最需要说清的一件事）

§6.3 要求点击时"重新校验权限与会话"。做出来的东西比这句话**窄**，而窄在哪里是设计事实而不是缺口：

* **权限那一半**：确认时**重新落一次主体快照**。`current_principal` 走
  `access/snapshot.resolve_principal`，它**每个请求都从数据库读**账号与任职，缓存键里带着
  `session_epoch`、角色戳、密级、任职版本与组织结构版本（`snapshot.py`），所以"角色被撤、部门被移、
  密级被降、任职被终止、账号被停用"都在**点击那一刻**被读到。然后 `ConfirmationService._require_permitted`
  拿这个主体去问内核，问的是**这份单据提交时需要的那个动作**（`leave.request_own` /
  `attendance.correction_own` / `timesheet.write_own` + `timesheet.submit_own`），资源是**调用者自己**的
  employee 资源。
* **但这三个动作都是 self-only 的**（`SELF_ONLY_ACTIONS`），而 `PrincipalBuilder` 永远给快照加上
  `employee`——所以**只要账号还在解析，任何"别的授权"都不可能让一次自我提交变得合法或非法**。部门搬了、
  密级降了、角色撤了，都不影响"我能不能给自己请年假"。这不是漏洞，正是 §6.3 第三条（提交人是员工本人）
  与这条清单是同一句话。
* **真正会变的是"账号还解不解析得出来"**：停用、终止任职、会话失效——也就是**离职发生在草稿与点击之间**。
  这条由上面两条测试钉住（401，草稿原封不动）。
* **单据自己的规则**是另一半，而且是"廉价的那一半"：确认时重新走 `LeaveService.check_request` /
  `CorrectionService.check_draft` / `TimesheetService.check_entry`（40 号工单为这个缝提取的），
  所以"表单开着的时候额度被别人花掉了"会被同一条规则拒绝，报的还是**提交路径自己的错误码**
  （`ERR_LVE_009` 等）。

**拒绝读起来是这样的**（`ERR_AGT_003`，409，`message_key` 借领域自己的）：

> No he podido registrar el borrador: las condiciones han cambiado desde que lo preparé.
> Pídeme que lo genere de nuevo para ver las cifras actuales.

配上 `message_key = errors.leave_balance_insufficient`（或周被锁、任务不存在等），所以员工既知道"要重新生成"，
也知道是**哪一条**变了。

## 实现

### 文件

**新增（api）：**

- `app/domain/agent/confirmation.py` —— `ConfirmationService`：`confirm` 与 `reject` 是全部接口。
  协作对象（三个领域服务 + 引擎装饰器）在这里按路由的方式装配，因为本模块要跨域编排；
  `AgentActionService.repository` 是复用**同一个**仓储，让 `load_for_update` 的行锁覆盖随后的实体写入。
  另有 `confirmed_values()`（把浏览器发来的字符串按 `PrefillField.kind` 与提交模型的类型还原——
  日期、整数、**uuid**、以及由 `business_date` + `HH:MM` 合成 Madrid 时刻的补卡 instant）、
  `message_key_of()`（领域错误码 → 目录键）。
- `app/api/v1/agent.py` —— 两条路由 `POST /api/v1/agent/actions/{id}/confirm` 与 `.../reject`，
  守卫 `session.read_own`（和 37 号工单的四条会话路由同一个动作、同一个理由）。
- `tests/test_agent_confirmation.py` —— 25 条。
- `web`：`draft-form.tsx` 改成有确认/拒绝两条路（各自一个 `<dialog>`）、`lib/api/answers.ts` 的
  `confirmDraft`/`rejectDraft`、`qa-store.ts` 的 `decideDraft`（调用 + **回读会话**）、
  `lib/i18n` 的 `qa.draft.*` 新增约 25 条双语键。

**改写（api）：**

- `app/domain/agent/repository.py` —— `load_for_update`（`FOR UPDATE` + 同一条语句里算出 `expired`）、
  `decide`（一条带守卫的 UPDATE，`confirmed_at` 用数据库的 `now()`）。删掉了第一版的
  `can_still_confirm`——理由见"两处自我修正"。
- `app/domain/{leave,attendance/timesheet}/…` —— `LeaveService.submit`、`CorrectionService.submit`、
  `TimesheetService.submit` 各多一个 `context: SubmitContext | None = None` 参数（默认 `SubmitContext()`，
  所以路由一个字没改）。**这是本工单唯一碰它们的地方**：确认调用的是**同一条写入路径**，
  `initiated_by="agent"` 与 `confirmed_by_user_id` 是那条路径多收的一个参数。
- `app/api/v1/{leave,attendance}.py`、`app/api/v1/schemas/timesheet.py` —— 三处 `ApprovalRead` 多两个字段
  （`initiated_by`、`confirmed_by_user_id`），从引擎自己的行上读。**这就是透明度标注的出处**，
  53 号工单直接渲染，不需要自己发明字段。
- `app/api/v1/schemas/answer.py` + `app/api/v1/answer.py` —— `DraftRead` 多 `confirmed_at` /
  `resulting_entity_type` / `resulting_entity_id`，会话读取因此能告诉界面"它变成哪张单据了"。
- `app/core/errors.py` + `app/core/messages.py` —— 三个码 `ERR_AGT_001/002/003` 与三句双语；
  401/409 的选择写在枚举的注释里。**`AppError` 多了一个可选 `message_key` 覆盖**：
  `ERR_AGT_003` 的句子要是领域自己的（点名额度/周/任务），而码仍是本模块的
  （否则 404 说不清"这是你的草稿"）。这是全场唯一一处覆盖，`build_envelope` 会先校验这个键真的在目录里。
- `app/main.py` —— 挂上 `agent_v1.router`。
- `tests/test_permission_matrix.py` —— `HTTP_MATRIX` 两行（confirm/reject，`session.read_own`），
  字面量 `81` → `83`，并且 `action_id` 加进 `.format()` 与 payload 表。
- `tests/tools/seed_agent_draft.py` —— 多两个"给决策用"的草稿（见下）。

**改写（web）：**

- `web/app/[locale]/(app)/qa/draft-form.tsx` —— 有确认/拒绝两个 dialog、`aria` 齐全、状态徽章四态、
  回答之后**按钮消失**（只剩说明）、确认后标题去掉"(borrador)"、并给出"查看已提交单据"的链接。
- `web/scripts/visual-check.mjs` —— `checkDraftForm` 改成断言"确认已提供 + dialog 的内容"，
  新增 `checkDraftDecisions`（真实点击链路）。

### 端点是这个形状，动作是这些

| 路由 | 动作（路由级守卫） | 处理器内部再问的动作 | 返回 |
|---|---|---|---|
| `POST /api/v1/agent/actions/{action_id}/confirm` | `session.read_own` | 按草稿实体：`leave.request_own` / `attendance.correction_own` / `timesheet.write_own` + `timesheet.submit_own` | 201 `{id, tool_name, status:"confirmed", confirmed_at, entity_type, entity_id}` |
| `POST /api/v1/agent/actions/{action_id}/reject` | `session.read_own` | 无（丢弃自己的草稿不与任何单据规则相撞） | 200 `{…, status:"rejected", entity_type:null, entity_id:null}` |

**为什么守卫是 `session.read_own` 而不是新动作**：草稿是会话材料（§3.6 用 `conversation_id` + `user_id`
给它定位），37 号工单的四条会话路由都用这个动作，理由是"这是一个关于调用者本人的界面"，而且
`session.read_own` 的**角色列表就是全部人**——再加一个同角色列表的动作就是同一条规则写两遍
（"确认点需要什么权限"的答案已经在库里：**提交那份单据需要的权限**，由处理器内部按实体问内核）。
**不新增任何 catalogue action**，所以 `DESIGN_GRANTS` / `KIND_FOR_ACTION` / `RULES` 都不用动，
只有 HTTP 矩阵多两行。

**404 而不是 403**：行级 `WHERE user_id` 让"没有这张草稿"和"这不是你的草稿"是同一个答案，
所以这个端点不能拿来探测别人的草稿 id 存在与否。

### 三个拒绝各自的读法

| 码 | 状态 | 什么时候 |
|---|---|---|
| `ERR_AGT_001` | 404 | 没有这张草稿，或者不是你的 |
| `ERR_AGT_002` | 409 | 草稿已不是 `proposed`：已确认、已拒绝、或**已过期**（过期时先把行写成 `expired`）。句子让员工"重新生成" |
| `ERR_AGT_003` | 409 | 调用者已经不能提交这类单据（权限），或者单据内容本身被拒（规则变了）。`message_key` 是领域自己的（`errors.leave_balance_insufficient` 等） |

**被拒的确认不消耗草稿**：只有真的创建了单据（或员工真的拒绝）才写 `status`。否则草稿留在 `proposed`，
员工可以在不丢失"助手提议过什么"的前提下重新生成。把一次被拒的确认记成 `rejected` 等于替员工说"他拒绝了"
——而他没被问过。

### 原子性：为什么是一条带守卫的 UPDATE，加一把行锁

`load_for_update` 用 `FOR UPDATE` 取行，并在**同一条语句**里由 PostgreSQL 算出 `expired`；
`decide` 的 `WHERE` 要求 `status = 'proposed' AND expires_at > now()`。
于是"两次点击"只会产生一张单据：后来的那个要么在锁上等到前一个提交（然后读到不再是 `proposed`），
要么在守卫的 UPDATE 上落空——两种情况都不会多出单据，因为实体的写入和守卫的那条 UPDATE 在**同一个事务**里。

### 事务边界：`record_draft` 会 commit，所以要重新发布权限上下文

40 号工单留下的第 2 条注意事项，本工单在**每一处需要匹配行的语句之前**都做了一遍：
`_create` 里每个 `draft`/`add_entry` 之后、`submit` 之前，`decide` 之前。
漏掉最后一处的后果是**第一版真实踩到的**：`decide` 的守卫在没有上下文的新事务里匹配不到任何行，
于是一次**已经创建了单据**的确认被报成"草稿已过期"。测试抓到了它
（`test_the_submitted_document_is_the_employee_s_own_and_walks_both_levels`），修法是加一行 `_republish`，
不是删一个 commit。

### 审计行

点击之后 `agent_actions` 的一行是：

| 你要的东西 | 在哪里 |
|---|---|
| 发起方为助手 | `approval_requests.initiated_by = 'agent'`（§3.4 的列） |
| 确认人 | `approval_requests.confirmed_by_user_id` = 点击者的 user id |
| 确认时间 | `agent_actions.confirmed_at`，**数据库的 `now()`**（迁移的 CHECK 要求非 `proposed` 的行都有它，拒绝也一样——"人是什么时候回答的"两个方向都值得留） |
| 使用的工具与入参 | `tool_name` / `tool_input` |
| 生成的草稿内容 | `produced_prefill_form`，**是提议时的那份**（可能和确认时不同：每个字段都可编辑，测试专门断言两者不同） |
| 最终单据 ID | `resulting_entity_type` + `resulting_entity_id`（拒绝时都为空，这是「不产生任何单据」在行上的表达） |

### 界面

- 点"确认提交"打开一个 `<dialog>`，写明**将创建什么**、**以谁的名义**、并逐条列出将写入的字段与值。
  第二个按钮才是真的 POST。**两个点击**，而第二个就是这张票的全部要点。
- 拒绝也是 dialog，理由可选。设计系统 §6.4 的"驳回必填理由"讲的是**审批人驳回别人的申请**
  （不给理由会让员工反复提交）；丢掉自己的草稿不是那条流程——草稿反正没了，没人在等解释，
  强制写一句话只是给自己加一道表。理由进日志，**不入库**（`agent_actions` 没有自由文本列，§10.1 也不该凭空造一个）。
- 回答过之后卡片说清结果：徽章换成语义色的"Confirmado"/"Descartado"，绿/灰提示块写明创没创建，
  成功后给一个指向**已提交单据**的链接（实体 id 来自审计行），字段只读，**按钮消失**。
- 标题在回答之后去掉"(borrador)"：API 的标题自带状态词，而点击之后它已经不是草稿了——
  绿面板写着"Documento enviado"、标题还写"(borrador)"是屏幕在自相矛盾。

### 夹具：为什么多两个草稿，以及三个日期各自怎么来的

确认**会消耗**草稿。`checkDraftForm` 要的是"仍待回答"的草稿（它断言字段可编辑、确认已提供），
`checkDraftDecisions` 要的是"可以回答"的草稿。所以 `seed_agent_draft.py` 多写两个，
各自一个会话，问题固定（`CONFIRM_QUESTION` / `REJECT_QUESTION`），界面按会话最新草稿渲染，
一个线程一张才可能同屏两张。

**三个日期都是查出来的，不是算出来的**，而这件事是**第五次跑视觉检查才做完的**——
每一次失败都换一种"夹具撞上自己的产品输出"的方式，三次都记在这里因为它们是同一类缺陷：

1. 决策那两张的日期：第一版用"上周一"固定偏移，第二次跑就被自己上一轮的确认单据
   （`ERR_LVE_008`）拒了。改成 `_free_weekday()`：查该员工近 `EAM_DRAFT_DECISION_SEARCH_DAYS`
   天（默认 60）内没有 live leave 覆盖的工作日。
2. 过期那张的日期：它原本复用请假草稿的日期，于是**同一个数据库里第二次跑必然被上一轮的草稿请求拒**
   （草稿请求也是 live 的）。现在它有自己的一天。
3. 主草稿那一天的日期：`_working_day` 现在除了问排班"这是不是工作日"，
   还问数据库"这个人这天有没有已经批准的假"——因为**视觉检查自己确认一张草稿之后，
   当前周就有一张 live 请假单了**，下一轮的主草稿就被它拒。
4. 补卡那张的日期：`_quiet_day` 从 `working - 1` 起找无打卡的工作日。原来从 `working` 起找，
   而 demo 数据的打卡打到明天为止，于是第一版把业务日定在**明天**，被
   `errors.attendance_event_in_future` 拒（"事件在未来"——补卡的时刻必须在过去）。

5. **`_free_weekday` 必须在 RLS 上下文发布之后调用**：`leave_requests` 是行级安全的，
   没有上下文时那张表**一行都读不到**，于是"找一个没人请假的星期"会把每个星期都算成空的。
   第一版就是这样，然后拿着一个其实被占用的日期去起草，报的却是"夹具参数不合法"。

**这些都不是产品缺陷，是夹具假设了它自己的产物不存在。** 修法是让夹具**读**数据库而不是**假设**它，
和 `_quiet_day`、`_working_day`（40 号工单原本那半）是同一种修法。顺带把错误信息改成能说清是哪一种原因
（排班没播 / 有 live 请假 / 那天已有打卡），原来的"has seed_timesheet_demo.py run?"在三种情况下都一样，
把第三种伪装成第一种。

**这个夹具现在对"连续跑两遍"是幂等的**（对着同一个库连跑两次都成功），这一点是它之前做不到的。

## 两处自我修正（都是 mutation 逼出来的，记在这里因为它们是真实缺陷）

1. **`select` 字段的值到了领域里还是字符串**。`project_id` / `task_id` 在表单里是 `select`，
   HTML 只能发字符串；`resolve_record_target` 里 `task.project_id != project_id` 于是把
   **uuid 和它的字符串**比较，报 `ERR_PRJ_008 "no task … in project …"` ——一个看起来像数据问题、
   实际是类型问题的拒绝。修法是 `confirmed_values` 里一张按**字段名**（不是按 `kind`，因为
   `select` 也承载请假类型这种字符串）列的 `_UUIDS`，把这三个 uuid 还原。
2. **`_require_answerable` 里那句"再问一次 can_still_confirm"是死代码，而且它掩盖了一个活的检查**。
   mutation 显示：把"锁定读回来的 `expired`"改成恒 `false`，**测试全绿**——因为那句重问在每条路径上
   都替它兜住了；而把 `can_still_confirm` 自己改坏，也测试全绿——因为锁保证了行不会在两者之间变。
   两个都不可证伪，于是删掉重问，只留**锁定读算出来的那一个**答案（`FOR UPDATE` 之下不可能有间隙），
   并把 `can_still_confirm` 从仓储里删掉。现在把那个 `expired` 改成恒 `false`，
   `test_an_expired_draft_cannot_be_confirmed_and_says_so` 会红。

## 变更测试（mutation）

每条规则被打断一次，记录**失败的那条测试**，然后还原；`git grep MUTATION-41 -- api/` 为空。
跑法：一个临时脚本（已删）把源码改动写进去、跑选中的测试、`finally` 还原。**注意第一版脚本用
空格拼多个 node id，pytest 把后面的当成额外参数，于是"没跑"被记成"抓住了"**——修正后重跑的结果如下。

| 被破坏的规则 | 破坏方式 | 失败的测试 | 层数 |
|---|---|---|---|
| 身份字段（`initiated_by`/`confirmed_by_user_id`） | `LeaveService.submit` 不接收 `context`，一律 `SubmitContext()` | `test_the_submitted_document_is_the_employee_s_own_and_walks_both_levels` | 1 |
| 只有一次（状态） | `decide` 的 `WHERE` 去掉 `status = 'proposed'` | `test_the_audit_row_cannot_be_re_decided_even_without_the_service` | 1 |
| 只有一次（窗口） | `decide` 的 `WHERE` 去掉 `expires_at > now()` | `test_the_audit_row_cannot_be_re_decided_even_without_the_service` | 1 |
| 过期即不可确认（原子读） | `load_for_update` 的 `expired` 恒 `false` | `test_an_expired_draft_cannot_be_confirmed_and_says_so` | 1 |
| 权限重校验 | 删掉 `_require_permitted` 调用 | `test_a_permission_that_no_longer_permits_is_refused_by_the_action_it_names` | 1 |
| 拒绝不产生单据 | `reject` 里先 `_create` 再写状态 | `test_a_rejection_is_recorded_and_creates_nothing` | 1 |
| 重新校验用提交自己的规则 | `check_request` 里跳过 `_require_affordable` | `test_a_rule_that_moved_refuses_the_confirmation_and_keeps_the_draft_proposed` | 2（同一处破坏同时打红 25 号工单的提交路径测试） |
| 审计守卫（带 `RETURNING` 的那条） | 同上去掉 `status = 'proposed'` | `test_the_audit_row_cannot_be_re_decided_even_without_the_service` | 1 |
| 拒绝的线上形状 | `ERR_AGT_003` 换成 `ERR_AGT_001` | `test_a_rule_that_moved_refuses_the_confirmation_and_keeps_the_draft_proposed` | 1 |
| **行锁** | `load_for_update` 去掉 `FOR UPDATE` | **没有测试红**（见下） | — |

**行锁那一条如实报告**：去掉 `FOR UPDATE` 之后全绿。原因是并发那一路其实还有第二道真实防线——
`leave_requests` 的"同一人同一日期不能有两条 live"（`ERR_LVE_008`）——
所以"两次同时点击只产生一张单据"在没有锁时仍然成立，只是**拒绝的形式不确定**
（`ERR_AGT_002` 或 `ERR_AGT_003`），我因此把并发测试的断言写成"恰好一个 201、恰好一张单据、
错误码是这两个之一"，那是对实际保证的诚实描述。锁保留：它是"读—判断—写"的正确原语，
让第二个请求的拒绝是**结构性的**而不是靠另一张表的约束碰巧兜住；但**行为层面无法证明它在**，
这一点写在这里而不是假装测过。

## 验证

- `uvx ruff check app tests` 干净（`All checks passed!`）。
- `cd web && npx tsc --noEmit` 干净；`npx next build` 成功（`Compiled successfully`，`/[locale]/qa` 54 kB）。
- 目标运行（scratch 库 `eam_test_t41`、Redis 9 号库）：
  - `pytest tests/test_agent_confirmation.py` → **25 passed**
  - `pytest tests/test_agent_confirmation.py tests/test_agent_draft_tools.py tests/test_agent_graph.py
    tests/test_permission_matrix.py` → **113 passed**
  - `pytest tests/test_permission_matrix.py tests/test_leave.py tests/test_attendance_corrections.py
    tests/test_timesheets.py tests/test_timesheet_lock.py tests/test_domain_approval.py tests/test_errors.py`
    → **374 passed**（矩阵字面量、三处 `ApprovalRead`、`AppError` 的 message_key 覆盖都没有破坏既有行为）
- **全量运行**（与 40 号工单同一条命令）：
  `docker compose exec -T -e TEST_DATABASE_NAME=eam_test_t41 -e REDIS_URL=redis://redis:6379/9 api sh -lc
  'cd /app && env -u DOCUMENT_PARSE_RUNNER_ENABLED python -m pytest -p no:warnings'`
  → **1486 passed**（0:27:24）。基线 1459，差 27：25 条确认测试，加上 HTTP 矩阵那两行带来的净增
  （矩阵测试按角色参数化，行数本身不改变条数；`assert checked == 83` 与 payload 表在同一条测试里）。
- 浏览器：`EAM_USERNAME=empleado EAM_PASSWORD=… node scripts/visual-check.mjs` → **ALL CHECKS PASSED**，
  669 条断言，其中本工单新增/改动的 **约 40 条**（三个草稿的"确认已提供 + 两个 dialog + 关闭后仍未回答"、
  决策链路的确认/拒绝/措辞/窄屏、以及标题与只读态）。**这是本工单代码状态的最终判定**——
  之后没有再动过 api 或 web 的任何一行。
  **如实记录**：再往后几次重跑（为了在夹具修好之后重截一遍图）开始出现
  `page.goto: Timeout 30000ms exceeded` / `apiRequestContext.post: Timeout …`——不是断言失败而是超时。
  原因是这个已经跑了很久的 `next dev` 进程（容器内存 6.4 GiB、单页 7–30 秒），
  与本工单的改动无关（时钟、请假、工时页面也一样慢，而它们本工单一行没碰）；
  按 brief 我没有重建或重启服务。最后一次**干净**的全量浏览器运行就是上面那次 PASSED，
  它跑在本节的代码上，因为此后我只改过夹具与 README。
  在看图之后改了三处真实缺陷：
  1. **卡片标题在确认后仍写"(borrador)"**，而正上方绿面板写着"Documento enviado"——
     标题由 `cardTitle()` 在回答后去掉状态后缀（`data-draft-title` 保留 API 原文供检查断言）。
  2. **"Confirmado" 徽章是灰底灰字**，读起来像一句被弱化的注解而不是状态——改成语义色
     （绿底绿字 / 灰底灰字），文案本身没变。
  3. **320px 下 `disabled:opacity-50` 的"Confirmar y enviar"看起来还能按**（320 截图暴露的）——
     回答过的卡片**不再画这两个按钮**，只留说明句。
  另有一处不是缺陷但值得记：说明句在语料里出现了两个连续句号（`…aprobación habitual.` 后面又接句号），
  顺手改写得更短。
- 窄屏与双语看图：`qa-draft-decided-320-es.png` / `-768-es.png` / `qa-draft-rejected-en.png` /
  `qa-draft-confirmed-es.png` / `qa-draft-*-confirm-dialog-es.png` / `qa-draft-*-reject-dialog-es.png` /
  `qa-draft-expired-es.png`，三档宽度、两种语言都读过。320px 无横向溢出、控件高度 44px。

## 仍然做不到 / 有意没做

- **没有"所有等我确认的草稿"列表**：确认界面仍是会话内的一张卡片（40 号工单的形态）。
  跨会话列表要一条新路由和 `ix_agent_actions_pending` 索引，那是另一个界面的事。
- **图形确认（LangGraph `Command(resume=…)`）没有接**：确认走的是 HTTP 端点，不是把值 resume 回
  被中断的图。原因是 §6.3 要的是"以员工本人名义提交并走两级审批"，而那件事的完整语义在领域写入路径上；
  把 resume 也接上会让同一件事有两个入口，而其中"图那半边"没有实体可写。被中断的 run 仍可恢复、
  仍会被记录，只是**确认不经过它**。`await_confirmation_node` 依旧只记录答案的类型。
- **同一会话的第二次点选不重读会话**（40 号工单记录过的 37 号工单缓存语义）仍保持原样；
  不过本工单在 `decideDraft` 之后**一定回读会话**，所以确认/拒绝之后的卡片是最新的。
- **约束 B 的第 2、3 层**（运行时只读上下文、只读数据库角色）：52 号工单，本工单不声称。
- **审批人的界面**：53 号工单。本工单只保证 API 里有那两个字（`initiated_by`、`confirmed_by_user_id`）
  并且是真的。

## 给 53 号工单的话

透明度的字段是 **`approval_requests.initiated_by`**（`user` | `agent` | `system`）与
**`approval_requests.confirmed_by_user_id`**。它们已经出现在三处单据详情的 `approval` 块里
（`app/api/v1/leave.py`、`app/api/v1/attendance.py`、`app/api/v1/schemas/timesheet.py`），
不需要新字段、不需要前端猜：`initiated_by == "agent"` 就是「由助手起草、本人确认」，
`confirmed_by_user_id` 是那位本人。手工提交的单据这两个值是 `"user"` 与 `null`，
所以判断条件不是"有没有这个字段"而是那个值。

## 给 42 号工单的话

- 模型函数调用要落到草稿分支，缝是 **state 里的 `tool` + `tool_arguments`**
  （`draft_tools_node` 已经这么取用），本工单没有动它。
- 确认**不走**模型，也**不走**图：`app/ai/**` 里没有任何模块 import `domain/agent/confirmation`，
  有一条测试钉住这一点（`test_the_chat_path_has_no_way_to_reach_a_confirmation`）。
  如果 42 号想让模型在确认之后说一句话，正确的做法是**在领域写入完成之后**由调用方渲染目录句子，
  而不是让模型去触发确认。
