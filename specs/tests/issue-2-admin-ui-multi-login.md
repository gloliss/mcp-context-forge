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

### 2.2 live-gateway 黑盒 E2E（待实施）

文件：`tests/live_gateway/mcp/test_admin_multi_login_e2e.py`（拟）

前置：ContextForge 已运行（`BASE_URL`，默认 `http://127.0.0.1:8080`），使用 `skip_no_gateway` 在无网关时跳过。

步骤（两个相互独立的 HTTP 会话）：
1. 会话 A、B 分别 `GET /admin/login` 取 CSRF，再 `POST /admin/login` 用同一 admin 账号登录，各持一份 `jwt_token` cookie。
2. 断言 A 的 cookie 能访问 `/admin`（非重定向到登录页）——**证明第二次登录未顶掉第一次**。
3. 用 A 的 cookie `POST /admin/logout`（带 CSRF）。
4. 断言 A 的 cookie 再访问 `/admin` 被拒（重定向登录 / 401），B 的 cookie 仍可访问——**证明登出隔离**。

### 2.3 手工验证

两个无痕窗口 / 两台设备同时用同一 admin 登录，确认：任一窗口操作不影响另一窗口；任一窗口登出，另一窗口仍在线。

## 3. 门禁与命令

- 单元：`make test`（或上面的定向 pytest）。
- live E2E：`uv run --frozen pytest tests/live_gateway/mcp/test_admin_multi_login_e2e.py -v -s`（需运行中的网关）。
- 代码质量：`make ruff bandit interrogate pylint verify`。

## 4. 退出标准

AC-1/AC-2/AC-3 全部有对应用例且通过；lint 干净；live E2E 在真实网关上通过（或记录豁免原因）。
