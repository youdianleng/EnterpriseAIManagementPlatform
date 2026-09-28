# 37 — 问答界面与引用回链

**What to build:** 员工有一个像样的问答界面：左侧会话列表、右侧对话流、底部输入框。答案流式出现，引用可以点击并**跳转到原文的对应页**。历史会话能翻，也能自己删掉。

**Blocked by:** 36 — 个人文档与可见性

**Status:** done

- [x] 会话列表展示历史会话，可新建、重命名、删除；删除后本人不可再看到
- [x] 对话流支持流式渲染、Markdown 排版、代码与表格的正确显示
- [x] 引用以可点击的角标呈现，点击后在侧栏展示原文片段，并提供"打开原文"跳转到对应页码
- [x] 拒答的回答有明确的视觉区分，不与正常回答混淆
- [x] 界面明确显示回答所依据的范围（公司知识库 / 含个人文档）
- [x] 会话与消息保留 90 天后自动清除，界面上明确告知该期限 —— 本工单交付的是**告知**那一半：期限
      按行写在 `expires_at`（0024 已有）上，侧栏与打开的会话各显示一次。**"自动清除"本身是 51 号
      工单的清扫任务，目前不存在**：没有任何进程会删除到期的行，界面因此只说"到期日"与"90 天期限"，
      不说"已经删除"。
- [x] 用户可自行删除自己的会话；删除是即时的
- [x] 会话与消息**没有**对普通人力资源角色开放的入口；只有合规角色在留痕的前提下可查（该入口在后续工单实现）
- [x] 界面西/英双语，流式过程中切换语言不影响正在生成的回答

## 实现

### API：三个新端点（工单第一行）

- `api/app/api/v1/answer.py` — `GET /answers/conversations`（列表）、
  `PATCH /answers/conversations/{id}`（重命名）、`DELETE /answers/conversations/{id}`（删除）。
  三者与工单 34 的读回端点用**同一个** `session.read_own` 守卫：D18 把会话给它的使用者，
  内核目录里没有第二个动作可加——再写一个 `session.manage_own`，它的角色列表与
  `session.read_own` 逐字相同，那是把同一条规则写两遍。
- `api/app/domain/answer/repository.py` — `list_for` / `count_for` / `rename_for` /
  `delete_for` / `owns`。**归属是 `WHERE` 子句**，不是 handler 里的比较：每个方法都带
  `user_id`，`rename_for` 用 `RETURNING` 一次返回写进去的行，`delete_for` 用 `rowcount`
  回答"是不是你的"，所以"不是你的"与"不存在"无法区分——与读回端点的 404 同源。
  `list_for` 的排序是 `last_message_at DESC, created_at DESC, id DESC`：前两个是"最近"，
  第三个让顺序**全序**，同一毫秒落地的两行不会在两次读之间互换。
- `api/app/api/v1/schemas/answer.py` — `ConversationSummaryRead`（列表行**不是**
  `ConversationRead`：列表是标签，不该带上每段对话的全部消息）、`ConversationPageRead`、
  `ConversationRenameRequest`（StrictModel；空白标题在这里 422，而不是撞
  `ck_rag_conversations_title` 变成 500）。
- `api/app/audit.py` — 新增 `conversation.deleted`。**删除入审计，重命名不入**：重命名改的是
  只有本人看得到的标签；删除改变的是那一行**意味着什么**，之后读到它的合规人员必须能分清
  "所有者删掉了它"和"从来没有动过"。条目里只有谁、何时，没有标题也没有正文。
- **不需要迁移。** 0024 已有 `deleted_by_user`，0025 已有 `source_notice`；写之前重新读了
  `api/alembic/versions/`，head 仍是 0025。（**运行栈的开发库当时停在 0023**，即 0024/0025
  从未应用，`rag_conversations` 不存在；已 `alembic upgrade head`，这是环境状态而不是本工单的改动。）

**"删除"在 51 号工单之前是什么意思**：`deleted_by_user = true`，行还在。对使用者而言是**立即**
不可见——列表里没有（`list_for` 的 `NOT deleted_by_user`）、读回是 404（`load_for` 同样带这个
条件）、**连追问也进不去**（`ensure_conversation` 带同一条件，所以对一个已删会话提后续问题与对
别人的会话提问同样是 404），第二次删除也是 404（否则这个端点会变成"行还在不在"的探测器）。
行本身留给 D18 的 90 天期限与 §5.3 的合规读取；**物理删除是 51 号工单的清扫任务，尚不存在**，
所以界面与工单都不会承诺"记录已经消失"。

**`rag_messages` 的库级策略没有跟着 `deleted_by_user` 收窄**，这是有意的：那条策略是**第二道
防线**，它保护的是"别人读不到"，而不是代替应用实现软删除。所有者本来就对内容有阅读权，所以
策略放行它不构成泄露；反过来把 `deleted_by_user` 写进策略，就要求合规视图（48 号工单）绕过它，
那时同一张表上就有了两条互相否定的规则。

### 发现并修掉的缺陷（都在本工单的路径上）

1. **对一个"不再属于你"的会话提问会让流崩掉。** `ensure_conversation` 抛
   `ConversationNotFound`，而它在生成器**内部**——那时 200 已经发出，异常只能表现为一个失败的
   响应。现在 `ask_route` 在构造流之前用 `repository.owns` 再问一次（`answer_filter_for` 在
   这里已是同一形状的先例），因此常规答案是带状态码的 404；生成器里仍保留竞态分支，产出一个
   `ERR_RESOURCE_001` 的 `error` 帧，因为 200 之后再无状态码可用。删除让这条路径**第一次变成
   可达的**：第二个标签页可以在第一个追问时把会话删掉。
2. **语言切换原本会杀掉正在生成的回答。** `locale-switcher.tsx` 用的是普通 `<a href>`——整页
   导航。`fetch` 随文档销毁而中止，服务端生成器在 `is_disconnected()` 处 break，整个事务回滚，
   答案是**彻底丢失**。现在改成 `next/link`（§3.2 的「不整页刷新」第一次为真），并且顺带修掉
   第二个缺陷：签署后的外壳传的是 `pathWithoutLocale=""`，所以"EN"一直把人送到 `/en` 首页；
   现在目标路径由 `usePathname()` 推出，`/es/qa` → `/en/qa`。

### 前端：答案怎么渲染

- **`react-markdown` + `remark-gfm`**（新增依赖，见下）。模型输出**可能包含文档里的任何东西**，
  `dangerouslySetInnerHTML` 等于把语料当作 XSS 攻击面。`react-markdown` 渲染的是树而不是 HTML
  字符串，`rehype-raw` 刻意不装，所以 `<img src=x onerror=alert(1)>` 就是这些字符。
- **引用角标在 AST 上改写**（`web/lib/qa/citation-markers.ts`）：一个 remark 插件只访问 `text`
  节点，把 `[N]` 换成 `#cite-N` 链接。**不**用正则改原始 Markdown——那会改写代码块里的
  `array[1]`，把语料里的引文改坏；也只改写落在 `1..citations.length` 范围内的标记，模型自己
  编的角标保持原样。
- **拒答用目录键，不是帧里的 `content`。** `refusal_text()` 故意是**双语的**（一条字符串里同时
  有西语和英语），因为它是写给"没有字典的客户端"的；而读西语界面的人应该只被告诉一次。
  所以 `refusal` 帧存的是 `message_key`，句子取 `errors.knowledge_base_no_basis`——这条键本来
  就在两本字典里（工单 34 加的），本工单没有新造文案键。
- **范围横幅（`source_notice`）渲染在回答顶部**，读者语言直接取自载荷的 `text.es` / `text.en`
  （工单 36 的契约已带两种语言），`message_key` 只作兜底。拒答永远不带它。
- 拒答块是**正常状态**（设计系统 §4.4）：warning 色对、左侧色条、图标 + 标题 + 目录句子 +
  三条可执行的下一步；里面**没有** `role="alert"`——它不是错误。
- `web/lib/stores/qa-store.ts` — 见下节。
- 依赖安装：`web/package.json` 写入 `react-markdown@^9.0.1` 与 `remark-gfm@^4.0.0`；
  容器内 `docker compose exec -T web npm install --no-package-lock react-markdown@^9.0.1
  remark-gfm@^4.0.0`（宿主上为 `npm install --no-package-lock --no-save …` 供 `tsc`/`next build`
  使用）。**仓库里没有 `package-lock.json`，也没有生成**。

### 语言切换为什么不会打断流：store 而不是组件

清单行是「流式过程中切换语言不影响正在生成的回答」。这个产品里语言在 URL 上，切语言就是一次
导航；导航会重挂载页面的组件。所以四样东西放在**模块级 zustand store**（`lib/stores/qa-store.ts`）：

1. **正在生成的这一轮**，由 store 自己持有的回调逐帧更新——请求不挂在任何组件的生命周期上，
   屏幕卸载不会中止它；
2. **打开的是哪个会话、已读回的誊本**，所以切换后的新页面显示同一个会话、答案还在里面；
3. **输入框里的文字**（§3.2 的「不丢失已填写的表单内容」）；
4. **打开的是哪条引用**。

一轮用**消息 id 作键**：`start` 帧一到就重键（`applyFrame` 返回新键），因为 id 才是跨重挂载
认得出来的东西，也是读回时誊本用的 id。`start` 帧同时决定"这是不是一条新会话的第一个问题"，
store 在那里**立刻**把会话选中——否则这一轮会从"选中会话的 turns"里消失，答案要等生成完才出现，
那正是本工单要消灭的缓冲。

`visual-check.mjs` 对这一条的验证是：把回答的**字节按 8 字节 / 3ms 分段投递**，采样每一帧的 DOM 状态，
然后断言**渲染过程出现过严格前缀**——即读者在答案完成之前就看到了部分答案——并断言语言切换**没有
发出第二次请求**（3 → 3）。最终一次运行观察到 **9 个渲染状态**，8 个是严格前缀。

（仪器的两个坑都记在脚本注释里：`addInitScript` 只在**下一次导航**生效，所以它安装在第一次
`goto` **之前**——第一版装晚了，等于没装；以及它只包装 `POST /api/v1/answers` 这一个请求，因为
会话读回/重命名/删除共用同一路径前缀，被一起拖慢会让后面几步在超时边缘挣扎。）

### 测试

- `api/tests/test_answer.py` 新增 7 条：
  - `test_the_conversation_list_is_the_callers_own_and_newest_first`
  - `test_a_conversation_can_be_renamed_by_its_owner_and_keeps_its_transcript`
  - `test_a_conversation_that_is_not_yours_cannot_be_renamed_or_deleted`
  - `test_deleting_a_conversation_hides_it_from_its_owner_at_once`（含"追问 404"与"行仍在、消息仍在"）
  - `test_deleting_a_conversation_is_recorded_in_the_audit_trail`
  - `test_the_conversation_list_has_no_entry_for_another_role`（hr 与 **compliance** 都只看自己的）
  - `test_the_conversation_writes_carry_the_caller_in_the_where_clause`（在**owner 连接**上，见下）
- `api/tests/test_permission_matrix.py` — 新增三行（列表/重命名/删除，均为 `SESSION_READ_OWN`）、
  `RESOURCE_FREE_ROUTES` 加入 `/api/v1/answers/conversations`、`http_payload` 加一条重命名体，
  字面量 `checked == 78` → `81`。
- `web/scripts/visual-check.mjs` — 新增 `checkQa()` 与 `textPdf()`；`PATHS` 加入 `/qa`。
- 校验命令：`cd web && npx tsc --noEmit`（干净）、`npx next build`（编译通过，`/qa` 首屏 157 kB）、
  `npm run visual`（**ALL CHECKS PASSED**，39 条 `qa:` 断言）。

## 变异测试（每条新规则、失败测试名，然后复原）

在 `eam_test_t37` 上逐条改坏、跑、复原：

| 变异 | 失败测试 |
|---|---|
| `list_for` 去掉 `NOT deleted_by_user` | `test_deleting_a_conversation_hides_it_from_its_owner_at_once` |
| `list_for` 去掉 `user_id = :user_id` | `test_the_conversation_list_has_no_entry_for_another_role`（compliance 看到了别人的会话） |
| `list_for` 的 `DESC` 改成 `ASC` | `test_the_conversation_list_is_the_callers_own_and_newest_first` |
| `rename_for` 去掉 `user_id` | `test_the_conversation_writes_carry_the_caller_in_the_where_clause` |
| `delete_for` 去掉 `user_id` | 同上 |
| `delete_for` 去掉 `NOT deleted_by_user` | `test_deleting_a_conversation_hides_it_from_its_owner_at_once`（"第二次删除被接受"） |
| `ask_route` 去掉流前 `owns` 检查 | `test_deleting_a_conversation_hides_it_from_its_owner_at_once`（"已删会话接受了新问题：200"） |
| 去掉标题的 `_not_blank` 校验 | `test_a_conversation_can_be_renamed_by_its_owner_and_keeps_its_transcript`（空白标题到库 → 500） |

**两次变异暴露了真实的测试缺口，两条都补了测试**：

1. 去掉 `list_for` 的 `user_id` 后，原有的列表断言**全部通过**——因为
   `rag_conversations_read` 的政策是 `user_id = app_setting(...)`，数据库替应用兜了底。补的
   `test_the_conversation_list_has_no_entry_for_another_role` 用 **compliance** 账号取证：那条
   政策**放行** compliance 跨用户读，所以只有应用自己的 `user_id` 子句能挡住它——这正是清单行
   「没有对普通人力资源角色开放的入口」的直接验证。
2. 去掉 `rename_for`/`delete_for` 的 `user_id` 后仍然全绿，因为 `rag_conversations_update` 的
   `USING` 是同一条规则。于是补的
   `test_the_conversation_writes_carry_the_caller_in_the_where_clause` 在 `platform.factory()`
   （**owner 连接，不受 RLS 约束**）上直接调仓储：在这条连接上唯一能拒绝写的就是 `WHERE` 子句
   本身。第二道防线挡住同一条规则是好事，但它让"应用层也写了这条规则"变得无法观测，所以这条
   规则必须在第二道防线看不见的地方钉住。

复原后 `git diff` 只有本工单的改动，无变异残留；`uvx ruff check app tests` 干净。

## 浏览器验证

`npm run visual`（`EAM_USERNAME=devlead`）：**ALL CHECKS PASSED**，含 39 条新的 `qa:` 断言。
截图在 `.scratch/visual/`。逐张看下来**发现的真实缺陷**（全部已修，并已重跑确认）：

1. **引用列表在侧栏打开时散架**：每行是 `flex flex-wrap`，线程列只剩约 290px，于是编号与文字
   分成两行——一列孤零零的数字，来源印在下面。改成不带 `wrap` 的 `flex`：编号 `flex-none`，
   文字 `min-w-0`，两者不可能分离。（截图 `qa-citation-panel-es.png` 前后对比）
2. **侧栏宣称"你还没有会话"，同一屏上却有一条回答**：`router.refresh()` 是一个来回，`items`
   还没跟上。空状态现在要求**两边都空**（列表空 **且** 屏幕上没有答案），
   `data-testid="qa-conversations-empty"` 的探针确认修复后为 0。
3. **线程标题在列表跟上之前写"新会话"**：现在回落到**这个问题本身**（服务端的
   `title_for` 就是从它派生标题的），列表一到就替换——不复制 `title_for` 的截断规则。
4. **角标基线不对**：`align-baseline` 让 24px 的方框压在基线上，行距忽高忽低；改 `align-middle`。
   （24×24 正好是 WCAG 2.2 AA 的目标尺寸下限。）
5. **会话行的"重命名/删除"在手机上只有 36px 高**：`min-h-11 md:min-h-9`——手指在的地方 44px，
   平板以上保持 §1 的密度。
6. **PDF 夹具跨运行重复**（第二次运行 409）：印章原本只写在文档**标题**里，而内容哈希不看标题。
   印章改放进 PDF 的 `/Info` 字典，页面文本逐字不变，所以每次运行解析出的分块、答案与引用都一致。
7. **"流式渲染"一度测的是套接字而不是屏幕**：MutationObserver 一次运行看到 2 步、下一次 1 步。
   仪器换成受控的分段投递（见上），现在稳定观察到 9 个状态（8 个是严格前缀）。

**顺带发现，未修（不属于本工单）**：`visual-check.mjs` 里工单 21 的打卡检查有一条
`absent: /Todavía no has fichado/`，而**这条字符串在整个产品里不存在**（只有检查里有；屏幕渲染的是
`dayStatus.label.notStarted` =「Sin fichajes」）。它只在"马德里当天还没有任何打卡"时才会被执行，
而本会话跨过了马德里午夜（容器 UTC 22:05 = 马德里 09-29 00:05），于是第一次运行撞上了它并失败；
该检查自己的"打卡—下班"序列随后把当天关闭，所以下一次运行就恢复全绿（最终一次即为
**ALL CHECKS PASSED**）。产品的状态文案与设计一致，是检查的期望过期了。

## API 全量测试

```
docker compose exec -T -e TEST_DATABASE_NAME=eam_test_t37 -e REDIS_URL=redis://redis:6379/8 \
  api python -m pytest -p no:warnings
```

**1371 passed in 25:06**（退出码 0）。只加了 `-p no:warnings`，没有第二个 `-q`。

⚠️ 工单给的 Redis 库是 `/16`，但 `redis:7-alpine` 的 `databases` 是 **16**，索引 0–15，所以 `/16`
每次都让登录 500（`DB index is out of range`），全表皆红。这里用 `/8`（当时空闲的一个）。运行结束后
`eam_test_t37` 已按要求删除。

`docs/architecture/frontend-design-system.md` §8.2 逐条（本屏）：

- **主角明确**：主操作是唯一的 `Preguntar`（`size="md"`，44px），其余是次级/文字按钮。✅
- **三档层级**：h1 页标题 → h2 侧栏/线程 → h3 问题 → h4 来源。视觉上 20/16/14/13px。✅
- **网格对齐、留白**：4px 基准（`gap-6`/`p-4`/`mt-2`），无魔法值。✅
- **主色 ≤2**：primary + 语义色（拒答 warning、失败 danger、范围 warning），中性四档。✅
- **正文对比度**：正文 `--eam-fg` on `--eam-surface`；辅助文字用 `fg-muted`/`fg-subtle`，与既有
  页面同一组 token，未新增组合。✅
- **状态不只靠颜色**：拒答是图标 + 标题 + 文字；范围横幅是图标 + 句子；流式是 `role="status"`
  的文字；引用角标有数字与 `aria-label`。✅
- **正文 14px / 行高 1.5**：`.answer-prose` 明确设定；数字（页码、角标、计数）用 `.tabular`。✅
- **六态**：按钮复用 §2 组件（默认/hover/focus/active/disabled 由 `Button` 提供），输入框有
  `aria-invalid` + 描述错误；引用角标有 hover/`aria-expanded`/focus ring。✅
- **表单**：`<label for="qa-question">`、`aria-describedby` 提示、本地拒绝（空问题禁用提交）、
  重命名字段同样有 label 与错误提示。✅
- **空/加载/错误**：空对话有说明与下一步；`loading.tsx` 是骨架屏（含 h1，不跳级）；列表读失败是
  带重试的句子；誊本读失败同样是句子 + 重试；模型失败是 `role="alert"` + 重试。✅
- **两种语言各检查一遍**：`qa-answer-es.png` / `qa-answer-en.png`、`qa-refusal-es.png`、
  320/768/1280 的三档截图；无截断、无错位、按钮未被撑破（西语最长的一条是
  "Conversaciones en pantalla: 1 de 1"，单行放得下；拒答标题与三条下一步在 320px 下正常换行）。
  ✅
- **320/768/1280 三档**：320 与 768 是堆叠（列表在上、对话在下——§7「侧栏收起」），≥1024 才是
  两栏；打开引用时为三栏。无横向溢出（自动检查逐宽度断言）。✅
- **移动端触控 ≥44px**：主按钮 44px；会话行按钮在窄屏 44px；引用角标 24×24（WCAG 2.2 AA 目标
  尺寸下限，行内链接的合理尺寸）。✅
- **语义化、单一 h1、不跳级**：`header/nav/main/aside/section/article`；自动检查逐页断言。✅
- **图标有 aria-label**：关闭引用的按钮有 `aria-label`；装饰性 SVG 一律 `aria-hidden`。✅
- **键盘可完整操作**：Tab 到列表行按钮 → 打开 → 焦点进入重命名输入框（`inputRef`，为此给
  `TextField` 加了这一个属性）；角标是真正的 `<button>`；对话框是原生 `<dialog>`（焦点陷阱 +
  Escape）。✅
- **不禁用缩放、尊重 reduced-motion**：未改 viewport；本屏唯一的动效是过渡色。

## 仍然做不到 / 有意没做

- **"打开原文跳转到对应页码"的最后一跳取决于浏览器。** 链接是既有下载路由加锚点
  （`/api/v1/documents/{id}/content#page=N`，N 来自引用本身，无页码的格式不带锚点），
  自动检查断言了 href 的形状与"这个原文仍然打得开"。但该路由作答时带
  `Content-Disposition: attachment`——工单 31 有意为之，为的是不让浏览器用自己的阅读器渲染一个
  服务端没有检查过的文件。因此锚点是否真的翻页取决于浏览器如何处理这个下载；要让 `#page=N`
  端到端生效，需要让 PDF 以 `inline` 作答，那是 31 号工单的安全取舍，不是本工单可以单方面推翻的。
- **回答的语种由提问决定，界面语言不影响它**（§5.2）。开发栈的 chat 适配器是
  `StreamedChatModel`（`passage-quoting-v1`），它只会写英文模板句，所以西语提问的截图里答案句是
  英文——这是工单 34 记录过的夹具性质，不是渲染缺陷；真正被断言的是"引用原文逐字不译"与
  "界面文案跟随界面语言"。
- **合规角色的会话视图不在本工单**（48 号），本工单只保证**没有**那个入口：hr 与 compliance 打开
  问答界面看到的都是**自己**的会话，且都读不到别人的誊本（有测试按名断言）。
- **90 天清扫不在本工单**（51 号）。界面告知期限（侧栏一句 + 打开会话时的到期日，日期取自
  服务端的 `expires_at`），但到期行**目前不会被删除**，因为没有任何任务在做这件事。
- **两栏断点是 1024px 而不是 768px**：§7 把 768–1279 归入平板并写明"侧栏收起"，所以本屏在平板
  宽度下是堆叠而不是硬挤两栏。（`qa-768-es.png` 是这一状态。）
- **"90 天"这个数字在界面文案里写了一遍**（`qa.conversations.retention` 的 `{days}` 传 90），
  而 API 里的唯一来源是 `answer.models.RETENTION_DAYS`。两者今天一致（都是 D18 的 90），但改期限
  要同时改两处；接口没有暴露这个常量，所以本工单没有把它接到客户端。行级到期日取自服务端的
  `expires_at`，所以**显示的日期**永远与清扫将依据的日期一致——不一致的只会是硬编码的那句提示。
