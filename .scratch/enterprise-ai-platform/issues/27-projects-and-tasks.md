# 27 — 项目与任务（含可计费标记）

**What to build:** 项目经理能建立项目，在项目下建任务，并标记哪些任务可计费。可计费与否由项目配置决定，员工在填工时时**不能**自行把不可计费的任务改成可计费。

**Blocked by:** 15 — 权限矩阵与越权测试套件

**Status:** done

- [x] 项目含：编码、名称、客户名、归属部门、项目经理、默认是否可计费、状态、起止日期
      — `test_a_manager_creates_a_project_and_it_carries_every_field`。
      编码**全局唯一且终身唯一**（归档不释放编码）：编码是工时条目与发票行所引用的身份，
      而归档项目仍可读，复用编码会让两个项目在四年前的记录里重名。名称按本仓惯例做
      `name_es` / `name_en` 双语，DESIGN §3.3 的 `name` 一词按此拆分。
- [x] 项目下有二级任务，每个任务有编码、名称、是否可计费、是否启用
      — `test_a_project_has_tasks_with_a_code_a_name_a_flag_and_a_switch`。
      任务编码**项目内唯一**（客户自己的编号就是这样编的），
      `test_a_task_code_is_unique_within_its_project_and_nowhere_else`。
- [x] 员工只能对"已启用且在其可见范围内"的项目与任务填报工时
      — `record-time` 端点 + `ProjectService.resolve_record_target`；
      `test_a_deactivated_task_cannot_receive_time`、`test_a_draft_project_cannot_receive_time`、
      `test_a_project_outside_the_employees_departments_cannot_receive_their_time`、
      `test_a_task_cannot_be_reached_through_another_project`。
      "可见范围" = 本人所属部门（含子部门）∪ 本人担任项目经理的项目，由内核
      `filter_for(principal, ResourceKind.PROJECT)` 描述，
      `test_the_project_filter_says_what_may_be_recorded_against` 断言的是**数据描述**本身，
      `GET /projects/selectable` 是它的列表形态。
- [x] 任务的可计费标记继承项目默认值，项目经理可逐任务覆盖
      — `test_a_task_with_no_flag_of_its_own_inherits_the_project_default`、
      `test_a_manager_overrides_the_default_per_task`。
      **存 NULL 而不是写入时解析**：NULL 的含义是"跟随项目"，未覆盖的任务必须随项目默认值
      的更正而更正；写入时固化会把今天的默认值冻在每个任务上，项目自己反而改不动。
      解析只发生在需要答案的两处：响应中的 `is_billable_effective`，以及
      `RecordTarget.is_billable`（票据 28 要写的那一列）。
- [x] 员工无法通过接口参数把不可计费任务提交为可计费——服务端以任务配置为准，忽略客户端传入的可计费值
      — **`test_a_client_cannot_make_an_unbillable_task_billable`**（发 `is_billable: true`，
      存/回显 `false`，同时回显 `claimed_billable: true`），反向
      `test_a_client_cannot_make_a_billable_task_unbillable_either`，
      覆盖优先于项目默认 `test_the_recorded_value_follows_the_task_override_not_the_project`。
      客户端传的值**不做请求字段**（`StrictModel` 仍然拒绝未知字段）：它被接受只为回显，
      从未被读取来决定任何事。
- [x] 项目归档后不可再新增工时，但历史工时保留可查
      — `test_an_archived_project_is_refused_by_every_write_and_stays_readable`：
      `record-time` 与三个写端点全部拒绝（`ERR_PRJ_010` / `ERR_PRJ_006`），
      项目、任务、配置字段全部仍可读，管理员同样能读。
      `closed` 与 `archived` 是**两个状态**：已结束项目的迟到工时在 8 周补填窗口内仍然合法，
      两者对"新工时"的效果相同，对票据 28 的补充提交路径不同——
      **票据 28 必须补上第三段保证**：`time_entries` 落库时，对
      `projects.status <> 'active'` 的项目拒绝写入（数据库层约束或策略），
      而不只是应用层再判一次。
- [x] 项目经理只能管理自己负责的项目；管理员与人力资源可管理全部
      — 规则在**内核**：`kernel._can_on_project` +
      `permissions.PROJECT_ADMIN_ROLES = {admin, hr}`；
      `can(principal, action, Resource(ResourceKind.PROJECT, department_id=…, manager_employee_id=…))`。
      `Resource` 新增一个字段 `manager_employee_id`——**不是** `owner_employee_id`：
      管理不是拥有，`owner_employee_id` 在核心里已经被 `SELF_ONLY_ACTIONS` 的
      `IS_OWNER` 分支占用，项目负责人若从"所有者"进来会继承那条规则。
      内核侧 `test_the_kernel_refuses_a_non_manager_and_allows_administration`
      （同部门同事被拒，理由 `role_lacks_permission` 而非 `not_project_manager`），
      端点侧 `test_a_project_manager_may_not_manage_somebody_elses_project`
      （403 + `access.refused` 审计，含 `not_project_manager` 理由）、
      `test_a_project_manager_may_manage_their_own`（对照）、
      `test_administration_and_hr_manage_any_project`。
      移交项目经理是 `PUT /projects/{id}/manager`，仅管理员与人力资源可用
      （`test_a_manager_may_not_hand_their_project_to_somebody_else`）。
- [x] 项目列表支持按部门、状态、客户筛选，并分页
      — `test_the_list_filters_by_department_status_and_client`、
      `test_the_list_is_paginated_and_reports_the_total`。

**状态集合（闭合，数据库 CHECK 同时约束）：** `draft` → `active` → `closed` / `archived`。
新建默认 `draft`（安全方向：草稿不收工时也不收任务）。只有 `active` 可填报工时。

**端点：** `POST /projects`、`GET /projects`、`GET /projects/selectable`、
`GET|PATCH /projects/{id}`、`PUT /projects/{id}/manager`、
`POST /projects/{id}/tasks`、`PATCH /projects/{id}/tasks/{task_id}`、
`POST /projects/{id}/tasks/{task_id}/deactivate`、
`POST /projects/{id}/record-time`（决策端点，票据 28 在此写入条目）。

**动作目录新增四个：** `project.read`、`project.manage`、`project_task.read`、
`project_task.manage`（`api/tests/test_permission_matrix.py` 的 `DESIGN_GRANTS`、
`KIND_FOR_ACTION` 与字面用例数 `7 * 28 * 13` 已同步更新）。

**与 `codebase-design` §1 的一处偏离：** 目录结构写的是 `domain/timesheet/`（projects, tasks,
timesheets, entries 同域），本票据按票据自己的措辞落在 `domain/project/`。工时条目进来时
（票据 28）应决定合并还是让 `timesheet` 依赖 `project`；此处记录以免成为无意的例外。
