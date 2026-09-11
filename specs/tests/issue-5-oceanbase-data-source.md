# 测试计划：OceanBase 数据源接入 MCP

- 关联设计：`specs/designs/issue-5-oceanbase-data-source.md`
- 关联需求：requirement `8ea117e2-50c5-4d9b-81c3-6be5d580653c`（GitLab issue #5）
- work_branch：`feature/issue-5-oceanbase-mcp-source-integration`
- 状态：规划（尚未进入实现）

## 1. 测试目标（验收标准）

直接采用需求 §7 的 A01–A13 作为验收标准（AC）：

| 编号 | 场景 | 通过标准 |
|---|---|---|
| A01 | 正确连接及错误连接 | 正确账号成功；密码错误、地址不通、模式不符分别返回可辨识错误，并在期限内结束 |
| A02 | 元数据及刷新 | 表、视图、注释、字段类型与数据库一致；修改测试表后刷新可见；无权限对象不泄露 |
| A03 | 普通查询和参数绑定 | 单表、关联、聚合、WITH、中文、日期及 NULL 结果正确；特殊字符参数不改变 SQL 结构 |
| A04 | 只读和范围限制 | 多语句、写入、锁定查询、范围外对象及未支持语法被拒绝；数据库账号也无写权限 |
| A05 | 模板查询 | 正确参数返回基准结果；漏参、错类型、禁用模板、模式不匹配均不执行 |
| A06 | 发布与实际客户端 | initialize、tools/list、tools/call 经 ContextForge 成功；重复发布不产生重复记录；目标 Agent 能读取结果和错误 |
| A07 | 超时和取消 | 长查询触发期限后，客户端收到超时，**数据库查询在清理期限内停止**，连接或执行资源回收 |
| A08 | 断线和恢复 | 活跃连接断开及空闲连接失效均可识别；不重放已发送 SQL；网络恢复后新请求可成功 |
| A09 | 并发及池满 | 达到既定成功率与耗时目标；连接预算不超限；过载请求有界等待后返回错误 |
| A10 | 大结果和类型 | 行数、字节、长文本限制生效且显式截断；小数及大整数不丢精度，未支持类型不静默丢失 |
| A11 | 换密及配置切换 | 新版本验证失败保留原有效配置；成功切换后旧池释放，日志和导出均无凭据明文 |
| A12 | 停用与多源隔离 | 停用拒绝新请求并取消在途查询；篡改 `source_id` 不得越权；一个数据源故障不拖垮另一数据源 |
| A13 | 重启与回归 | 重启后配置可恢复且无重复注册；新增能力可回退；已有 gRPC、HTTP 和 MCP 冒烟通过 |

**验收记录内容**（需求 §7）：版本、配置、输入、预期、实际输出、请求编号与必要日志。

## 2. 分层测试

分层原则：能在低层锁定的语义不放高层；只有真实数据库/真实网关才能验证的语义不放在低层假装通过。

| 层 | 目标 | 依赖 | 是否进 `make test` |
|---|---|---|---|
| L1 单元 / 路由级 | SQL 只读判定、结果与错误契约、配置版本语义、发布幂等、授权校验 | 无外部依赖（stub 驱动 + SQLite 替身） | 是 |
| L2 适配器集成 | 驱动能力、元数据、参数绑定、超时与取消、类型保真 | **真实 OB（或经确认的等价替身）** | 否（默认 skip，环境开关开启才跑） |
| L3 live-gateway 黑盒 E2E | 经 ContextForge 的 MCP 全链路与发布幂等 | **运行中的 ContextForge + OB 适配服务** | 否（`tests/live_gateway` 被 `PYTEST_IGNORE` 排除，`Makefile:920-921`） |
| L4 故障注入与性能基线 | 连接预算、成功率、P95、故障隔离、重试边界 | 固定测试环境（需求 §5） | 否（按需触发） |
| L5 目标 Agent 平台验收 | A06 的「目标 Agent 能读取结果和错误」 | 目标 Agent 平台 | 否（手工/平台侧） |

### 2.1 L1 单元 / 路由级

建议文件：`tests/unit/mcpgateway/services/test_ob_database_service.py`、`tests/unit/mcpgateway/routers/test_ob_sources_router.py`，并复用 `tests/unit/mcpgateway/services/test_sql_data_service.py` 的既有手法（`monkeypatch.setattr` 于 `settings` 与引擎构造；路由层直接调用 `handler.__wrapped__(...)` 并构造 `{"email":..., "is_admin":...}` 用户字典）。

| 覆盖 | 用例要点 | AC |
|---|---|---|
| 配置模型 | `compat_mode` 必填且仅 `mysql`/`oracle`；`engine` 固定 `oceanbase`；`port` 无写死默认；未通过验证的模式被拒绝连接与发布 | A01 A05 |
| 只读判定（**核心**） | 表驱动拒绝：多语句、DML、DDL、CALL、事务/会话修改、`SELECT FOR UPDATE`、文件导出、无法确认只读性质的语法/函数；并显式断言**不是**靠 `SELECT` 前缀判断（构造形如 `WITH ... DELETE`、`SELECT ... INTO OUTFILE`、注释绕过用例） | A04 |
| 参数绑定 | 特殊字符参数（引号、分号、注释符、Unicode）不改写 SQL 结构；动态表名/字段名必须来自已授权标识符集合 | A03 A04 |
| 结果契约 | 列按序号与二维 `rows` 对齐（含重复列名）；DECIMAL/大整数按字符串；日期 ISO 8601 且不伪造时区；NULL 保留；长文本截断标记；未支持类型返回 `UNSUPPORTED_RESULT_TYPE` 而非丢列 | A10 |
| 截断语义 | `max_rows`/`max_result_bytes` 生效并置 `truncated=true` + 原因；`returned_rows` 语义为本次返回行数；服务端硬上限取调用与配置的较小值 | A10 |
| 错误码 | 12 个错误码（`DB_AUTH_FAILED`…`REGISTRATION_FAILED`）各自可触发且 `success=false`/`data=null`/`error.stage`/`retryable` 正确；MCP `isError=true` 仅用于工具执行失败 | A01 A07 |
| 模板 | 漏参、参数类型不符、模板未启用、租户模式不匹配均不进入数据库；`query_mode` 为 `template_only` 时受限 SQL 工具不可用 | A05 |
| 配置版本 | 新版本验证失败**保留原有效版本**并显示失败原因；成功后 `config_version` 递增 | A11 |
| 发布幂等 | 重复发布/注册重试不产生重复数据源、工具或虚拟服务（稳定标识 + upsert）；停用后服务端拒绝访问（即使客户端仍缓存工具） | A06 A12 |
| 授权 | 篡改/伪造 `source_id` 不得越权；固定数据源发布形式隐藏该参数并由服务端注入；工具入参中不出现地址/账号/密码/密钥引用 | A12 |
| 脱敏 | 配置查询、导出、日志、工具描述均不含凭据明文 | A11 |

### 2.2 L2 适配器集成（需真实 OB）

建议文件：`tests/integration/ob/test_ob_driver_capability.py`、`test_ob_metadata.py`、`test_ob_query_timeout_cancel.py`，以环境变量门控（例如 `OB_TEST_HOST`/`OB_TEST_COMPAT_MODE` 等），未配置时整体 `pytest.skip`；标记 `slow`/`integration`。**不得指向生产库**（`tests/AGENTS.md`）。

| 覆盖 | 用例要点 | AC |
|---|---|---|
| 驱动能力（任务 1 的前置） | 在**目标租户模式**上逐项证明：连接、参数绑定、语句超时、取消、类型支持；保留版本记录 | A01 A07 |
| 连接与错误分类 | 正确账号成功；密码错误→`DB_AUTH_FAILED`；地址不通→`DB_UNREACHABLE`；配置模式与实际不符→`UNSUPPORTED_COMPAT_MODE`/明确错误；均在期限内结束 | A01 |
| 元数据 | 表/视图、字段名、原生类型、长度、精度、小数位、可空、注释、可读取主外键与数据库一致；标识符大小写与引用语义保留；注释/约束缺失返回空值而非推断 | A02 |
| 刷新 | 修改测试表后刷新可见（新增/删除/改名/字段类型变化可被工具查询到）；缓存记录更新时间 | A02 |
| 范围与不泄露 | 无权限对象不出现在列表；跨 Schema/视图/同义词语义明确，无法解析的对象首版不开放 | A02 A04 |
| 查询正确性 | 单表、关联、聚合、WITH、中文、日期、NULL；参数查询返回基准结果 | A03 |
| 账号只读 | 运行账号对目标对象无写权限（以测试 DBA 账号核对） | A04 |
| 模板基准 | 至少 1 条可执行模板返回基准结果 | A05 |
| 超时与取消（**核心**） | 长查询触发期限后，客户端收到 `QUERY_TIMEOUT`；**在清理期限内以数据库侧观测确认查询已停止**（测试 DBA 账号观测会话与运行查询），连接与执行资源回收 | A07 |
| 断线与恢复 | 活跃连接断开、空闲连接失效均可识别；已发送 SQL 不重放；网络恢复后新请求成功 | A08 |
| 大结果与类型 | 行/字节/长文本限制生效且显式截断；小数与大整数不丢精度；未支持类型不静默丢失 | A10 |
| 换密与切换 | 新版本验证失败保留原有效配置；成功切换后旧连接池停止接收请求并在受控时间内释放 | A11 |
| 停用与隔离 | 停用拒绝新查询、取消在途查询、回收连接；一个数据源故障不拖垮另一数据源 | A12 |

> 观测方式说明（需求 §7）：数据库会话与运行查询的观测可由**测试 DBA 账号**完成，不因此给运行账号增加管理权限。

### 2.3 L3 live-gateway 黑盒 E2E

建议文件：`tests/live_gateway/mcp/test_ob_mcp_e2e.py`，沿用既有约定：`pytestmark = [pytest.mark.e2e, skip_no_gateway]`；基址取 `MCP_CLI_BASE_URL`（默认 `http://127.0.0.1:8080`）；参考 `tests/live_gateway/mcp/test_admin_multi_login_e2e.py` 的写法与 `tests/live_gateway/helpers/mcp_test_helpers.py` 的 `run_mcp_cli` / `send_jsonrpc_via_wrapper`。

| 用例 | 覆盖 | 断言要点 |
|---|---|---|
| `test_mcp_initialize_and_tools_list` | A06 | `initialize` 成功；`tools/list` 含 4 类工具且 JSON Schema 完整（参数缺失/类型不符不会进入数据库执行） |
| `test_tools_call_metadata` | A02 A06 | `ob_list_tables`、`ob_describe_table` 返回与基准一致；`structuredContent` 与文本 JSON 同源且可完整解析 |
| `test_tools_call_query_and_template` | A03 A05 A06 | 受限 SQL 与模板调用返回基准结果、模板版本正确 |
| `test_error_surface_via_gateway` | A01 A04 A07 | 越权/非法 SQL/超时分别返回可辨识错误码与 MCP `isError=true`；协议层错误交由 SDK 处理 |
| `test_publish_is_idempotent` | A06 | 重复发布/注册重试后数据源、工具、虚拟服务数量不变 |
| `test_disable_blocks_access` | A12 | 停用后即使客户端仍持缓存工具定义，调用仍被服务端拒绝 |
| `test_source_isolation` | A12 | 一个数据源故障时另一数据源的发现与调用仍正常，工具返回明确错误而非阻塞 |

运行（需运行中的网关 + OB 适配服务）：

```bash
uv run --frozen pytest tests/live_gateway/mcp/test_ob_mcp_e2e.py -v -s
```

> `tests/live_gateway` 被 `make test` 排除（`PYTEST_IGNORE`，`Makefile:920-921`），必须单独触发；这与 CLAUDE.md「凡行为可经运行中的网关验证的 PR 必须包含黑盒测试」的要求一致。

### 2.4 L4 故障注入与性能基线

按需求 §5「首版稳定性验证」执行，在固定测试环境上先测直连数据库基线，再测适配服务与 ContextForge 全链路：

- 负载：已知结果、直连耗时不超过 100 ms 的简单查询；建议 **10 个并发调用方连续 30 分钟、累计不少于 10000 次调用**。
- 验收目标（**待评审指标**，实施前记录环境规格、数据规模及参数）：成功率不低于 **99.9%**，全链路 **P95 不超过 2 秒**。
- 运行期间连接数不得超过配置总预算；结束后无连接泄漏、持续挂起查询或未释放的执行任务。
- **故障注入与故意非法请求单独统计**，不计入上述成功率。
- 故障注入用例：池满（过载请求有界等待后返回 `POOL_EXHAUSTED`）、查询中杀连接、超时取消、换密重连、重启后核对不重复注册。

### 2.5 L5 目标 Agent 平台验收

A06 必须在**目标 Agent 平台**完成；单独 `curl` 成功或其它客户端调用成功**不能替代**该项验收（需求 §7）。记录目标 Agent 能正确读取结果与错误。

## 3. 门禁与命令

| 阶段 | 命令 |
|---|---|
| 单元/路由级 | `make test`（或定向 `uv run --frozen pytest tests/unit/mcpgateway/services/test_ob_database_service.py -q`） |
| 代码质量 | `make ruff bandit interrogate pylint verify`（提交前对改动文件） |
| 迁移 | `cd mcpgateway && alembic heads` 必须单一 head，再 `make test` |
| live E2E | `uv run --frozen pytest tests/live_gateway/mcp/test_ob_mcp_e2e.py -v -s` |
| 协议与 RBAC 面 | `make test-mcp-protocol-e2e test-mcp-rbac`（网关可用时） |
| Rust 侧（若涉及） | `cd tools_rust/mcp_runtime && cargo fmt --check && cargo clippy -- -D warnings && cargo test` |

## 4. 退出标准

1. A01–A13 全部有对应用例；L1 全绿，L2/L3 在可用环境上通过，未通过项**记录豁免原因**（不得静默跳过）。
2. A06 在目标 Agent 平台上完成并通过（不可用其它客户端替代）。
3. 代码质量门禁干净；Alembic 单一 head。
4. 验收记录齐备：版本、配置、输入、预期、实际输出、请求编号与必要日志。
5. 已有 gRPC、HTTP 与 MCP 冒烟通过（A13 回归部分）。
6. 回退路径可执行：停用 OB 接入并撤销新增发布绑定，迁移回退不破坏已有数据。

## 5. 当前不可执行项（阻塞于待补输入）

以下用例在需求 §8「实施前输入」补齐前**无法真正运行**，届时先补环境再判定，不得以替身结果宣称通过：

| 项 | 阻塞原因 | 影响的 AC |
|---|---|---|
| L2 全部 | 缺 OB 环境与租户模式，无法完成驱动验证 | A01 A02 A03 A04 A05 A07 A08 A10 A11 A12 |
| L3 全部 | 需可访问 OB 的适配服务与运行中的 ContextForge | A06 A12 |
| L4 | 缺并发量、连接预算、结果上限、超时与日志保留要求 | A09 |
| L5 | 缺目标 Agent 与协议配置 | A06 |

> 说明：L1 不依赖外部环境，可在待补输入补齐前先落地，用于锁定只读判定、结果契约、错误码与发布幂等等确定性语义。

## 6. 备注：与需求正文的对应关系

本计划不重复需求的验收标准文本，只补充「怎么测、在哪测、用什么断言、什么算通过」。需求 §5 的稳定性建议值（如 `max_rows=1000`、`max_result_bytes=1 MiB`、`query_timeout_s=30`）为实现初值，正式阈值须由实际数据库与 Agent 超时共同确定（见设计文档 §6）。
