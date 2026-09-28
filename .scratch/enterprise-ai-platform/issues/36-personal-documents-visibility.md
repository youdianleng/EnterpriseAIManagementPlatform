# 36 — 个人文档与可见性

**What to build:** 员工可以上传自己的文档（合同、证明、个人笔记），默认**只有自己能看到**。他可以选择公开给本部门。但公开不等于绕过密级——个人文档公开后仍受公司密级规则约束。个人文档**不会**进入公司知识库的检索池，只在所有者本人提问时才可能被召回。

**Blocked by:** 35 — 密级 × 部门检索过滤与越权测试

**Status:** done

- [x] 上传时可选择可见性：仅自己 / 本部门；默认仅自己
- [x] 个人文档标记为非公司知识库内容，与公司文档在数据上可区分
- [x] 个人文档的所有者始终是自己，不能被转交给他人
- [x] 公司知识库检索时**不召回**任何他人上传的个人文档；只有提问者本人的个人文档可被召回
- [x] 公开给本部门的个人文档，对同事仍须满足密级条件才可见
- [x] 员工可撤销公开，撤销后同事立即无法访问，缓存同步失效
- [x] 他人访问未公开的个人文档返回 403 且资源在列表中完全不存在
- [x] 回答中若引用了个人文档，必须在回答顶部明确标注"以下内容来自个人文档（非公司知识库）"
- [x] 有测试覆盖上述全部可见性组合

## 实现

### 0. 先读到的两件事，它们决定了整张工单的形状

`documents` 表里本来就有工单需要的两个字段：`is_company_kb`（公司知识库与否）与
`visibility`（`private`/`department`/`company`，迁移 0021 建的，CHECK 已限定三个值）。所以本工单
**不建新表**：设计 §3.6 列的 `document_permissions`（按人共享）在这套 schema 里没有被写，工单要的
「仅自己 / 本部门」两档正好就是 `visibility` 的两值 —— 见下面第 5 节为什么**故意不建**。

另一件事是检索谓词里 §4.2 第 1 条的写法。原来它是

```python
clauses.append("d.owner_employee_id = :filter_employee_id")
```

即"谁拥有就算谁的"；公司文档的 owner 是 NULL，所以它实际上**碰不到**公司文档。但这是一句
"任何带 owner 的文档"，而不是一句"个人文档"，而本工单的全部规则都挂在"这是谁的文档"上。所以它
被改成 `(NOT d.is_company_kb AND d.owner_employee_id = ...)` —— 一个子句，两个词，一句关于个人
文档的话。

### 1. §4.2 只有一处实现，本工单只加一个具名字段（第 4、5 条）

`docs/architecture/codebase-design.md` 约束 C 与工单的"不允许复制第二份 §4.2"都指向同一处：
`api/app/domain/access/kernel.py::filter_for(principal, ResourceKind.DOCUMENT)` 产出 spec，
`api/app/repositories/retrieval.py::visible_document_clauses` 把它渲染成 SQL。工单的检查项把
"可见性"和"检索池"分成两句话，两者不是同一条规则：

* **§4.2 的可达范围**（列表、读取、下载）包含"所有者公开给本部门的个人文档"—— 这是设计里
  `explicit_grant` 那一档在本 schema 的落地形态；
* **提问的召回池**只有公司知识库 + 提问者**本人**的个人文档：「个人文档不进入公司知识库的检索池」、
  「只有提问者本人的个人文档可被召回」。

差别因此被写成一个**具名字段**而不是第二份实现：`FilterSpec.personal_documents_via_department`。

* 内核的文档分支把它置为 `True`（§4.2 的完整可达范围）；
* `api/app/domain/retrieval/filtering.py::answer_filter_for(principal)` —— 唯一的"提问 → spec"
  翻译处 —— 调 `FilterSpec.only_my_personal_documents()` 把它清掉；
* 两个渲染器读同一个字段：`repositories/retrieval.py::visible_document_clauses` 只在它是 `True`
  时渲染第 3 条子句；`repositories/document.py::_visible` 同理（列表要看得见）。

`only_my_personal_documents()` 是 `FilterSpec` 上的方法而不是调用点上的 `dataclasses.replace`，
原因是本工单撞到的一个真实缺陷：`FilterSpec` 是 `__slots__` 类、构造函数私密、**不是 dataclass**，
`replace()` 会抛 `TypeError: replace() should be called on dataclass instances` —— 在一次请求里，
表现为 500。把副本的构造放回持有 token 的那个类里，这个错误就不可能再写出来；`_cleared()` 只清
具名字段，其余字段逐个从 `__slots__` 取值，所以"清掉了什么、留下了什么"是可读的，而且是**收窄**
（没有任何分支会置位）。

`api/app/domain/document/service.py` 加了 `corpus_specs` 属性（= `specs` 收窄一格），让测试能
直接问"文档模块认为的检索池"，但这张工单里**没有任何请求路径读它** —— 入口仍然只有
`answer_filter_for` 一个。

### 2. 渲染出的 SQL：第 1 条改成关于个人文档，第 3 条按 spec 渲染

`repositories/retrieval.py::visible_document_clauses` 现在是：

1. `(NOT d.is_company_kb AND d.owner_employee_id = :filter_employee_id)` —— 自己的**个人**文档，
   不看密级；
2. `(d.is_company_kb AND clearance IN :filter_clearances AND department IN :filter_departments)`
   —— 公司知识库，密级与部门两条都要满足；
3. `(NOT d.is_company_kb AND d.visibility = 'department' AND clearance IN ... AND department IN ...)`
   —— 同事公开给本部门的**个人**文档，**只在 `personal_documents_via_department` 为真时渲染**，
   而且带 `clearance_level` 上限（D11：「显式共享不能突破密级上限」）；
4. `(d.is_company_kb AND clearance IN :filter_clearances)` —— 例外角色，仅公司文档。

旧版那句 `# Clause 3, unwritten because the table it needs does not exist yet (ticket 36)` 的占位
注释被第 3 条真规则取代，不是被推迟。`repositories/document.py::_visible` 是同一条规则的第二处
渲染（列表用），同样加了 `NOT is_company_kb` 的门与第 3 条，并且第 1 条也加了门。

### 3. 上传：默认私有，两档可选，`company` 不是第三档（第 1 条）

* `visibility` 仍然是上传的表单字段；**不传就是 `private`**，而 `effective_visibility()` 现在先看
  `is_company_kb`：公司文档恒为 `company`，个人文档才是调用方说的那个值。默认写在派生方法里而不是
  字段默认值里，因为默认取决于另一个字段 —— 一个 `default="private"` 会让公司文档自称一种与自己
  的 flag 矛盾的可见性，而"忘了传字段"必须意味着私有，绝不能意味着公开。
* `DocumentMetadata.require_coherent()` 新增两条，都是 400 且点名出错的字段：
  * 个人上传写 `visibility='company'` → 拒绝。"company 不是第三档共享程度"，它是 `is_company_kb`
    派生出来的东西；静默降级成 `private` 会对一个系统没有执行的请求回 201（工单 31 为上传论证过
    同一条）。
  * `visibility='department'` 但没给 `department_id` → 拒绝。§4.2 通过 `department_id` 够到共享
    文档，没有它的"公开"谁也没命中。
* `_require_upload_allowed` 的部门可达性检查对个人文档**不放宽**：`visibility='department'` 只能
  公开到自己工作的部门（公司文档那条角色旁路只属于公司文档）。
* 迁移 0025 把这层关系变成数据库的 CHECK：`(visibility = 'company') = is_company_kb`。个人文档
  只能是 `private`/`department`；公司文档只能是 `company`。

### 4. 所有者不可转移（第 3 条）

"加一个所有者转移接口"的正确答案是**这个接口不存在**，测试就断言这个不存在，并且分四处：

* **路由**：从 `app.openapi()["paths"]` 读，`/api/v1/documents` 下非 `GET` 的只有上传与重解析；
* **请求体**：上传时故意多发一个 `owner_employee_id` 表单字段，落库的 owner 仍是调用者；
* **接口**：用 `inspect.signature` 遍历 `DocumentService` / `DocumentRepository` /
  `PostgresDocumentRepository` —— 取 owner 的只有两条 **create** 路径（创建时声明本行 owner，值由
  服务从 principal 派生），且没有任何参数叫 `new_owner` / `to_employee_id`；
* **数据库**：以同事身份、以同事自己的范围发布上下文，跑一条 `UPDATE documents SET
  owner_employee_id = ...` —— 影响 0 行（`documents_write` 是读规则正向读法，而 §4.2 根本没把这个
  调用者放进这一行）。

`DocumentMetadata` 里没有 owner 字段，所以"上传的 metadata 不能指定 owner"从类型上就成立。

### 5. 为什么不建 `document_permissions`

设计 §3.6 列了这张表（按人/按部门/按角色的显式共享）。本工单的检查项要的是两档：仅自己 / 本部门，
而 `documents.visibility` 的两个值正好就是这两档，`department_id` 说明"本部门"是哪个。再建一张没有
写入方的表来承载同一件事的第二种说法，就是 §4.3 检查项明确拒绝的"第二套规则"；设计文档 §3.6 里
`knowledge_bases`（`company`/`personal`，"后者按 owner 过滤"）同理 —— 它的判别式在本 schema 里就是
`is_company_kb`，本工单把"个人 = 非公司知识库"变成 **CHECK**，而不是再补一张表。

三处渲染/判定现在都能区分两类文档，且都是断言的（`test_a_personal_document_is_not_company_knowledge_base_in_the_data`）：
行上的 `is_company_kb` + owner（公司文档 owner 为 NULL，数据库 CHECK 保证）、检索谓词里第 1 条的
`NOT d.is_company_kb` 门、列表渲染里的同一个门。

### 6. 撤销公开立即生效（第 6 条）—— 缓存键一行未动

**结论先说：权限快照的缓存键没有加任何输入，因为文档可见性本来就不是它的输入。**

`api/app/domain/access/snapshot.py` 的键由 `session_epoch` + roles + 账户密级 + 部门/职位/汇报关系
的 `hashtext` + 组织树版本拼成（`resolve_principal`）。文档的 `visibility` 是**行上的列**，每次查询
现读现渲染，快照里没有它的副本，所以"撤销后同事立即无法访问"是构造上成立的，而不是靠 TTL 兜底。
这条性质本身被两个测试钉住：

* `test_revoking_the_publication_takes_effect_on_the_next_request` —— 先读一次（把缓存焐热），
  公开后同事可读/可列表/可下载，撤销后**下一次请求**即 404、列表里彻底消失、下载 404，而所有者
  仍然拿得到；
* `test_the_snapshot_cache_key_carries_no_document_visibility` —— 直接比较两次 `resolve_principal`
  的 `version`：改文档可见性前后 `version` 不变。若将来有人把可见性塞进键（或把它复制成快照里的
  一个集合），这条会红，然后必须把新输入加进键。

本工单**没有**新增元数据编辑路由：翻转可见性用的是列本身（UI 归 37 号工单）。测试里的 `set_visibility`
先发一个 `PATCH` 并断言它是 404/405 —— 一旦有人加了编辑路由，这条测试会红并迫使测试改用那条路由，
而不是悄悄继续走列。

### 7. 回答里的个人文档标注，落在回答契约里（第 8 条）

§5.2 第三条回答规则：「若命中的是个人文档，回答顶部追加标记："以下内容来自个人文档（非公司知识库）"」。
本工单把它变成契约的一部分，**不画界面**（37 号工单拥有界面）。

新增 `api/app/domain/answer/models.py::SourceNotice`，由 `SourceNotice.of(citations)` 从**真正落了
地的引用**上算出来：`any(not citation.is_company_kb)`（ticket 34 特意放在 `SearchHit.document` 上的
字段）。因为检索谓词只召回提问者本人的个人文档，能出现在引用里的个人文档就只有他自己的。

它出现在四个地方，37 号工单四处都能读：

| 位置 | 形状 |
| --- | --- |
| SSE `citations` 帧（**任何文本之前**） | `source_notice: {personal_documents, message_key, text:{zh,es,en}}` 或 `null` |
| SSE `done` 帧 | 同上（"存了什么"的完整记录） |
| SSE `refusal` 帧 | 显式 `null`（拒答没有引用可标注，写出来比省略好让客户端不必猜） |
| `rag_messages.source_notice`（迁移 0025 新增列，JSONB，可空） | 同上；`GET /answers/conversations/{id}` 经 `MessageRead.source_notice` 返回 |
| `AskOutcome.source_notice` | 驱动里算一次，落库与入帧用的是同一个决定 |

`message_key` = `answer.source_notice.personal_document`；`text` 三语写死在
`PERSONAL_DOCUMENT_NOTICE_TEXT`（zh 逐字用设计原文，es/en 同义）。**客户端需要的新 `message_key`
只有这一个**（另见文末）。

### 8. RLS：第二道防线原先是**宽于**规则，本工单收窄它（结构约束）

这是本工单真正的安全修复。迁移 0021/0022 的
`document_visibility_predicate(owner, department, clearance)` 判的是

```
owner = me OR (clearance_ok AND department_ok)
```

**它不知道文档是哪一类**。于是一份"落在某部门里的个人上传"，对**该部门所有密级够的同事**在数据库
层就是可读的 —— 不管它的 `visibility` 说什么。这正是"兜底只能更严、绝不能更宽"被违反的方向。

迁移 `0025`（`20261007_1000_personal_documents.py`）给谓词加两个参数（`is_company_kb`、
`visibility`），三条子句：

1. 自己的文档（ownership 无附加条件，§4.2 第 1 条）；
2. 公司知识库文档：密级 + 可达部门；
3. 所有者公开给本部门的个人文档：密级 + 可达部门。

**函数是替换而不是重载，所以策略先摘后挂**：引用函数的策略是 `DROP FUNCTION` 的依赖，不带
`CASCADE` 会失败，带 `CASCADE` 会把策略一起悄悄删掉、留下没有过滤的表。所以先按顺序
`DROP POLICY`（`document_chunks_access` 在前，它的 `USING` 读 `documents`），再替换函数，再原样
重建每条策略；`downgrade` 沿同一条路回到迁移 0013/0021 的三参数版本。`documents_insert` 未被触碰
（本工单不改任何写规则）。在 scratch 库上实测 `upgrade head → downgrade -1 → upgrade head` 通过，
`alembic current` 回到 `0025 (head)`。

CHECK 约束前有两条数据修复 UPDATE：`ADD CONSTRAINT` 会校验既有行，一台机器上若有一行两列不一致就
会在部署中途失败；这两条 UPDATE 只朝规则的方向移动（改 `visibility`，从不改 `is_company_kb`），
个人文档的 `company` 被挪到 `private`（两者中更窄的那个）。

测试断言的是**数据库自己的拒绝**，用受限角色 `eam_app` 的连接、按同事的上下文发布
（`test_the_database_refuses_a_private_personal_document_in_the_callers_department`）：私密个人
文档的 chunk 在**无过滤**的 join 里为 0、`documents` 行为 0、谓词对行上五列答 `false`；对照有两
个 —— 同一条无过滤查询对**已公开**的那份返回 >0（所以 0 是策略在读 `visibility`，而不是策略什么
都拒绝），以及 `test_the_second_line_admits_the_owners_own_document`（所有权无附加条件）。

### 9. 测试（第 9 条）

新增 `api/tests/test_personal_documents.py`（23 条），语料经**真实上传端点 + 真实解析**入库，五个
principal 由**真实快照**解析。检查项 → 测试的对照表在该模块 docstring 里，逐条对应。

要点：

* 所有"不召回/不可见"的断言都在**命中集合**（document id / chunk id / 正文短语）上，并且**必配
  对照**（同一问题由读得到的人提问确实命中）或不带过滤的同一次运行（`filter_spec=None`）—— 工单
  35 的教训，缺了对照的"命中集合为空"同样也是空语料、坏索引、坏阈值的表现。
* **谓词与绑定值同样被钉**，而且钉在请求路径真正会跑的那个 spec 上（`answer_filter_for`，不是
  `filter_for`）。`test_the_whole_matrix_agrees_with_the_kernel` 把 §4.2 + 召回规则手写成
  3 文档 × 5 调用者 = 15 行的矩阵，逐行比对**列表 / 读取 / 下载 / 命中集合 / `can()`**，差异一次
  性列出。
* 天花板用两个**同部门**、不同密级的同事：`colleague`(medium) 与 `junior`(low)，文档本身是
  `medium` —— 所以 junior 被拒是密级造成的，不是部门造成的。
* 公司文档放在**另一个**部门、`low` 密级：`outsider` 靠部门条款够到，HR 靠例外角色跨部门够到，
  这同时钉住了"例外条款仅公司文档"（`test_hr_does_not_reach_a_personal_document_through_the_exception_clause`）。

### 10. 工单 35 的钉子被移动了：移到了新的事实上，而且是加强

工单 35 的套件（`api/tests/test_retrieval_escalation.py`，现 12 条）钉的是谓词的**文本**与绑定值，
这是它存在的理由。本工单改了文本，所以钉子必须移动，逐条说明**没有放松**：

1. `predicate_of()` 由 `visible_document_predicate(filter_for(...))` 改为
   `visible_document_predicate(answer_filter_for(...))`。这是**收紧**：改之前它钉的是一个**没有
   任何请求会跑**的 spec（内核的完整可达范围，包含别人的公开个人文档）。
2. `company_clause_parameters()` 里的 `assert "d.is_company_kb" in predicate` 换成两条更具体的：
   `"d.is_company_kb AND d.clearance_level = ANY("` 必须在（公司条款在），且
   `"d.department_id = ANY(CAST(:filter_departments AS uuid[]))"` 恰好出现一次。原来那条断言在
   新文本下**会因为错误的理由通过**（第 1 条和第 3 条里都有 `is_company_kb`）—— 一并把"部门项只写
   一次"钉死，比原来只查一个子串更强。另外新增 `"d.visibility" not in predicate`：检索谓词不允许
   出现共享条款。
3. 新增 `test_the_personal_document_clause_is_pinned_term_by_term`：第 1 条子句逐词钉
   （`(NOT d.is_company_kb` 开头、含 `d.owner_employee_id = :filter_employee_id`、`filter_employee_id`
   绑定等于调用者本人），并断言检索谓词里没有 `d.visibility`。这是本工单新条款的**专属**钉子，四个
   场景（都是关于公司条款的）碰不到它。
4. `test_the_database_refuses_the_same_rows_without_any_application_predicate` 里直接调用
   `document_visibility_predicate` 的地方改传五个参数（多读两列），断言不变、且更强。
5. 模块 docstring 记录了这次移动（第三节）。

`api/tests/test_retrieval.py`：

* `test_the_filter_is_a_disjunction_of_clauses` —— 原来断言 `predicate.count(" OR ") == 1`
  与 `startswith("((")`。子句数现在是 spec 的函数，所以改成**把渲染器的子句列表重新拼回整条谓词并
  比较**（`OR` 连接、括号配平），再加一条"第 1 条子句带 `NOT d.is_company_kb` 门"。计数换成重建是
  加强：`AND` 连接任何一个位置都会按名字失败，而计数只能发现"变了"。原来
  `assert "is_company_kb" not in predicate` 也换成公司条款的**具体文本**不在（新谓词里第 1 条就含
  `is_company_kb`，旧断言会因错误理由失败）。
* `test_the_spec_the_search_applies_is_the_one_the_document_list_applies` —— 名字不变，内容重写：
  对**公司文档**仍断言列表与检索一致（列表是公司语料的三处渲染之一），而"列表比检索宽的那一格"被
  拆到新测试 `test_a_shared_personal_document_is_a_list_clause_and_not_a_retrieval_one` 里逐字段
  钉死（列表子句含 `visibility='department'` + 密级 + 部门；检索子句**恰好**等于列表子句去掉那一条）。
  这个测试是本工单唯一"列表与检索有意不一致"的地方，工单要求它是一句写下来的决定，而不是一个缝。
* `test_the_debug_endpoint_shows_the_filter_a_run_applied` —— 期望的谓词改用
  `answer_filter_for(principal)`（视图跑的就是它），否则会拿一次运行去比一条没人跑的规则。

**未新增路由、未新增目录动作**，所以 `api/tests/test_permission_matrix.py` 的
`assert len(cases) == 7 * 57 * 13` 与 `assert checked == 78` 一行未动（全量套件通过）。

### 11. 突变验证（必须做的那几个）

每个突变改完即跑，跑完即还原；`git diff -- api/app` 现在只有本工单的真实改动（见第 12 节），没有
残留标记。

| # | 突变（位置） | 失败的测试 |
| --- | --- | --- |
| 1 | `retrieval.py`：第 1 条子句去掉 `NOT d.is_company_kb` | 5 条：`test_a_personal_document_is_not_company_knowledge_base_in_the_data`、`test_the_owners_own_personal_document_is_recalled_for_them`、`test_the_filter_is_a_disjunction_of_clauses`、`test_no_filter_spec_means_no_clause_and_a_spec_means_one`、`test_the_personal_document_clause_is_pinned_term_by_term`（三条是**谓词级**，两条是数据级 —— 命中集合在这个 schema 下恰好不变，见下） |
| 2 | `retrieval.py`：**公司条款**去掉 `d.is_company_kb` 门（真正的泄漏） | 7 条：`test_a_colleagues_private_personal_document_is_not_recalled`、`test_the_search_does_not_recall_a_colleagues_published_personal_document`、`test_someone_elses_private_personal_document_is_absent_and_refused`、`test_the_whole_matrix_agrees_with_the_kernel`、以及工单 35 的三条场景（`test_a_low_clearance_question...`、`test_the_department_clause_is_what_excludes_it_not_the_ceiling`、`test_an_ordinary_employee_does_not_reach_what_only_hr_may_read`）—— 前四条是**命中集合**抓到的 |
| 3 | `retrieval.py`：共享条款从"按 spec 渲染"改成 `if True`（即检索也渲染第 3 条） | 9 条：上面四条 + `test_a_shared_personal_document_is_a_list_clause_and_not_a_retrieval_one` + `test_the_personal_document_clause_is_pinned_term_by_term` + 工单 35 三条 |
| 4 | `retrieval.py`：共享条款去掉 `clearance_level` 上限 | 2 条：`test_a_colleague_below_the_ceiling_is_refused_a_published_personal_document`（**第一版只抓到 `test_retrieval.py` 里那条子句级断言**，本工单自己那条因为只检查了子句文本里的密级项、没检查绑定值，漏了 —— 已补上"绑定值只含 `low` 且谓词文本里不出现 `'medium'`"，现在本工单自己的钉子也能抓到） |
| 5 | `answer/models.py`：`SourceNotice.of` 永远返回 `None` | 1 条：`test_an_answer_grounded_in_a_personal_document_carries_the_marker` |

突变 1 值得单独写一句：它**没有**让"同事问不到私密个人文档"那几条红，因为在当前 schema 下
（公司文档 owner 必为 NULL，数据库 CHECK 保证）"去掉 `NOT is_company_kb`"在**命中集合上**不改变
结果。真正会泄漏的是突变 2。两条的区别就是为什么第 1 条子句既要有谓词级钉子、又要点明它的门是
"关于个人文档"这句话本身 —— 而第 4 条又演示了"子句文本对、绑定值错"这种只在参数层看得见的突变
（工单 35 的原话：删掉一个子句、参数却还在）。

### 12. 验证

* 定向：`test_personal_documents.py`（23）、`test_retrieval_escalation.py`（12）、
  `test_retrieval.py`（27）、`test_answer.py`（26）、`test_documents.py`、
  `test_database_security.py`、`test_permission_matrix.py`、`test_access_kernel.py`、
  `test_document_access.py` —— 全绿。
* 全量（隔离 scratch 库）：
  `docker compose exec -T -e TEST_DATABASE_NAME=eam_test_t36 -e REDIS_URL=redis://redis:6379/13 api python -m pytest -p no:warnings`
  → **1364 passed**（23m57s）。工单 35 记录的全量是 **1339 passed**，本次多 25 条（新模块
  `test_personal_documents.py` 23 条 + 工单 35 套件新增 1 条 + `test_retrieval.py` 新增 1 条）。
* `docker compose exec -T api sh -c "uvx ruff check app tests --output-format concise"` clean。
  （`api/alembic/versions/` 不在该命令的路径里；该目录在本仓库本就有既存的 E501，本工单新增的
  迁移文件对 `ruff check <该文件>` 是 clean 的。）
* 迁移：在 scratch 库上跑过 `upgrade head → downgrade -1 → upgrade head`，`alembic current` 回到
  `0025 (head)`；写文件前重读 `api/alembic/versions/` 并跑 `alembic heads`，head 是 `0024`，
  取下一个空闲号 `0025`，没有动任何已冻结的 revision。
* 真实 PostgreSQL 与 Redis，没有 mock：嵌入器是 32 号工单的 `DeterministicEmbedder`（第二种适配器
  而不是 double），聊天模型是 34 号工单的 `StreamedChatModel`。
* scratch 库 `eam_test_t36`、`eam_test_t36_mut` 收尾时已 DROP。
* 改动文件的行尾已统一为 LF（仓库 `.gitattributes` 声明 `eol=lf`），`git diff --numstat` 中没有
  行尾噪声。

### 13. 客户端需要的新 `message_key`

**一个**：`answer.source_notice.personal_document`（个人文档标注）。

* `zh`：以下内容来自个人文档（非公司知识库）
* `es`：El contenido siguiente procede de un documento personal (no de la base de conocimiento de la empresa)
* `en`：The following content comes from a personal document (not the company knowledge base)

`web/lib/i18n/` 不归本工单改（交父工单）；API 侧不新增任何 `ERR_*` 错误码 —— 本工单的拒绝全部落在
既有的 `ERR_AUTH_002`（越权）、`ERR_DOC_001`（越界即"不存在"）、`ERR_VALIDATION_002`（可见性与
部门不自洽）上，没有第二种"权限拒绝"要客户端区分。
