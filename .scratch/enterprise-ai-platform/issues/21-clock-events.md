# 21 — 打卡事件流与当日记录

**What to build:** 员工在网页上点"上班"和"下班"。每次点击都作为**不可变事件**落库，当天的工时由这些事件推导出来。已写入的打卡事件任何人都无法修改或删除——包括人力资源——这正是西班牙工时记录义务所要求的形态。

**结构约束:** 接口只接受与返回**日期**，从不暴露原始时间戳用于日期聚合（`docs/architecture/codebase-design.md` §2.4）。时区与业务日转换是本模块的内部实现——调用者根本没有机会混淆 UTC 与业务日。

**Blocked by:** 15 — 权限矩阵与越权测试套件

**Status:** done

- [ ] 打卡事件含：员工、事件类型（上班 / 下班）、发生时刻（UTC）、**业务日期**（马德里当地的日历日）、来源、IP、创建者
- [ ] 业务日期在写入时一并确定，读取与聚合一律使用它，绝不从 UTC 时刻反推
- [ ] 跨午夜的班次（晚 22:00 上班、次日 06:00 下班）归属规则被明确定义并测试
- [ ] 夏令时切换日的打卡归属正确（马德里每年两次切换，需有针对性的测试）
- [ ] 打卡事件表在数据库层禁止 UPDATE 与 DELETE，任何修改尝试失败
- [ ] 重复点击（网络重试导致的 1 秒内重复）不会产生两条事件
- [ ] 员工当天状态在界面上实时可见：已上班计时中 / 已下班 / 今日未打卡
- [ ] 员工只能为自己打卡，为他人打卡返回 403 并留审计
- [ ] 打卡接口在 200ms 内返回

---

## 前端实现

**新增**：`web/app/[locale]/(app)/clock/`（`page.tsx` 服务端组件 + `clock-screen.tsx` 客户端 +
`loading.tsx` 骨架屏）；客户端 `web/lib/api/attendance.ts`、`web/lib/api/schedule.ts`；
共享件 `web/lib/ui/status-badge.tsx`（图标 + 文字 + 颜色）、`web/lib/ui/attendance-words.ts`
（状态/异常文案的唯一映射）、`web/lib/format/day.ts`。导航条目在
`web/app/[locale]/site-header.tsx`。

**关闭的清单行** —「员工当天状态在界面上实时可见：已上班计时中 / 已下班 / 今日未打卡」：
三种状态直接用 API 自己推导的 `day.status`（`working` / `ok` / `absent`），不做客户端二次推导；
界面是「图标 + 文字 + 颜色」三者齐备的徽章，颜色从不单独承担状态（§5）。`holiday` / `non_working`
这两个「无人期待」的日子同样有词，`missing_out` / `incomplete` 两个异常态也一样。
计时中的秒表只认 `GET /attendance/punches` 返回的 `punches[]` 与 `effective_at`（当天真正读的那个值），
因此「界面显示的时长」与「API 算出的时长」不可能各说各话。

**状态覆盖**
- 空：当天无打卡 → 中性提示 + 下一步（不是空表格）。
- 加载：`loading.tsx` 骨架屏，形状与答案一致，无动画。
- 错误：读失败 → 标题 + 可执行说明 + 重试。
- 权限拒绝：写被内核拒绝时显示目录里的双语句子；为他人打卡的入口界面上不存在，只会以 403 呈现。
- 设计态（刻意不当错误）：`ERR_ATT_001`「已有打开的班次」（陈旧标签页重复点击）以信息色呈现并把按钮
  切换成下班；`expected_minutes === null`「今天没有班次」以信息色说明；`missing_out`（班次超过
  `MAX_SHIFT`）**不画下班按钮**——API 会用自己的错误码拒绝——改为说明 + 指向「我的考勤」更正流。
- 独立 `h1` + 每区块 `h2`，无跳级；`⌘` 计时器 `role="timer"` 且不在 live region 内（每秒播报比不播报更糟）。

**API 调用**：`GET /attendance/day`、`GET /attendance/punches`、`GET /schedules/mine`（首屏服务端并发）；
`POST /attendance/clock`（唯一写操作，不传 `at`：时刻由服务端决定）。

**§8.2 自查**：全部通过，两处按项目口径判定——(1)「移动端触控目标 ≥ 44px」实测 320px 下主按钮 44px；
(2)「两种语言各检查一遍」在 320/375/768/1280 四档、西/英两语下逐页断言无截断、无横向溢出、控件皆有可访问名。
六态（默认/hover/focus/active/disabled/错误）由 `lib/ui/button.tsx` 与全站 focus ring 提供。

**验证**：`npx tsc --noEmit` 干净；`npx next build` 成功（`/[locale]/clock` 4.35 kB）；
`npm run visual`（`EAM_USERNAME=devlead`）全绿，其中本屏 18 条断言，截图
`.scratch/visual/{320,375,768,1280}-{es,en}-clock.png`、`clock-working-es.png`、`clock-confirm-es.png`。
看图后改了两处：下班按钮原为次级样式、与「主操作只有一个」不符，已改为主色；页头因导航增至八条而在
1280 下折行错位，已改为「品牌/语言/身份一行 + 导航一行」。

**已知边界**：`clock` 接口只关闭**当前**打开的班次（`_day_of_punch` 取全流最新一条打卡），
因此无法用它补写历史日期的下班卡——那是更正流（票据 24）的职责，本屏不提供该入口。

