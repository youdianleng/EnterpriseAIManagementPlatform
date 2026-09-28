# 50 — worker 服务：让计划任务有地方跑

**What to build:** `docs/DESIGN.md` §2 的服务清单是 **7 个**：`web`、`api`、`worker`、`postgres`、`redis`、
`mailpit`（dev）、`caddy`（可选提供域名时用），并且专门用一句话说明为什么 worker 独立：
文档解析、嵌入生成、每日汇总邮件、月度应出勤快照、PersonnelChange 生效落库**不能占用 HTTP 请求周期**。
今天 `docker compose config --services` 只有 5 个（`api`、`web`、`postgres`、`redis`、`mailpit`），
`docker-compose.yml` 自己写着"development stack has no cron and no worker"。

任务本身都写好了（`api/app/jobs/`：解析、汇总邮件、异常扫描、应出勤快照、节假日导入、人事变更生效），
都以 CLI 形式可跑，其中两个还能在 api 进程里以 asyncio 循环跑。问题不在任务，而在**部署形态**：单机
Compose 上没有 cron、没有 worker，于是"每日汇总邮件""每日异常扫描""每月应出勤快照"在一个全新部署上
**永远不会自己跑**，除非有人手工敲命令。本票把 DESIGN 的第七个服务补上。

**Blocked by:** 04 — 数据库迁移、pgvector 与 Redis 接入（Compose 形状）；31 — 文档上传与异步解析（第一个异步任务）

**Status:** ready-for-agent

- [ ] Compose 增加 `worker` 服务：与 `api` 同一个镜像、同一份代码与配置，`command` 跑任务循环而不是 uvicorn；依赖 `postgres`、`redis` 的健康检查
- [ ] worker 覆盖**全部**计划任务：文档解析、每日汇总邮件、每日异常扫描、应出勤快照、人事变更生效、节假日导入（按各自应有的频率；频率来自配置，不硬编码）
- [ ] **不允许双跑**：worker 起来之后，api 进程内的同名循环必须关掉（`parses_documents_in_process`、
      `personnel_apply_runner_enabled` 的默认值要跟着改，或由 compose 显式传入关闭值）。两个进程抢同一批
      待解析文档会造成重复分块与重复嵌入——这正是"幂等由数据库唯一约束保证"之外还需要避免的浪费
- [ ] 每个任务循环失败**不退出进程**：记一行结构化日志（D30），下次周期继续；单次失败不能让整个 worker 死掉
- [ ] worker 不暴露端口、不写审计以外的东西；它用的数据库角色与 api 相同的既有约定（`APP_DATABASE_URL`），
      不新增角色
- [ ] 有一个验证：**证明任务真的被调度**而不是"配置里写了"。至少一条端到端断言——例如造一个待解析文档，
      只启动 worker（不起 api 的进程内循环），断言它被解析完成；以及一条断言 compose 的 worker 命令与
      api 的进程内开关不会同时生效
- [ ] README 更新服务清单与"任务在哪跑"，与 DESIGN 的 7 服务对齐（加上票据 49 的备份服务后，compose
      恰好是 `web`、`api`、`worker`、`backup`、`postgres`、`redis`、`mailpit` 七个）

## 边界

- 不重写任务逻辑：`api/app/jobs/` 里的函数就是 worker 的入口，本票只决定它们**在哪个进程里跑**。
- 不引入 Celery/arq 之类的新依赖，除非能用一句话说明当前 asyncio 循环做不到什么；DESIGN 只说"worker 独立"，
  没说必须用某种队列库。
- 若实现者认为"宿主机 cron + CLI"才是这个单机部署的正确形态，**必须**先在 `docs/DESIGN.md` §10 记下这条
  偏离（含理由），再据此实施——不允许既不改设计文档、也不加 worker 服务，让 §2 的 7 服务停留在纸面上。
