# 设计文档：Admin UI 多端登录（一处登录不顶掉另一处）

- 关联需求：`admin ui的账号可以在多处登陆`（GitLab issue #2）
- work_branch：`feature/issue-2-admin-ui-multi-login`

## 1. 背景与目标

同一 admin 账号需要能够在多个地点（不同浏览器 / 设备 / 网络）同时登录 Admin UI；任一新登录**不得**使其它地点的既有会话失效（"顶掉"）。

## 2. 现状分析（代码事实）

- **登录链路**：`POST /admin/login` → `admin_login_handler`（`mcpgateway/admin.py:4607`）。每次登录都新铸 `session_jti = uuid.uuid4()`（`admin.py:4749`），经 `create_access_token(user, jti=session_jti)` 生成无状态 JWT（`admin.py:4752`），写入 `jwt_token` cookie（`admin.py:4759`）。
- **令牌形态**：`create_access_token`（`mcpgateway/routers/email_auth.py:131`）JWT 声明含 `sub/iss/aud/iat/exp`、`jti`（每次随机）、`last_activity`、`auth_provider`、`token_use: "session"`、`scopes`。
- **会话校验**：`get_current_user`（`mcpgateway/auth.py:1385`）只做 JWT 签名 + `exp` + `jti` 吊销名单（`TokenBlocklistService.is_token_revoked`，`mcpgateway/services/token_blocklist_service.py:164`）。
- **无用户级会话记录**：`EmailUser`（`mcpgateway/db.py:1471`）没有 `session_version` / `token_version` 字段。
- **登出**：`_admin_logout`（`mcpgateway/admin.py:4934`）只吊销当前 cookie 的 `jti`（`admin.py:5116`，reason=`admin_logout`）。
- **`password_reset_invalidate_sessions`**（`mcpgateway/config.py:1099`）在密码重置后只清 auth 缓存（`email_auth_service.py:1172`），不吊销已发放 JWT。

## 3. 设计决策

**保持无状态 JWT + `jti` 吊销名单模型，不引入服务端单会话强制。**

理由：

1. 现有架构已天然满足"多端并存、互不顶掉"——每次登录独立 `jti`，登录路径不做任何吊销，登出仅影响自身会话。
2. 引入用户级"当前会话"字段或"登录时吊销其它会话"会**破坏**本需求，且增加分布式一致性与竞态风险。
3. 真正缺口是该行为属**隐式、未被测试锁定、未文档化承诺**，未来可能被静默回归。

因此落地范围是"显式承诺 + 回归锁定 + 文档"，不改动核心认证逻辑。

## 4. 改动范围

| 类别 | 内容 | 状态 |
|------|------|------|
| 回归测试（单元/路由级） | `tests/unit/mcpgateway/test_admin_multi_login.py`：登录路径不吊销、两次登录 `jti` 互异、登出仅吊销当前 `jti` | 已实现 |
| 黑盒 E2E（live gateway） | `tests/live_gateway/mcp/test_admin_multi_login_e2e.py`：登录 A → 登录 B → A 仍有效；A 登出 → B 仍有效 | 已实现 |
| 文档 | 本设计文档 + 测试计划；认证契约说明 | 本文档 |
| 源码 | 无（刻意不改） | — |

## 5. 非目标与边界

- 不改 API token / MCP 会话语义（仅 Admin UI 浏览器会话）。
- 同一浏览器（同一 cookie jar）内多次登录会覆盖同名 `jwt_token` cookie，属浏览器行为，不在服务端范围。
- 密码重置是否"全端下线"是独立安全议题，另立需求。

## 6. 验证方式

见 `specs/tests/issue-2-admin-ui-multi-login.md`。核心为：单元/路由级用例 + live-gateway 黑盒用例 + 手工双浏览器验证。
