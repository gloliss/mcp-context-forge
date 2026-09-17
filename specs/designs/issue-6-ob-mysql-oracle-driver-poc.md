# 设计文档：OceanBase MySQL / Oracle 双模式 Driver POC（OB-00）

- 关联需求：requirement `d8395176-2691-4ca5-b67f-bc2ec335306e`（GitLab issue #6）《[OB-00] OceanBase MySQL / Oracle 双模式 Driver POC》
- work_branch：`feature/issue-6-ob-mysql-oracle-driver-poc`
- 状态：规划（尚未进入实现）
- 上游依据：`specs/designs/issue-5-oceanbase-data-source.md` §10 R9/R10、§11.1、§11.2
- 下游去向：结论回填 issue-5 的驱动候选表与 `validated_compat_modes`，作为 Database Runtime 选型依据

> 本需求是 **spike（可行性验证）**，不是产品功能。产物是实验目录与结论文档，**不进入 `mcpgateway/` 产品代码**。

## 1. 背景与目标

### 1.1 为什么要做这个 POC

issue-5 的设计已经给出两条硬约束：租户模式必须显式配置为 `mysql` 或 `oracle`，首版只对**实际验证通过的那一种模式**负责；且「仅凭『兼容 Oracle』不能认定原 Oracle 驱动与 SQL 可直接复用」（需求 §1）。

该设计同时把两个未决问题登记为头号风险，本 POC 就是来终结它们的：

- **R9 — Oracle 模式 Python 驱动硬冲突**：仓库 `requires-python = ">=3.12,<3.14"`；OceanBase 官方给出的 Oracle 模式 Python 路径是 `cx_Oracle` + `libobclient`/OBCI（需 `LD_LIBRARY_PATH`），而 `cx_Oracle` 8.3.0 已是末版、不支持 Python 3.12+；`python-oracledb` **Thin 模式**（纯 Python、无需客户端库）与 OB Oracle 模式的兼容性**未见证实**。
- **R10 — Oracle 模式缺 MySQL 式语句级超时**：issue-5 的 A07 要求「数据库端查询必须停止」，该模式更难满足。

### 1.2 目标（需求 §「本需求目标」）

1. 完成 OceanBase MySQL / Oracle 两种模式的 Driver POC；
2. 给出 Database Runtime 的技术选型结论；
3. 只做验证，不做大规模 ContextForge 重构。

### 1.3 需求边界（需求 §「开发边界」）

本需求**不实现**：MCP Tool、Database Source UI、SQL Policy、ContextForge 大规模重构、Production Deployment。

## 2. 现状与已确认事实

### 2.1 代码现状

| 事实 | 证据 |
|---|---|
| `experiments/` 目录当前**不存在** | `ls experiments` → No such file or directory |
| 仓库对 `oceanbase` 关键字**零命中**（`*.py`/`*.toml`/`*.txt`/`*.lock`） | 全仓 grep 无结果 |
| Python 运行时 `>=3.12,<3.14`，实测 **3.12.13** | `pyproject.toml:55`、`python3 --version` |
| 既有依赖含 `PyMySQL>=1.1.2`；**无** `oracledb`/`cx_Oracle` | `pyproject.toml:227`、`uv.lock` |
| 不存在 `oceanbase` 官方 PyPI 包 | PyPI 核对 → 404 |

即：**当前无任何 OB 驱动代码或依赖**，POC 与产品代码可完全解耦。

**门禁范围（决定 POC 的落位方式）**：

- `make ruff|pylint|bandit` 的 `DEFAULT_TARGETS := mcpgateway`（`Makefile:3393`）——**不扫描 `experiments/`**；
- `pytest testpaths = ["tests"]`（`pyproject.toml:687`）——**不收集 `experiments/`**。

结论：把 POC 放进 `experiments/`，既不会污染主依赖锁、也不会因默认门禁失败阻塞 CI；代价是 POC 必须**自己提供门禁入口**（见 §9 与测试计划 §6）。

### 2.2 已确认环境事实（承 issue-5 §11.1，2026-09-14 登记）

| 项 | 值 |
|---|---|
| OB 版本 | **4.3.5.6** |
| 租户 / 兼容模式 | **Oracle 兼容模式**（租户 `rptdb`） |
| 连接方式 | 经 **OBProxy**，`10.88.8.29:2883` |
| 服务名 | `rtd6` |
| 完整用户名 | `askquery@rptdb#hldw`（OBProxy 形式，**原样使用**） |
| 实际用户名 / Schema | `ASKQUERY` / `RTD6` |
| TLS | **不要求** |
| 已知可达宿主机 | `aysap01` = `10.250.11.201`，容器 `code_code_sandbox`（`172.18.0.2:8080` → 宿主 `8989`） |

> **已知实例只有 Oracle 模式租户**。需求同时要求 MySQL 模式验证，因此「MySQL 模式目标实例」是必须补齐的环境输入（见 §12、风险 R2）。OceanBase 单集群可承载多种兼容模式的租户，但本集群是否存在可用的 MySQL 模式租户**尚未确认**。

### 2.3 本次规划执行环境的实测限制（决定实施方式）

| 探测项 | 结果 |
|---|---|
| `10.88.8.29:2883`（OB） | **超时不可达** |
| `10.250.11.201:8989` / `:22`（已知宿主机） | **超时不可达** |
| `docker` | **不可用**（command not found） |
| `libobclient` / OBCI / `libclntsh` | **不存在** |
| PyPI 出网 | 可用 |

**结论**：本执行环境**无法执行真实数据库验证**。POC 骨架、无 DB 自检与 stub 单元可以在此完成；**L2 真实验证必须在能访问 OB 的机器上执行**。

这正对应需求测试要求：「如果 CI 环境没有 OceanBase 测试数据库：不允许伪造成功结果；明确标记需要真实环境验证的测试；单元部分仍需通过」。

## 3. 设计决策

**D1 — POC 完全隔离在 `experiments/`，不触碰产品代码与主依赖锁。**
依赖使用**独立**的 `requirements-poc.txt`，**不写入** `pyproject.toml` / `uv.lock`。三个理由：① 需求边界明确不重构 ContextForge；② Oracle 模式的 `cx_Oracle` 路径在 Python 3.12 上**无法安装**（R9 的直接后果），一旦进主锁会让 `uv lock` / CI 立即失败；③ `experiments/` 天然处于 `DEFAULT_TARGETS` 与 `testpaths` 之外（§2.1）。

**D2 — 目录结构按需求硬性要求落地。**

```
experiments/oceanbase_driver_poc/
├── README.md               # 环境变量、运行方式、结果解读（需求硬性要求）
├── .env.example            # 占位符样例，不含真实凭据
├── requirements-poc.txt    # POC 独立依赖，固定版本
├── run_poc.py              # 可执行测试脚本（需求硬性要求）：--mode mysql|oracle
├── common/                 # 配置读取、DSN 脱敏、结果 JSON schema、检查编排
├── mysql_mode/             # 需求硬性要求
├── oracle_mode/            # 需求硬性要求
├── runtimes/               # TypeScript（限定范围）/.NET（按需）评估
├── tests/                  # 无 DB 自检 + stub 单元（CI 可跑）
└── results/                # 机器可读结果，每次运行一份 JSON
```

`mysql_mode/`、`oracle_mode/`、`README.md`、可执行测试脚本是需求逐字要求的四项，目录名不做改写。

**D3 — 每项验证的结果状态必须能区分「未执行」与「通过」，禁止把 skip 记成 pass。**
状态取值固定为五种：`PASS` / `FAIL` / `SKIP_NO_ENV`（无环境，未执行）/ `UNSUPPORTED`（驱动本身不支持该能力）/ `ERROR`（执行异常）。每次运行落盘 `results/<mode>-<runtime>-<timestamp>.json`，内含脱敏后的连接目标、驱动名与版本、OB 版本（由连接读出）、每项检查的证据（命令、耗时、错误码）。

**`docs/oceanbase-driver-poc.md` 的每条结论都必须由这些 JSON 支撑。** 这是需求「不允许伪造成功结果」的机制化落实——不是靠自觉，而是靠「结论可溯源到一次真实运行」。

**D4 — 连接信息只从环境变量读取，且所有出口统一脱敏。**
变量按模式分组（`OB_MYSQL_*` / `OB_ORACLE_*`）。必填项缺失时该项检查记 `SKIP_NO_ENV`，**不**抛未捕获异常。

**禁止**（需求原文）：口令写入源码、真实凭据提交仓库、日志打印完整口令或 DSN。注意本仓库已启用 detect-secrets 门禁（`.secrets.baseline`），POC 不得新增未审计命中。

**D5 — Runtime 评估范围按需求的条件性表述收敛，不做等量齐观。**
需求原文是「Python Runtime / TypeScript Runtime（**如当前环境准备采用**）/ .NET Runtime（**如需要**）」。据此：

- **Python：全矩阵**（两种模式 × 9 项），主评估对象；
- **TypeScript：限定范围** —— 只需回答需求点名的那一个决定性问题「TS 能否稳定支持 OB Oracle Mode」，外加 MySQL 侧基本连通与查询。因为需求已明令「如果 TypeScript 方案不能稳定支持 OceanBase Oracle Mode，不允许为了统一语言强行采用 TypeScript」；
- **.NET：暂不评估**，除非确认存在采用意向（列入 §12 待补输入）。

**D6 — Oracle 模式驱动候选先过「可安装性」，再过「可连接性」，最后过「能力项」。**

| 候选 | 机制 | Python 3.12 可安装 | 说明 |
|---|---|---|---|
| `python-oracledb` **Thick** + OB `libobclient`/OBCI | OCI | **是**（`oracledb`，PyPI 核对时点 26.0.0，`requires_python>=3.10`） | 需客户端库 + `LD_LIBRARY_PATH`；绑定/池/超时/取消能力最全 |
| `python-oracledb` **Thin** | 纯 Python（Oracle TNS 协议） | **是** | **无需客户端库**；与 OB Oracle 模式的兼容性是本 POC 首要待证问题 |
| `cx_Oracle` 8.3.0 + OBCI | OCI | **否**（末版，不支持 3.12+） | 需为 POC 单列旧版 Python（≤3.10） |
| `OceanBase Connector/J`（Java） | 官方 | 不适用 | 官方声明同时支持两种模式；引入新栈 |

可安装性筛选必须**先做**——它能在不接触数据库的前提下先淘汰掉一批候选（`cx_Oracle` 就是被这一步淘汰的，而非被兼容性淘汰；两者要分清，否则结论会误导下游）。

**D7 — MySQL 模式驱动以「纯 Python 可安装」为默认，超时优先用 OB 系统变量而非 MySQL 方言特性。**
MySQL 模式走 MySQL 线协议：`PyMySQL`（已是仓库既有依赖）为主候选，`mysql-connector-python` 为对照。

超时**优先验证 OceanBase 自身的 `ob_query_timeout` 系统变量**（服务端语义，两种模式都适用），`MAX_EXECUTION_TIME` hint 作为对照项。理由：MySQL 方言特性在 OB 上是否完整实现需要实测，而 OB 自己的系统变量是它承诺的语义。

**D8 — Query Timeout 一项必须区分「客户端观察到超时」与「服务端查询真的停止」。**

需求只要求验证 Query Timeout，但 issue-5 的 A07 / D6 / R10 明确要求「数据库端查询必须停止」。POC 把该项拆成两级证据：

- **一级（需求内）**：调用方在配置期限内收到超时错误；
- **二级（追加，选型决定性）**：以另一只观测连接确认**服务端会话/查询确已终止**（Oracle 模式考察 `connection.cancel()`、`ob_query_timeout`、会话终止等手段）。

**二级证据是各 Runtime 候选能否进入稳定版的一票否决项。** 仅让客户端等待结束、后台线程继续执行**不算达标**——这正是 issue-5 记录的现有缺口（`tool_service.py:7273-7284`，客户端取消不终止 DB 查询）。

**D9 — 元数据验证走「驱动原生字典视图」与「SQLAlchemy inspect 反射」两条路径，并记录差异。**
下游 issue-5 的元数据能力计划走 SQLAlchemy 通用反射路径，而 SQLAlchemy 的 `mysql` / `oracle` dialect 并非为 OB 编写。Oracle 模式需覆盖 `ALL_TABLES` / `ALL_TAB_COLUMNS` 等字典视图；并记录标识符大小写与引用语义（Oracle 未加引号即大写，`RTD6` 按大写原样处理，对应 issue-5 §10 R11）。

**D10 — 结论必须能回填 issue-5，而不是一份独立报告。**
`docs/oceanbase-driver-poc.md` 除需求指定章节外，须显式给出：`validated_compat_modes` 建议值、驱动与版本、超时与取消机制结论（回填 R10）、R9 的关闭结论。POC 的终态之一就是让 issue-5 §11.2 的候选表从「待 spike 定夺」变为「已定」。

## 4. 验证矩阵（需求的 18 项）

需求对两种模式各列 9 项，统一编号 M1–M9 / O1–O9：

| # | MySQL Mode（需求原文） | # | Oracle Mode（需求原文） |
|---|---|---|---|
| M1 | 建立连接 | O1 | 建立连接 |
| M2 | SELECT 1 / 简单查询 | O2 | 简单 SELECT |
| M3 | 参数绑定 | O3 | Named Bind Parameter |
| M4 | Connection Pool | O4 | Connection Pool |
| M5 | Connection Timeout | O5 | Connection Timeout |
| M6 | Query Timeout | O6 | Query Timeout |
| M7 | Metadata 查询 | O7 | Schema / Table / Column Metadata |
| M8 | Connection close / reconnect | O8 | Connection close / reconnect |
| M9 | 异常处理 | O9 | 异常处理 |

**每项的统一测量口径**（防止「跑通即通过」）：

| 项 | 判为 PASS 的最小证据 | 失败 / 不达标信号 |
|---|---|---|
| 建立连接 | 连接成功，且认证后可读出实例版本与兼容模式 | 认证错误不可辨识；超时不收敛 |
| 简单查询 | 返回预期行与列，类型可映射 | 协议或方言不兼容（驱动握手被拒） |
| 参数绑定 | 占位符 / 命名绑定返回正确结果；**特殊字符与类型边界参数不改变 SQL 结构** | 需字符串拼接才能工作 |
| Connection Pool | 借出/归还/复用可达；池满时有界等待并返回可辨识错误；失效连接被剔除 | 连接泄漏；池满即挂死 |
| Connection Timeout | 对不可达地址在配置期限内返回，错误可辨识 | 超时参数被忽略；实际期限远超配置 |
| Query Timeout | 期限内收到超时错误 **且** 服务端查询已停止（D8 二级证据） | 仅客户端返回超时，服务端查询仍在跑 |
| Metadata | 表/列/类型/可空/注释与基准一致；标识符大小写与引用语义保留 | 反射失败；大小写被改写导致对象对不上 |
| close / reconnect | close 后复用报错可辨识；重连成功；失效检测（ping/health）有效 | 连接池把死连接还给调用方 |
| 异常处理 | 认证失败/库不存在/表不存在/语法错误/权限不足分别映射到稳定可辨识的错误码 | 全部塌缩成同一个泛化异常 |

**统一错误码集合**（跨模式，供下游 issue-5 直接复用）：

`DB_AUTH_FAILED`、`DB_UNREACHABLE`、`DB_TIMEOUT`、`DB_SYNTAX_ERROR`、`DB_OBJECT_NOT_FOUND`、`DB_PERMISSION_DENIED`、`DB_PROTOCOL_UNSUPPORTED`、`DB_DRIVER_ERROR`。

Oracle 模式需额外交付原生 `ORA-xxxxx` → 上述错误码的映射表；未识别的 ORA 码落入 `DB_DRIVER_ERROR` 且**保留原始码**（不丢信息）。

## 5. Runtime 评估方案

### 5.1 Python（全矩阵）

两种模式 × 9 项全跑，是选型结论的主依据。

### 5.2 TypeScript（限定范围）

- **MySQL 模式**：`mysql2`（纯 JS，MySQL 线协议）做连通 + 简单查询 + 参数绑定。
- **Oracle 模式**：`node-oracledb` 的 **Thin**（纯 JS）与 **Thick**（需 OCI/OBCI）两条路径，重点回答「能否**稳定**支持」。
- 结论文档必须明确写出「TS 是否可用于 OB Oracle Mode」及其证据；**若不能，按需求禁止为统一语言强行采用 TS**。
- 边界：面向 MySQL 协议的 ORM/驱动生态对 OB Oracle Mode **不构成候选**，不得把「MySQL 侧可用」当成「Oracle 侧可用」。

### 5.3 .NET（按需）

仅在确认有采用意向时评估。候选：MySQL 模式 `MySqlConnector`（纯托管）；Oracle 模式 `Oracle.ManagedDataAccess.Core`（纯托管，无需客户端库）。后者「无需 OCI」这点值得实测，但 OB 兼容性同样未证。

### 5.4 选型判据与结论形式

判据按优先级：

1. **能否在目标模式上通过 D8 二级证据（服务端真正停止）** —— 一票否决；
2. 是否受维护、能否在当前仓库 Python 运行时（3.12）上安装运行；
3. 能力覆盖度（池、超时、绑定、元数据、类型保真）；
4. 引入成本（是否新增运行时 / 客户端库 / 部署依赖）。

结论按「模式 → 推荐驱动 + 版本 → 理由 → 已知缺口」输出，并给出 **Database Runtime 的总体建议**。

## 6. 凭据与脱敏设计

**环境变量**：`OB_MYSQL_*` / `OB_ORACLE_*` 前缀分组，`.env.example` 只放占位符（如 `OB_ORACLE_PASSWORD=__FILL_ME__`），不含任何真实值。

**`redact()` 覆盖范围**：

- 口令整体（含空串、NULL、含特殊字符的情形）；
- DSN 中的 userinfo 段；
- 用户名 `@tenant#cluster` 后缀（`askquery@rptdb#hldw` → `askquery@***`）。

**必须过脱敏的出口**：stdout/stderr、日志、异常消息、结果 JSON、结论文档。

**自检用例**：给定含口令的 DSN，断言上述每个出口均不含口令子串。详见测试计划 §5。

**仓库门禁**：提交前跑 `make detect-secrets-scan`，POC 不得新增未审计命中。

## 7. 产出物与结论文档结构

`docs/oceanbase-driver-poc.md`（需求指定路径）章节**固定按需求列出**：

1. 使用 Driver
2. Driver 版本
3. MySQL Mode 验证结果
4. Oracle Mode 验证结果
5. Pool 支持情况
6. Timeout 支持情况
7. Parameter Bind 形式
8. 已知问题
9. 最终 Runtime 建议

**追加章节（回填 issue-5，见 D10）**：

10. `validated_compat_modes` 建议
11. R9 / R10 关闭结论
12. 复现方式与环境清单（运行时、驱动、客户端库版本）

**硬性要求**：§3–§7 的每条结论都要能指回 `results/*.json` 的一次运行；未在真实环境执行的项**显式写「未验证（缺环境）」**，不得留空、不得写推测值。

## 8. 实施拆分

| # | 任务 | 交付 | 退出条件 |
|---|---|---|---|
| 1 | 目录骨架 + 配置/脱敏/结果 schema + 无 DB 自检 | D2 结构、`common/`、`tests/` | 无 DB 自检全绿；脱敏用例通过 |
| 2 | MySQL 模式驱动与 M1–M9 | `mysql_mode/` | 有真实环境时出状态；无环境时全部 `SKIP_NO_ENV` 且可辨识 |
| 3 | Oracle 模式驱动候选与 O1–O9 | `oracle_mode/` | 同上；D6 候选逐条出「可安装 / 可连接 / 能力」结论 |
| 4 | Query Timeout 二级证据（D8） | 服务端终止验证脚本 | 能给出「已停止 / 未停止 / 无法判定」的明确结论 |
| 5 | 限定范围 TS 评估 | `runtimes/typescript/` | 明确回答「TS 能否稳定支持 OB Oracle Mode」 |
| 6 | 结论文档 + 结果归档 + 回填 issue-5 | `docs/oceanbase-driver-poc.md` | 每条结论可指回结果 JSON；R9/R10 有结论 |

## 9. 门禁与命令

| 阶段 | 命令 |
|---|---|
| POC 无 DB 自检 | `uv run --frozen --no-project pytest experiments/oceanbase_driver_poc/tests -q` |
| POC 真实验证 | `python experiments/oceanbase_driver_poc/run_poc.py --mode mysql\|oracle --runtime python --out results/` |
| POC 代码质量 | `make ruff TARGET=experiments/oceanbase_driver_poc` |
| 提交前 | `make detect-secrets-scan`；`make ruff bandit interrogate pylint verify`（对改动文件） |
| 主仓库回归 | `make test`（确认 POC 未影响既有套件） |

> `make ruff` 默认 `TARGET=mcpgateway`（`Makefile:3393`），POC 必须**显式传 TARGET** 才会被检查（§2.1）。

## 10. 非目标与边界

- 不实现 MCP Tool / Database Source UI / SQL Policy / ContextForge 大规模重构 / Production Deployment（需求原文）。
- 不把 POC 代码合入 `mcpgateway/`，不改主 `pyproject.toml` / `uv.lock`。
- 不做性能压测与容量规划（属 issue-5 的 L4，在实现阶段进行，不在本 POC）。
- 不新建密钥平台；凭据只走环境变量。
- 不因 POC 结论直接改动 issue-5 的实现计划——**只回填结论与建议**，改计划需另行走流程。

## 11. 风险与待验证项

| # | 风险 / 待验证 | 影响 | 处置 |
|---|---|---|---|
| R1 | **本执行环境不可达 OB**（实测 `10.88.8.29:2883` 超时），且无 docker、无 OBCI | 无法在此完成真实验证；需求明令不得伪造成功 | L2 全部记 `SKIP_NO_ENV`；改到可达主机执行（承 issue-5 §11.1 的 `10.250.11.201`）；测试计划 §8 单列阻塞清单 |
| R2 | **已知实例只有 Oracle 模式租户 `rptdb`，MySQL 模式无目标实例** | M1–M9 可能整体无环境可测，直接影响验收标准 1 与 3 | 确认 OB 集群是否存在 / 可否创建 MySQL 模式租户；否则 MySQL 侧只能出「未验证」结论 |
| R3 | `python-oracledb` **Thin** 与 OB Oracle 模式不兼容 | Python 首选的无客户端库路径失效，退回 Thick 或旧版 Python | 列为 D6 首个实验项；`UNSUPPORTED` 是**合法结论**，不得记为 PASS |
| R4 | Thick 路径需 OBCI 客户端库，且版本需与 OB 匹配 | 部署复杂度上升；容器镜像需携带库 | POC 记录所需库名/版本/`LD_LIBRARY_PATH`，作为选型成本项 |
| R5 | `cx_Oracle` 在 Python 3.12 上不可安装 | 该候选需第二套运行时 | 仅在 Thin / Thick 均不通时启用；成本计入选型结论 |
| R6 | Oracle 模式无法证明 Query Timeout 时服务端停止（issue-5 R10） | 命中 A07 硬要求，可能导致该模式不能进稳定版 | D8 二级证据为一票否决项；无法判定时**明确写「无法判定」** |
| R7 | 标识符大小写与引用语义（Oracle 未加引号即大写） | 元数据与查询对不上对象 | O7 覆盖；按 issue-5 R11 记录 |
| R8 | 脱敏不彻底导致凭据泄漏（含用户名后缀） | 违反需求配置要求与仓库门禁 | D4 出口清单 + 自检用例 + `make detect-secrets-scan` |
| R9 | 把「驱动能连上」当成「能力全通过」 | 结论虚高，误导下游选型 | 每项独立状态；连接成功**不**自动推导其余 8 项 |
| R10 | POC 独立依赖与主仓库锁漂移 | 复现困难 | `requirements-poc.txt` 固定版本 + README 记录运行时与库版本 |

## 12. 待补输入（阻塞真实验证，不阻塞骨架与自检实现）

| 待补 | 需要明确的内容 |
|---|---|
| **MySQL 模式环境** | 目标租户 / 实例、地址端口、用户名格式、TLS 要求；或确认「本次不做 MySQL 模式实测」 |
| **凭据** | OB 只读测试账号口令（**只经环境变量注入，不写入任何提交物**） |
| **执行环境** | 一台能访问 OB 的主机 + 可安装 OBCI 客户端库的权限；已知候选 `10.250.11.201`，但**本 run 不可达** |
| **Runtime 意向** | 是否确实计划采用 TypeScript / .NET（决定 D5 的评估范围） |
| **观测手段** | 是否有测试 DBA 账号可观测服务端会话 / 运行中查询（D8 二级证据依赖于此） |

## 13. 验证方式

见 `specs/tests/issue-6-ob-mysql-oracle-driver-poc.md`：需求 6 条验收标准到分层测试（无 DB 自检 / stub 单元 / 真实 OB 集成 / Runtime 横向评估）的映射、命令门禁与退出标准，以及**当前因环境缺失而不可执行的项**清单。

其中「两种模式均有真实代码验证」（验收标准 3）在补齐 §12 的执行环境与 MySQL 模式目标之前**无法达成**，按需求必须显式标记为未验证，不得以替身结果宣称通过。
