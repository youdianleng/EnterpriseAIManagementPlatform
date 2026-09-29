# 39 — 只读工具（本人 + 经理）

**What to build:** 员工可以直接问"我这个月工时多少""我还有几天年假""昨天我几点下的班"，助手调用只读接口给出准确数字，而不是靠模型猜。经理可以问自己团队的汇总。

**Blocked by:** 38 — LangGraph 编排骨架

**Status:** done

- [x] 提供工具：查我的考勤、查我的年假余额、查我的工时表状态、查同事联系方式（仅姓名 / 职位 / 邮箱 / 照片）
- [x] 提供工具：经理查本人直属下属的考勤或工时汇总
- [x] 所有只读工具以**被调用者的身份**执行查询，复用权限内核；助手无法以更高权限读取数据
- [x] 经理查询的范围严格限定为直属下属，非下属返回空且不提示"存在但无权"
- [x] 查同事联系方式受通讯录可见性规则约束，不返回住址、编号等敏感字段
- [x] 工具返回结构化数据，回答中的数字直接来自查询结果，不允许模型重算或推测
- [x] 工具调用失败时给出明确提示并回退为"无法获取该数据"，不编造数值
- [x] 有测试覆盖：普通员工调用经理工具被拒；经理查询非下属返回空
- [x] 工具清单是白名单，未注册的工具无法被模型调用

## 依赖可用性

无。本工单没有改 `api/pyproject.toml` 或 `api/uv.lock`：38 号工单已经把
`langgraph` / `langgraph-checkpoint-postgres` / `langchain-openai` 装进镜像，只读工具
只需要 `sqlalchemy`、`domain/` 与 `repositories/`，全部已在。

## 实现

### 文件

**新增（`api/app/ai/tools/`，38 号工单留的空包现在有五件东西）：**

- `models.py` — `ToolKind`（只有 `READ_ONLY`/`DRAFT`）、`ToolOutcome`（`ok`/`refused`/
  `failed`/`unknown`）、`ToolContext`、`ToolCall`、`ToolResult`、`Tool`、`UnknownTool`，
  以及 `ALLOWED_PARAMETERS`。
- `services.py` — 四个只读协作者的装配：`AttendanceService`（带排班与加班账本）、
  `LeaveService`、`TimesheetService`（`principal` 就是调用者）、员工仓储的通讯录读。
- `readonly.py` — §6.2 只读半边的五个实现 + `READ_ONLY_TOOLS` 字面量。
- `render.py` — `ToolResult` → `ToolAnswer`（`message_key` + `es` + `en` + 二者合成的
  `text`），以及三个常量答复。
- `selection.py` — 问题 → `ToolCall`（工具名 + 默认参数）：`select_tool()` 与
  `arguments_for()`。

**改写：**

- `registry.py` — `REGISTRY`（`MappingProxyType`）、`registered()`、`lookup()`、`invoke()`。
- `__init__.py` — 以上全部的重导出。
- `agents/state.py` — `AgentState` 新增 `tool` / `tool_arguments` / `tool_result` /
  `tool_answer` / `tool_outcome`；`AgentContext` 新增 `session` 与 `today`。
- `agents/nodes.py` — `read_only_tools_node` 真正调用工具；新增 `_tool_call()` / `_run()`。
- `agents/records.py` — `NodeRecord.tool_name`（§10.1 的 `tool_name`），
  `recorded(..., tool_key=...)`，`_decision` 改名 `_constant`（同一个函数现在服务于
  "决策"与"工具名"两个声明常量）。
- `agents/replies.py` — 删除 `NO_READ_ONLY_TOOL`（占位说明随占位一起消失）。
- `agents/intents.py` — 新增 `_CONTACT_DETAIL`/`_PERSON`/`_TEAM`/`_TEAM_DATA` 与
  `team_data_query`、`contact_query` 两条规则（都是 `READ_ONLY_QUERY`）；补齐
  `_OWN` 与 `_OWN_DATA` 的两个词表缺口（见下）。
- `agents/__init__.py`、`ai/__init__.py` — 导出与 DESIGN §1 映射表。
- `agents/graph.py` — 路由表注释里 `read_only_tools` 不再是"具名占位"（拓扑本身没变）。
- `app/core/messages.py` — 15 条 `agent.tool.*` 双语文案；并修正
  `errors.forbidden_attendance_of_another` 中已被本工单推翻的一句话（原文说团队汇总
  "不是我在这里能打开的"，现在它能）。
- `tests/test_agent_graph.py` — 38 号工单的
  `test_the_read_only_branch_says_no_tool_is_registered` 删除（该分支的行为属于本工单，
  测试搬到 `tests/test_agent_readonly_tools.py`），草稿分支那条改为断言**草稿半边**
  仍为空。

**新增测试：** `api/tests/test_agent_readonly_tools.py`（22 条）。

### 白名单的形状：名字 → 实现，只有一扇门

`REGISTRY` 是以**同名**为键的 `MappingProxyType[str, Tool]`，`lookup(name)` 是唯一把
名字变成实现的函数；`invoke(call, context)` 是唯一执行它的函数。所以
`ai/tools/registry.py` 的 `lookup` 是"未注册的工具无法被调用"的落点，而不是提示词里
的一句话：

```python
REGISTRY: Final[MappingProxyType[str, Tool]] = MappingProxyType({**READ_ONLY_TOOLS})
def lookup(name: str) -> Tool: ...   # KeyError → UnknownTool，绝不回退
async def invoke(call, context) -> ToolResult: ...   # 抛出的异常 → FAILED
```

`Tool` 携带实现本身（`run`）、种类、一句话说明，以及**闭词表** `parameters`：
`models.Tool.__post_init__` 拒绝任何不在 `ALLOWED_PARAMETERS`
（`from_date`/`to_date`/`year`/`status`/`name`）里的参数名。因此"给工具一个能指定读谁
的参数"不是被禁止，而是**构造不出来**：一个声明了 `employee_id` 的 `Tool` 会抛
`ValueError`。`name` 看着像例外，其实不是——它是通讯录自己的查找键，通讯录对全员可读
（`employee.directory`），而匹配到谁返回什么由 `employee/visibility.py` 的投影决定，
不由参数决定。

`invoke` 把异常**陈述**为 `ToolOutcome.FAILED`，异常类名进日志与 `error_type`，
`str(error)` 不进（查询的异常可能带着参数）。`UnknownTool` 故意不在这里吞：那是白名单
的答复，由节点转成"没有这样的工具"的文案。

### 调用者的身份怎么到达每个工具

一条路径，没有第二条：`AgentContext.principal` → `ToolContext.principal` →
实现里**只**用 `context.principal`。五个工具各自先问权限内核（`kernel.can`）：

| 工具 | 动作 | 资源 | 数据来源 |
|---|---|---|---|
| `get_my_attendance` | `attendance.read_own` | `EMPLOYEE(owner=本人)` | `AttendanceService.range_view(principal.employee_id, …)` |
| `get_my_leave_balance` | `leave.read_own` | `EMPLOYEE(owner=本人)` | `LeaveService.balances(principal.employee_id, year=…)` |
| `get_my_timesheets` | `timesheet.read_own` | `EMPLOYEE(owner=本人)` | `TimesheetService(…, principal=…).list_weeks()`（服务自己的 `employee_id` 就是调用者） |
| `get_colleague_contact` | `employee.directory` | `EMPLOYEE(owner=本人)` | `EmployeeRepository.list_directory()` + `project_directory_row(调用者的 ViewerContext, …)` |
| `get_team_attendance_summary` | `attendance.read_report`（**无资源**：角色检查） + 每个下属一次同动作带资源 | `EMPLOYEE(owner=下属)` | `AttendanceService.range_view(下属, …)` |

前三个是 `SELF_ONLY_ACTIONS`：内核只允许 owner 等于调用者本人，所以"以更高权限读取"
在类型与目录两侧都不成立。经理工具的动作 `attendance.read_report` 的角色表是
`{manager}`，普通员工连角色检查都过不去。

### 经理的范围：直属下属，且非下属"和没有数据"是同一句话

三层机制，缺一不可：

1. 内核问一次 `attendance.read_report`（无资源）——不是 manager 就在任何查询之前被拒；
2. 候选集是 `Principal.reports_employee_ids`——`domain/access/snapshot.py` 记录过它
   是什么（**曾经**把调用者自己的审批上级也算进去的 bug 在这里被复述），本工单消费它
   而不是自己从 assignment 重新推导；
3. 每个候选再作为资源问内核一次，所以"谁能进汇总"的答案在内核，快照只提供候选。

**非下属返回什么。** 汇总里没有他们的任何一行——不是查出来再过滤掉，而是**从来没有被
选中**（`employee_ids` 那一层根本不存在，候选集里就没有他们）。为了让"存在但无权"不可能
被读出来，答案侧只有**一句话**：
`agent.tool.team_attendance.empty`（"Tu equipo no tiene datos de jornada en ese
periodo."）。`test_a_stranger_is_absent_and_indistinguishable_from_an_empty_team` 的
证明方式是**字符串相等**：一个有下属但期内无数据的经理，和一个完全没有下属的经理，两个
`tool_answer` 逐字段相等；同时断言那位同部门非下属的 id / 分钟数 / 姓名在结果 JSON 与
答案文本里都不出现，且答案里没有任何"permission / permiso / denied"一类词。

### 通讯录：投影不是记录

`project_directory_row(调用者的 ViewerContext, entry)` 是 07 号工单的规则本身，本工单
只调用它。在此之上工具再取 DESIGN §6.2 的「仅姓名/职位/邮箱/照片」：`CONTACT_FIELDS`
六个键，且是**投影自己键集的子集**（`{k: projected[k] for k in CONTACT_FIELDS if k in
projected}`），所以它是"窄化"，不是第二份可见性规则——投影 drop 掉的键这里取不到。
住址、员工编号、生日、入职/离职日期根本不在通讯录行的键里，因此也不可能被这里的 bug
带出来（测试按名字逐条断言它们不在序列化结果中）。邮箱被投影收回时是**缺席**，不是
null、更不是"你看不到"：`agent.tool.colleague_contact.no_details` 与"此人没登记邮箱"
是同一句话。

### 数字来自查询

工具只返回结构化 `data`（分钟、天数、日期、状态、计数），**句子在 `render.py` 里由
`template.format(**data)` 生成**——没有算术、没有单位换算、没有四舍五入、没有默认值。
少数在工具里算出来的聚合（一段期间的合计分钟、有打卡的天数）是对服务返回的行求和，与
API 自己的读取者做法一致；每个数字都被测试拿**测试自己写进去的数据**核对过（插入的
480 分钟、HR 授予的 22+5、`timesheets` 的 SQL `GROUP BY status`），而不是拿工具自己的
字段自证。

### 失败：明确提示，且答案里一个数字都没有

`registry.invoke` 把异常变成 `FAILED`，`render` 返回 `agent.tool.unavailable` /
`agent.tool.unknown` / `agent.tool.not_permitted` 三个常量之一。这三条文案在**两种语言
里都不含任何数字字符**，测试同时断言：
`test_a_failing_tool_states_no_figure_at_all`（真实失败：把日期倒过来，`range_view`
拒绝，走完整图）与
`test_the_three_constant_answers_carry_no_digits_in_either_language`（文案层面，
逐 locale）。同一条测试里的对照跑证明"成功答案里确实有那个数字"，否则"没有数字"这个
断言会因为它对任何答案都成立而变得空洞。

失败是**真的**失败：`get_my_attendance` 的 from > to 由
`AttendanceService.range_view` 的 `RANGE_INVALID` 抛出，不是替身。

### 分类与选择：两层词法，谁负责什么

- `agents/intents.classify` 回答「这是不是一次数据查询」，路由到 `read_only_tools`；
- `ai/tools/selection.select_tool` 回答「是哪一份数据」，并填默认参数（本月 / 今年 /
  全部状态 / 从问句里抽姓名）。

这一层是 42 号工单用模型函数调用替换的接缝，本工单在 docstring 里写明了。两层必然共享
一部分词表，重复被记录而不是隐藏。

**`search_policy` 故意没有注册。** §6.2 把它列为第六个只读行；工单原文是「如果把它作为
工具暴露」。暴露它就是把 35 号工单钉住的检索再开一个入口，而且工具要返回结构化值，一个
带引用的答案不是结构化值。制度问答仍然只走 `answer_policy` → 34 号工单的
`AnswerService.stream()`。`test_design_6_2s_search_policy_is_not_a_second_retrieval_path`
把这一点钉成一条测试。

**38 号工单的分类器词表补了两个洞**，两个都是工单自己举的例子问不出来的原因：

- `¿Cuántas horas he fichado este mes?` —— `_OWN` 原来只认 `mi/mis/me/tengo`，不认西班牙
  语用助动词 `he` 表第一人称的那一半，补 `\bhe\b`；
- `昨天我几点下的班` / `我这个月的工时是多少` —— 中文用光杆"我"标主语，`_OWN` 原来只认
  "我的 / 我有 / 我还"；补 `我(?:几|昨|今|上|这|本|下)`（**不是**光杆"我"：
  「我同事的年假」也以"我"开头，匹配它就会把别人的问题读成自己的记录），并在
  `_OWN_DATA` 补 `班`（"下的班"里没有"下班"这个词）与 `打卡`。

### 记录：名字、计数、时长，没有值

`read_only_tools` 的记录新增 `tool_name`（§10.1 允许的字段，也是 38 号工单
`registry.py` docstring 早就承诺的"工具执行的记录是它的名字、时长与结果"），
`counts` 里是 `tools_registered` 与 `result_fields`（结果有几个字段，不是结果），
`decision` 是 `tool_outcome`。`tool_result` 与 `tool_answer` 只进 state（安装内自己的
Postgres 检查点），绝不进记录：§10.1 的 `tool_output` 必须留在安装内。

**`tool_name` 是注册表的键，不是模型给的字符串。** `outcome` 为 `unknown` 时节点写入
`tool=None` 与 `tool_arguments={}`，而不是它拿到的那个名字——"记录里的两个值读取点都是
常量"这句话必须继续成立，否则 `tool_name` 就成了一条可以写模型输出的 trace 字段。测试
`test_a_named_tool_that_is_not_registered_is_answered_without_a_result` 断言了
`state["tool"] is None`、`tool_name is None`，以及那个杜撰的名字在记录里完全不出现。

## 变更测试（mutation）

每条规则被打断一次，记录失败的测试名，然后还原；`git grep MUTATION-39 -- api/` 为空。

| 被破坏的规则 | 破坏方式 | 失败的测试 | 层数 |
|---|---|---|---|
| 白名单 | `lookup` 的 `except KeyError` 回退到 `registered()[0]` | `test_an_unregistered_name_is_refused_and_nothing_runs`、`test_a_named_tool_that_is_not_registered_is_answered_without_a_result`、`test_the_tool_names_a_tool_can_be_reached_by_are_the_classifiers_read_only_ones` | 3 |
| 本人可达范围 | `ALLOWED_PARAMETERS` 加入 `employee_id` | `test_no_tool_parameter_can_name_an_employee` | 1 |
| 本人身份 | `my_attendance` 把 `principal.employee_id` 换成 `principal.user_id` | `test_every_self_tool_reads_the_callers_own_record`、`test_my_attendance_states_the_figures_the_query_returned`、`test_a_failing_tool_states_no_figure_at_all`、`test_the_read_only_record_names_the_tool_and_carries_no_values` | 4 |
| 直属下属范围 | 候选集换成通讯录全员、并且去掉每个候选的内核判定 | `test_the_team_summary_reaches_the_callers_direct_reports_and_nobody_else`、`test_a_stranger_is_absent_and_indistinguishable_from_an_empty_team` | 2 |
| 通讯录投影 | 行改成直接从 `DirectoryEntry` 取值（绕过 `project_directory_row`） | `test_a_contact_is_the_directory_projection_and_nothing_beyond_it`、`test_an_email_outside_the_callers_departments_is_absent_not_denied` | 2 |
| 数字来自查询 | `_attendance` 在渲染时把分钟换算成小时（`round(minutes/60)`） | `test_every_self_tool_reads_the_callers_own_record`、`test_my_attendance_states_the_figures_the_query_returned`、`test_a_failing_tool_states_no_figure_at_all` | 3 |
| 失败不编造数值 | `FAILED` 的文案后面拼一个 `0` | `test_a_failing_tool_states_no_figure_at_all`、`test_invoke_turns_a_raise_into_a_stated_failure` | 2 |

**第四条变体暴露了一个真实的层次**：第一次只把候选集换成通讯录全员、保留每个候选的内核
判定时，**测试全绿**——因为内核的 `MANAGER_OF_SUBJECT` 子句会把非下属挡掉。也就是说
直属下属范围真正的守门人是内核的逐资源判定，快照集合只是候选。第二次连同内核判定一起
去掉，两条测试立刻红。这条记在这里，因为它说明"内核对每个 subject 再判一次"不是装饰。

## 验证

- `uvx ruff check app tests` 干净（`All checks passed!`）。
- 目标运行：
  `pytest tests/test_agent_readonly_tools.py tests/test_agent_graph.py tests/test_architecture_constraints.py`
  → **52 passed**（新文件 28 条 + 图 18 条 + 约束 A 6 条）。
- 回归面：`pytest tests/test_errors.py tests/test_permission_matrix.py
  tests/test_access_kernel.py tests/test_employee_visibility.py tests/test_answer.py
  tests/test_permission_snapshot.py` → **365 passed**（没有新增 HTTP 路由，所以
  `test_permission_matrix.py` 的计数一行都不用动）。
- 全量运行（本工单的 scratch 库与 Redis 2 号库）：

  ```
  docker compose exec -T -e TEST_DATABASE_NAME=eam_test_t39 -e REDIS_URL=redis://redis:6379/2 \
    api sh -lc 'cd /app && env -u DOCUMENT_PARSE_RUNNER_ENABLED python -m pytest -p no:warnings'
  → 1423 passed in 1007.07s (0:16:47)
  ```

  38 号工单的基线是 1396 passed；28 条新测试减去被删掉的那条占位测试，正好是 1423。
  全量跑过两次（改动冻结前、冻结后），两次都是 1423 passed。

  与 38 号工单同一条理由带 `env -u DOCUMENT_PARSE_RUNNER_ENABLED`：容器里显式导出的
  `DOCUMENT_PARSE_RUNNER_ENABLED=true` 正是 pydantic-settings 优先读取的东西，会让
  `tests/test_documents.py::test_the_parsing_loop_is_on_in_development_and_off_elsewhere`
  失败。那条与本工单无关，仍然留给后续处理。
- 清理：scratch 库 `eam_test_t39` 已 drop。

## 客户端需要的 `message_key`

`tool_answer` 里给的是 `{message_key, es, en, text}`，其中 `es`/`en` 是**已经填好占位符
的句子**，客户端直接渲染即可。15 个键：

`agent.tool.unavailable`、`agent.tool.unknown`、`agent.tool.not_permitted`、
`agent.tool.my_attendance`、`agent.tool.my_attendance.open`、
`agent.tool.my_attendance.empty`、`agent.tool.my_leave_balance`、
`agent.tool.my_leave_balance.none`、`agent.tool.my_timesheets`、
`agent.tool.my_timesheets.empty`、`agent.tool.colleague_contact`、
`agent.tool.colleague_contact.no_details`、`agent.tool.colleague_contact.not_found`、
`agent.tool.team_attendance`、`agent.tool.team_attendance.empty`。

带 `{}` 占位符的键（前面四条 `my_*` / `team_*` / `contact`）如果客户端要用自己的字典
重渲染，值要从 state 的 `tool_result` 里取；`tool_answer.text` 是服务端已经合成好的版本。
41 号工单读的 state 键是 `tool`、`tool_arguments`、`tool_result`、`tool_answer`、
`tool_outcome`。

## 仍然做不到 / 有意没做

- **仍然没有 HTTP 路由**：图只被测试调用，41 号工单接请求时再加。这也让
  `test_permission_matrix.py` 一个字都不用改。
- **没有真实 LLM 参与只读分支**：`read_only_tools` 一次模型都不调（测试断言
  `model.calls == []`），因为工具的名字今天来自 `selection.py` 的词法选择器，参数来自
  同一个地方。42 号工单把它换成模型函数调用时，只需要替换 `_tool_call` 那一个函数。
- **`search_policy` 没有注册**（见上）。
- **`get_my_timesheets(status)` 的 `status` 只窄化结果里的周列表，不窄化计数**：计数是
  为了回答"我现在的状态如何"，过滤计数会把"我有几周待审"答成"待审的里面有几周待审"。
  `data.filtered_by` 记录了这次是否过滤。
- **分类器仍然是词法的**，不认识人名也不理解迂回说法；它现在能接住工单自己举的三个例子
  （有测试），但 `¿A qué hora salió Ana ayer?` 这类没有第一人称标记的问句仍然会落到
  制度问答。真正挡住越权内容的是 35 号工单的检索前过滤与"注册表里没有读他人记录的工具"，
  不是这个分类器。
- **`PrefillForm`（40 号工单）与确认/拒绝（41 号工单）不在本工单**：本工单只把只读半边
  的注册表、执行路径与记录填满，草稿半边仍然返回点名 40 号工单的占位说明。
