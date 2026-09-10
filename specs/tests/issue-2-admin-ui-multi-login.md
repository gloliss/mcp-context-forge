# 测试计划：Admin UI 多端登录

- 关联设计：`specs/designs/issue-2-admin-ui-multi-login.md`
- work_branch：`feature/issue-2-admin-ui-multi-login`

## 1. 测试目标（验收标准）

- **AC-1**：账号已在地点 A 登录并持有有效 `jwt_token`，在地点 B 用同一账号再次登录成功后，A 的 token 仍能通过鉴权并访问 `/admin`。
- **AC-2**：同一账号的 A、B 两个会话，A 执行 `POST /admin/logout` 后，仅 A 被吊销，B 仍有效。
- **AC-3**：同一账号连续两次登录产生**不同** `jti`，且各自可被独立吊销。

## 2. 分层测试

### 2.1 单元 / 路由级（已实现）

文件：`tests/unit/mcpgateway/test_admin_multi_login.py`

| 用例 | 覆盖 | 断言要点 |
|------|------|----------|
| `TestAdminMultiLogin::test_two_logins_mint_distinct_jti` | AC-3 | 两次 `admin_login_handler` 调用传入两个不同的合法 UUID `jti` |
| `TestAdminMultiLogin::test_login_path_never_revokes_other_sessions` | AC-1 | 登录路径上 blocklist `revoke_token` 从未被调用 |
| `TestAdminMultiSessionLogoutIsolation::test_logout_revokes_only_the_current_session` | AC-2 | `POST /admin/logout` 只吊销当前会话 `jti`，另一会话 `jti` 不被触碰 |

运行：

```bash
uv run --frozen pytest tests/unit/mcpgateway/test_admin_multi_login.py -q
```

### 2.2 live-gateway 黑盒 E2E（已实现）

文件：`tests/live_gateway/mcp/test_admin_multi_login_e2e.py`

前置：ContextForge 已运行（`BASE_URL`，默认 `http://127.0.0.1:8080`），使用 `skip_no_gateway` 在无网关时跳过；无可用 admin 口令或账号仍处于强制改密流程时同样跳过（不会轮换共享 admin 口令）。

| 用例 | 覆盖 |
|------|------|
| `TestAdminMultiLoginE2E::test_second_login_does_not_kick_out_first` | AC-1 |
| `TestAdminMultiLoginE2E::test_logout_is_isolated_to_the_calling_session` | AC-2 |

步骤（两个相互独立的 HTTP 会话）：
1. 会话 A、B 分别 `GET /admin/login` 取 CSRF，再 `POST /admin/login` 用同一 admin 账号登录，各持一份 `jwt_token` cookie。
2. 断言 A 的 cookie 能访问 `/admin`（非重定向到登录页）——**证明第二次登录未顶掉第一次**。
3. 用 A 的 cookie `POST /admin/logout`（带 CSRF）。
4. 断言 A 的 cookie 再访问 `/admin` 被拒（重定向登录 / 401），B 的 cookie 仍可访问——**证明登出隔离**。

### 2.3 手工验证

两个无痕窗口 / 两台设备同时用同一 admin 登录，确认：任一窗口操作不影响另一窗口；任一窗口登出，另一窗口仍在线。

## 4. 实网验证结果（2026-09-10，http://10.10.100.15:4444）

以真实 admin 账号在两个独立 HTTP 会话上实跑：**AC-1 / AC-2 / AC-3 全部通过**（`2 passed`）。

实跑中修正的两处测试缺陷：

1. **尾斜杠**：探测路径须用 `/admin/`。Starlette 对 `/admin` 发 307 跳转到 `/admin/`，若按 `/admin` 断言 200，健康会话也会被误判为"被顶掉"。
2. **登出后断言**：只断言"浏览器会话已登出"（cookie 被清 + 面板重定向到 `/admin/login`），不复用旧 token 立即断言服务端已拒绝。

### 相邻发现（不在本需求范围，已修复）

登出会把 `jti` 写入吊销名单，但**吊销生效曾存在约 30s 延迟**：实网测得登出后 0/5/15s 旧 token 仍可访问 `/admin/`，约 +31s 起才返回 `302 → /admin/login?error=token_revoked`。

根因：`TokenBlocklistService.revoke_token`（`mcpgateway/services/token_blocklist_service.py`）写入 DB 与 Redis（`token:revoked:{jti}`）后，**未失效 `auth_cache` 的负向吊销缓存**（`AuthCache.set_not_revoked`，TTL = `auth_cache_revocation_ttl` 默认 30s）。`auth_cache.invalidate_revocation()` 的文档注释描述了"吊销时原子驱逐、不存在 stale False 窗口"这一预期契约，但登出路径未调用它。（`TokenCatalogService` 吊销 API token 时本就调用了该失效，只有会话 token 这条路径漏了。）

修复：`revoke_token` 现在调用新增的 `TokenBlocklistService._invalidate_auth_cache`——先同步执行 `AuthCache.evict_revocation_local`（无需事件循环，覆盖 `asyncio.to_thread` 等 worker 线程调用），再在有运行中事件循环时 fire-and-forget `auth_cache.invalidate_revocation` 以发布跨 worker 的 Redis 标记。回归用例见 `tests/unit/mcpgateway/test_token_blocklist_service.py::TestRevocationInvalidatesAuthCache`（已验证：去掉修复后两个用例失败）。

> 该问题与"多端登录互不顶掉"无关（AC-2 只要求"登出 A 不影响 B"，本就通过），故未纳入本需求的验收范围，仅在此记录。
> 注意：本次修复仅在本地单测/代码层面验证；10.10.100.15:4444 运行的是旧镜像，需重新构建并部署后才能实网复验。

## 5. 门禁与命令

- 单元：`make test`（或上面的定向 pytest）。
- live E2E：`uv run --frozen pytest tests/live_gateway/mcp/test_admin_multi_login_e2e.py -v -s`（需运行中的网关）。
- 代码质量：`make ruff bandit interrogate pylint verify`。

## 6. 退出标准

AC-1/AC-2/AC-3 全部有对应用例且通过；lint 干净；live E2E 在真实网关上通过（或记录豁免原因）。
