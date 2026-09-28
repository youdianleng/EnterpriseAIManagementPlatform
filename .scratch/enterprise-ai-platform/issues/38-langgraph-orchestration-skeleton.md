# 38 — LangGraph 编排骨架

**What to build:** 助手有了一个明确的"大脑"：先判断用户想干什么（闲聊 / 问制度 / 查自己的数据 / 想办一件事 / 想干被禁止的事），再路由到对应的处理路径。中间状态持久化在数据库里，**服务重启后正在进行的对话不丢**。

**Blocked by:** 37 — 问答界面与引用回链

**Status:** ready-for-agent

- [ ] 图包含意图分类节点，分类结果至少覆盖：制度问答、只读数据查询、待办操作、硬禁止请求、闲聊
- [ ] 制度问答路由到已有的检索与生成路径并复用原有流式与引用行为
- [ ] 硬禁止请求（查他人薪资、查他人考勤、要绩效或晋升建议、任何改库请求）在**代码层**被拒绝，且给出明确的双语说明，不转发给模型去"委婉处理"
- [ ] 中间状态使用 Postgres 检查点持久化在独立 schema 中，**不使用 Redis 承载**（状态不可丢）
- [ ] 有测试验证：在一个中断的流程中重启服务后仍能继续
- [ ] 图的拓扑以代码表达且可被单测直接调用（不需要走 HTTP）
- [ ] 每个节点的输入输出被记录，但**不含**对话正文与检索内容（为后续脱敏可观测性留出接口）

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

