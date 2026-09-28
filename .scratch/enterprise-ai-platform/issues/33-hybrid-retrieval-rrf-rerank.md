# 33 — 混合检索 + 融合排序 + 重排

**What to build:** 一次查询同时走两条路——向量近邻和关键词全文检索——把两边的结果用倒数排名融合成一个候选集，再重排取最相关的几条。纯向量检索对制度编号、缩写、专有名词不敏感，混合检索能显著补上这块。

**Blocked by:** 32 — 分块与嵌入（父子分块）

**Status:** done

代码：`api/app/domain/retrieval/`（models、errors、fusion、rerank、repository、service）、
`api/app/repositories/retrieval.py`、`api/app/api/v1/retrieval.py` + `schemas/retrieval.py`、
`api/tests/test_retrieval.py`、`api/tests/support/retrieval_sample.py`、
`api/tests/tools/eval_retrieval.py`。**没有新增迁移**：两路检索都读 31/32 已建好的
`document_chunks`（`embedding` 列与生成的 `search_vector` + GIN 索引）。

- [x] 向量检索与全文检索并行执行，各取前 20 条
      —— `PostgresChunkSearchRepository.search_legs`，`retrieval_leg_limit=20`。
      **「并行」在这里的诚实含义是「一条语句里的两个 CTE」，而不是两个协程**：同一条
      `AsyncSession` 就是同一条连接、同一个事务，两个协程 `gather` 只会被 psycopg 串行化，
      看起来并发而实际没有。一条语句换来的是三件真东西：**一次数据库往返**（延迟）、
      **同一个快照**（不会一个 leg 看得到、另一个看不到）、以及**权限条件只有一处**
      （不可能半边检索漏掉过滤）。测试 `test_both_legs_are_one_round_trip` 用计数 session
      断言 `execute` 恰好调用一次，且那一条语句里同时出现 `vector_leg` 和 `text_leg`。
- [x] 使用倒数排名融合把两路结果合并为一个候选集，融合参数可配置
      —— `domain/retrieval/fusion.py::reciprocal_rank_fusion`，**纯函数**，输入是两个以
      `chunk_id` 为键的映射，输出带 `fusion_score` 与每条 leg 的 `(rank, contribution)`
      的候选表。`retrieval_fusion_k` 默认 60（文献值）。单测
      `test_rrf_scores_are_the_sum_of_the_legs_contributions` 逐项断言
      `1/(k+rank)` 之和；`test_the_fusion_constant_scales_the_ranks_it_is_given` 断言
      `k` 真的在起作用。签名按 `chunk_id` 取键而不是按参数位置——写第一版时参数顺序弄反过，
      两次分数都"看起来合理"，这个测试当场抓住了它。
- [x] 融合后取前 5 条作为最终结果，每条返回所属文档、页码、父块内容与得分
      —— `SearchHit`：`document{id,title,filename}`、`page_from/page_to`、
      `parent_content`、`quote`（父块内容；没有父块时回落为子块自身，`context_scope`
      说明用的是哪一个）、`content`（子块原文）、`vector_distance`、`text_rank`、
      `vector_rank`、`text_rank_position`、`fusion_score`、`rerank_score`。
- [x] 检索结果携带足够信息以生成"文件名 + 页码"的引用
      —— 同上；`test_a_hit_carries_everything_a_citation_needs` 与
      `test_a_pdf_hit_names_the_page_it_came_from`（后者用真实 PDF，断言
      `page_to == 2`，即引用指向答案所在的那一页；跨页块会同时给出两页，这正是 31 号
      工单逐页采集页码的意义）。
- [x] 提供一个检索调试视图（仅授权角色可见），展示两路召回、融合排名与最终入选
      —— `GET /api/v1/retrieval/debug`，动作 `retrieval.debug`（新增，`admin` + `hr`）。
      为什么是这两个角色：视图展示的是**语料库的内部形态**（有哪些块、排序器怎么打的分、
      哪个文档答的），而这两个角色是能对「为什么没检索到」采取行动的人；同时视图会整段引用
      正文，工单本身要求它是"仅授权角色可见"。视图包含：两条 leg 各自的前 20（含 leg 内
      rank、融合分、是否进入 rerank 窗口）、逐候选的融合分/融合排名/rerank 分与
      **落选原因**（`below_top_n` / `outranked` / `below_threshold`，句子写明是融合窗口还是
      重排把它挤掉的）、最终 5 条，以及 `filter_explanation`（本次生效的权限条件原文 +
      绑定值，`None` 表示未过滤）。它复用**同一次** `search()` 的结果，不重跑，因此视图
      不可能与用户看到的答案不一致。测试：`test_the_debug_view_shows_both_legs_the_fusion_
      and_why_others_were_dropped`、`test_the_debug_endpoint_is_administration_and_hr_only`、
      `test_the_debug_endpoint_shows_the_filter_a_run_applied`、
      `test_the_debug_endpoint_shows_what_the_rerank_window_dropped`。
      权限矩阵已加入 `GET /api/v1/retrieval/debug → retrieval.debug`，并把
      `assert checked == len(HTTP_MATRIX)` 改成字面量 `assert checked == 89`。
      同时新增的 `GET /api/v1/retrieval/search → document.read`。
- [x] 当最高得分低于阈值时，判定为"无依据"并向上层明确返回该状态，不返回勉强的低分结果
      —— `SearchOutcome.insufficient_evidence` + `hits == ()` + 仍然携带
      `best_score/threshold`；阈值是 `retrieval_min_score`（默认 0.35，作用在**重排后**
      的 `[0,1]` 分数上，理由见 `domain/retrieval/service.py`）。它是一次**成功的检索**
      而不是错误：HTTP 200 + 显式状态，因为 §5.2/D20 的拒答是正常答案（34 号工单据此渲染）。
      边界由 `test_the_threshold_boundary_is_pinned_from_both_sides` 两侧钉死：阈值
      **正好等于**最高分时返回结果（比较是 `<`），高出一个 epsilon 时返回空。
- [x] 离线评估脚本的能量化指标：对一组已知答案的问题，混合检索的命中率不低于纯向量检索
      —— `api/tests/tools/eval_retrieval.py` 现在一次运行同时报告三种模式。
- [x] 检索层不假设调用者是谁；权限过滤将在下一张工单接入，本工单只保证接口留出了过滤入口
      —— `search(query, *, filter_spec=None, limit=5)`；`filter_spec` 由
      `access.kernel.filter_for` 产出，仓库把它渲染成 SQL 并**下推到两条 leg 的
      WHERE 里**（§4.3 的 pre-filter，不是检索后再筛）。没有 spec 时检索是**未过滤**的，
      而这件事在结果里可见：`SearchOutcome.filtered`。工单 35 只需把真实 spec 传进来。

## 实测（`--sample`，假嵌入器，PostgreSQL 18 + pgvector 0.8.6，2026-09-28）

样本改为 **6 份西语制度、12 个问题**（`tests/support/retrieval_sample.py`）：三份是
题面文档，另外三份（劳动健康/PRL、信息安全、培训）是**真实的干扰文档**——它们同样在讲
"人、天、申请、审批"，会把答案挤到向量 leg 的五名之外。这正是融合存在的理由；三份文档的
语料里每种模式都是 1.000，那样的对照是空的。

```
provider: fake   model: deterministic-bag-of-words-v1   reranker: lexical-structural-v1
questions: 12   scored: 12   top-k: 5   fusion k: 60   threshold: 0.35

mode         hit@5       MRR
vector       0.917     1.000
text         1.000     1.000
hybrid       1.000     1.000

questions where the baseline (vector top 5) missed and the hybrid found: 1
  ... of which the answer was inside the vector leg's own top 20: 1
```

**结论：混合不低于纯向量，工单验收线成立（1.000 ≥ 0.917）**，而且赢在**唯一一处
真会出错的地方**——`¿Cuántas horas de permiso individual de formación puedo pedir?`，
向量 leg 的前五名里没有《培训与发展的制度》，但它在向量 leg 的前 20 之内、并且是文本 leg
的第一名，所以融合 + 重排把它捞了回来。**必须说明这不是"混合更好"的普遍证据**：12 个
问题、6 份文档、假嵌入器（哈希词袋，因此所有数字都是**词汇**分数，见脚本的 NOTE），
不足以证明 RRF 优于加权和；真实结论要用组织自己的语料与问题跑。

**过程中发现并修掉的两个真实缺陷**（都由这次量化评估暴露，不是推测）：

1. **`websearch_to_tsquery` 不会剥掉西语问号**，而是把它并进词元：`¿Cuántos días?` 变成
   `'¿cuant' & 'dias'`，`'¿cuant'` 匹配不到任何东西。后果是**每一句人真正会打的问题在
   文本 leg 上都召回 0 条**（实测：`text hit@5 = 0.111`，而那唯一命中的一句恰好以
   `¿En qué` 开头、其 `'¿en'` 是停用词）。修法是 `repositories/retrieval.py::text_query`
   在进入解析器之前归一化查询（去掉 `¿?¡!`、连字符、引号、括号）。
2. **`websearch_to_tsquery` 用 `AND` 连接所有词元**，对搜索框是对的、对问句是错的：
   `¿Cuántos días de permiso por matrimonio corresponden?` 要求文档里同时出现
   "corresponden"，而没有任何制度会这么写。修法是 `tsquery_expression`：一到两个词用
   `websearch_to_tsquery`（短语/引号/`or` 仍然有意义，且短查询多半就是制度编号），
   更长的查询用同一批词元的 `OR`，再用 `ts_rank_cd` 把命中了更多词的块排在前面。
   修复前 `text hit@5 = 0.111`，修复后 `1.000`。

## 仍然做不到 / 有意没做

- **重排器不是 cross-encoder。** `LexicalReranker` 只用表层特征（查询词在**父块**中的
  覆盖率、相邻查询词的最小间隔、标题命中），加上融合分作为先验。它没有语义：把
  `¿Cuántos días de asueto me corresponden?` 与"veintitrés días laborables de vacaciones"
  放在一起，它只能看到 `dias` 一个词，这一点由
  `test_the_reranker_cannot_do_what_a_cross_encoder_can` 断言成测试而不是写成免责声明。
  `Reranker` 是接缝（Protocol），`RETRIEVAL_RERANKER` 决定装配哪一个，
  `test_the_reranker_seam_is_used` 用记录型适配器断言这个接缝**真的被调用**。
- **权限规则不是本工单的。** 检索层不产生 `FilterSpec`；35 号工单接。
- **多语言**：全文配置是 `spanish`（迁移 0022 定的，理由见那里）。
