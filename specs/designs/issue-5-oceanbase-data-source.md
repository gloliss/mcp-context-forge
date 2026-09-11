# 设计文档：OceanBase 数据源接入 MCP

- 关联需求：requirement `8ea117e2-50c5-4d9b-81c3-6be5d580653c`（GitLab issue #5）《OceanBase 数据源接入 MCP 开发需求单》
- work_branch：`feature/issue-5-oceanbase-mcp-source-integration`
- 状态：规划（尚未进入实现）
- 说明：需求单声明「接口名称、配置字段和运行参数为建议设计；落地时应映射到当前分支的实际结构」。本文档即完成该映射，所有「现状」结论均带代码位置。

## 1. 背景与目标

在 ContextForge 中增加受治理的 OceanBase（下称 OB）数据源能力：管理员配置连接并完成验证后，把**表结构查询、受限只读 SQL、固定查询模板**发布为 MCP Tool，供现有 Agent 调用。账号由服务端保管，连接、查询与结果转换由确定性代码执行。

**P0（首版必需）**：数据源新增/维护、测试连接、表与视图元数据读取、受限只读 SQL、固定查询模板、MCP 发布与停用、连接池与超时、调用日志与错误输出。

**P1（后续）**：另一租户模式完整适配、模板可视化编辑、大结果下载、业务用户数据库身份透传、复杂报表聚合、高可用扩容。

**非目标（首版不做）**：数据写入、存储过程、DDL、自动生成业务口径、本体/Text2SQL 推理链路。

**首要约束**：租户模式必须显式配置为 `mysql` 或 `oracle`。首版只对**实际环境验证通过的那一种模式**负责；另一模式在界面标记不可用，服务端拒绝连接与发布。仅凭「兼容 Oracle」不能认定原 Oracle 驱动与 SQL 可直接复用（需求 §1）。

## 2. 现状分析（代码事实）

### 2.1 本分支已有的 SQL 数据源子系统

本 fork 已实现一套「受治理的外部 SQL 数据源」能力，默认关闭（`MCPGATEWAY_SQL_API_ENABLED=false`）：

| 关注点 | 位置 |
|---|---|
| 数据模型 `SQLDataSource` / `SQLTable` / `SQLRelation` / `APISQLTableBinding` | `mcpgateway/db.py:5448` / `:5470` / `:5504` / `:5522` |
| 服务 `SQLDataService`（全部 classmethod，同步） | `mcpgateway/services/sql_data_service.py`（1184 行） |
| 管理路由 `/admin/sql/*` 与数据路由 `/api/v1/data/*` | `mcpgateway/routers/sql_data.py:42-43` |
| Schema 定义 | `mcpgateway/schemas.py:8900-9072` |
| 权限常量 `ADMIN_SQL_SOURCES` / `SQL_TABLES_READ` / `SQL_TABLES_MANAGE` | `mcpgateway/db.py:1390` / `:1397` / `:1398` |
| 权限种子（`team_admin` 含 `sql.tables.read/manage`） | `mcpgateway/services/bootstrap_db.py:400-401` |
| 建表迁移 `c8d9e0f1a2b3` | `mcpgateway/alembic/versions/c8d9e0f1a2b3_add_grpc_sql_debug_platform.py` |
| 管理 UI（「SQL Data」页签 + `dataOperations.js`） | `mcpgateway/templates/admin.html:7622-7652`、`mcpgateway/admin_ui/dataOperations.js:40-274` |
| 主要测试 | `tests/unit/mcpgateway/services/test_sql_data_service.py`（833 行，真实 SQLite 文件） |

现有能力要点：

- **凭据存储**：`SQLDataSource.connection_url` 为 `EncryptedText()`，另有 `masked_url` 用于无凭据渲染；`dialect`、`enabled`、`reachable`、`last_error`、`last_tested_at`、`last_discovered_at` 独立记录。
- **方言支持**：白名单 `SUPPORTED_SQL_DIALECTS = {"postgresql+psycopg", "mysql+pymysql", "sqlite+pysqlite"}`（`sql_data_service.py:44`），且该白名单在 `schemas.py:8919` 与 `:8947` 重复了两份。
- **无引擎适配器抽象**：方言差异以 `if/elif` 分支散落在 6 处（白名单 `:44`、URL 校验 `:168-199`、`_engine` 连接参数 `:214-222`、连接超时钳制 `:132-135`、语句超时 `:474-489`、schema 枚举 `:586-592`），元数据反射本身走 SQLAlchemy 通用路径。
- **执行模型**：`execute()`（`sql_data_service.py:978-1070`）**只构造 SQLAlchemy Core 的 select/insert/update/delete**，用反射列表校验列名与主键，**不接受用户传入的 SQL 文本**；其他操作抛 `SQLDataError("Unsupported SQL operation")`。
- **连接池**：进程内引用计数 LRU 引擎缓存 + `_DeadlineQueuePool`（`pool_pre_ping`、`pool_size`、`max_overflow`、`pool_recycle`、`pool_use_lifo`，`sql_data_service.py:224-239`），池参数为**全局配置**而非按数据源。
- **同步驱动不阻塞事件循环**：执行经 `asyncio.to_thread` 下放（`tool_service.py:7273-7284`）。
- **工具生成与幂等**：`sync_tools()` 按 `sql.{source}.{schema}.{table}.{operation}` 命名生成 `Tool`（`integration_type="SQL"`），以 `(sql_table_id, source_operation)` 做「查存量→更新或创建」的 upsert（`sql_data_service.py:848-924`），不产生重复行。

### 2.2 与需求的差距：为什么不能直接复用现有执行路径

| 维度 | 现有 `sql_data` 子系统 | 本需求 | 结论 |
|---|---|---|---|
| 执行模型 | 仅 allowlist Core 语句，用户无法提供 SQL（`:978-1070`） | 受限**只读 SQL**（单条 SELECT / 以查询为主体的 WITH）+ 固定模板 | 需新增专用执行器与 SQL 结构校验 |
| 写能力 | `allow_insert/update/delete` 列存在，公开端点可 PATCH/DELETE（`sql_data.py:477-499`） | 首版**完全禁写** | 若把 OB 源放进 `sql_tables`，等于为 OB 打开写路径 → **必须隔离存储与端点** |
| 取消语义 | 仅 driver 级 statement timeout；客户端取消时 `asyncio.shield` **等待线程跑完**，不终止 DB 查询（`tool_service.py:7273-7284`） | 「应用层返回超时后，数据库端查询也必须停止。仅让异步等待结束、后台线程继续执行不满足要求」（需求 §5） | 需**显式取消通道**，现有机制不达标 |
| 类型契约 | 结果用 `orjson.dumps(default=str)` 序列化（`:1055` 起） | DECIMAL/大整数按字符串保精度、日期 ISO 8601、NULL 保留、截断显式标注、未支持类型 `UNSUPPORTED_RESULT_TYPE` | 需专用结果序列化层 |
| 连接池预算 | 池参数全局（`settings.mcpgateway_sql_pool_*`） | **每数据源**每进程独立池与总预算 | 需按源配置 |
| 租户模式 | 无 `compat_mode` 概念 | `engine=oceanbase` + `compat_mode ∈ {mysql, oracle}` 必填 | 需新增建模 |
| 查询模板 | 无 | `template_id` + 参数绑定 + 版本/启用状态 | 需新增建模与执行路径 |
| 发布绑定 | `APISQLTableBinding`（工具↔表血缘，`binding_type=auto/manual`） | 数据源↔上游注册项↔工具↔虚拟服务关联 + 同步状态 | 需新增发布绑定模型 |

> 注意：`APISQLTableBinding`（`db.py:5522`）语义是「影响分析/血缘」，`create_binding` 的文档明确写明**不改变执行策略**（`sql_data_service.py:1149-1158`）。它不能承担「发布授权」职责，不要误用。

### 2.3 网关侧发布链路（已具备，可复用）

| 关注点 | 位置与事实 |
|---|---|
| 上游 MCP 注册 | `POST /gateways` → `GatewayService.register_gateway`（`main.py:7632`、`gateway_service.py:1660`）；经 mcp SDK `streamablehttp_client` 连接并物化 `Tool` 行（`gateway_service.py:1787-1900`），`integration_type="MCP"`、`created_via="federation"` |
| 传输支持 | `GATEWAY_SUPPORTED_TRANSPORTS = {"SSE", "STREAMABLEHTTP"}`（`schemas.py:3151-3168`）→ 需求「首选 Streamable HTTP」可直接满足 |
| 注册幂等 | url+凭据唯一性检查 `_check_gateway_uniqueness`（`gateway_service.py:1374`）；name/slug 冲突行锁（`:1457`）；DB 约束 `uq_team_owner_slug_gateway`（`db.py:4969`）；异步路径对同 slug 返回既有 pending 行（`:1727-1729`） |
| 虚拟服务 | `Server` 组合已注册项（`db.py:4537`、`server_service.py:516`），挂载于 `/servers/{id}/mcp` |
| 工具命名 | 网关前缀 `{gateway_slug}{gateway_tool_name_separator}{tool}`（`db.py:3542-3577`，分隔符默认 `-`，`config.py:3300`） |
| 结构化结果载体 | `Tool.output_schema`（`db.py:3432`）、`CallToolResult.structured_content`（`common/models.py:659`）、出口校验 `ToolService._extract_and_validate_structured_content`（`tool_service.py:1863`） |
| 独立 MCP 服务的既有落地方式 | `mcp-servers/python/*`（如 `data_analysis_server`）+ cookiecutter 模板与 `mcp-servers/scaffold-python-server.sh` |

## 3. 设计决策

**D1 — 形态：独立 `ob-database-mcp` 适配服务，ContextForge 负责配置与发布。**
采纳需求 §2 的建议。落点：仓库内 `mcp-servers/python/ob_database_mcp/`（沿用既有独立 MCP 服务布局），部署为独立进程/容器，需能访问配置的 OB 或 OBProxy 地址。理由：OB 驱动依赖、每源连接池预算、取消通道与同步驱动必须与网关主进程隔离；且「一个数据源故障不耗尽其他数据源资源」在独立进程内更易保证。

**D2 — 配置权威：ContextForge DB 为唯一来源，新增 OB 专用表，不复用 `sql_tables`。**
需求 §4 要求「配置须只有一个权威来源」。选择新表而非扩展 `sql_data_sources`/`sql_tables`，核心是**安全隔离**：现有 `sql_tables` 关联的公开写端点（`sql_data.py:477-499`）与 `allow_*` 策略会为 OB 源打开首版明确禁止的写路径。
复用（不重造）：`EncryptedText` + `AUTH_ENCRYPTION_SECRET` 凭据存储、`masked_url` 式脱敏渲染、`SecurityValidator` 的 SSRF/TLS 校验、RBAC 装饰器与权限播种模式、`ToolService` 的审计/观测/指标流水线。**不新建密钥平台**（需求 §3.OB01）。

**D3 — 适配服务取配置：由适配服务持服务令牌，从 ContextForge 内部管理 API 拉取，带 `config_version` 校验。**
优点：凭据不落到适配服务磁盘；服务重启后可自恢复；版本校验可发现漂移。不采用「共享 DB 直读」（耦合 schema）与「推送」（推送状态易与权威库分歧）。该内部 API 需独立鉴权，且不得对外暴露。

**D4 — 传输：Streamable HTTP 优先**（`GATEWAY_SUPPORTED_TRANSPORTS` 已含），注册为上游 MCP gateway，再由虚拟服务组合暴露给 Agent。若目标网关仅透传文本，须按需求 §4 验证 JSON 可完整解析并记入兼容矩阵。

**D5 — 只读判定：SQL 结构解析 + 数据库账号权限双保险。**
明确禁止「只通过字符串以 SELECT 开头判断安全性」（需求 §4.OB04）。解析层拒绝多语句、DML、DDL、CALL、事务/会话修改、`SELECT FOR UPDATE`、文件导出及无法确认只读性质的语法与函数；同时运行账号本身只有目标对象查询权限。

**D6 — 取消与超时：显式取消通道，且必须在目标模式上验证。**
MySQL 模式考虑用独立连接对被取消查询执行 `KILL QUERY`；Oracle 模式考虑驱动层 `cancel()`/OCIBreak 等机制。**具体机制以 spike 结果为准**（见 §10）。无法证明 DB 端终止能力的模式不进入稳定版验收。这会补上现有子系统在 `tool_service.py:7273-7284` 处的语义缺口。

**D7 — 结果契约：统一 envelope，`structuredContent` 与文本 JSON 同源。**
`{schema_version, request_id, source_id, success, data, error}`；查询结果列按序号与二维 `rows` 对齐以支持重复列名；失败时 `success=false`、`data=null`、`error={code, message, stage, retryable}`，并置 MCP `isError=true`。

**D8 — 工具授权：`source_id` 由服务端绑定/注入，优先 1 个虚拟服务绑定 1 个数据源。**
固定数据源的发布形式可隐藏 `source_id` 参数（需求 §4）。聚合多数据源时，服务端按调用身份校验可访问集合，**绝不信任 Agent 传入的 `source_id`**。地址、账号、密码、密钥引用不得出现在任何工具入参中。

**D9 — 与现有 SQL Data API 的关系：并存、互不复用执行路径。**
OB 数据源不出现在 `/api/v1/data/*` 端点，也不使用 `sql.*` 工具命名空间，避免写路径与命名冲突。

## 4. 数据模型与迁移

新增表（逻辑字段映射需求 §4「数据源最小字段」）：

**`ob_data_sources`** — `source_id`（稳定唯一，`name` 变更不改编号）、`name`、`engine`（固定 `oceanbase`）、`compat_mode`（`mysql`|`oracle`，必填）、`host`/`port`（不写死默认端口）、`connect_mode`（`direct`|`obproxy`）、`username`、`database`、`schema`、`credential_ref`、`tls_config`、`allowed_objects`、`enabled_tools`、`query_mode`（`template_only`|`readonly_sql_and_template`）、`pool`/`timeouts`/`limits`、`session_settings`、`enabled`、`config_version`、`last_verified_at`/`last_verification_result`。

**`ob_query_templates`** — `template_id`、`name`、`description`、`compat_mode`、`sql`、参数类型与必填规则、结果字段说明、`version`、`enabled`。首版用受控配置文件或配置表维护即可，模板编辑器列入 P1。

**`ob_publications`** — 数据源 ↔ 上游注册项（gateway）↔ 工具 ↔ 虚拟服务的关联与同步状态，支撑「重复点击/注册重试不产生重复数据源、重复工具或重复虚拟服务」与「停用后服务端阻止访问」。

**权限**：沿用既有命名风格新增 `admin.ob_sources`（源 CRUD/测试/发现，仅平台管理员）、`ob.tables.read`、`ob.tools.manage`；按 `bootstrap_db.py` 的播种模式授予角色（`platform_admin` 通配已覆盖）。

**迁移**：

- 新建 Alembic 迁移，`down_revision` 必须指向当前单一 head `a4b5c6d7e8f9`（已核对：129 个 revision 中唯一 head）。
- 按 CLAUDE.md 的幂等模式写 `upgrade()`（`inspector` 检查表/列存在性后再改），并在 `downgrade()` 中保证**不破坏已有数据**（需求 §6：迁移回退不得破坏已有数据）。
- 若 `downgrade()` 依赖 `settings`，须采用 migration_metadata 配置快照模式。

## 5. 工具与结果契约

首版 4 个逻辑工具（实际暴露名须符合既有命名/命名空间规则并避免跨源重名，参考 `sql.{source}.{schema}.{table}.{op}` 的组织方式）：

| 工具 | 主要输入 | 主要输出 |
|---|---|---|
| `ob_list_tables` | `source_id`、可选 `schema`、`cursor`、`page_size` | 表/视图名、类型、注释、下一页游标 |
| `ob_describe_table` | `source_id`、`schema`、对象名 | 字段、原生类型、注释、可读取约束、元数据时间 |
| `ob_query_readonly` | `source_id`、`sql`、`params`、可选 `max_rows` | 列定义、二维数据行、截断状态、耗时 |
| `ob_run_query_template` | `source_id`、`template_id`、`params` | 查询结果 + 本次使用的模板版本 |

每个工具提供 JSON Schema；参数缺失或类型不符时**不进入数据库执行**。参数一律走驱动绑定，禁止拼接进 SQL；动态表名/字段名必须从已授权标识符中解析验证。

**错误码**（至少区分，需求 §4）：`DB_AUTH_FAILED`、`DB_UNREACHABLE`、`UNSUPPORTED_COMPAT_MODE`、`SOURCE_DISABLED`、`ACCESS_DENIED`、`INVALID_ARGUMENT`、`SQL_NOT_ALLOWED`、`QUERY_TIMEOUT`、`POOL_EXHAUSTED`、`TEMPLATE_NOT_FOUND`、`DB_QUERY_ERROR`、`REGISTRATION_FAILED`。

**类型与截断规则**：DECIMAL 用字符串保精度；整数可能超出 JSON 安全范围时整列以字符串输出并标注类型；日期 ISO 8601（原值不含时区时不伪造 `Z`/偏移）；NULL 保留为 `null`；长文本达上限时显式标注截断；未适配的二进制/复杂类型返回 `UNSUPPORTED_RESULT_TYPE`，**不得静默丢列**。达到行数/字节上限时 `truncated=true` 并给原因；`returned_rows` 只表示本次返回行数，不为总数追加 COUNT，也不承诺任意 SQL 的续页或快照一致性。

## 6. 稳定性设计

**超时梯度**（需求 §5 建议值作为初值，正式阈值须由实际数据库与 Agent 超时共同确定；外层 Agent 与网关超时必须大于适配服务期限并留余量，剩余时间不足时不启动新查询）：`connect_timeout_s=5`、`pool_acquire_timeout_s=3`、`pool_min/pool_max=0/5`（**每数据源每进程**，多进程/多副本须计算总连接预算）、`query_timeout_s=30`、`request_deadline_s=40`（排队+连接+查询+清理共享剩余预算）、`cleanup_timeout_s=5`、`max_rows=1000`、`max_result_bytes=1 MiB`（计入结构化与文本两种表示）、`max_cell_bytes=64 KiB`。

**连接池**：每数据源独立池；借出前有效性检查，失效连接丢弃重建；成功/异常/取消/超时路径都必须释放游标与连接并清理事务与会话状态；`session_settings`（字符集、时区等）在借还连接时保持一致；清理失败丢弃连接，不得把失效连接还池。

**取消**：见 D6。DB 端必须真正停止，仅结束异步等待不算达标。

**恢复与故障隔离**：仅当**尚未向数据库发送 SQL** 时允许对失效连接重建一次；SQL 发出后的超时/断线**不自动重放**。须一并检查 ContextForge、适配服务与 Agent 三层的重试策略，避免叠加重试放大数据库请求。MCP 会话与数据库连接分开管理，每次调用借还连接；一个数据源故障不耗尽其他数据源资源。服务重启后恢复配置与发布关联，并与网关核对以避免重复注册。

## 7. 实施拆分（对齐需求 §6）

| # | 任务 | 必须交付的结果 | 退出条件 |
|---|---|---|---|
| 1 | 核对当前分支与 OB 环境 | 版本清单、租户模式、**驱动验证结果**（连接/绑定/超时/取消/类型）、可复用代码与拟修改位置 | 确定首版验收模式；驱动能力在真实模式上被证明（见 §10 风险） |
| 2 | 实现数据源配置与测试连接 | 配置模型或迁移、凭据引用、管理接口与页面、验证结果输出 | 配置版本切换语义（新版本验证失败保留原有效版本）可用 |
| 3 | 实现适配与查询 | 连接池、元数据、SQL 约束、模板绑定、取消、结果转换 | 只读判定、类型契约、取消终止均有测试证据 |
| 4 | 实现 MCP 发布 | 四类工具契约、数据源授权、网关注册与幂等同步、停用行为 | 重复发布不产生重复记录；停用后服务端阻止访问 |
| 5 | 完成联调与故障验证 | 验收案例结果、性能基线、故障恢复记录、现有功能回归结果 | 见测试计划退出标准 |

**交付内容**（需求 §6）：代码变更、配置或迁移脚本、脱敏配置示例、至少一个可执行的测试查询模板、工具输入输出 Schema、部署与回退说明、版本兼容矩阵、测试记录。

## 8. 回归、回退与兼容

- **回退**：能停用 OB 接入并撤销新增发布绑定；保留必要配置以供恢复；迁移回退不破坏已有数据。
- **回归**：现有 gRPC、HTTP 及其他 MCP 服务需执行针对性回归，确认新增数据源、依赖与路由未影响已有注册、发现及调用。这里要求验证已有能力，**不要求重构现有协议适配主链路**。
- **版本兼容矩阵**：记录 OB 版本/补丁、租户模式、驱动版本、网关传输（Streamable HTTP vs 仅文本透传）、以及已验证/未验证项。无法证明终止能力的模式不进入稳定版验收。

## 9. 非目标与边界

- 不实现写路径：数据写入、存储过程、DDL、`SELECT FOR UPDATE`、文件导出一律拒绝。
- 不做模板可视化编辑、大结果下载、业务用户数据库身份透传、复杂报表聚合、高可用扩容（P1）。
- 不做本体、Text2SQL 推理链路或自动生成业务口径；工具返回实际数据，不补写业务解释或业务结论。
- 不改造现有 `sql_data` 子系统，也不在其上复用执行路径（D9）。
- 查询语法错误直接返回错误，**不由适配服务擅自改写并再次执行**。
- 不新建密钥平台。

## 10. 风险与待验证项

| # | 风险/待验证 | 影响 | 处置 |
|---|---|---|---|
| R1 | 首版实际验收哪种租户模式未定；另一模式驱动/元数据/参数绑定/超时/类型未验证 | 阻塞任务 1 与最终验收口径 | 列入待补输入；未验证模式界面标记不可用、服务端拒绝连接与发布 |
| R2 | 「兼容 Oracle」不等于驱动与 SQL 可复用 | 可能推翻驱动选型 | spike 逐项验证连接、绑定、超时、取消、类型支持后再定选型；不为沿用样例强改架构 |
| R3 | DB 端取消能力（D6）在目标模式上可能不可用 | 无法满足需求 §5 的硬要求 | spike 先证明；不达标则不进稳定版验收 |
| R4 | 现有子系统客户端取消不终止 DB 查询（`tool_service.py:7273-7284`） | 资源泄漏与超额负载 | OB 适配器不复用该路径，独立实现取消通道 |
| R5 | OB 系统库/同义词/跨 Schema 语义不明 | 元数据越界或误报 | 首版不开放无法解析的对象；访问语义需明确后再放开 |
| R6 | 复用时误用 `APISQLTableBinding` 作发布授权 | 授权语义错误 | 明确其仅为血缘语义（`sql_data_service.py:1149-1158`），发布授权用新模型 |

## 11. 待补输入（需求 §8，不阻塞先开发配置模型与工具契约）

| 待补充信息 | 需要明确的内容 |
|---|---|
| OB 环境 | 版本及补丁、部署形态、租户模式，首版实际验收哪一种模式 |
| 连接入口 | 直连或 OBProxy、地址端口、完整用户名格式、TLS 要求、网络放行路径 |
| 数据权限 | 专用查询账号、首批数据库/Schema、表视图范围、必要注释读取权限 |
| 查询样例 | 至少 3 条已验证 SQL 与结果，覆盖普通查询、参数查询、一次关联或聚合 |
| 发布环境 | 当前 ContextForge 分支及版本、扩展代码位置、部署方式、目标 Agent 与协议配置 |
| 运行约束 | 并发量、数据库连接预算、结果上限、超时、日志保留要求 |

## 12. 验证方式

见 `specs/tests/issue-5-oceanbase-data-source.md`：需求 §7 的 A01–A13 验收案例到分层测试（单元/路由级、适配器集成、live-gateway 黑盒 E2E、故障注入与性能基线）的映射、命令门禁与退出标准。其中 A06 必须在目标 Agent 平台完成，单独 curl 或其它客户端成功不能替代。
