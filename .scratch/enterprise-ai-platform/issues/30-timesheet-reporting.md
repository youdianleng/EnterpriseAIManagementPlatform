# 30 — 工时报表与可计费汇总

**What to build:** 管理者和人力资源能按项目、部门、期间查看工时汇总，并区分可计费与不可计费。这让人力投入第一次变得可见。

**Blocked by:** 29 — 审批锁定、补充提交与周锁

**Status:** done

- [x] 支持按项目、按部门、按员工、按期间四个维度汇总，并可组合筛选
- [x] 汇总严格区分可计费与不可计费工时，分别给出小计
- [x] 汇总只计入已审批通过（含补充提交通过后）的净额，草稿与审批中的不计入
- [x] 冲销记录在汇总中正确抵减，报表数值与逐条明细可对账
- [x] 经理只能看自己下属的工时；项目经理能看自己项目的工时；人力资源可看全员
- [x] 越权访问返回 403，且被拒绝的资源在响应中完全不存在
- [x] 支持导出为表格文件供财务或客户使用
- [x] 报表查询在 seed 数据规模下响应时间可接受（实测并记录）

## 实现

- `api/app/domain/timesheet/report.py` — 报表的值对象：四个维度、筛选、行、小计；模块文档说明
  「按行所属**工单**是否通过」、`period` 是周、`department` 是项目所属部门等取舍。
- `api/app/domain/timesheet/export.py` — CSV 列契约（双语表头）、`TOTAL` 合计行、刻意排除的列。
- `api/app/repositories/timesheet.py` — `report_rows` / `report_totals`：一条聚合语句 + 一条合计语句，
  **共用同一个 `WHERE`**，所以合计行与上表必然描述同一批行；`_reach` 把内核的 `FilterSpec` 翻译成 SQL。
- `api/app/domain/timesheet/service.py` — `report()`（取 `filter_for(principal, TIMESHEET_REPORT)`）
  与 `export_report()`（导出记账 `data.exported`）。
- `api/app/domain/access/{permissions,kernel}.py` — 目录新增 `timesheet.read_report`（manager，
  两条资源可达：下属 **或** 自己管理的项目）与 `timesheet.read_all`（hr），
  `ResourceKind.TIMESHEET_REPORT` 有自己的内核分支与 `FilterSpec`（新增 `reports_employee_ids`）。
- `api/app/api/v1/timesheets.py` — `GET /api/v1/timesheets/report`、`GET /api/v1/timesheets/report/export`。
- 测试：`api/tests/test_timesheet_reporting.py`（12 条），`api/tests/test_permission_matrix.py`（新增行与字面量计数）。
- 探针：`api/tests/tools/probe_timesheet_report.py`。

## 响应形状

一行一个分组，末尾一条合计（`total`），`group_by` 原样回显（行键是位置化的）：

```json
{
  "from_date": "2026-03-30", "to_date": "2026-09-21",
  "group_by": ["period", "project"],
  "rows": [{
    "dimensions": [
      {"kind": "period", "week_start": "2026-03-30", "id": null, "code": null},
      {"kind": "project", "id": "…", "code": "FIJO-2026", "name_es": "…", "name_en": "…"}
    ],
    "totals": {"billable_minutes": 960, "non_billable_minutes": 120, "total_minutes": 1080,
               "gross_minutes": 1140, "reversal_minutes": 60, "entries": 4, "weeks": 1}
  }],
  "total": {"billable_minutes": 0, "non_billable_minutes": 0, "total_minutes": 0,
            "gross_minutes": 0, "reversal_minutes": 0, "entries": 0, "weeks": 0}
}
```

`billable + non_billable = total`（净额，冲销在各自那一侧）；
`gross`/`reversal` 是净额由来的两个数。**没有任何费率、单价或金额字段**——本系统只累计与导出分钟数（DESIGN §7.3）。

## 实测（票据 30「响应时间可接受」）

探针 `api/tests/tools/probe_timesheet_report.py`，容器内 `postgres 18`，2026-09-28。
规模：**100 员工 × 26 周 × 每日 5 条 + 每 5 周一次补充更正 = 14 200 条条目 / 3 200 张工单**，全部已审批。

| 查询 | p50 | p95 |
|---|---|---|
| 人力资源，按项目（全公司） | 22.2 ms | 24.7 ms |
| 经理，按员工（10 名下属） | 19.6 ms | 23.9 ms |
| 项目经理，按期间（自己的项目） | 4.5 ms | 7.7 ms |
| 人力资源，按部门 + 项目 | 38.3 ms | 43.8 ms |
| 人力资源，单人按期间 | 4.0 ms | 7.1 ms |

**没有加索引，这是实测的结论而不是省略：**

- 全公司报表的执行计划是 `Seq Scan on timesheet_entries → Hash Join ×2 → Sort → GroupAggregate`，
  13 800 / 14 200 行都落在期间内，执行 13.7 ms、729 个 buffer 全命中缓存。**一个季度的报表按定义就是表的大部分**，
  `entry_date` 索引在这个形状上不会被选中，只会增加写入成本。
- 选择性的报表已经走既有索引：项目经理那条计划是
  `Bitmap Index Scan on ix_timesheet_entries_project` + `Index Scan using timesheets_pkey`，执行 1.06 ms；
  单人那条走 `ix_timesheet_entries_employee_week`。
- 因此**不写迁移**；若未来单期数据量再上一个量级（例如四年全量报表），重跑本探针再决定，
  结论记录在 `docs/DESIGN.md` §10.7。
