# 02 — 后端骨架：统一错误码 + 结构化日志 + 健康检查

**What to build:** 后端有一套所有接口共用的骨架：任何失败都返回同一种错误信封（含一个稳定的错误码、request_id 和时间戳），错误码集中定义并配有西班牙语/英语两份文案；每个请求从入口到出口都带同一个 request_id 打进结构化 JSON 日志；`/health` 与 `/ready` 分别报告进程存活与依赖可用性。

**Blocked by:** 01 — Monorepo 与 Compose 骨架

**Status:** ready-for-agent

- [ ] 错误信封字段固定为：错误码、HTTP 状态、双语消息键、request_id、时间戳
- [ ] 错误码按 `ERR_<DOMAIN>_<NNN>` 命名并集中登记，禁止在业务代码里散落字符串
- [ ] 每个错误码在西语与英语文案表中都有对应条目，缺条目会导致测试失败
- [ ] 日志为结构化 JSON，每条至少含 request_id、路径、方法、状态码、耗时；同一请求内的所有日志共享同一 request_id
- [ ] `/health` 与 `/ready` 语义区分开；`/ready` 会实际探测 Postgres 与 Redis 并在依赖不可用时返回非 200
- [ ] 有一个故意抛错的测试接口，验证错误信封与日志格式
