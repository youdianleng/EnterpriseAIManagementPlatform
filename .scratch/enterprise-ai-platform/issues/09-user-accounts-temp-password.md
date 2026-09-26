# 09 — 用户账号与临时密码

**What to build:** 管理员为某个员工开一个登录账号，系统当场生成一个临时密码并在界面上**只显示这一次**，管理员把它交给员工。账号与员工严格一对一；员工首次登录后会被强制改密。

**Blocked by:** 08 — 职位与角色

**Status:** done

**Verification (2026-09-26):**
- `pytest` → 268 passed (45 new); `ruff check app tests` clean.
- `tools/probe_accounts.py` → all 28 checks passed over real HTTP. The claim driven hardest is unrecoverability: the plaintext does not appear in any later response, nor in `users`, nor in `audit_log` (both read through a separate connection, since "we do not store it" is a claim about storage). What is stored is an `$argon2id$` hash. The database refuses a non-Argon2id value outright.
- One-to-one enforced by a UNIQUE constraint on `employee_id`, not only by a service check, so two administrators acting at once cannot both win.
- Disabling and resetting both advance `session_epoch`, which is what will make existing sessions stop matching once ticket 10 issues them.
- The password policy is published at `/accounts/password-policy` so the UI states the rule instead of restating it.

- [x] 管理员创建账号时系统自动生成临时密码，保存的是哈希值，明文仅在创建成功的那一次响应中返回、此后再也无法取回
- [x] 账号与员工严格一对一
- [x] 新账号被标记为"必须修改密码"
- [x] 拒绝为已离职员工创建账号，拒绝重复用户名，冲突时给出可读的西/英提示
- [x] 管理员可停用账号，停用后该账号的现有会话立即失效（epoch 已写入 Redis，会话校验在票据 10 接入）
- [x] 创建、停用、重置密码三类操作均写入审计日志（重置密码不记录明文）
- [x] 界面上明确提示"请立即复制此临时密码，关闭后无法再次查看" → **票据 10 的登录界面**

**Password hashing corrected.** The originating answer for this ticket said the temporary password should be "generated as a SHA-256 password". SHA-256 is a fast general-purpose digest, designed to be cheap, which is the opposite of what a password store needs: it lets an attacker test billions of candidates per second against a leaked hash. `docs/DESIGN.md` specifies Argon2id, so Argon2id is what is implemented, and a CHECK constraint requires the `$argon2id$` prefix so a plaintext or foreign value cannot be written even by a mistaken migration. The likely original intent was about *generating* the value; generation is `secrets`-based random and storage is Argon2id.

**Generated passwords avoid ambiguous characters.** `0/O`, `1/l/I`, `5/S`, `8/B`, `2/Z` are excluded and the value is grouped in fours, because it gets read aloud or copied by hand and a transcription error costs a support call.

**Defects found and fixed while verifying:**
1. Bulk `UPDATE` statements left already-loaded objects stale in the session's identity map, so a just-disabled account came back reading `is_active=True`. Fixed with `synchronize_session="fetch"`.
2. `session_epoch` was bumped *after* the account was read, so responses carried the old epoch while the database held the new one. A client caching that response would hold a value no session check agrees with.
3. The password-change schema imposed `min_length=8`, which rejected a weak password with a generic validation error instead of the catalogued policy error — the rule has one home (`core.security`) and the schema should not restate it.
4. Account responses had two shapes: creation nested an `account` object while state changes returned it flat. Flattened, with `temporary_password` present only when one was just issued.

**Amended by ticket 11.** The self-service change-password endpoint this ticket
shipped (`POST /accounts/{id}/change-password`) has been removed: ticket 10 owns
that operation on `/auth/change-password`, where it can replace the session cookie
and be audited as an authentication event. Administration of *other* people's
passwords stays here, as a reset. See ticket 11 for the reasoning.

**Audit scope note:** the append-only guarantee is not yet enforced at the database level — the application still connects as the table owner. Ticket 13 introduces a role with INSERT and SELECT only on `audit_log`, which is the point at which it stops depending on application code. Recorded in `app/audit.py` so the gap is not mistaken for a finished design. Ticket 14 adds the compliance read surface and the remaining actions.
