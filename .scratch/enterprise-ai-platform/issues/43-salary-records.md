# 43 — 薪酬档案

**What to build:** 人力资源能维护每位员工的薪酬档案：基本工资、津贴明细、调整历史。这份数据只有人力资源、财务和员工本人可见——经理看不到下属薪资，系统管理员也看不到。

**Blocked by:** 15 — 权限矩阵与越权测试套件

**Status:** done

- [x] 薪酬记录含：员工、生效起止日期、基本工资、币种、发放周期、津贴明细（结构化）、调整原因、录入人
      — `test_the_exact_figure_survives_storage_and_read_back`（八个字段逐个出现在 `GET
      /api/v1/salary/records/me` 的 201/200 响应里，并逐个回读 `salary_records` 的列）、
      `test_allowance_amounts_are_strings_all_the_way_down`（`components` 是 JSONB **数组**，
      每行 `{code,label,amount}`，落库后逐字相等）、
      `test_writing_is_hrs_and_the_actor_comes_from_the_session`（`created_by_user_id` 是会话里的登录，
      不是请求体字段——"录入人"是事实不是声明）。
- [x] 同一位员工的历史记录形成按时间排列的调整链，任一时点可查出当时有效的值
      — `test_the_chain_is_ordered_and_any_instant_is_one_query`（三条记录，链按 `effective_from`
      升序；五个时点逐个比对 `GET /api/v1/salary/records/as-of` 的答案与
      `domain/payroll/windows.in_force` 这个**独立谓词**——查询不能拿自己当参照）、
      `test_the_window_rule_is_inclusive_at_both_ends`（`[d, d]` 覆盖一天、`NULL` 上端是开放）、
      `test_the_range_guard_and_the_overlap_guard_agree_on_a_boundary`（同一天起止的记录合法且只算一天）。
- [x] 新增记录不覆盖旧记录，旧记录保留起止日期
      — `test_a_new_record_never_rewrites_an_old_one`（先写 2024 一条、再写 2025 一条；直接读
      `salary_records` 断言 2024 那条的 `effective_from`/`effective_to`/`base_salary` 一字未动）、
      `test_the_archive_cannot_be_updated_or_deleted_by_the_application`（受限角色对 `salary_records`
      的 `UPDATE` 与 `DELETE` 都是 `permission denied`——不覆盖是**授权**的性质，不是服务层的自觉）。
- [x] 可见范围：人力资源、财务、员工本人；**经理看不到下属薪资**，系统管理员默认也看不到
      — `test_the_company_read_admits_hr_and_finance_and_nobody_else`（hr、finance 读到；经理、
      另一位经理、管理员、同部门同事全部 403，且响应体里没有那个数字）、
      `test_every_role_sees_its_own_archive_and_only_its_own`（七个角色各自的 `/records/me`
      只回自己的链）、`test_the_catalogue_refuses_a_manager_and_an_administrator_by_role`
      （内核层：`salary.read_own` 只给本人，`salary.read_all` 只给 hr/finance，
      `compliance` 也不在其中）、`test_the_policy_admits_the_owner_hr_and_finance_and_nobody_else`
      （数据库层同一条规则，见下）。
- [x] 员工只能看自己的；访问他人薪酬返回 403 并写审计
      — `test_a_refused_read_is_recorded_as_a_refusal_and_not_as_a_read`（403 + 目录错误码
      `ERR_AUTH_002`；`salary.record_read` 条数**不变**，多出来的是 `access.refused`，其
      `after.action` 正是 `salary.read_all`——"被拒绝"和"看过"在证据链上是两件事）、
      `test_every_role_sees_its_own_archive_and_only_its_own`。
- [x] 薪酬数据的**每一次读取**都写审计日志（而不只是写入），因为这类数据的访问本身就是敏感事件
      — `test_reading_your_own_archive_writes_an_audit_entry`（自读）、
      `test_a_list_read_writes_one_entry_not_one_per_row`（列表读：一条记录，带行数，不是每行一条）、
      `test_asking_about_a_day_records_the_look`（`as_of` 读，条目里带着被问的那一天）、
      `test_a_person_with_no_records_is_recorded_as_a_look_and_not_a_zero`（**没有记录的人也被记了一次**，
      行数为 0）、`test_the_read_entries_carry_no_amounts`（条目里没有金额、币种、事由字符串）、
      `test_a_reading_cannot_be_built_by_a_caller`（载体不可伪造，见下）+
      `test_the_anchor_that_makes_the_seal_testable`（反向对照：封印不是一堵把服务自己也挡住的墙）。
- [x] 系统内**不存在**任何从薪酬记录计算个税、社保或实发金额的逻辑
      — `test_the_archive_schema_holds_no_derived_money`（`information_schema.columns` 逐列断言：
      表就是那十二列；`numeric` 列**只有** `base_salary`；列名里不许出现
      `net/total/gross/annual/tax/irpf/social/prorat/...`；响应体的字段集合也逐字断言）、
      `test_an_amount_finer_than_a_cent_is_refused_rather_than_rounded`（本模块唯一的"算术"是
      拒绝比一分更细的数）。
- [x] 界面上明确提示该模块为档案记录，实际发放以财务出具的工资单为准
      — `test_the_disclaimer_is_served_as_a_catalogue_key_alongside_the_record`
      （`notice.message_key == "salary.archive_notice"`，与 `app/core/messages.py` 两个语言的目录、
      与 `ARCHIVE_NOTICE_TEXT` 三语正文逐字相等；中文那句就是本节的原话）。
      **本工单没有做界面**：`docs/architecture/frontend-design-system.md` §8.1 的落地表把薪酬相关界面
      划给 44/45，`INDEX.md` 也记着这条裁定。本工单交的是**让界面能拿到那句话**：key + 三语正文随记录一起返回。

## 金额为什么是一分都不会丢的类型

**`base_salary` 是 PostgreSQL 的 `numeric(14, 2)`，Python 侧是 `decimal.Decimal`，线上形态是字符串。**

不是风格问题，是"一分钱不能消失"的问题。`double precision`（或 JSON 的数字类型、或 Python 的 `float`）
承诺不了这件事，而且失败是无声的：

- 二进制浮点表示不了 0.1，`0.1 + 0.2` 已经是 `0.30000000000000004`；
- 超过 15 位有效数字的值在**进入**的那一步就被舍入，写进列里的不是输入的那个数；
- JSON 只有一种数字类型，就是浮点——所以 `components` 里的每个 `amount` 也是**字符串**，
  一个"客户端算出来的 0.30"（浮点求和后是 `0.30000000000000004`）会被 `parse_components` 拒绝，
  而不是被存成一个谁也还原不回去的近似值。

`numeric` 是精确十进制算术，`Decimal` 也是，`"{:.2f}"` 只是把已经精确的值写成字符串——
三者之间没有一次转换会丢数字。证据是
`test_the_exact_figure_survives_storage_and_read_back`：`123456789.01` 走完
**真实 HTTP 请求 → ORM → `numeric(14,2)` 列 → 回读 → 响应体**，断言的是**响应文本里的那几个字符**，
所以"schema 把 Decimal 序列化成了 JSON 数字"这种写法在这里也会红。
比一分更细的输入（`33000.005`）是**拒绝**而不是四舍五入：入库时四舍五入就是一分悄悄消失的地方，
而且没有第二处会发现它。

域类型里两个出口都只有一处：`SalaryRecord.stored_amount` 是"金额怎么写上线"的唯一写法，
`parse_amount` 是"金额怎么读进来"的唯一入口。

## 读取审计为什么绕不过去

「每一次读取都写审计」在别处通常靠"三个路由都记得调 `record()`"实现，而**记得**是会漏的。这里换成一个形状：

```python
# domain/payroll/models.py
@dataclass(frozen=True, slots=True, init=False)
class SalaryReading(Reading):
    def __init__(self, *, _token: object = None, reading: Reading | None = None) -> None:
        if _token is not _READING_TOKEN or reading is None:
            raise TypeError("SalaryReading is produced by SalaryService.read(); "
                            "it has no public constructor")
        ...
    @classmethod
    def _issued(cls, reading: Reading) -> "SalaryReading":
        return cls(_token=_READING_TOKEN, reading=reading)
```

- **薪酬行只装在 `SalaryReading` 里**，而 `SalaryReading` 只能由 `SalaryService.read(...)` 产出；
- `read()` 在同一事务里写 `salary.record_read`，**然后**才`_issued(...)`出载体。
  没有第二条路能拿到行——路由不经过 `read()` 就没有东西可返回；
- 这是 `FilterSpec` 的同一套手法（私有构造 + 身份令牌），只是问的问题不同：那边防"捏一个全通过的过滤条件"，
  这边防"端出一份没有被记录过的读取"。

**测试挑的是最容易漏的三条路径**，而不是最显眼的那条：列表读（一条条目、带行数、不是每行一条）、
自读（本人读自己的也要留痕）、以及**查一个根本没有记录的人**（第一次读就得到空链，路由层顺手
`record()` 的写法最容易在这里漏掉）。`test_no_salary_route_serves_rows_without_the_recorded_read`
再在**路由源码**上钉一遍：三个读端点都只能经由 `service.read(`，路由里不许出现仓储的 `chain(`/`append(`。

`as_of` 那条读是唯一由**主体**决定 action 的（叫自己的名字 → `salary.read_own`，叫别人的 →
`salary.read_all`），所以它和考勤/请假同形：路由级守卫是"已登录"，内核在 handler 里针对
请求点到的那个人被问一次，拒绝记在**实际尝试**的那个 action 上。

## 生效日期与非重叠：一条规则，三种写法，互相当参照

| 写法 | 在哪 | 是什么 |
|---|---|---|
| SQL | `repositories/payroll.covers()` | `effective_from <= d AND (effective_to IS NULL OR effective_to >= d)`，`as_of` 就在 `WHERE` 里折叠，不是取回一批行再循环 |
| 数据库 | `EXCLUDE USING gist (employee_id WITH =, daterange(effective_from, effective_to, '[]') WITH &&)` | **重叠不可表示**：不是"服务拒绝"，是 PostgreSQL 拒绝 |
| Python | `domain/payroll/windows.in_force()` | 测试用来给查询的答案做**独立**参照——只跑查询的测试是拿查询证明它自己 |

`btree_gist` 由迁移 0001 打开（04 号工单），`daterange(..., '[]')` 两端闭合：3 月 31 日属于"到 3 月 31 日"
那条，下一条就从 4 月 1 日开始——两条**相接而不重叠**，链才可能存在。`effective_to IS NULL` 是合法的
"尚无终止日"，`daterange(from, NULL, '[]')` 因此是一个从 `from` 起无上界的区间，重叠判定自然正确。

`ex_salary_records_no_overlap` 是**第二道**，服务层那道 `latest_covering()` 只在前面给出人话：

- 服务层命中 → `ERR_PAY_003`，detail 里点名那个冲突的日期与被挡住的旧窗口；
- 服务层被绕过（脚本、直接 SQL、将来某条新写入路径）→ 数据库照拒，
  `test_overlapping_records_are_refused_by_the_database` 用 `platform.refused_by_database` 断言
  报的正是 `ex_salary_records_no_overlap` 这个约束名。

"区间反转"（`effective_to < effective_from`）同理：服务层给 422，`ck_salary_records_effective_range`
让数据库自己也拒。每个员工最多一条 `initial` 由**部分唯一索引** `uq_salary_records_initial` 保证，
服务层的 `ERR_PAY_004` 只是把话说清楚——而且检查顺序是先"有没有开档记录"再"那天有没有被占"，
否则一条**窗口空着**的第二条 `initial` 会被报成"日期冲突"，那是错的诊断。

## 可见性：catalogue 两个 action，内核一个分支，数据库一条策略

```python
# domain/access/permissions.py
SALARY_READ_OWN = "salary.read_own"     # {employee}，并登记在 SELF_ONLY_ACTIONS
SALARY_READ_ALL = "salary.read_all"     # {hr, finance}
SALARY_WRITE    = "salary.write"        # {hr}
SALARY_COMPANY_ROLES = frozenset({"hr", "finance"})
SALARY_CROSS_ACTIONS = frozenset({Action.SALARY_READ_ALL, Action.SALARY_WRITE})
```

- **为什么是新的一对，而不是把 `employee.read` 放宽**：经理看得到下属的考勤、请假、工时，
  在这里**一个字节都看不到**；管理员默认也看不到（§4.1 把"看不到工资单内容"写在管理员那一行，
  理由是职责分离）。把薪资挂进员工档案那条规则，就等于让"改一次部门可见性"顺手改掉薪资的可见性。
- **`ResourceKind.SALARY_RECORD` 是新的 kind**：`EMPLOYEE` 那条路会被**部门**条款答出来
  （经理和同事同部门 → "我部门里的就是我能看的"），而「经理看不到下属薪资」拒绝的正是这个读法。
  新 kind 自带一个内核分支 `_can_on_salary`：公司范围只看角色，此外只有本人（经
  `SELF_ONLY_ACTIONS` 的 `salary.read_own`）。`filter_for(..., SALARY_RECORD)` 把同一条规则
  说成数据：`allow_all` 给 hr/finance，`own_employee_id` 是本人，
  **`department_ids` 与 `reports_employee_ids` 都是空的**——它们空着就是规则本身，
  `test_the_reach_carries_no_department_and_no_reporting_clause` 钉住这一点。
- **`compliance` 不在公司读里**：它的那一行是审计日志与 RoPA，而"谁看过这份薪资"正是审计日志回答的。
  读审计和读被审计的数字是两种权限。
- **`salary.write` 只给 hr**：财务读薪酬档案以便发放，不决定某人的工资是多少——这是第三种权限，
  所以是第三个 action，而不是 `salary.read_all` 的一种用法。
- **`_can_on_salary` 的拒绝理由是 `Reason.NOT_PAYROLL_ROLE`**（新加的），但经理/管理员实际撞到的是
  更早的 `ROLE_LACKS_PERMISSION`（他们连 action 的角色表都不在）。两条都留着：前者是"这个角色对它
  有某些权力但不够"，后者是"它根本不是你的"。测试断言的是**真正会触发**的那一条
  （`test_the_catalogue_refuses_a_manager_and_an_administrator_by_role`），不假装另一条更常见。

### RLS：比规则**更窄**，不是更宽

迁移 0028 的策略**从角色名写起**，而不是读 `app.is_privileged`：

```sql
USING (employee_id = app_setting('app.current_employee_id')::uuid
       OR app_setting_array('app.current_roles') && ARRAY['hr', 'finance'])
```

理由就是 36 号工单在文档上发现并修掉的那个方向：**后备比规则宽，就是泄漏**。
`app.is_privileged` 对 `compliance` 也是真（§4.1 让它读人事文件的有部分），
而它不该读到薪酬数字；写角色名让后备与 `SALARY_COMPANY_ROLES` 同窄，
`test_the_policy_admits_the_owner_hr_and_finance_and_nobody_else` 里专门有一条
**控制组**：把 `app.is_privileged` 设为 `true` 而角色只有 `compliance` 时，仍然只看到自己的两条。

写入策略是 `WITH CHECK (app_setting_array('app.current_roles') && ARRAY['hr','finance'])`，
**不是** `employee_id = me`——没有人给自己写工资，HR 写的是**别人**那一行。
`test_a_row_the_context_cannot_reach_is_refused_by_the_insert_policy` 用员工上下文直接 INSERT，
断言拿到的是 row-level security 拒绝。

## 没有记录的人看到什么

**200，空链，`empty: true`，`total: 0`。** 不是 403，不是 404，更不是一个"看着像 0"的 200：

- 入职第一周的人确实还没有档案，这是**合法状态**而不是错误——把它做成 404/403 会让客户端把
  "这个人还没有薪资记录"误读成"这个人不存在"或"你没权限"；
- **`"0.00 EUR"` 是个人可以据以行动的数字**，所以这条路径上任何地方都不产生金额：
  `test_a_person_with_no_records_is_recorded_as_a_look_and_not_a_zero` 除了断言 `items == []`、
  `empty is True`、`total == 0`，还断言响应体里**既没有 `0.00` 也没有 `base_salary` 这个键**；
  `as_of` 落在一个没有任何记录覆盖的日期时同理（`items: []` + `empty: true`），
  **不是**一个金额为零的记录；
- 这次"看"照样写审计（行数 0）——这正是"每一次读取"里最容易被漏掉的那一种。

## 明确提示：一句话的三种形态

| 形态 | 位置 | 谁用 |
|---|---|---|
| catalogue key | `salary.archive_notice`（`app/core/messages.py` 的 es / en 两本目录） | 有字典的客户端，按读者语言渲染 |
| 三语正文 | `ARCHIVE_NOTICE_TEXT`（zh / es / en，中文那句就是 checklist 的原话） | 没有字典的客户端照样有句子 |
| 随数据返回 | 链式响应的 `notice: {message_key, text}` | 界面不必自己拼一句话 |

API 不把这句话写进 `salary_records` 的任何一列：一个写在列里的免责声明是可以被喂给客户端一句过期文案的，
而一个由 API 拼出来的句子只有一种语言。这是**拒绝**（错误码 → key）的形状，不是 36 号工单
`source_notice` 的形状——后者是**答案**的永久记录，"读者当时被怎样告知"是那个答案的一部分；
这里说的是一块**界面**的性质，每次请求解析一次即可。

## `employee_private` 与人事变动单：17 号工单那条备注的交代

17 号工单写得明白：「The agreed figures live in the change's payload and are stamped into
`applied_values` when the change takes effect... `test_a_salary_change_is_applied_to_the_change_itself`
also asserts `to_regclass('salary_records') IS NULL`, so nobody helpfully adds the table here.」
本工单就是那张票，所以：

- **那条断言按原意改写了**，而不是删掉：它现在断言表**存在**、而且一次已生效的薪资变更写下了**恰好一行**
  （`effective_from` 是变更的生效日、`effective_to` 为 NULL、金额逐字是 `33000.00`、
  `change_reason_type='initial'`）。同一事务里，变更自己的记录与审计里的前后值一字未动；
- **两条记录回答两个问题**：变动单是**审批**（谁提的、批了什么、哪天生效、`applied_values` 落库写了什么），
  `salary_records` 是**薪酬历史**（一个窗口、一个当时有效的数额、津贴明细、可回溯的链）。
  互相都不是副本，谁也不删谁；
- **写入是强制的、不是可选的**：`PersonnelChangeService` 现在把归档仓储当**必需**构造参数
  （`jobs/apply_personnel_changes.py` 与 `api/v1/personnel.py` 两处都传），
  和"termination 必须有账户仓储 + Redis 吊销器"同一条理由——一个没接上的服务会静默地生效变更而留下空归档。
  领域侧用一个 `SalaryArchive` Protocol 命名它需要的两个方法（`has_any` / `append`），
  方向因此是单向的：人事驱动归档，归档不认识人事；
- **归档字段的缺口说清楚**：变动单的 payload 仍是通用的五类字段
  （text / date / identifier / money / flag），**不含** `pay_period`、`components` 与 `change_reason`。
  从生效单据写归档时，这三个用归档自己的默认值（`monthly` / `[]` / 一句"由已批准的变动单生效写入"），
  `change_reason_type` 由归档**自己**问出来（这个人此前没有任何记录 → `initial`，否则 `adjustment`）。
  把一份津贴明细塞进通用 payload 需要给 `FieldSpec` 加第六种"值类型"，为一张单据放宽全部五种变更类型——
  明细属于归档自己的端点（`POST /api/v1/salary/records`）。
- 生效路径也走归档自己的解析器（`parse_amount` / `parse_currency` / `parse_pay_period` /
  `parse_components` / `parse_reason`）：否则一个三位小数的金额会被 `numeric(14,2)` **静默四舍五入**，
  而那正是"一分钱不能丢"要禁止的事；现在它会**拒绝**，单据停在未生效并被重试/上报。

## 结构落位

```
api/app/domain/payroll/          薪酬域（新）
  errors.py       PayrollErrorCode（别名到 core/errors 的 ERR_PAY_*）
  models.py       AllowanceLine / SalaryRecord / RecordInput / RecordQuery / RecordView /
                  Reading / SalaryReading（封印载体）/ parse_* / ARCHIVE_NOTICE_KEY + 三语
  windows.py      in_force() / on()——窗口规则的 Python 形态（查询的独立参照）
  service.py      SalaryService.read()（唯一出口 + 唯一审计点）/ append()
api/app/models/payroll.py        salary_records 的 ORM（SALARY_COLUMNS 是"不许有派生金额"的名单）
api/app/repositories/payroll.py  covers() / reach(FilterSpec) / chain() / latest_covering() / append()
api/app/api/v1/salary.py         四个路由 + 读写 schema（金额上线一律字符串）
alembic/versions/20261010_1000_salary_records.py  0028：建表、EXCLUDE、RLS、REVOKE UPDATE/DELETE
```

改动到的既有文件：`domain/access/permissions.py`（三个 action、两个集合、`SELF_ONLY_ACTIONS` 加一条）、
`domain/access/kernel.py`（`ResourceKind.SALARY_RECORD`、`Reason.NOT_PAYROLL_ROLE`、`_can_on_salary`、
`filter_for` 分支）、`audit.py`（`salary.record_read` / `salary.record_written`）、
`core/errors.py` + `core/messages.py`（四个 `ERR_PAY_*` 与 notice 的两种语言）、
`main.py`（挂路由）、`models/__init__.py`、`domain/personnel/{service,models}.py`、
`jobs/apply_personnel_changes.py`、`api/v1/personnel.py`。

## 权限矩阵：移动的字面量

| 断言 | 旧 | 新 | 为什么 |
|---|---|---|---|
| `assert len(cases) == 7 * N * 13` | `7 * 57 * 13` | `7 * 60 * 13` | 新增三个 action：`salary.read_own` / `salary.read_all` / `salary.write` |
| `assert checked == N`（HTTP 矩阵） | `83` | `87` | 新增四个路由：`GET /salary/records/me`、`GET /salary/records`、`GET /salary/records/as-of`、`POST /salary/records` |

`DESIGN_GRANTS` 里三行新条目（`salary.read_own: EVERYONE`，`salary.read_all: {hr, finance}`，
`salary.write: {hr}`），`KIND_FOR_ACTION` 里三行都指向新的 `ResourceKind.SALARY_RECORD`。
`design_says` 里加了**一整块**薪资策略而不是只加交叉 action 的一条：三个 action 全部由它回答，
因为没有这一块，自读会被通用路径用**部门**条款答成"同部门就能看"。
`RESOURCE_FREE_ROUTES` 加两条（`/records/me`、`/records/as-of`——它们不带参数就有确定答案：空链），
`http_payload` 加 `POST /salary/records` 的合法 body，`as_of` 那行由循环补上日期参数
（`MATRIX_SALARY_DAY`），否则路由会先给 422，被矩阵记成"端点是死的"。

## 验证

- `uvx ruff check app tests` → **All checks passed!**
- 新增/改动的目标运行（scratch 库 `eam_test_t43`、Redis 12 号库）：
  - `pytest tests/test_salary_records.py` → **33 passed**
  - `pytest tests/test_permission_matrix.py` → **35 passed**
  - `pytest tests/test_personnel_changes.py` → **49 passed**
  - `pytest tests/test_database_security.py tests/test_overtime.py tests/test_termination.py
    tests/test_seed.py tests/test_architecture_constraints.py tests/test_errors.py
    tests/test_error_aliases.py tests/test_permission_snapshot.py tests/test_employee_schema.py`
    → **272 passed**
- **全量运行**（由父代理在清掉一次死锁后补跑）：
  `docker compose exec -T -e TEST_DATABASE_NAME=eam_test_v43full -e REDIS_URL=redis://redis:6379/5 api python -m pytest -p no:warnings`
  → **1561 passed**（0:25:30）。基线 1521，差 40（33 条本工单测试 + HTTP 矩阵新增两行/两个 action 带来的净增）。
  **为什么是父代理跑的**：本工单收尾时的三条 pytest 进程（两条全量 + 一条针对本模块）同时跑在
  `eam_test_t43` 上，`TRUNCATE` 拿不到关系锁、`pg_blocking_pids` 显示互相阻塞、有一条会话
  `idle in transaction` 握着锁——那条运行不构成任何证据，因此被终止、库被删掉、重跑一次。
  教训与 41/42 号工单记的一样：**一次只跑一个**，被杀掉宿主侧 `docker compose exec` 不等于容器里的
  pytest 结束了。
- 临时变更脚本 `api/tests/tools/mutate_t43.py` 已删除；Redis 用的是 12/13 号库（0-15 之内）。

## 变更测试（mutation）

每条规则被打断一次、跑**一个** node id、`finally` 还原。结果：**7/7 全部被抓住**。

| 被破坏的规则 | 破坏方式 | 失败的测试 |
|---|---|---|
| **读取审计** | 删掉 `read()` 里的 `await self._record_read(...)` 一行 | `test_reading_your_own_archive_writes_an_audit_entry`（同时打断列表读与空读那两条——同一个删除点被三条独立测试抓到） |
| 审计的粒度 | 列表读改成每行一条 | `test_a_list_read_writes_one_entry_not_one_per_row` |
| **可见性规则** | `SALARY_COMPANY_ROLES` 加进 `manager` 与 `admin` | `test_the_company_read_admits_hr_and_finance_and_nobody_else` |
| **生效日期"某时点的值"** | `covers()` 改成恒真（每个时点都匹配所有行） | `test_the_chain_is_ordered_and_any_instant_is_one_query` |
| **只增不改** | 仓储 `append()` 改成对已有行做 `UPDATE` 再返回 | `test_a_new_record_never_rewrites_an_old_one` |
| **非重叠（服务层）** | `latest_covering()` 的调用改成 `None` | `test_overlapping_records_are_refused_by_the_database`（冲突走数据库那道，仍然 409） |
| **非重叠（数据库）** | 从 scratch 库 `DROP CONSTRAINT ex_salary_records_no_overlap` | `test_overlapping_records_are_refused_by_the_database`（约束名那一条断言变红） |

跑法是一个临时脚本（`api/tests/tools/mutate_t43.py`，**已删除**）：改源码 → 跑一个 node id →
`finally` 还原。数据库那条破坏用 `ALTER TABLE ... DROP CONSTRAINT`（迁移文件改了也没用——
测试库已经在 head 上，被断言的约束活在**数据库**里），跑完立刻 `ADD CONSTRAINT` 加回去。
每次只给一个 node id：多个 node id 用空格拼会被 pytest 当成额外参数，
于是"没跑"会被记成"抓住了"（41 号工单记录过的坑）。

## 有意没做 / 边界

- **没有做界面。** §8.1 的落地表把薪资界面划给 44/45，INDEX 里也记着这条裁定。本工单交的是
  "让界面能拿到那句话"（key + 三语）。
- **没有动 `employee_private`，也没有把薪资塞进人事变动单的 payload**：见上，写的是归档自己的表。
- **没有算任何东西**：没有总额、没有年薪、没有换算、没有个税/社保/实发、没有按周期折算。
  §8.3 把西班牙工资单计算列为非目标，D9 把它写成"只存档案 + PDF 自助下载"。
  唯一的"算术"是拒绝比一分更细的金额。
- **没有做 44/45/46**（工资单上传/自助下载/撤回），也没有做 47。
- **没有给回收/删除路径**：归档只增不改，`REVOKE UPDATE, DELETE` 是数据库的事实。
  撤销一条错记录靠再写一条（`change_reason_type='correction'`），旧行原样保留。
- **没有货币换算**：`currency` 是一个标签，不是换算因子；`EUR`/`USD` 两条记录不会相加，也不会比较。
- **`pay_period` 是标签**：`monthly` / `biweekly` / `weekly` 只说明这个金额覆盖的周期，
  系统不把一个折算成另一个（那是 proration，属于计算）。
- **没有 `salary.update` / `salary.delete` action**：表是 append-only，这样的 action 无法被履行，
  所以 catalogue 里根本不提供。
