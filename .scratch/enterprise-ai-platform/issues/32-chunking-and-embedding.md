# 32 — 分块与嵌入（父子分块）

**What to build:** 解析出的文本被切成父子两级：小块（约 400 token）用于检索，大块（约 1500 token）作为喂给模型的上下文。每个小块记住自己来自哪一页，这是引用能精确到页码的前提。向量写入数据库并建立索引。

**Blocked by:** 31 — 文档上传与异步解析管道

**Status:** done

- [x] 先按文档的标题层级做结构感知切分，再在章节内部按长度切分，不在句子中间硬断
- [x] 每个子块约 400 token、重叠约 15%；父块约 1500 token，子块与父块关联
- [x] 每个块记录：所属文档与版本、父块引用、顺序、内容、token 数、**起止页码**、标题路径
- [x] 页码信息在解析阶段就已捕获（PDF 按页抽取），不是事后猜测
- [x] 向量维度为 1536 并作为迁移中的常量；嵌入模型名与分块版本记录在版本记录上，便于将来重新嵌入
- [x] 同时生成全文检索所需的文本索引，供后续混合检索使用
- [x] 建立向量近邻索引并实测在 1 万份文档规模下的检索耗时
- [x] 重新处理同一文档版本时会先清除该版本的旧分块再写入，不产生重复
- [x] 有一个离线评估脚本：给定若干问题与期望命中的文档，输出命中率，用于调参

## 落地记录（2026-09-28）

**分块**：`api/app/domain/document/parsing.py`。先按标题层级切章节（Markdown 标题 / DOCX Heading 样式 / XLSX 工作表名 / PDF 的编号条款与短大写行），章节内再按句子切分并打包：子块目标 400 token、上限 500，重叠取尾部整句约 15%；父块目标 1500、上限 2000，跨子块但绝不跨章节。句子边界规则只在 `_sentences` 一处，西班牙语与英语共用（`¿`/`¡` 是开句符，故终止符两语言相同）；唯一会切断句子的地方是「单句超过 500 token」的兜底 `_split_oversized`，有测试钉住。

**tokenizer**：`api/app/domain/document/tokenizer.py`。真实 tokenizer `cl100k_base`（`text-embedding-3-small` 所用编码），BPE 词表按 tiktoken 的期望文件名**随仓库分发**（`data/9b5ad71b….tiktoken`，1.6 MB，SHA-256 由测试断言），因此完全离线。测得的精确值（`tests/test_chunking.py` 以字面量钉住）：西语样本 39 token、英语样本 34 token、四句西语段落 72 token；退化适配器 `ApproximateTokenizer` 的实测误差为西语 0.72–0.74×、英语 0.94×，测试钉住区间。`text-embedding-3-small` 的 1536 维是该模型的原生宽度，故 `EMBEDDING_MODEL` 由 `-large` 改为 `-small`（两者都是 1536，见 §10.3）。

**嵌入接缝**：`api/app/domain/document/embeddings.py`，一个 Protocol 两个适配器——真实现 `OpenAIEmbedder`（stdlib `urllib` + `asyncio.to_thread`，`dimensions` 显式传 1536）与开发/测试用确定性假实现 `DeterministicEmbedder`（分词哈希成 1536 维单位向量，可复现且**词面**相似）。`EMBEDDING_PROVIDER` 未设时：development/test → `fake`，其它 → `openai`；`none` 是受支持的配置。缺少 key 时是编入目录的失败（`ERR_DOC_009`，503，双语提示），文档仍为 `ready` 且分块保留，`embedding IS NULL` 即重嵌工作清单，`parse_documents --embed` 可修复。

**写入**：只有**子块**带向量（检索搜的是它），父块是上下文；`embedding_model` 与 `chunking_version` 每行记录。`reprocess` 先删该版本全部分块（含父子与向量），再按新分块重建链接。

**全文检索**：`search_vector` 为**生成列** `to_tsvector('spanish', coalesce(heading_path,'') || ' ' || content)`，配 GIN 索引。选 `spanish` 配置、**不使用** `documents.language` 列：语料以西语为主，且生成列要求配置为常量（`to_tsvector(regconfig,text)` 在配置字面量时才 IMMUTABLE）。西语词干把 `vacaciones`/`vacación` 归一，正是让问句命中不同措辞条款的一半。

**索引实测**（`api/tests/tools/probe_hnsw_scale.py`，20 000 行 × 1536 维，真实表结构 + RLS 策略 + GIN 与部分索引，本机 compose 的 postgres 18 / pgvector 0.8.6）：

| 指标 | 数值 |
|---|---|
| 建 HNSW + GIN + 部分索引 | 19.2 s |
| 表 + 全部索引 | 255.5 MB（HNSW 索引 83.6 MB、行数据 128.4 MB） |
| 查询 p50 / p95（强制 HNSW） | 1.00 ms / 2.02 ms |
| 查询 p50 / p95（精确全表扫描，对照） | 0.55 ms / 0.97 ms |
| 查询 p50 / p95（`eam_app` + RLS + 强制 HNSW） | 0.79 ms / 1.37 ms |
| recall@5 | 1.000 |

关键读法：**这个规模下规划器主动选择顺序扫描**（255 MB 全在 page cache，精确扫描比 HNSW 更快），所以「有 HNSW 就一定更快」在 2 万行上不成立；HNSW 的代价近似对数、全表扫描线性，交叉点在更大语料。RLS 策略在强制走索引时几乎不增加成本。recall@5 = 1.000 是合成随机向量的结果，只说明索引没坏，不代表真实语料召回。数字同时记入 `docs/DESIGN.md` §10.3。

**离线评估**：`api/tests/tools/eval_retrieval.py`（示例输入 `questions.example.jsonl`）。测的是**配置的嵌入提供方**在真实的 `ORDER BY embedding <=> …` 上的 hit@k 与 MRR；分母只算「期望文档已索引」的问题，未解析/未嵌入/标题不存在的期望文档单独列出不计入。明确不测：假提供方下不是语义质量（是词面分数，脚本每次打印 provider 与 model 警示）、不含权限过滤（以 system 上下文运行，看得到全部文档）、不含 rerank/融合/生成。冒烟跑过 6 问小语料：hit@5 1.000、MRR 0.833。

**迁移**：`api/alembic/versions/20260928_1100_chunk_embeddings.py`（revision `0022`，down_revision `0021`）新增 `embedding_model`、生成列 `search_vector`、GIN 索引与部分索引 `(document_id) WHERE parent_chunk_id IS NOT NULL AND embedding IS NULL`。

**对抗性验证**：(a) 把 `_pack` 改成按固定字符数切分后 `tests/test_chunking.py::test_a_child_never_ends_mid_sentence` 失败；(b) 让 `replace_chunks` 不先删除后 `tests/test_embeddings.py::test_reprocessing_rebuilds_the_same_split_and_the_same_vectors` 失败。两处均已还原。
