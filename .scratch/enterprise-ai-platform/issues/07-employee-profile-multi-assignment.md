# 07 — 员工档案与一人多职位

**What to build:** 人力资源能在界面上建立员工档案，并给同一个员工挂上多个职位（可以跨部门），指定其中一个是主职位。员工详情页展示其全部职位与各自的审批上级。个人敏感字段（住址、编号）只有本人与人力资源/财务/合规能看到，本部门同事看不到。

**Blocked by:** 06 — 部门树与组织架构管理

**Status:** done

**Verification (2026-09-26):**
- `pytest` → 191 passed (98 new). `ruff check app tests` clean.
- `tools/probe_employees.py` → all 23 checks passed over real HTTP: a forbidden field is refused rather than dropped; the first position becomes primary and a second in another department does not; a client asking for `is_primary` gets 422; the effective approver falls back to the department manager; HR and the person see the withheld block while a colleague's payload contains neither it nor the staff number anywhere.
- `tools/probe_forbidden_columns.py` → 6/6. The DDL guard rejects `national_id`, `iban`, `health_data` and `fingerprint` on `employee_private`, accepts a permitted column, and does not obstruct unrelated DDL.
- Visibility is a pure function (`domain/employee/visibility.py`) with an exhaustive viewer × field-group matrix test, so the rule is verified without a database.

**Schema decisions:**
- `employees` (directory-visible) and `employee_private` (withheld) are separate tables, so "did you remember to hide the address" stops being a question the query has to answer.
- Code uniqueness is partial and scoped; assignment primary-ness is enforced by a partial unique index on `(employee_id) WHERE is_primary AND end_date IS NULL`, so at most one active primary exists per person at the database level too.
- `departments.manager_employee_id` finally gets its foreign key here (deferred from revision 0002, which had no employees table).

**Five defects found and fixed while verifying:**
1. A CHECK constraint over column names is impossible — PostgreSQL rejects subqueries inside CHECK. Replaced with a DDL event trigger, which fires on the change that would actually introduce a forbidden column.
2. Pydantic ignores unknown fields by default, so a client sending `national_id` got a 201 and could believe it was stored. Request schemas now use `extra="forbid"` and return 422.
3. A field the projection dropped came back as `null`: the response model owns the attribute and serialised its default. Both the profile and directory routes needed `response_model_exclude_none`.
4. `project_directory_row` emitted `"email": None` for withheld rows, contradicting the "absent, not null" contract the profile endpoint follows.
5. The in-memory employee substitute lacked `_as_dict`, which only surfaced when the service began patching records.

**Two test-only traps worth recording:** calling `next()` inside an async test raises `RuntimeError: coroutine raised StopIteration` rather than failing the generator, and comparing `"email" in row` tests key presence rather than whether the value was withheld — the first version of the directory test failed for that reason while the behaviour was already correct.

- [ ] 员工档案字段严格限定为：姓名、常用名、邮箱、照片、住址、城市、邮编、国家、入职日期、离职日期、状态、出生日期、紧急联系人
- [ ] **系统内不存在**身份证号、银行卡、健康状况、生物特征字段
- [ ] 支持同一员工拥有多个职位分配，可跨部门，含生效起止日期与兼职标识
- [ ] 第一个被赋予的职位自动成为主职位；此后只有管理员能更换主职位
- [ ] 每个职位分配上可指定审批上级，未指定时回退到该部门的负责人
- [ ] 员工详情页展示全部职位，并标出主职位
- [ ] 访问控制：本部门同事可读姓名/照片/职位/邮箱；住址、员工编号、合同类信息仅本人与人力资源/财务/合规可读，越权访问返回 403
- [ ] 管理员更换主职位后，以该员工为下属的审批与通知对象随之改变
