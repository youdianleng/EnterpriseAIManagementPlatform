# 04 — 数据库迁移、pgvector 与 Redis 接入

**What to build:** 后端有一条可靠的数据库迁移通道，从空库一路迁到最新结构可重复执行；数据库已启用向量扩展并验证过向量列的读写与索引；Redis 连接池可用且能从后端读写。这是此后所有数据表变更的唯一入口。

**Blocked by:** 02 — 后端骨架：统一错误码 + 结构化日志 + 健康检查

**Status:** done

**Verification (2026-09-25):**
- `pytest` → 45 passed (14 new: database + cache); `ruff check app tests` clean.
- `tools/probe_migration.py` → all checks passed on a scratch database: upgrade reaches `0001`, `vector` + `ltree` installed, the vector column carries 1536, an HNSW index with `vector_cosine_ops` exists, a real cosine query orders the nearest vector first, downgrade removes the table, and re-upgrade returns to head.
- `/ready` now executes `SELECT 1` against the pooled engine and pings Redis, reporting `connected postgres:5432/eam` and `connected redis:6379/0`.
- Tests run against a separate `eam_test` database with an outer transaction rolled back per test, so a run can never touch development data.

**Three defects found and fixed while verifying:**
1. `asyncio_default_fixture_loop_scope = "session"` made fixtures build connection pools in the session event loop while each test ran in its own function-scoped loop, failing with "attached to a different loop". Fixture and test loop scope must match.
2. The HNSW index assertion assumed one row from `pg_indexes`; the primary-key index is also there, so it must search rather than take the first.
3. Ruff treated `alembic` as a first-party package because `api/alembic/` is a local directory, and demanded it be grouped with `app`. Declared `known-third-party` so the third-party package wins.

**Schema decision corrected by measurement.** The design doc claimed 3072 dimensions merely cost more index memory and wanted `halfvec` to be worthwhile. Measured on 10k chunks: `vector(3072)` **cannot be HNSW-indexed at all** (pgvector caps `vector` at 2000 dimensions and `halfvec` at 4000). `vector(1536)` + HNSW was chosen and `docs/DESIGN.md` §10.3 rewritten with the real numbers; the probe is kept at `api/tests/tools/probe_vector_dimensions.py` so it can be re-run.

- [ ] 迁移工具已接入，空库执行 upgrade 能到达最新版本，downgrade 能干净回退
- [ ] 向量扩展在迁移中创建，不依赖手工执行的 SQL
- [ ] 有一个冒烟迁移验证向量列可写入、可做余弦相似度查询、HNSW 索引已建立
- [ ] 向量维度在此阶段一次定死为 1536 并在迁移中以常量表达，任何改动都会被测试拦住
- [ ] Redis 连接池接入后端，有读写往返的集成测试
- [ ] README 记录迁移的创建、升级、回退命令
