# 10 — 登录、会话与强制改密

**What to build:** 员工用账号密码登录。新账号或刚被重置的账号在登录后不能进入系统，必须先设一个新密码。登录状态存在服务端，管理员可以主动把某人踢下线。连续输错密码会被临时锁定。

**Blocked by:** 09 — 用户账号与临时密码

**Status:** done (backend). The login and change-password **screens** are ticket 03's frontend milestone work and are not in this ticket's scope; the API, the cookie and the gate are complete.

**Verification (2026-09-26):**
- `pytest` → 306 passed (38 new across auth, error aliases and security); `ruff check app tests` clean.
- `tools/probe_auth.py` → all 32 checks passed over real HTTP with a cookie jar, and passes on a second consecutive run: the cookie is httpOnly, SameSite=Lax and opaque; an unknown username and a wrong password are indistinguishable to a caller; a correct password during lockout is still refused and the message states the remaining time; the gate refuses every endpoint with `ERR_SES_002` while `must_change_password` is set and lifts once the password changes; a weak password is refused naming every broken rule; the device that changed the password stays signed in while other devices get `ERR_SES_001`; disabling an account ends its session at once; logout is idempotent; and every outcome is audited with the client address.
- Session revocation is a comparison, not a scan: a session carries the epoch it was issued under, and a password change, reset or deactivation moves the epoch past it.

- [x] 登录成功后，若账号被标记为必须改密，则**只能**进入改密页面，直接访问其他任何页面都会被拦回
- [x] 新密码策略：至少 8 位，且同时包含大写、小写、数字、特殊符号；不满足时逐条给出西/英提示
- [x] 新密码不得与当前密码相同
- [x] 改密成功后，该账号在其他设备上的会话全部失效（改密的设备本身保留，否则用户会被自己踢出）
- [x] 会话存于服务端（Redis）并以 httpOnly Cookie 承载，浏览器脚本无法读取凭证
- [x] 连续 5 次登录失败后锁定 15 分钟，锁定期间即使密码正确也拒绝登录并提示剩余时间
- [x] 登录成功、登录失败、登出、改密、被强制登出五类事件均写入审计日志（含 IP 与客户端信息）
- [ ] 登录页与改密页支持西/英双语 → **前端，随票据 03 的里程碑交付**

**Two design decisions worth recording:**
1. **The gate is applied to every route, not per route.** It is an application-level dependency, so an endpoint added later cannot be accidentally exempt — which is the failure mode the requirement exists to prevent.
2. **Lockout fails open when Redis is unreachable.** A cache outage must not lock every account out of the system; the password check still runs. The alternative turns a cache blip into a total outage.

**Six defects found and fixed while verifying:**
1. `AccountErrorCode` was missing four aliases (`ACCOUNT_LOCKED`, `ACCOUNT_INVALID_CREDENTIALS`, `ACCOUNT_DISABLED`, `ACCOUNT_PASSWORD_REUSED`). A missing alias is invisible until the exact branch that raises it runs, at which point it is an `AttributeError` inside a 500. `tests/test_error_aliases.py` now derives the expected set from the catalogue for every domain.
2. `PASSWORD_CHANGE_REQUIRED` was defined twice in the catalogue, which is a `TypeError` at import time.
3. The three domain alias classes used three different naming conventions. Unified on "the alias name is the catalogue name".
4. `messages.py` had a duplicated dictionary key; the second silently won. Ruff's `F601` caught it.
5. The forced-change guard opened its own database session, so it could not see rows the route's session saw. It now resolves the same overridden session the routes use.
6. **`conftest` used `setdefault` on `DATABASE_URL`, which the container already exports.** The default never applied, so a full test run wrote **181 audit rows into the development database**. Now assigned, and `test_database.py` asserts the invariant that actually matters instead of asserting the old bug. This is the most serious defect of the round: tests were mutating development data, and the old test documented that as expected behaviour.

**Probe hygiene:** `probe_auth` trips the lockout on purpose, and the counters live in Redis beyond the process. Both auth-related probes now clear their own counters and rows before running, so a second run measures the same thing as the first.
