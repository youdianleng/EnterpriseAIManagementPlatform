# 19 — 通知中心与投递追踪

**What to build:** 系统内部有一个通知中心：每个人能看到发给自己的通知、标记已读。每条通知的投递状态（站内是否已读、邮件是否发出）可追踪，便于排查"我没收到"这类问题。

**Blocked by:** 16 — 审批引擎内核与状态机

**Status:** done

- [x] 通知记录含：收件人、类型、双语标题键、负载数据、关联实体、已读状态、创建时间、过期时间
- [x] 前端提供通知中心入口、未读数角标、标记已读与全部已读
- [x] 通知标题走双语字典，负载数据以结构化字段承载，不在记录里存拼接好的句子
- [x] 投递记录分渠道（站内 / 邮件）分别追踪状态、尝试次数、失败原因、发送时间
- [x] 同一事件对同一收件人不会重复生成站内通知（幂等键）
- [x] 审批流转、审批结果、被退回、被撤销这几类事件会自动产生通知给相关人
- [x] 收件人不能读取或标记他人的通知，越权返回 403
- [x] 过期通知不再出现在列表，但记录保留

**Acceptance record**

- Backend: `api/tests/test_notifications_api.py` (40 tests, real PostgreSQL). `docker compose exec -T api python -m pytest tests/test_notifications_api.py -q` → 40 passed.
- Frontend: `web/scripts/visual-check.mjs` covers `/notifications` in Spanish and English at 320 / 768 / 1280, plus the badge's accessible name, the list being a list, and marking one read as a button whose effect is visible. `node scripts/visual-check.mjs` → ALL CHECKS PASSED (screenshots in `.scratch/visual/`).
- Approval events reach the notifier through `api/app/domain/notification/approval.py` (`ApprovalNotifier`), a decorator over the engine; `api/app/domain/approval/service.py` is unchanged.
- Schema deviation from `docs/DESIGN.md` §3.7 recorded there as 实现注记（票据 19）: `recipient_employee_id`, `read_at`, and the per-recipient `dedupe_key`.
