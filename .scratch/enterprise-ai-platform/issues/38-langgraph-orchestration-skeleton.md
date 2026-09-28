# 38 — LangGraph 编排骨架

**What to build:** 助手有了一个明确的"大脑"：先判断用户想干什么（闲聊 / 问制度 / 查自己的数据 / 想办一件事 / 想干被禁止的事），再路由到对应的处理路径。中间状态持久化在数据库里，**服务重启后正在进行的对话不丢**。

**Blocked by:** 37 — 问答界面与引用回链

**Status:** done

- [x] 图包含意图分类节点，分类结果至少覆盖：制度问答、只读数据查询、待办操作、硬禁止请求、闲聊
- [x] 制度问答路由到已有的检索与生成路径并复用原有流式与引用行为
- [x] 硬禁止请求（查他人薪资、查他人考勤、要绩效或晋升建议、任何改库请求）在**代码层**被拒绝，且给出明确的双语说明，不转发给模型去"委婉处理"
- [x] 中间状态使用 Postgres 检查点持久化在独立 schema 中，**不使用 Redis 承载**（状态不可丢）
- [x] 有测试验证：在一个中断的流程中重启服务后仍能继续
- [x] 图的拓扑以代码表达且可被单测直接调用（不需要走 HTTP）
- [x] 每个节点的输入输出被记录，但**不含**对话正文与检索内容（为后续脱敏可观测性留出接口）

## 依赖可用性（预验证）

容器内为 Python 3.13.15，PyPI 可达。以下版本已用 `pip download --no-deps` 实测可取得，
所以本工单不需要为了"能不能装"而改设计：

| 包 | 实测版本 | 用途 |
|---|---|---|
| `langgraph` | 1.2.12 | 图与 `interrupt()` |
| `langgraph-checkpoint-postgres` | 3.1.2 | Postgres checkpointer（DESIGN §10.2） |
| `langchain-openai` | 1.6.6 | 供应商适配（DESIGN D34、§5.3 的降级链） |

**装配方式（这是本项目唯一的交付方式）：** 写进 `api/pyproject.toml` 的 `dependencies`
并更新 `api/uv.lock`，然后 `docker compose build api && docker compose up -d api`。
镜像在构建时跑 `uv sync --no-install-project`，`UV_PROJECT_ENVIRONMENT=/usr/local`，
所以依赖是镜像的一部分；容器里临时 `uv pip install --system ...` 只存在于容器层，
重建即丢，不能作为交付。

**重建会重启 api 容器。** 不要在并发测试运行时重建（会打断测试、留下孤儿事务并让下一次
运行以 `40P01` 死锁收场——本仓库已经因此损失过时间）。重建前确认没有别的 pytest 在跑。

**装配结果（本工单实测）：** `pyproject.toml` 里三个包都写成**精确版本**（`==`）而不是下限。
checkpointer 通过自己的 `MIGRATIONS` 列表建表，一个次版本升级就是一次 schema 变更，
应当被 review 而不是被吸收。`uv lock` 在容器内执行（uv 0.12.19），
`uv.lock` 新增 langgraph 1.2.12 / langgraph-checkpoint 4.2.0 /
langgraph-checkpoint-postgres 3.1.2 / langchain-openai 1.6.6 / langsmith 0.14.1 /
openai 3.20.0 / psycopg-pool 3.3.3 等；解析副作用把 `websockets` 从 17.1 降到 16.1.1
（`uvicorn[standard]` 要求 `>=10.4`，容器重建后 healthy）。

重建证据：

```
docker compose build api && docker compose up -d api
docker compose exec -T api python -c "import langgraph, langgraph.checkpoint.postgres, langchain_openai"
  → langgraph 1.2.12 / langgraph-checkpoint-postgres 3.1.2 / langchain-openai 1.6.6
```

`langchain-openai` **目前没有任何 import**：§5.3 的降级链和供应商适配是 42 号工单的事，
本工单的一次模型调用走 34 号工单已有的 `ChatModel` 接缝（`app/domain/answer/chat.py`）。
它进 `dependencies` 是因为依赖是镜像的一部分，晚一步加就要再重建一次运行中的容器。

## 实现

### 代码落位：本仓库与 DESIGN §1 的映射（**`ai/` 不是被忘了**）

`docs/architecture/codebase-design.md` §1 把 `ai/` 画成与 `api/`、`domain/`、`adapters/`
并列的顶层包。**本仓库早已偏离**：所有 Python 包都在 `api/app/` 下
（`app/domain`、`app/repositories`、`app/jobs`、`app/api`），README 与每张工单都如此记录。
本工单沿用该约定而不是半迁移，映射关系写在这里（也写在 `api/app/ai/__init__.py` 的模块
docstring 里，避免下一个读者以为 `ai/` 被漏掉）：

| DESIGN §1 | 本仓库 | 状态 |
|---|---|---|
| `ai/providers/` | `api/app/domain/answer/chat.py` | 34 号工单已建，本工单**复用** |
| `ai/rag/` | `api/app/domain/retrieval/` + `app/domain/document/` | 31-35 号工单已建，本工单**调用** |
| `ai/agents/` | `api/app/ai/agents/` | **本工单** |
| `ai/tools/` | `api/app/ai/tools/` | 空注册表；39-40 号工单填 |
| `ai/observability/` | （未创建） | 42 号工单 |

### 文件

- `api/app/ai/__init__.py` — 包说明与上面的映射；依赖方向 `ai → domain`。
- `api/app/ai/agents/intents.py` — `Intent`（五种）、`Rule`/`RULES`/`FORBIDDEN_RULES`、
  `classify()`/`classify_intent()`、`Classification`。
- `api/app/ai/agents/replies.py` — 四条硬禁止的双语拒答（文案从 `app/core/messages.py` 读，
  不复制第二份）、闲聊定式回复、两条工具分支的占位说明、`CONFIRMATION_PENDING` 中断载荷。
- `api/app/ai/agents/records.py` — `NodeRecord`、`ALLOWED_FIELDS`、`recorded()` 装饰器、
  `records_of()`/`node_names()`。
- `api/app/ai/agents/state.py` — `AgentState`（被检查点的）与 `AgentContext`
  （`Principal` + `AnswerService`，**绝不检查点**）。
- `api/app/ai/agents/nodes.py` — 七个节点。
- `api/app/ai/agents/graph.py` — `NODES`、`ROUTES`、`branch_for`/`branch_of`、
  `build_graph()`、`thread_config()`、`nodes_of()`。
- `api/app/ai/agents/checkpoint.py` — `CHECKPOINT_SCHEMA`、`checkpoint_dsn()`、
  `open_checkpointer()`。
- `api/app/ai/tools/registry.py` + `__init__.py` — 白名单注册表，**空**；
  `ToolKind` 只有 `READ_ONLY`/`DRAFT`，没有 `WRITE`（约束 B 结构层的可断言对象，见 §6）。
- `api/alembic/versions/20261008_1000_langgraph_checkpoints.py`（0026）— 建 `langgraph`
  schema、授权、注释。表由库的 `setup()` 建，理由写在迁移的 docstring 里。
- `api/app/core/messages.py` — 四条 `errors.forbidden_*` 双语文案（不是 `ErrorCode`：
  拒答是正确结果而不是失败，与 34 号工单的 `errors.knowledge_base_no_basis` 同一处理）。
- 测试：`api/tests/test_agent_graph.py`（19 条）、`api/tests/test_architecture_constraints.py`（6 条，
  约束 A）。`api/tests/support/platform.py` **刻意没有**把 `langgraph.*` 加进 `CLEANUP_TABLES`：
  四张表是库的 `setup()` 建的，命名它们会让**新建空库的第一次 wipe** 就撞上"表不存在"
  （这条是实测撞出来的），而隔离并不需要它们——检查点按 thread id 分片，本工单每个测试都自己
  生成 thread id。原因写在那个列表的注释里。

### 路由表（intent → 分支）

| 意图（checklist 原文） | `Intent` | 分支节点 | 说明 |
|---|---|---|---|
| 硬禁止请求 | `forbidden` | `refuse` | 代码层常量拒答，**不构造任何模型调用** |
| 制度问答 | `policy_question` | `answer_policy` | 调 34 号工单的 `AnswerService.stream()` |
| 只读数据查询 | `read_only_query` | `read_only_tools` | **具名占位**：39 号工单注册工具 |
| 待办操作 | `pending_action` | `draft_tools` → `await_confirmation` | **具名占位**：40 号工单出草稿；41 号工单做确认 |
| 闲聊 | `small_talk` | `small_talk` | 定式回复（双语常量），不调模型 |

分类是一条 `RULES` 有序表，先匹配先赢，**硬禁止永远第一**（`RULES = (*FORBIDDEN_RULES, ...)`）：
「¿Cuántos días de permiso por matrimonio tiene mi compañero?」既是"假期数据"又是"他人考勤"，
必须被拒答而不是被查询。规则是**词法**的（每组正则全部命中才算命中），docstring 里写明了
它做不到什么：它不认识人名，「la nómina de Marta」不会被这条规则拒绝——挡住那种问题的不是
分类器，而是 35 号工单的检索前权限过滤和"注册表里没有读他人薪资的工具"。

### 检查点：schema、saver、thread id

- **schema**：`langgraph`，由迁移 0026 创建并 `GRANT USAGE, CREATE ... TO eam_app`；
  四张表（`checkpoints`/`checkpoint_blobs`/`checkpoint_writes`/`checkpoint_migrations`）
  由 `AsyncPostgresSaver.setup()` 建，owner 是**请求角色** `eam_app`。
  把 DDL 抄进迁移会把库的一个版本冻结成永远跟不上库的 revision，而且是在本仓库里放第二份
  checkpoint 格式；`setup()` 是幂等的，`open_checkpointer()` 在它开的连接上调用它。
- **saver**：`langgraph-checkpoint-postgres` 的 `AsyncPostgresSaver`，
  连接串 `postgresql://eam_app:…/eam?options=-csearch_path%3Dlanggraph`。
  schema 由**连接**选择（库的 SQL 用的是不带限定的表名），所以调用者不可能写成 `public.checkpoints`。
- **thread id**：`graph.thread_config(thread_id)` = `{"configurable": {"thread_id": …}}`；
  **一个 thread 就是一次会话**（41 号工单把会话 id 传进来）。本工单只把这个映射写清楚，
  让重启测试能用它。
- **dev 库已就位**：`open_checkpointer(get_settings())` 对运行中的 dev 库跑过一次
  （表 owner `eam_app`），所以 `docker compose up` 的库里有这套表；测试库由 conftest
  `upgrade head` 建 schema、由测试自己 `setup()` 建表，走的正是同一条路径。
- **Redis 不参与**：测试直接扫真实 Redis 的 keyspace，断言没有任何 key 沾得上 checkpoint
  或 thread id（§10.2 的"可丢的放 Redis，不可丢的放 Postgres"）。

### 重启测试（本工单的重点）

`test_an_interrupted_run_resumes_on_a_graph_rebuilt_against_the_same_database` 与
`test_resuming_does_not_re_run_the_nodes_before_the_pause` 做的是同一件事的两面：

1. 用 `AsyncPostgresSaver` #1 编译图 A，跑一个"待办操作"问题：`classify` → `draft_tools` →
   `await_confirmation` 里 `interrupt()` 暂停，`snapshot.next == ("await_confirmation",)`。
2. **退出 #1 的 context manager**（连接关闭、saver 对象丢弃）——"进程重启"就是这一步。
3. 用**新的连接**开 saver #2，`build_graph()` 编译出**另一个图对象**，配一个**新的上下文**，
   只给 `Command(resume={"action": "confirm"})` + 同一个 thread id，**不重发问题**。
4. 断言：状态里仍有第一轮的问题与 `pending_action`；`records` 为
   `["classify", "draft_tools", "await_confirmation"]`（`classify` 只出现一次，说明暂停前的
   工作没有被重做）；`confirmation == {"received": True, "value_type": "dict", "interpreted": False}`
   ——记录的是答案的**类型**而不是内容；`snapshot.next == ()`。

让它**能失败**的两处：`test_the_pause_is_written_to_the_langgraph_schema_and_not_to_redis`
用普通 SQL（libpq，绕开写它的库）读 `langgraph.checkpoints` 里该 thread 的行；
`test_a_thread_with_no_checkpoint_cannot_be_resumed` 是同一个代码路径的负向对照。
把 `open_checkpointer` 换成 `InMemorySaver` 后这三条都红（见下面的变更测试）。

### 节点记录（42 号工单的脱敏接口）

`recorded(node_name, reads=…, counts=…, decision_key=…)` 只从四样东西构造记录：
声明过的输入**键名**、返回值里的输出**键名**、声明过的 `(标签, 状态键)` 计数、以及一个
**枚举值**决策。没有一条代码路径把值拷进记录，所以"记录里没有正文"是结构事实而不是纪律。
字段集合恰好 `ALLOWED_FIELDS`，是 §10.1 `ALLOWED_TRACE_FIELDS` 的子集。
失败记录（`error_type`）只进结构化日志并重新抛出：抛出的节点不会返回，状态更新不会被
检查点写入，日志是失败记录唯一能存在的地方；`str(error)` 刻意不抄进去（答案路径的异常可能
带着提示词），调用方会连同 traceback 一起记。
`GraphBubbleUp`（`interrupt()` 用的就是它）**不算失败**，否则每次暂停都会带一个假 `error_type`。

### 约束 A 的测试

`api/tests/test_architecture_constraints.py` 用 `ast` 遍历 `api/app/domain/**` 的每个模块，
把三种 import 形式（`import app.ai.…`、`from app.ai.… import …`、`from app import ai`）与
**相对 import**（`from ..ai.…`，按文件所属包解析——`__init__.py` 的包是它自己）都解析成
模块名，命中 `app.ai` 即失败。两条防线保证它不会"因为什么都没找到"而变绿：
`test_the_walk_is_not_empty`（模块数下限 40、且必须找到 `app.domain.answer.driver` /
`app.domain.retrieval.filtering` / `app.domain.access.kernel` 三个锚点）与
`test_the_walker_catches_a_module_that_imports_ai`（五种写法的正向对照）；
另有一条负向对照，确保普通 domain import 不会被误报。

**约束 B 与约束 C 不在这里**：§8 的表写着 B 的结构层是 40 号工单（运行时只读上下文 41、
只读数据库角色随后），C 落在 35 号工单。本工单只把 `ToolKind` 做成没有 `WRITE` 成员，
给 40 号工单的那个断言留出一个具体的对象。

## 变更测试（mutation）

每条规则都被打断过一次，记录失败的测试名，然后还原；`git diff` 与全仓 grep 确认没有留下
`MUTATION-38` 标记。

| 被破坏的规则 | 破坏方式 | 失败的测试 | 层数 |
|---|---|---|---|
| 硬禁止在代码层拒绝 | `ROUTES[FORBIDDEN] = "answer_policy"`（转给模型） | `test_the_routing_table_covers_the_five_intents_and_names_real_nodes`、`test_each_prohibited_ask_is_refused_without_calling_the_model_or_the_answer_path`、`test_a_prohibited_ask_never_creates_a_conversation`、`test_the_refusal_record_carries_the_decision_and_none_of_the_copy` | 4 |
| 路由决策 | `ROUTES[READ_ONLY_QUERY] = "small_talk"` | 同上第一条 + `test_the_classifier_decides_each_of_the_five_outcomes` + `test_the_read_only_branch_says_no_tool_is_registered` | 3 |
| 记录不含正文 | 在每条记录里塞 `counts["excerpt"] = question` | `test_records_carry_names_counts_and_timings_and_never_content`、`test_the_refusal_record_carries_the_decision_and_none_of_the_copy`、`test_the_read_only_branch_says_no_tool_is_registered`、`test_resuming_does_not_re_run_the_nodes_before_the_pause` | 4 |
| 检查点是数据库的 | `open_checkpointer` 改成 yield `InMemorySaver()` | `test_the_pause_is_written_to_the_langgraph_schema_and_not_to_redis`（`assert 0 >= 1`）、`test_an_interrupted_run_resumes_on_a_graph_rebuilt_against_the_same_database`（`KeyError: 'question'`）、`test_resuming_does_not_re_run_the_nodes_before_the_pause`（同） | 3 |
| 约束 A | 在 `app/domain/answer/driver.py` 加 `from app.ai.agents.records import recorded` | `test_no_domain_module_imports_the_ai_package`（报出 `{'app.domain.answer.driver': {'app.ai.agents.records', …}}`） | 1 |

第二次变更（路由）暴露了一条**假断言**：`test_the_classifier_decides_each_of_the_five_outcomes`
原来写的是 `branch_of(...) == ROUTES[intent]`，即拿表跟自己对答案，表怎么改都过。现在它对照
测试里独立写出的 `EXPECTED_BRANCHES`，那句话才是报告与 §6.1 里的路由表。

## 验证

- `uvx ruff check app tests` 干净（`All checks passed!`）。
- 目标运行：`pytest tests/test_agent_graph.py tests/test_architecture_constraints.py` → **25 passed**。
- 全量运行（本工单的 scratch 库与 Redis）：

  ```
  docker compose exec -T -e TEST_DATABASE_NAME=eam_test_t38 -e REDIS_URL=redis://redis:6379/11 \
    api sh -lc 'cd /app && env -u DOCUMENT_PARSE_RUNNER_ENABLED python -m pytest -p no:warnings'
  → 1396 passed in 1250.57s (0:20:50)
  ```

- **同一条命令去掉 `env -u` 就是 `1395 passed, 1 failed`**，失败的是
  `tests/test_documents.py::test_the_parsing_loop_is_on_in_development_and_off_elsewhere`，
  与智能体无关。机制：`docker-compose.yml`（提交 586481d，31 号工单）给 api 容器导出了
  `DOCUMENT_PARSE_RUNNER_ENABLED: ${DOCUMENT_PARSE_RUNNER_ENABLED:-true}`，
  而该测试断言的是 `Settings(app_env="production").parses_documents_in_process is False`
  ——显式导出的环境变量正是 pydantic-settings 会优先读取的东西。在容器里
  `env -u DOCUMENT_PARSE_RUNNER_ENABLED python -m pytest tests/test_documents.py -k parsing_loop`
  单条通过，证明成因是环境变量而非代码。本工单没有碰 `docker-compose.yml`、
  `app/config.py` 或那个测试文件；但**重建 api 容器（本工单要求的步骤）会用当前 compose
  文件重建它**，所以这条失败现在会在容器内跑全量时出现。记录在此，留给后续处理，
  没有在本工单里"顺手改绿"。
- 清理：scratch 库 `eam_test_t38` 已 drop。dev 库的 `langgraph` schema 与四张 checkpoint 表
  保留——那是这个功能的存储而不是脚手架，且迁移的 docstring 已经写明表由库的 `setup()` 建。
- 一条踩过的坑记在这里：宿主上 `job kill` 一个 `docker compose exec … pytest` **不会**杀掉
  容器里的 pytest 进程，它会继续跑并与下一次运行抢 `TRUNCATE` 的表锁，表现为
  `DeadlockDetected`。本次是进容器按 `/proc` 找到 pid 后 `kill -9`、再 `pg_terminate_backend`
  + drop 库解决的。清单里"不要在并发测试运行时重建"的同一条理由，对"杀掉一个测试运行"也成立。

## 仍然做不到 / 有意没做

- **没有 HTTP 路由**，这是刻意的：只有测试调用的图不需要路由，加一条就会多一行权限矩阵
  （`test_permission_matrix.py` 的 `checked == N`）却没有人用。41 号工单接请求时再加。
- **工具、`PrefillForm`、确认/拒绝处理都没做**（39-41 号工单）：两条工具分支返回的是一句
  **点名工单号**的说明，`await_confirmation` 只记录"收到了一个答案、类型是什么"，
  **不解释**它——那是 41 号工单的 confirm/reject 分支，猜测一个半成品只会多一个要拆的东西。
- **§6.2 的第五条硬禁止没有实现**：DESIGN §6.2 列了五项，checklist 的括号里是四项
  （他人薪资、他人考勤、绩效/晋升建议、改库）。上传/删除文档（Q39）**故意没有**写成第二条规则，
  记在这里当作缺口，而不是留一个做了一半的第五类。
- **分类是词法的**，不认识人名，也不理解迂回说法；真正的分类器属于 42 号工单的模型接入。
  但拒答不依赖它完美：越权内容在检索前就被权限过滤挡住（35 号工单），注册表里也没有写工具。
- **不是真实 LLM 的端到端验证**：`CHAT_PROVIDER=fake` 时答案是 `StreamedChatModel` 写的，
  与 34 号工单的结论一致——有证据的是管线（路由、拒答、复用、流式转发、检查点、记录），
  不是答案质量。
