# 42 — 可观测性脱敏与模型降级

**What to build:** 助手的运行情况可以在追踪平台上看到，便于排查问题——但**员工对话内容、提示词、检索到的文档片段一律不出境内**，只发送工具名、节点流转、耗时、token 数与错误类型。同时，主模型不可用时自动切到备用供应商，并把实际使用的供应商记下来。

**Blocked by:** 41 — 人审确认点与 agent_actions 审计

**Status:** done

- [x] 存在唯一的追踪上报出口，且带有一个**白名单**过滤器，只有被允许的字段能外发
      — `test_only_the_observability_module_names_a_tracing_client`（结构性：`ast` 遍历 `app/**` 的每个模块，
      除 `app/ai/observability/` 外任何地方出现 `langsmith` / `langfuse` / `opentelemetry` /
      `langchain.callbacks` 即失败，附正对照 `test_the_walker_would_catch_a_second_export_point`）、
      `test_the_export_point_delivers_what_was_projected_and_nothing_else`（出口是
      `TraceExporter.export` 一个函数；sink 收到的是它投影出来的那个 payload）、
      `test_the_whitelist_is_the_one_design_10_1_names`（白名单逐字段字面量断言）。
- [x] 被禁止外发的字段明确列出：对话正文、消息列表、提示词、模型补全、查询语句、文档片段、引用内容、工具入参、工具出参
      — `test_every_forbidden_field_design_10_1_names_is_forbidden_here`（§10.1 的九个名字逐个断言，
      外加本仓库的实际拼法 `tool_arguments` / `tool_result`；并断言两张表互不相交）。
- [x] 过滤器有单元测试，断言序列化结果中**不出现**任何禁止字段与它们的值（包括嵌套结构）
      — `test_forbidden_material_nested_at_every_depth_does_not_survive`（五个深度：state 顶层、
      record 的 `counts` 里、`input_keys` 列表里、`output_keys` 列表里、`model_used` 这个**白名单字段**里；
      先断言输入真的带着这五段文字，再断言 `json.dumps(payload)` 里一个都没有）、
      `test_an_entire_record_cannot_be_smuggled_under_an_undocumented_key`、
      `test_the_serialised_payload_names_no_forbidden_field`、
      `test_a_forbidden_field_on_a_record_is_an_error_rather_than_a_redaction`、
      `test_a_state_full_of_conversation_material_still_exports_a_clean_payload`。
- [x] 模型供应商抽象为统一接口，配置中定义调用顺序（OpenAI 主，DeepSeek / Claude 备）
      — `test_the_chain_is_configuration_and_the_default_is_openai_first`（`CHAT_PROVIDERS=openai,deepseek`；
      **未设置时链长为一**，即环境推导出的那个 provider——现有部署的行为一字不变）、
      `test_the_route_assembles_the_chain_from_configuration`（`api/v1/answer.py::_service` 是设置变成
      adapter 的唯一地点）、`test_the_claude_adapter_is_a_second_dialect_on_the_wire`（Anthropic **真的实现了**，
      不是"配置了但没做"）。
- [x] 降级**只在技术失败时发生**（超时、限流、服务端错误、连接失败）；不因"回答质量下降"而自动切换
      — `test_the_chain_moves_on_only_for_a_technical_failure`（四类技术失败各切换一次；
      **主供应商返回一个又短又不带引用的"成功"回答时，备用供应商调用次数为 0**——这是本条的 mutation 目标）、
      `test_a_failure_kind_that_is_not_technical_is_refused_by_the_chain`（
      `TECHNICAL_FAILURES` 里没有任何"质量/太短/空"之类的词，且分类不合格的失败不会触发切换）、
      `test_an_unclassified_failure_kind_cannot_be_constructed`、
      `test_a_stream_that_fails_halfway_is_not_completed_by_another_provider`（一旦吐出过增量就不再切换，
      否则会把两家供应商的半句话拼成一个没人写过的答案）。
- [x] 每次请求记录实际使用的供应商与模型，可在会话记录中查到
      — `test_a_fallback_records_the_provider_that_actually_answered`（主供应商超时、备用的回答：
      `rag_messages.provider_used`/`model_used`、流的 `done` 帧、回读三处一致都是备用的）、
      `test_every_configured_provider_is_present_in_the_chain_even_without_a_key`（没有 key 的 provider
      是"在链里并失败"，不是"不在链里"——否则记录里看不出为什么没试它）。
- [x] 降级事件写入结构化日志与审计（不含对话正文）
      — `test_a_fallback_writes_a_structured_log_and_an_audit_entry_without_text`（
      `answer.provider_fallback` 一条审计行为 `system` 发起，字段只有 provider/model 名、
      `failures` 的**技术类型**、尝试次数与 message id；日志行同字段；断言问题原文不在其中；
      同一事务里 `conversation.asked` 也带上了 `attempts`）。
- [x] 嵌入模型**不参与**自动降级（维度不同不可混用）；若嵌入服务不可用，明确报错并停止入库，不写入维度不符的向量
      — `test_the_embedding_rule_is_pinned_and_has_no_fallback`（`build_embedder` 返回**一个** adapter，
      `app/domain/document/embeddings.py` 里没有 `EMBED_CHAIN`、没有任何 `fallback` 名字；仓库里没有
      `FallbackEmbedder`；没有 key 时 `EmbeddingUnavailable` 而不是退化成另一个维度的哈希向量）、
      `test_the_pipeline_leaves_a_chunk_unembedded_rather_than_writing_a_wrong_vector`
      （维度常量仍是 1536，模型仍是固定的；`WHERE embedding IS NULL` 是待办清单而不是补一个错向量）。
      **本工单只是把这条规则钉住，没有加第二个嵌入 provider。**
- [x] 有测试：模拟主供应商故障，验证自动切换且记录正确；模拟全部供应商故障，验证给出明确错误而非静默空回答
      — `test_the_openai_adapter_falls_over_to_a_second_provider_on_the_wire`（真实 HTTP：主 provider
      的桩服务器返回 500、备用的桩服务器按 SSE 流出答案；两次请求的路径、Bearer、模型名、messages
      逐项断言）、`test_every_provider_failing_is_one_explicit_error`（错误里按顺序点名每个 provider 与
      它的技术类型，**一个增量都没吐出**）、`test_every_provider_failing_is_an_explicit_error_and_stores_no_answer`
      （端到端：`ERR_ANS_001` + `retryable`，行是 `failed`、`content` 为空、`provider_used` 为 `NULL`，
      且没有产生 fallback 审计行）。

## 白名单的形状，以及唯一出口在哪里

`app/ai/observability/whitelist.py` 是**允许出去的东西**，`app/ai/observability/exporter.py` 是
**从哪里出去**。

```python
ALLOWED_TRACE_FIELDS = frozenset({
    "node_name", "tool_name", "decision", "input_keys", "output_keys", "counts",
    "latency_ms", "token_in", "token_out", "is_refusal", "error_type",
    "provider_used", "model_used", "retrieval_hit_count",
})          # 就是 DESIGN §10.1 那份，一个不多

FORBIDDEN = frozenset({
    "content", "messages", "prompt", "completion", "query",
    "chunk_text", "citations", "tool_input", "tool_output",
    "tool_arguments", "tool_result",   # 本仓库对"工具入参/出参"的实际拼法
})
```

**为什么是白名单。** 黑名单会被"下一个新增字段"打败：`tool_result` 今天在名单上，`tool_payload`
不在，而泄漏是无声的（没有任何断言会红）。白名单把失败方向反过来——**没被列出的字段不出境**，
新增一个运维字段的代价是这里一行加上钉住它的那条测试。

**`FORBIDDEN` 仍然存在，但它是警报器不是过滤器。** 名字里带 `citations` 的 `counts` 标签
（`{"citations": 2}`，引用的**数量**）是合法的，所以序列化扫描用的是一条收窄过的标记表
（`_FORBIDDEN_MARKERS`），只扫那些白名单字段与装饰器标签都不会使用的名字。这一点是实测出来的：
第一版把 §10.1 九个名字整个拿去扫描，`test_a_record_is_projected_down_to_the_whitelist` 的
`counts={"citations": 2}` 直接被自己的守卫拒掉——一个用不了的守卫等于一个会被删掉的守卫。

**`TraceExporter.export(state, run_id=…)` 是唯一的出口**，顺序就是接口的一部分：

1. `project_record` 把每条 record **从零重建**成只含白名单字段的 dict——不是"复制后删掉几个 key"，
   所以形状不对的值（`counts` 里塞了一段文档正文）也进不来，因为它的值不通过该字段的谓词；
2. `violations_in(payload.as_dict())` 在**序列化后的文本**上再查一遍禁止字段名，非空则 raise
   `TraceExportRefused`；
3. 只有前两步都过了才 `sink.send(payload)`。

`TraceSink` 是一个 Protocol（`send(payload)`）。`sink=None` 时这仍然是一次完整的过滤，只是不投递——
这正是"没有配置后端"的部署和测试套件需要的行为（验证标准不允许测试去连一个真实追踪后端）。
§10.1 留的选项 (C)（自托管 Langfuse）因此是**换一个 sink**，而不是重写 exporter。

**每个字段还带一条值谓词**（`TRACE_FIELDS`）：`node_name`/`tool_name`/`input_keys`/`output_keys`
是标识符，`decision` 与 `tool_outcome` 是枚举目录里的键（有测试断言与 `Intent`、`ToolOutcome` 一致），
`error_type` 是异常类名，`counts` 是"标识符 → 有界非负整数"且**不接受 `bool`**
（`isinstance(True, int)` 为真，这是 `records._size` 已经记过的同一个坑），token/耗时/命中数有上限。
这几个都是**代码自己选的值**，所以白名单字段不可能变成模型输出的落脚点——这正是 39 号工单
"名字没注册时存 `tool=None` 而不是模型编出来的那个字符串"的同一条道理。

## 嵌套泄漏是怎么被挡住的，测试又怎么抓到它

投影是**按字段名重建 dict**：一个值不在 `TRACE_FIELDS` 里就没有任何代码路径被复制过去。
于是"藏在更深处"的三种形态都出不去：

| 藏法 | 为什么出不去 |
|---|---|
| `state["answer"]["content"]`（state 顶层） | `build_payload` 只读 `state["records"]` 与 `is_refusal`，从不读 `answer` |
| `record["counts"]["passage"] = "<文档正文>"` | `counts` 的谓词要求值是整数，`str` 过不了，整个 `counts` 被丢弃 |
| `record["input_keys"] = ["question", "<提示词>"]` | 列表每个元素都必须是标识符 |
| `record["model_used"] = "<文档正文>"` | 这个**字段名是白名单的**，只有它的**值**谓词（`_LABEL`）挡住 |
| 整条 record 塞在 `record["tool_result"]["rows"][…]["chunk_text"]` | `tool_result` 是禁止键 → `reject_forbidden` 直接 raise；即便绕过它，`tool_result` 也不在白名单里 |

测试（`test_forbidden_material_nested_at_every_depth_does_not_survive`）把这五种同时放进一个 state，
**先断言输入里确实有这五段文字**（否则一条"夹具忘了夹带"的测试会绿得毫无意义），
然后断言 `payload.serialised()` 里一个都没有，并且 `violations_in(...) == frozenset()`。
断言的落点是**序列化后的字符串**而不是 dict 的顶层键——只走顶层键的测试在"文档正文躺在
`counts` 里"时照样全绿，而那正是这张票存在的理由。

还有一条**方向相反**的测试：`test_a_state_full_of_conversation_material_still_exports_a_clean_payload`。
state 里本来就该有对话文本（`AgentState` 带着 `question`、`tool_result`、`prefill_form`——
那是本安装自己的 Postgres，`state.py` 里已经论证过），所以警报器只装在 **record** 上：
state 脏而 payload 干净是**通过**；record 上出现禁止键才是 bug，而那是 `ForbiddenTraceField`。
一个"凡是 state 里有问题就拒绝"的过滤器是没人跑得起来的过滤器。

## 供应商链的配置，以及哪些 adapter 是真的

```python
CHAT_PROVIDERS=openai,deepseek,anthropic,ollama   # 逗号分隔，第一个是主
CHAT_MODEL=gpt-4o                                 # 默认 provider 的模型
DEEPSEEK_CHAT_MODEL=deepseek-chat
ANTHROPIC_CHAT_MODEL=claude-3-5-sonnet-latest
OPENAI_API_KEY= / DEEPSEEK_API_KEY= / ANTHROPIC_API_KEY=
OPENAI_BASE_URL= / DEEPSEEK_BASE_URL= / ANTHROPIC_BASE_URL=
```

**未设置 `CHAT_PROVIDERS` 时链长为一**（`chat_provider_names` = 环境推导的单个 provider）。
这是刻意的：一个没人配置的链不能在运维背后长出第二个 provider，`provider_used` 才不会变成惊吓；
而 `docker compose up` 与所有现有部署的行为因此一字不变。`test_the_chain_is_configuration_and_the_default_is_openai_first`
钉住这一点。

**可接受的名字 = `PROVIDERS` ∪ 本环境推导出来的那一个**（`parse_provider_chain` 的 `fallback`）。
配置里的陌生名字仍然被拒绝并列出可用适配器，但**推导出来的名字永远可接受**——
`fake` 是最能说明问题的一个：它没有 `PROVIDERS` 条目（没有端点、没有模型、没有 key），
却是开发与测试环境自己选定的适配器，拒掉它等于"一个部署不能点名自己正在跑的适配器"。
（这条规则是被外部复核抓出来的，见下面那一节。）

| provider | 适配器 | 真实？ | 说明 |
|---|---|---|---|
| `fake` | `StreamedChatModel` | 是（开发/测试） | 只引用检索到的段落，D20 要求的行为；测试环境默认就是它 |
| `openai` | `OpenAIChatModel` | **是** | `POST {base}/chat/completions`，`stream: true`，读 SSE 的 `choices[0].delta.content` |
| `deepseek` | `OpenAIChatModel`（第二份配置） | **是** | DeepSeek 说的是 OpenAI 方言，所以是**一个实现两份配置**，`provider` 字段区分是谁答的 |
| `anthropic` | `AnthropicChatModel` | **是** | 另一种方言，所以是另一个适配器：`POST {base}/v1/messages`、`x-api-key`、顶层 `system`、`content_block_delta` |
| `ollama` | `KeylessChatModel` | 是（本地、无 key） | OpenAI 方言但**不发 `Authorization` 头**；这是让 `docker compose up` 无需任何 key 就能真的跑链的那一条 |

**为什么不用 `langchain-openai`（它装着但一直没用）。** 工单允许两种做法，所以这里记录选择：
沿用 34 号工单的 urllib 适配器并给它加"每个 provider 一份 base URL 和模型"，让仓库里只有**一条**
HTTP 路径（也就是 `ai/__init__.py` 早已把 `ai/providers/` 映射到的那一个），
而不是在一条已经用真实 SSE 帧测过的适配器旁边再引入一套更大的请求/响应机制
（它自己的重试、自己的流式解码、自己的 callback 层）。DeepSeek 与 OpenAI 同方言，
所以一个适配器两份配置是对这件事的诚实描述；Anthropic 不同方言，所以有它自己的适配器——
"配置了但没实现"是本工单明确不接受的（工单文件没有这句话）。

**`UnavailableChatModel`：没有 key 的 provider 是"在链里并失败"。** 构造期跳过它会让记录变成
"deepseek 答的"而对"为什么没试 openai"无话可说——那正是 `provider_used` 存在的意义所在。
它报的是 `authentication`，是技术失败，链因此继续往下走，运维从审计行里看到该设哪个变量。
`ollama` 例外：catalogue 里标了 `anonymous=True`，它**本来就不需要 key**。

## 技术失败与"回答很差"是怎么在代码里分开的

```python
TECHNICAL_FAILURES = ("timeout", "rate_limit", "server_error", "connection_error",
                      "authentication", "permission", "not_found", "protocol_error")
```

这个元组里**没有任何**表示"回答质量"的词，而链只读 `AnswerModelUnavailable.failure` 这一个字段来
决定是否切换——没有分支去看文本。三层都钉住了：

1. `AnswerModelUnavailable.__init__` 拒绝不在 `TECHNICAL_FAILURES` 里的 kind（`ValueError`），
   所以适配器**无法**把"回答太差"塞进失败类型；
2. `FallbackChatModel._refuse_unknown_kind` 在链里再查一次，第三方或测试适配器绕过构造函数也会被拒；
3. `test_the_chain_moves_on_only_for_a_technical_failure` 的后半段是行为证据：主供应商
   **成功返回**一个"我不到"式的烂答案，备用供应商的调用次数必须是 0。

HTTP 状态到 kind 的映射是**一处**（`failure_kind_for`），两个方言共用：
429→`rate_limit`、5xx→`server_error`、504/408→`timeout`、401/403→`authentication`/`permission`、
404→`not_found`，其余→`protocol_error`。socket 一侧另有一个 `failure_kind_for_os_error`
区分"时钟到了"和"连不上"（`urllib` 把 socket 超时包在 `URLError(reason=TimeoutError)` 里，
只看外层类型会把每个超时都叫成连接失败）。

一旦某个 provider **已经吐出过增量**就不再切换（`test_a_stream_that_fails_halfway_is_not_completed_by_another_provider`）：
前半句已经发给客户端了，要不回来；把第二家的文本接在第一家的半句话后面会造出一个没人写过的答案。

## 日志与审计里到底写了什么

`chat_provider_fallback`（结构化日志，WARNING）：

```json
{"event": "chat_provider_fallback", "message_id": "<uuid>", "primary": "openai",
 "providers_tried": ["openai"], "failures": ["rate_limit"],
 "provider_used": "deepseek", "model_used": "deepseek-chat", "attempts": 2}
```

`answer.provider_fallback`（`audit_log` 新动作，**追加式表**，D24 的规则照旧）：

| 列 | 内容 |
|---|---|
| `entity_type` / `entity_id` | `rag_conversation` / 会话 id——按会话过滤就能看到"这个线程发生了什么" |
| `before` | `{"provider": "openai", "model": "gpt-4o"}`（第一个失败者） |
| `after` | `message_id`、`provider_used`、`model_used`、`failures`（技术类型数组）、`providers_tried`、`attempts` |
| `reason` | "主供应商技术性失败（rate_limit），配置的链继续往下（§5.3/D17）；此处不记录对话文本" |
| `initiated_by` | `system`（没有人在动作；记到提问者头上是记录在对自己说谎——与 `document.parsed` 同一条理由） |

**两者都不含对话正文**，而且不是靠"记得不要写"：字段本身就是 provider 名、模型名、一个运行内计数
和 `TECHNICAL_FAILURES` 的成员。测试除了逐字段断言，还把整条记录 `json.dumps` 后查问题原文。
`conversation.asked` 那一行同时多了一个 `attempts` 计数——"这次问了几家"是 4 年证据链要能回答的事，
而单看行本身答不出来。

## 全部失败时的行为

`FallbackChatModel` 在最后一个 provider 也失败后 raise，且是**最后一个 provider 自己的异常**，
前面加上链的句子：

```
all 2 configured chat providers failed — openai(timeout), deepseek(rate_limit).
the last provider tried was deepseek
```

它一路走到 34 号工单就有的 `ERR_ANS_001`：503、`expose_detail=False`、`retryable: true`，
行是 `failed`、`content` 为空、`provider_used`/`model_used` 为 `NULL`、`token_out = 0`。
所以「明确错误而非静默空回答」用的是**既有路径**而不是第二条路径，客户端拿到的还是那个重试入口。
没有产生 fallback 审计行——没有任何东西答过，就没有降级可记，只有失败，而 `conversation.asked`
已经用 `error_key` 把它记下来了。

## 验证

- `uvx ruff check app tests` → **All checks passed!**
- 新增/改动的目标运行（scratch 库 `eam_test_t42`、Redis 10 号库）：
  - `pytest tests/test_trace_redaction.py` → **16 passed**
  - `pytest tests/test_provider_chain.py` → **15 passed**
  - `pytest tests/test_answer.py -k "provider or fallback or every_provider"` → **3 passed**
  - `pytest tests/test_trace_redaction.py tests/test_provider_chain.py tests/test_answer.py
    tests/test_agent_graph.py tests/test_permission_matrix.py` → **全绿**（含权限矩阵的 83 条 HTTP 字面量——
    本工单没有新增路由或 action，所以那个数字没动）
- **全量运行**：
  `docker compose exec -T -e TEST_DATABASE_NAME=eam_test_t42 -e REDIS_URL=redis://redis:6379/10 api python -m pytest -p no:warnings`
  → **1521 passed**（0:21:27，`exit=0`）。基线是本工单开工时的 **1486**（41 号工单冻结时的数字），
  差 35 = 本工单新增的 16（`test_trace_redaction.py`）+ 15（`test_provider_chain.py`）
  + 4（`test_answer.py` 里的链/降级四条）。**这是本工单树冻结时的数字。**
- `cd web && npx tsc --noEmit`：**没有跑**，本工单没有碰 `web/` 的任何一行（这张票没有界面）。

## 一次被外部复核抓到的真实缺陷（记在这里，因为它是本工单唯一一个）

全量跑之前，**父代理**在同一棵树上跑了
`tests/test_trace_redaction.py tests/test_provider_chain.py tests/test_answer.py tests/test_agent_graph.py`
→ **1 failed, 84 passed**：

```
test_the_route_assembles_the_chain_from_configuration
    AnswerModelUnavailable: CHAT_PROVIDERS names ['fake'], which this deployment has no
    adapter for; the adapters are ['anthropic', 'deepseek', 'ollama', 'openai']
```

**诊断**：`parse_provider_chain` 把"配置里的名字"一律拿去 `PROVIDERS` 里查，而 `PROVIDERS` 是
**部署端点**的目录——`fake` 刻意不在里面（它没有端点、没有模型、没有 key，是 `chat_provider_name`
为开发/测试推导出来的进程内适配器）。可是**这个环境推导出来的名字就是它自己**，所以把它拒掉等于
"一个部署不能点名自己正在跑的适配器"。测试里那句 `monkeypatch.setattr(settings, "chat_providers", "fake")`
是有意为之（`_provider_models` / `_provider_base_urls` / `_provider_keys` 对 `fake` 返回 `None`
而不是抛异常，路由那一半本来就容得下目录外的名字），所以这是**解析器少了一条规则**，不是测试写错了。

**修法**（`chat.parse_provider_chain`）：可接受集合 = 目录 **∪ {fallback}**。

```python
allowed = set(PROVIDERS) | {fallback}
unknown = [name for name in names if name not in allowed]
```

docstring 承诺的性质一条没丢：**配置里的陌生名字仍然被拒绝**（`mistral` 依旧报错并列出可用的适配器），
被放行的只有"这个环境自己推导出来的那一个"。回归测试加在
`test_the_chain_is_configuration_and_the_default_is_openai_first`：`parse_provider_chain("fake", fallback="fake")
== ("fake",)`、`("fake,openai", fallback="fake") == ("fake", "openai")`、
`assert "fake" not in PROVIDERS`，以及一条"生产环境（fallback 是 `openai`）写 `CHAT_PROVIDERS=fake`
仍然被拒"——因为放行的是**推导出来的名字**，不是 `fake` 这个字符串无条件成立。
修完重跑该模块与全量：见上。

**为什么它会漏到我这里**：我先跑的目标模块里包含 `test_provider_chain.py` 与
`tests/test_answer.py -k "provider or fallback or every_provider"`，但那条测试**不是**被 `-k`
选中的三个之一（它名字里没有 provider/fallback/every_provider 的组合命中词），
而我第一次不带 `-k` 跑整个 `test_answer.py` 时那棵树还停在"路由用 `build_chat_model` 直接构造"的版本上。
教训与 41 号工单记录的是同一条：**筛选用 `-k` 挑出来的绿灯不等于模块的绿灯**，
冷启动的那一次全量必须跑在最终代码上——本工单的最后一次全量（1521 passed）就是。

## 变更测试（mutation）

每条规则被打断一次，记录**失败的那条测试**，然后还原。跑法是一个临时脚本
（`api/tests/tools/mutate_t42.py`，**已删除**）把源码改动写进去、跑**一个** node id、
`finally` 还原——41 号工单记录的"多个 node id 用空格拼会被 pytest 当成额外参数、
于是'没跑'被记成'抓住了'"那个坑，这里用"每次只给一个 node id"避免。
结果：**9/9 全部被抓住**。

| 被破坏的规则 | 破坏方式 | 失败的测试 |
|---|---|---|
| 白名单（放一个禁止字段回进来） | `project_record` 改成 `{k: v for k, v in record.items() if k not in FORBIDDEN}`（去掉值谓词） | `test_forbidden_material_nested_at_every_depth_does_not_survive` |
| 白名单（整个丢掉） | `project_record` 直接 `return dict(record)` | `test_an_entire_record_cannot_be_smuggled_under_an_undocumented_key`（以及同模块另外两条） |
| 警报器 | 删掉 `reject_forbidden(record, …)` 调用 | `test_a_forbidden_field_on_a_record_is_an_error_rather_than_a_redaction` |
| 出口的后置检查 | `export` 里的 `violations_in` 结果恒为空 | `test_the_export_refuses_when_the_serialised_payload_carries_a_forbidden_name` |
| 技术失败才降级 | 成功之后也 `continue` 到下一家（等价于"按回答质量降级"） | `test_the_chain_moves_on_only_for_a_technical_failure` |
| 记录的 provider | 驱动里 `answered` 换成链的**第一个** adapter | `test_a_fallback_records_the_provider_that_actually_answered` |
| 全部失败 | 链在最后一个 provider 失败后 `return` 而不 raise | `test_every_provider_failing_is_one_explicit_error` |
| 半途不切换 | 去掉 `if yielded: raise` | `test_a_stream_that_fails_halfway_is_not_completed_by_another_provider` |
| 分类不合格的失败 | `kind = error.failure or "protocol_error"` 改成恒 `"protocol_error"` | `test_every_provider_failing_is_one_explicit_error`（`Liar` 适配器那一段） |

`git grep -n "MUTATION-42" -- api/` 为空（本工单不在源码里留标记；破坏都在临时脚本里做并已还原，
脚本本身也已删除）。

## 有意没做 / 仍然做不到

- **没有接 LangSmith 客户端。** §10.1 选的是**过滤器**，本工单的验收线也是过滤器与白名单，
  所以投递端是 `TraceSink` 这个 Protocol 加一个 `Delivered` 双替身；没有配置 `TRACE_SINK` 时
  **什么都不出去**（这也是默认值）。一个真实的 LangSmith sink 是
  `langsmith.Client.create_run` 上的一个小类，而它背后的 key 是测试套件不允许依赖的东西。
- **没有让模型去选工具。** 41 号工单把缝指出来了（state 里的 `tool` + `tool_arguments`，
  `draft_tools_node` 已经这么取用），本工单没有动它：这张票的题目是脱敏与降级，
  函数调用是另一张票的事。因此 `app/ai/agents/**` 一行没改。
- **没有给模型任何触发确认的路径。** 按 41 号工单的交代，若将来要在确认之后说一句话，
  正确做法是**领域写入完成之后**由调用方渲染目录句子，而不是让模型去碰
  `domain/agent/confirmation`。本工单没有新增这类句子，因为本工单没有新增确认流程。
- **`observability` 目前是被测模块，还不是请求路径上的调用点。** 图跑完后 `build_payload` 可以
  直接把 `AgentState` 变成 payload 投递（`records` + `is_refusal` 就是它读的两个键，
  有测试用真实的 state 形状证明这一点），但"什么时候导出、导出到哪"是一个部署的选择，
  而 `TraceSink` 就是那个选择的位置。
- **嵌入维度与模型仍然写死**（1536 / 固定模型名）。这是 §10.3 的决定，本工单的职责是不让它松动。
