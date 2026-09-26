# 05 — seed 数据脚本：组织与员工

**What to build:** 一条命令把一套可信的演示数据灌进空库：一棵四层深的部门树、一批职位、约一百名员工（含多职位与兼职案例）、以及一个系统管理员账号。数据规模足以让权限过滤、分页和列表页在真实体量下被验证，而不是在三个人的假数据上"看起来能跑"。

**Blocked by:** 06 — 部门树与组织架构管理；07 — 员工档案与一人多职位；09 — 用户账号与临时密码

> **Dependency corrected during ticket 06.** This ticket was originally blocked only by 04, but its
> acceptance criteria require departments, employees and a login account, whose tables arrive with
> tickets 06, 07 and 09. Sequencing it first would have meant writing a seed script against tables
> that do not exist yet, so 06 was done before it and this edge was added.

**Status:** ready-for-agent

- [ ] 一条命令灌入数据，可重复执行且不产生重复记录（幂等）
- [ ] 生成 6 个部门、四层嵌套，部门名与职位名为西班牙语
- [ ] 生成约 100 名员工，字段仅限设计文档允许的范围（姓名、地址、照片、公司资料），**不含**身份证、银行卡、健康或生物特征数据
- [ ] 数据中至少包含：一人多职位案例、兼职作息案例、每层部门都有负责人、若干员工不属于任何管理岗
- [ ] 生成一个系统管理员账号用于首次登录
- [ ] 有一条校验命令打印各部门人数与职位分布，用于人工确认数据合理
