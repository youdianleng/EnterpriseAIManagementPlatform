# 44 — 财务批量上传工资单与缺失清单

**What to build:** 财务选定月份，一次性上传该月全部工资单 PDF。系统给出两份清单：**已上传**的与**缺失**的（哪些员工这个月还没有工资单）。这样漏发能被当场发现，而不是等员工来问。

**Blocked by:** 43 — 薪酬档案

**Status:** done

- [x] 财务可按月份一次上传多个 PDF，系统通过文件名中的员工编号或界面选择完成归属匹配
- [x] 无法匹配到员工的文件被单独列出并说明原因，不静默丢弃
- [x] 上传完成后展示两张清单：成功归属的、以及该月**应有但缺失**的员工（依据在职状态与薪酬档案判定）
- [x] 同一员工同一月份重复上传被视为替换，界面明确提示将覆盖，并要求确认
- [x] 每个工资单记录文件大小与内容校验和，用于事后核对是否被替换
- [x] 工资单状态为"已发布"或"已撤回"，只有已发布的对员工可见
- [x] 上传完成自动通知相关员工并写入审计（记录上传人、月份、份数）
- [x] 只有财务角色能上传；人力资源与管理员上传返回 403
- [x] 缺失清单可导出，便于财务跟进

## 归属匹配：文件名或界面选择，以及两者冲突时

**规则在 `domain/payslip/matching.py` 一处，优先级写在那里，也写在这里。**

1. **文件必须是 PDF**——扩展名**和**前四个字节都要说 PDF（`%PDF-`）。扩展名是客户端选的，字节是文件本身的；改名成 `.pdf` 的 JPEG 被 `not_a_pdf` 拒绝，而不是存下来再交给员工一个打不开的文件。
2. **文件名按"整段字符"匹配，不做子串匹配。** 员工编号在库里是自由文本（`employee_private.employee_no` 是 `String(32)`，没有格式），所以规则不假设编号长什么样：它假设**文件名**是一串用分隔符隔开的字符，把编号折成纯字母数字，再在名字里找这一段，两侧都要求不是字母数字。
   - `nomina_E-0007_2026-03.pdf`、`nomina-E-0007.pdf`、`E-0007.pdf`、`e-0007.pdf` 都命中 `E-0007`：模式由**编号自己的字符**拼出、中间允许任意非字母数字，所以连字符既不是被切掉的字符、也不需要被特殊对待。
   - `E-00071` **不**命中 `E-0007`（后一位是数字，边界不成立）；`XE1001` 也不命中。子串规则两条都会命中——后果不是"漏了一份工资单"，而是**发错人**。
   - 规则用 `re.compile(r"[^A-Za-z0-9#]+")` 切名字的**报表**用途（扩展名判定、以及"这个看着像工号的串对不上任何人"的 detail）。这里踩过一个坑记在代码注释里：`[^0-9A-Za-z#]` 与 `[^A-Za-z0-9#]` 不是同一个字符类——前者把 `9-A`、`Z-a` 读成**区间**，于是连字符被吞掉，`E-1001` 被切成 `E`、`1001`。
3. **界面选择（`employee_id` 表单字段，与 files 同序）补文件名答不了的那些**：名字里没有编号的、名字里有**两个**编号的。
4. **文件名与选择"冲突"时拒绝，而不是按优先级归属。** 优先级本身只够"归给谁"，但选择是一次点击、文件名是薪酬外包实际写下的东西：一份名叫 `E-0007` 却被人选到 `E-0011` 的文件，是打错的概率远大于"覆盖文件名"。归给选择方会把一个人的工资单交给另一个人，**并且让第一个人出现在缺失清单里**——而这份清单的全部价值就是它说的是真话。所以这种文件以 `duplicate_for_employee` 拒绝，detail 里两个编号都写出来，由上传人决定。

**"不静默丢弃"是控制流的性质，不是复核者的注意力。** `service._prepare()` 里每个文件只有两个出口：归属给某人（进 `_Prepared.attributed`），或者进 `unmatched` 并带一个原因——没有第三条分支、没有 `continue` 把它丢掉、没有异常跳过它。提交阶段走的正是这两张表。响应里 `partitioned` 是服务端自己的核对，`tests/test_payslips.py` 在它构建的**每个**批次上都断言"两清单加起来等于送进来的文件数"。

八种原因是一个**封闭词表**（`UnmatchedReason`，没有 `unknown`）：

| token | 什么时候 |
|---|---|
| `no_employee_number` | 名字里没有任何工号，也没指定人 |
| `unknown_employee_number` | 名字里那个像工号的串不在册（detail 里写出是哪个） |
| `ambiguous_employee_number` | 名字里有两个不同工号 |
| `not_a_pdf` | 扩展名或前几字节说它不是 PDF |
| `oversized_file` | 超过 10 MiB（detail 写出实际字节数） |
| `empty_file` | 零字节 |
| `duplicate_for_employee` | 同一次上传里同一员工出现两次（detail 写出第一份的名字） |
| `duplicate_file` | 同一次上传里同一份字节出现两次 |

`unknown_employee_number` 与 `no_employee_number` 的分界用了**公司自己的数据**：候选串的"形状"（`A`/`0` 掩码，`E-9999` 是 `A-0000`）必须与在册工号的形状之一相同。这样 `nomina_2026-03.pdf`（`0000-00`）报 `no_employee_number` 而不是让财务去找一个工号为 `2026` 的人——这个"细节写错比不写更糟"的判断有测试钉住（`test_a_filename_is_looked_up_as_a_whole_name_not_by_substring`、`test_the_number_rule_is_whole_token_and_case_insensitive`）。

## 缺失清单从哪来：薪酬档案 + 在职状态，且经由 `SalaryService.read`

**两个条件都要满足，而且两个都问在正确的地方。**

- **在职那一半**在 `repositories/payslip.py::candidates()` 里，而且是**按日期**问的，不是按 `status`：`hire_date <= 月末 AND (termination_date IS NULL OR termination_date >= 月初)`。3 月 20 日离职的人 3 月是有工资单的，去年离职的人没有；只看 `status = 'terminated'` 会把前者答错。上端闭合，和薪酬档案的窗口规则（`domain/payroll/windows.py`）同一条。
- **薪酬在册那一半**必须是「那个月当时有效」。**这一问只能走 `SalaryService.read(...)`**：薪酬行只装在 `SalaryReading` 里，而它的构造函数拒绝一切不是该模块签发的实例——所以"绕过去"不是纪律问题，是没有行可拿。于是这一遍推导为每个在职员工留一条 `salary.record_read` 审计（`as_of` = 当月 1 日，`limit=1`）。这正是这次访问应得的轨迹：**"财务在归档 3 月时，看了每一位应有员工的档案"**，而这是任何"路由顺手 `record()` 一次"的写法都捕捉不到的事实。
- **`missing` 只剔掉 `published` 的行**，不是"有这个员工的行就行"：§7.5 说撤回的工资单员工看不见，所以被撤回的人**重新变成缺失**。数据库策略和这条查询读的是同一个字段，所以两者不会各说各话。
- **列表是活的，批次行是当时的。** 每一次读都重新推导（今天录入的薪酬记录、今天生效的离职都会改变它），而 `payslip_batches.missing_employee_ids` / `.unmatched` 存的是**上传当时告诉上传人的那份**——两张表回答两个不同的问题，一个是"现在谁还缺"，一个是"财务当时看到的是什么"。

**一个只有实现的人才知道的坑，写在 `service._recontextualise()` 的 docstring 里**：`apply_rls_context` 用 `set_config(..., is_local => true)` 发布行的上下文，这个作用域是**事务**的；而 `SalaryService.read` 每次调用都 commit（这就是"每次读取都有自己的审计条目"的实现方式）。所以第二个人开始读的时候上下文已经没了，策略对一位财务答「没有行」——**静默地**，看起来就像一个正常答案。这个方法在每次已知的 commit 之后重新发布调用者，`upload` 里批次的 INSERT、推导之后的写入、以及每条通知之后都调一次（通知服务也是一次一条事务）。

## 替换、校验和、状态

- **`(employee_id, period)` 唯一**，所以"这个月这个人哪一份"只有一个答案。重复上传是 `INSERT … ON CONFLICT ON CONSTRAINT uq_payslips_employee_period DO UPDATE`：校验和、大小、存储键、原始文件名、批次一起走，`created_at`（这个槽位是什么时候开的）和 `download_count`（45 号工单已经交出去的那几份）**不动**。读-再-写会是两次上传之间的竞态，输的那一方才是一年后员工拿到手的那份。
- **校验和用在哪，说清楚**：`content_sha256` 与 `file_size` 是票面「用于事后核对是否被替换」的那两样。响应里替换过的那份带 `replaced: true` 加 `previous_sha256` / `previous_file_size`，所以**替换是可核对的，不只是被告知的**；磁盘上的键就是 `<sha256[0:2]>/<sha256>.pdf`，行里的哈希和文件本身是同一次摘要的两种读法（`test_the_stored_checksum_is_the_hash_of_the_bytes_on_disk` 把三者对起来）。
- **`status` 是 `published` / `withdrawn`，CHECK 是一条**；撤回行必须同时有时间和理由、已发布行两者都必须为空（`ck_payslips_withdrawal_is_stated`）——这条 CHECK 现在就写，是为了让 46 号工单第一次撤回**无法**留下一条没有理由的行（事后补的规则需要能回溯的旧数据，而它恰恰没有）。
- **只有 `published` 对员工可见，而这条在数据库里**：`payslips` 的 SELECT 策略是 `(employee_id = me AND status = 'published') OR roles && ARRAY['finance']`。所以「已撤回」对本人是**任何**读取路径都看不见的，包括以后有人写了一条忘了过滤的查询。测试用**受限角色**加上本人的上下文来跑（表的所有者对自己的策略免疫，用服务的连接跑等于什么都没验）。
- **行的"身份"不可变，文件本身不是**——这是一处改过的地方，改的理由写在迁移文件里：触发器先只护住 `id / employee_id / period / created_at / uploaded_by_user_id`（谁的、哪个月的、什么时候开的槽、哪个登录开的），**不**护 `storage_path` / `content_sha256` / `file_size`，因为票面的替换规则**就是**这三列的 upsert；第一版把这三列也冻上，结果把一次合法的重复上传拒了，这就是这条分界的来历。`DELETE` 对两张表都 revoke 掉。
- **`payslip_batches` 需要一条 UPDATE 策略**，这也是一个只有跑起来才知道的点：批次行先写、计数后填，而 RLS 打开却没有 `FOR UPDATE` 策略时 PostgreSQL 拒绝**所有**更新——而且是报告 0 行而不是报错，症状就是一个说"这个月传好了"却什么都没装的批次。

## 发布、通知、审计，以及只有财务

- **通知**：每个被归属的人一条，`NotificationType.PAYSLIP_PUBLISHED`，payload 只有月份。通知是**指针**，数字在文件里；去重键是批次——同一个月重传是**新**通知（那个人的确换了文件），一次请求的重试被抑制。被拒绝的文件不发通知：告诉某人工资单好了、其实没好，比什么都不说更糟。前端 `notifications.ts` 的 `TITLE_KEYS` 和两本字典都加了这一条（`payslips` 的 keys 在标题键表里有测试覆盖的意义：服务端只发 key，从不下发句子）。
- **审计**：每次上传**一条**（不是每个文件一条）`payslip.uploaded`，记 **actor、月份、四个数**：请求带了几份、归属了几份、拒绝了几份、还缺几人。**没有金额、没有币种、没有文件名**——金额在 `salary_records` 后面那道更窄的策略里，而 `audit_log` 是追加式、留 4 年、compliance 可读的；文件名不带是因为薪酬外包的命名里有工号，而拒绝的文件名已经存在批次行的 `unmatched` 里。导出写 `data.exported`，带月份、行数、以及**这份文件不带金额**这一条（读者事后无法自己核对的性质，正是要写下来的）。
- **只有财务**：不是处理器里的 `if`。catalogue 两个 action —— `payslip.manage`（上传 + 两张清单）、`payslip.export`（缺失清单文件）—— 角色集合都只有 `finance`，`ResourceKind.PAYSLIP` 是新的 kind，内核的 `_can_on_payslip` 是纯角色判断（不看行、不看部门：经理和同事同部门，这条规则根本不问部门）。**hr 与 admin 是按名字被排除的**，测试把这个"缺席"直接断言出来（`PAYSLIP_COMPANY_ROLES & {hr, admin} == ∅`，以及两个 action 的 `rule.roles`），而不只是断言"调用被拒"——因为放宽角色集合之后，一个记得问内核的处理器照样拒 HR，只有这条断言会红。数据库侧的策略是从同样的角色名写的（不是 `app.is_privileged`：那个对 compliance 也是真，而 §4.1 给 compliance 的是审计日志）。
  - 导出单独一个 action，理由和 26 号工单的加班导出一致：文件带**每一位缺失员工的工号**（withheld field），有些安装会想把"看屏幕"和"把一份薪酬跟进文件交出去"分开授予。

## 工资单绝不进入语料库

**决定是"不放进那张表"，不是"打一个标记"**，理由是一行 SQL：

- `repositories/retrieval.py::visible_document_clauses` 的第 1 条是 `NOT d.is_company_kb AND d.owner_employee_id = :filter_employee_id`。也就是说，一份**个人文档**的正文**对它的所有者是可召回的**。工资单必须**任何人**（包括本人）都召回不到——而 `documents` 上没有哪个标记能表达"连本人也不行"：`is_company_kb` 把文档送到**更宽**的规则，从来不会更窄。36 号工单那套机器在这里帮不上忙，这一条写在 `app/models/payslip.py` 的 docstring 里。
- 所以：文件走**薪酬模块自己的存储根**（`PAYSLIP_STORAGE_PATH=/data/payslips`，compose 里是独立卷），行写进 `payslips`，`documents` 里一个字节都不加，永远不产生 chunk。检索路径读的是 `document_chunks ⋈ documents`，它到不了工资单——对谁都到不了。
- **证据走检索路径而不是断言一处缺失**（`test_a_payslips_text_cannot_be_retrieved_by_anybody`）：PDF 里写一个唯一标记，然后用**本人 / 财务 / 另一个员工**三份 `answer_filter_for` 的主客体去搜。**对照组是必须的**：同一句话写进一份普通个人文档，用同一个调用能被它的所有者搜到——否则一个"什么也搜不到"的检索会让这个测试**空过**。（第一版的对照组是 `.txt`，解析器切不出 chunk，测试确实是空过的；第二版换成 `.md` 并显式断言"对照组有 ready 的 chunk"。）再往下断言 `documents` 只有那一行对照、其它 chunk 数为 0，而工资单文件在磁盘上、行在 `payslips` 里——被排除的是**可检索性**，不是上传本身。
- **变更测试**把这条反过来验：把工资单当作普通个人文档 `ingest` 进 `documents`，这个测试立刻红（见下）。

## 界面（§6.3）

`web/app/[locale]/(app)/payslips/`：`page.tsx`（服务端读缺失清单与可选员工表，首屏就有）、`payslip-screen.tsx`（客户端）、`loading.tsx`（骨架，§4.3）。

- **两张清单同时呈现**（§6.3 第一条）：一次响应里的 `attributed` 与 `missing`，两个 section 在同一页上，中间不需要点击。第三张 `unmatched` 也在一起——§6.3 第二条说的是"单独列出并给原因"。
- **缺失清单视觉上醒目**，而且是**量出来的**而不是"类名对了"：`<section>` 有独立的底色（`bg-warning-bg`，实测 `rgb(253,243,226)` 与另一个 section 的透明底色不同）加 2px 警告色边框，标题字号不小于"已分配"那节的标题。视觉自查读的是 computed style，所以一个不再渲染的样式表会让它红。导出链接就在这一节里。
- **未归属文件带原因**：原因词表整本译进两本字典（`dict.payslips.reasons`，封闭的 8 项 + `unknown`），所以服务端只发 token、句子是读者的语言；一个更新的服务端发来第七个 token 会退化成一句通用话，而不是空格子。
- **覆盖必须用文字确认**（§6.3 第三条）：`POST /batches` 带 `confirm=false` 是**试运行**——匹配做完、两张清单答出来、**一个字节都不写**（没有批次行、没有文件、没有通知、没有审计）。响应里 `reserved_count` 就是"会覆盖几名员工"，对话框据此写出「Se van a reemplazar las nóminas de 1 empleado(s) de 2026-08」；点确认才发第二个同样的请求（`confirm=true`）。取消之后月份和原来一模一样——这正是"确认"有意义的条件，也是它和"点一下继续"的区别。
  `reserved_count` 在**两次响应里是同一个意思**："这个月、这些人原本就有的工资单有几张"——它是关于**月份**的事实，不是关于"流程走到哪一半"的事实。两半都已经读到过这些行（试运行先读、提交在写的同一处读），所以没有第二个问题要问。真正在变化的是 `replaced_count`："这个回答对这些行做了什么"——试运行是"将会覆盖 1 张"，提交是"已经覆盖 1 张"。这个区分是被一个**把两次响应放在一起比**的测试逼出来的：第一版只在试运行里报这个数，于是 `reserved_count: 0` 会和 `replaced_count: 1` 并排出现，确认过的人无法判断替换到底发生了没有（`test_the_dry_run_presents_the_overwrite_without_writing_anything` 断言两半的 `reserved_count` 相等）。
- **权限被拒是一个状态，不是坏掉的界面**：不是财务的人打开这一页看到的是 `dict.payslips.permission` 的整句话，且**看不到**任何上传控件。导航里这一项只对持有 `finance` 的会话出现（会话现在带 `roles`），所以不会有人被广告到一个会拒绝他的入口——§4.1「界面不显示读者打不开的入口」。

## 结构落位

```
api/alembic/versions/20261011_1000_payslips.py   0029：payslips / payslip_batches、CHECK、
                                                 身份触发器、RLS（读/写/改）、REVOKE DELETE
api/app/models/payslip.py                        两张表的 ORM + 列清单
api/app/domain/payslip/
  errors.py     PayslipErrorCode（别名到 ERR_PAY_005..009）
  models.py     Payslip / AttributedPayslip / MissingEmployee / UnmatchedReason（封闭词表）/
                BatchOutcome（partitioned()）/ parse_period / period_bounds
  matching.py   整段匹配、编号折叠、FileVerdict/Resolution、duplicate_positions
  export.py     缺失清单 CSV 的列、排除项、render/file_for
  service.py    一次上传（dry run + 提交）、活的缺失清单、导出、_recontextualise
api/app/repositories/payslip.py                   candidates / employee_refs / upsert /
                                                  batches / finalize_batch
api/app/api/v1/payslips.py                        五个路由 + 读写 schema
```

改动到的既有文件：`domain/access/permissions.py`（两个 action、`PAYSLIP_COMPANY_ROLES`、
`PAYSLIP_CROSS_ACTIONS`）、`domain/access/kernel.py`（`ResourceKind.PAYSLIP`、`_can_on_payslip`、
`filter_for` 分支）、`audit.py`（`payslip.uploaded` 的完整注释）、`core/errors.py` + `core/messages.py`
（五个 `ERR_PAY_*` 与两本目录）、`domain/notification/models.py`（`PAYSLIP_PUBLISHED` + 标题键）、
`main.py`、`models/__init__.py`、`api/v1/auth.py` + `schemas/auth.py`（会话多带 `roles`）、
`config.py`（`payslip_storage_path`）、`docker-compose.yml`（`PAYSLIP_STORAGE_PATH` + 卷）、
`tests/conftest.py`（本次运行自己的工资单存储根）、`tests/support/platform.py`（wipe 列表加两张表）。

前端：`web/lib/api/payslips.ts`（新）、`web/app/[locale]/(app)/payslips/`（新，三个文件）、
`web/lib/i18n/index.ts` + `messages/{es,en}.ts`（`nav.payslips`、`payslips` 整节、
`notifications.titles.payslipPublished`）、`web/lib/api/notifications.ts`（标题键）、
`web/lib/api/auth.ts`（`roles`）、`web/app/[locale]/site-header.tsx`（只对 finance 显示的入口 +
`roles` 参数）、`(app)/layout.tsx`（把会话的 roles 传下去）、`web/lib/ui/alert.tsx`
（转发 `data-testid`，供视觉自查定位）、`web/scripts/visual-check.mjs`（`/payslips` 进 PATHS +
`checkPayslips`）。

## 权限矩阵：移动的字面量

| 断言 | 旧 | 新 | 为什么 |
|---|---|---|---|
| `assert len(cases) == 7 * N * 13` | `7 * 60 * 13` | `7 * 62 * 13` | 新增两个 action：`payslip.manage` / `payslip.export` |
| `assert checked == N`（HTTP 矩阵） | `87` | `91` | 新增四个路由：`GET /payslips/missing`、`GET /payslips/missing/export`、`GET /payslips/batches`、`POST /payslips/batches` |

`DESIGN_GRANTS` 两行（两个 action 都是 `{"finance"}`——**hr 与 admin 缺席是票面的原话**）、
`KIND_FOR_ACTION` 两行都指向新的 `ResourceKind.PAYSLIP`、`design_says` 加**一整块**领工资单策略
（不能落进档案那一块：那一块会答"HR 也能"）、`RESOURCE_FREE_ROUTES` 加 `missing` 与 `missing/export`
（空月份是"一个头、零行"，这一层能精确断言）、`http_payload` 加 `POST /payslips/batches` 的 body
（**故意不是合法的 multipart**：合法的 body 会真的落一份工资单，这一层只问守卫有没有接上目录），
`missing` 两行由循环补上 `period` 参数（`MATRIX_PAYSLIP_MONTH`），否则路由先给 422、被矩阵记成
"端点是死的"。

`GET /payslips/employees` **不在 HTTP 矩阵里**：它和 `missing` 同一个 action、同一层守卫、同一组参数，
矩阵里多一行不会多知道任何东西。它由 `test_payslips.py` 的插入路径间接覆盖（那张表是插入选择器的
数据来源），而它的**权限**由内核矩阵覆盖。

## 验证

- `uvx ruff check app tests` → **All checks passed!**
- `cd web && npx tsc --noEmit` → 干净；`npx next build` → 干净，`/[locale]/payslips` 在路由表里。
- 目标运行（scratch 库 `eam_test_t44` / `eam_test_t44b` / `eam_test_t44r`、Redis 7 号库）：
  - `pytest tests/test_payslips.py` → **57 passed**（整模块，不带 `-k`）
  - `pytest tests/test_permission_matrix.py` → **35 passed**
  - `pytest tests/test_architecture_constraints.py tests/test_errors.py tests/test_error_aliases.py
    tests/test_notifications_api.py` → 全绿（`test_architecture_constraints` 会读域目录，
    新增 `domain/payslip/` 也是它的一条断言）
- **全量运行**（scratch 库 `eam_test_t44s`、Redis 7 号库，一次只跑一个）：
  `docker compose exec -T -e TEST_DATABASE_NAME=eam_test_t44s -e REDIS_URL=redis://redis:6379/7
  api python -m pytest -p no:warnings` → **1625 passed**（1:10:12）。基线 1561，差 64。
  **一次同库并发跑出来的红不算数**：那一轮（`eam_test_t44full2`）出现 `DeadlockDetected`
  与一批 `ERROR`，原因是同一个库上还有另一个 pytest 在跑——scratch 库必须一人一个，这条
  纪律本身也是这轮验证学到的。
- **浏览器**：`node web/scripts/visual-check.mjs`（宿主，Chromium，finance 会话）。
  工资单相关的每一条都过；320/375/768/1280、es/en 两语的截图在 `.scratch/visual/payslips-*.png`
  （`payslips-es`、`payslips-en`、`payslips-320-es`、`payslips-768-es`、`payslips-overwrite-es`），
  逐张读过。跑完后自查里仍有 4 条**与本工单无关**的红：`leave` 两条、`timesheets` 一条、
  `draft`/`decide` 的 fixture 两条——它们在开发库上就红（fixture 依赖 `seed_timesheet_demo.py` 与
  agent draft fixture，本工单没有碰），工资单的 39 条断言全绿。
- **视觉自查发现的两个只有浏览器才会暴露的问题，都修了，也都在注释里留了话**：
  1. 客户端请求没带会话——`signIn` 返回的 cookie 是**host-only 指向 web 源**的（Server Component
     是手工转发它的），而浏览器从 `localhost:3000` 取 `localhost:8000` 不会带上别的 host 的 cookie。
     于是**服务端渲染的那一半**照常绿，而两张清单在客户端读的那一半显示错误状态。
     `checkPayslips` 现在给 API 源也放一份同样的 cookie（`apiCookie()`），注释说明真实浏览器也是这个
     行为。
  2. `page.tsx` 一开始用 `lib/api/client.request()` 读服务端——它在浏览器是对的，在 Server Component
     里每次都 `fetch failed`（同一个 URL、同一个 cookie，直接 `fetch` 两次都 200）。服务端读改成页面
     自己的三行 `fetch` + 显式转发 cookie（和 `readServerDocuments` 同形），并保留一次重试。

## 变更测试（mutation）

脚本 `api/tests/tools/mutate_t44.py`（**临时，已删除**）：每条规则打断一次、跑**一个** node id、
`finally` 还原。结果：**6/6 全部被抓住**；复核之后补做的第 7 条也抓住了（见下）。

| 被破坏的规则 | 破坏方式 | 失败的测试 |
|---|---|---|
| **只有财务能上传** | `PAYSLIP_COMPANY_ROLES` 加进 `hr` 与 `admin` | `test_the_two_payslip_actions_are_finances_alone_in_the_catalogue` |
| **不静默丢弃** | `_outcome` 里 `unmatched` 恒为 `[]` | `test_the_four_file_refusals_each_report_their_own_reason` |
| **缺失清单的"在职"那一半** | `candidates()` 的日期条件换成恒真 | `test_somebody_terminated_before_the_month_is_not_expected` |
| **替换** | `_store` 里 `replaced=False` | `test_a_re_upload_replaces_the_payslip_and_says_what_it_replaced` |
| **只有已发布可见** | 从**数据库**里重建 `payslips_read` 策略，去掉 `status = 'published'` | `test_only_a_published_payslip_reaches_its_owner` |
| **工资单不进语料库** | 把工资单 `ingest` 成一份普通个人文档 | `test_a_payslips_text_cannot_be_retrieved_by_anybody` |
| **`reserved` 两半同义** | 提交路径的 `reserved=reserved` 改回 `reserved=()` | `test_the_dry_run_presents_the_overwrite_without_writing_anything` |

第 7 条是**复核发现的缺陷**，不是自查发现的：`reserved_count` 第一版只在试运行里报"会覆盖几名
员工"、提交时恒为 0，于是 `reserved_count: 0` 会和 `replaced_count: 1` 并排出现，确认过替换的人
反而读不出替换有没有发生。修法是给这个词**一个意思**（"这个月、这些人原本就有的工资单"，两半都
报它），并让那个"把两次响应放在一起比"的测试把它钉住——补做的破坏证明这条钉子是真的（§6.3 的
第三条要求"明确提示将覆盖"，一个在确认之后立刻改口的字段等于没有提示）。

两条记录：**第一条一开始没被抓住**，因为"HR 上传返回 403"的端点测试在内核答"可以"之后照样绿（处理器
确实问了内核，内核改口了）——所以拒绝被钉在**规则所在的层**：`PAYSLIP_COMPANY_ROLES & {hr, admin} == ∅`
和两个 action 的 `rule.roles` 也逐字断言，重跑才抓住。**策略那一条的破坏只能在数据库里做**（迁移文件
改了没用，测试库已经在 head 上，被断言的策略活在**数据库**里），所以脚本那一处用
`DROP POLICY` / `CREATE POLICY` 重建，跑完立刻还原；`psql` 二进制镜像里没有，改用 `psycopg`——
第一版用 `psql` 就是在这一条上崩的。

## 有意没做 / 边界

- **没有做 45/46/47**：自助下载、撤回的动作与通知、培训/导出/留存。本工单**存了** `withdrawn_at` /
  `withdraw_reason` / `download_count` 三列、写了"撤回必须说明时间与理由"的 CHECK、也建好了
  `payslip.withdrawn` 这个 action 与通知类型（都在目录里、注释指向 46），但**没有任何写入路径**——
  撤回是 46 的动作，`test_only_a_published_payslip_reaches_its_owner` 用直接 SQL 造出撤回行来验证
  可见性规则。
- **没有给员工看的端点**：`payslip.read_own`（或等价物）是 45 号工单的，本工单不做；所以
  「只有已发布的对员工可见」是在**数据库策略**和**缺失清单的剔除条件**两处成立的，不是靠一个还不存在
  的路由。
- **不解析工资单内容**：不算金额、不算合计、不算税、不做汇总。文件被存储、被校验和、被交回；模块里
  没有一条路径能产生一个数字（D9、§8.3）。
- **没有货币换算、没有按周期折算**：`pay_period` 只在薪酬档案里做标签，本工单完全不读它。
- **没有把 `period` 做成日期**：月份是 `YYYY-MM` 字符串，因为被命名的是一个**周期**而不是一天。
- **没有做"删除工资单"**：`DELETE` 在两张表上都被 revoke；撤回是状态，行留在状态后面。
