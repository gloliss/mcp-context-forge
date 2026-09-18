# 测试计划：OceanBase MySQL / Oracle 双模式 Driver POC（OB-00）

- 关联设计：`specs/designs/issue-6-ob-mysql-oracle-driver-poc.md`
- 关联需求：requirement `d8395176-2691-4ca5-b67f-bc2ec335306e`（GitLab issue #6）
- work_branch：`feature/issue-6-ob-mysql-oracle-driver-poc`
- 状态：L0/L1 已实现并通过（2026-09-18，150 项全绿）；18 项 L2 检查代码已实现并经 MariaDB 自检验证 harness，真实 OceanBase 验证待环境（见 §9）
- 上游依据：`specs/tests/issue-5-oceanbase-data-source.md`

## 1. 验收标准映射（需求 §验收标准）

| # | 需求验收标准 | 对应测试 | 判定方式 |
|---|---|---|---|
| A1 | MySQL Mode Driver 方案明确 | L2 的 M1–M9 + 设计 §5.4 选型判据 | 结论文档给出唯一推荐驱动 + 版本，且证据来自真实运行 |
| A2 | Oracle Mode Driver 方案明确 | L2 的 O1–O9 + 设计 D6 候选过滤表 | 同上 |
| A3 | 两种模式均有真实代码验证 | L2 全部用例 | 两种模式各有 ≥1 次真实环境运行记录；不达标**不得**宣称通过（§8） |
| A4 | 有明确 Runtime 技术选型结论 | L3 Runtime 评估 | 给出 Python 结论 + TS 的决定性结论 |
| A5 | 密码未进入代码、日志和仓库 | §5 脱敏用例 + 门禁 | 自检通过 + `make detect-secrets-scan` 无新增命中 |
| A6 | 输出完整 POC 文档 | §6 文档完备性检查 | `docs/oceanbase-driver-poc.md` 章节齐全且结论可溯源 |

## 2. 分层测试

分层原则：**能在低层锁定的语义不放高层；只有真实数据库才能验证的语义，不放在低层假装通过。**

| 层 | 目标 | 依赖 | 是否进默认 CI |
|---|---|---|---|
| L0 无 DB 自检 | 配置读取、脱敏、结果 schema、skip 语义、编排 | 无 | 是（POC 自有入口） |
| L1 stub 驱动单元 | 每项检查的判定归类、错误码映射、超时证据规则 | 无（fake 驱动） | 是（POC 自有入口） |
| L2 真实 OB 集成 | 需求 18 项验证矩阵 + D8 二级证据 | **真实 OB（两种模式）** | 否（env 门控，缺环境整体 skip） |
| L3 Runtime 横向评估 | TS / .NET 的决定性问题 | 对应运行时 | 否（按需触发） |

> **收集范围说明**：POC 不在 `tests/` 下，`pytest testpaths = ["tests"]`（`pyproject.toml:687`）不会收集它；POC 使用自己的入口（§6）。这是刻意的——`experiments/` 的依赖（尤其 Oracle 模式客户端库）不应污染主仓库的依赖锁与 CI。

### 2.1 L0 无 DB 自检（必须全绿）

建议文件：`experiments/oceanbase_driver_poc/tests/test_config_redaction.py`、`test_result_schema.py`、`test_skip_semantics.py`、`test_harness_orchestration.py`

| 覆盖 | 用例要点 | AC |
|---|---|---|
| 配置读取 | 必填环境变量缺失 → 对应检查记 `SKIP_NO_ENV`，**不抛未捕获异常**、不以非零码退出（除非显式 `--fail-on-skip`） | A5 |
| **脱敏** | 构造含口令 / 完整 DSN / `user@tenant#cluster` 的输入，断言 `redact()` 后 stdout、日志记录、异常消息、结果 JSON **均不含口令子串**；口令为空串、NULL、含特殊字符各一例 | A5 |
| 结果 schema | 状态枚举合法（5 值）；每项含 `evidence` 与 `mode`；JSON 可被结论文档生成器消费 | A6 |
| **skip 语义** | 构造 `SKIP_NO_ENV` 与 `PASS` 混合的结果，断言聚合与文档生成**不会**把 skip 表述为「通过」 | A3 A6 |
| 编排 | 单模式、单运行时、双模式批量三种调用方式均可用；中断后结果文件不残缺 | A6 |

### 2.2 L1 stub 驱动单元

建议文件：`experiments/oceanbase_driver_poc/tests/test_check_logic.py`、`test_error_mapping.py`

| 覆盖 | 用例要点 | AC |
|---|---|---|
| 判定归类 | 注入 fake 驱动模拟：认证失败 → `DB_AUTH_FAILED`；地址不可达 → `DB_UNREACHABLE`；驱动不支持命名绑定 → `UNSUPPORTED`（**而不是** `FAIL`） | A1 A2 |
| 错误码映射 | Oracle 原生 `ORA-xxxxx` → 统一错误码映射表逐条断言；**未识别的 ORA 码落 `DB_DRIVER_ERROR` 且保留原始码** | A2 |
| **超时证据规则（核心）** | 模拟「客户端超时但服务端未停止」→ 该项**不得**判 `PASS`（落实设计 D8）；模拟「已停止」「无法判定」各一例 | A2 A4 |
| 连接成功不外推 | 仅 M1/O1 为 `PASS` 时，断言聚合结果**不**把其余 8 项标为通过（落实设计 R9） | A1 A2 |

### 2.3 L2 真实 OB 集成（需求 18 项）

**env 门控**：`OB_MYSQL_HOST` / `OB_ORACLE_HOST` 等未设置时整体 `pytest.skip`，并在结果 JSON 中落 `SKIP_NO_ENV`。**不得指向生产库**（`tests/AGENTS.md`）。

每条用例须记录：驱动名 + 版本、OB 版本与兼容模式（由连接读出）、耗时、原始错误码、脱敏后的连接目标。

| 用例 ID | 检查项 | 断言要点 | AC |
|---|---|---|---|
| `test_mysql_m1_connect` | M1 建立连接 | 连接成功，且能读出实例版本与兼容模式确为 MySQL 模式 | A1 |
| `test_mysql_m2_simple_query` | M2 SELECT 1 / 简单查询 | `SELECT 1` 与多类型列查询返回预期行列，类型可映射 | A1 |
| `test_mysql_m3_param_bind` | M3 参数绑定 | 占位符绑定返回正确结果；**引号、分号、注释符、Unicode 参数不改变 SQL 结构** | A1 |
| `test_mysql_m4_pool` | M4 Connection Pool | 借出/归还/复用可达；池满时有界等待并返回可辨识错误；失效连接被剔除 | A1 |
| `test_mysql_m5_connect_timeout` | M5 Connection Timeout | 对不可达地址在配置期限内返回，错误可辨识；实际耗时接近配置值而非系统默认 | A1 |
| `test_mysql_m6_query_timeout` | M6 Query Timeout | 期限内收到超时错误；**并断言服务端查询已停止**（二级证据，见下） | A1 A4 |
| `test_mysql_m7_metadata` | M7 Metadata 查询 | 表/列/类型/可空/注释与基准一致；`information_schema` 与 SQLAlchemy `inspect` 两路径差异被记录 | A1 |
| `test_mysql_m8_close_reconnect` | M8 close / reconnect | close 后复用报错可辨识；重连成功；失效检测有效 | A1 |
| `test_mysql_m9_error_handling` | M9 异常处理 | 认证失败/库不存在/表不存在/语法错误/权限不足分别映射到稳定可辨识的错误码 | A1 |
| `test_oracle_o1_connect` | O1 建立连接 | 连接成功，且能读出实例版本与兼容模式确为 Oracle 模式 | A2 |
| `test_oracle_o2_simple_select` | O2 简单 SELECT | 含 `FROM DUAL` 的查询返回预期结果；日期/数值类型可映射 | A2 |
| `test_oracle_o3_named_bind` | O3 Named Bind Parameter | `:name` 命名绑定返回正确结果；**特殊字符与类型边界参数不改变 SQL 结构** | A2 |
| `test_oracle_o4_pool` | O4 Connection Pool | 同 M4 | A2 |
| `test_oracle_o5_connect_timeout` | O5 Connection Timeout | 同 M5 | A2 |
| `test_oracle_o6_query_timeout` | O6 Query Timeout | 同 M6，且额外覆盖 `connection.cancel()` 路径 | A2 A4 |
| `test_oracle_o7_metadata` | O7 Schema / Table / Column Metadata | `ALL_TABLES`/`ALL_TAB_COLUMNS` 等字典视图与 SQLAlchemy `inspect` 均覆盖；**标识符大小写与引用语义保留**（`RTD6` 按大写原样处理） | A2 |
| `test_oracle_o8_close_reconnect` | O8 close / reconnect | 同 M8 | A2 |
| `test_oracle_o9_error_handling` | O9 异常处理 | 原生 `ORA-xxxxx` 映射表逐条可用；未识别码保留原始码 | A2 |

**D8 二级证据专用用例**：

| 用例 | 断言 |
|---|---|
| `test_query_timeout_server_side_stops` | 长查询触发超时后，用**观测连接**在约定期限内确认该查询/会话已终止；输出三态结论「已停止 / 未停止 / 无法判定」，**不得**把「无法判定」写成通过 |

> 观测方式：由**测试 DBA 账号**观测服务端会话与运行中查询，不因此给运行账号增加管理权限。

> **实施期落定（2026-09-18）**：上表 18 项**不是 pytest 用例函数**，而是驱动适配器里的
> 检查方法（`mysql_mode/driver.py` 的 `_check_*` / `oracle_mode/driver.py` 的 `_check_*`），
> 由 `run_poc.py` 显式调用。相应地：
>
> - **无数据库时**它们整体记 `SKIP_NO_ENV`（不是 skip 掉的 pytest 用例）；
> - `tests/test_driver_params.py::test_every_declared_check_has_a_handler` 断言 9 项检查
>   各有一个 handler，防止某天悄悄退化成「未实现」；
> - 纯函数部分（SQL 构造、判定规则、参数映射、错误码）由 L1 覆盖；
> - MySQL 侧另有一层 **MariaDB 自检**（`scripts/mariadb_smoke.sh`）真跑 M1–M9，用于发现
>   「SQL 写错、控制流写错、驱动 API 用错」——它验证的是 harness 代码，**不是** OceanBase
>   兼容性，结果不得写入结论文档（详见结论文档 §12）。
> - **Oracle 侧没有对应自检后端**，O1–O9 只经过 L1 纯函数测试。

### 2.4 L3 Runtime 横向评估

| 用例 | 断言 | AC |
|---|---|---|
| `ts_mysql_basic` | `mysql2` 连通 + 简单查询 + 参数绑定通过 | A4 |
| **`ts_oracle_decisive`** | `node-oracledb` Thin / Thick 在 OB Oracle Mode 上的连通与简单查询；结论**只能是三选一**：「稳定支持 / 不稳定支持 / 无法判定」，并据此决定是否禁止为统一语言采用 TS | A4 |
| `dotnet_oracle`（按需） | 仅在确认有采用意向时执行；`Oracle.ManagedDataAccess.Core` 无客户端库路径同样需实测 OB 兼容性 | A4 |

**边界断言**：把「MySQL 侧可用」记为「Oracle 侧可用」的推导必须被显式拒绝——测试与文档中两者分开陈述（落实设计 §5.2）。

## 3. 验证矩阵覆盖检查

需求 18 项与用例一一对应，无遗漏：

| 模式 | 需求项 | 用例数 | 覆盖 |
|---|---|---|---|
| MySQL | 9（M1–M9） | 9 | 一一对应 |
| Oracle | 9（O1–O9） | 9 | 一一对应 |
| 跨模式 | Query Timeout 二级证据（D8 追加） | 1 | M6 / O6 共用 |

## 4. Runtime 评估判据检查

对应设计 §5.4，每条判据都需有可核对的检查点：

| 判据 | 检查点 |
|---|---|
| ① D8 二级证据（一票否决） | `test_query_timeout_server_side_stops` 的三态结论 |
| ② 可维护性与可安装性 | 驱动是否在维护；能否在 Python 3.12 安装运行（D6 可安装性筛选） |
| ③ 能力覆盖度 | M1–M9 / O1–O9 的 `PASS` 计数与缺口清单 |
| ④ 引入成本 | 是否新增运行时 / 客户端库 / 部署依赖；记录库名与版本 |

## 5. 凭据与脱敏测试

| 用例 | 断言 | AC |
|---|---|---|
| `test_redact_password_in_dsn` | 含口令的 DSN 经脱敏后，stdout / 日志 / 异常消息 / 结果 JSON / 文档草稿均不含口令子串 | A5 |
| `test_redact_username_suffix` | `askquery@rptdb#hldw` → `askquery@***`（租户与集群名不泄漏） | A5 |
| `test_no_credential_in_artifacts` | 对一次完整运行（含失败路径）的 `results/*.json` 全量扫描，无口令明文 | A5 |
| `test_env_example_placeholders` | `.env.example` 只含占位符，无真实账号或口令 | A5 |
| `test_error_path_no_leak` | 认证失败与连接失败的错误路径**同样**经脱敏（失败路径最易漏） | A5 |

**仓库门禁**：`make detect-secrets-scan` 必须无新增未审计命中。

## 6. 文档完备性检查（A6）

`docs/oceanbase-driver-poc.md` 逐项核对：

| 需求指定章节 | 检查 |
|---|---|
| 使用 Driver / Driver 版本 | 两种模式各给出驱动名与确切版本 |
| MySQL Mode 验证结果 | M1–M9 逐项状态，每项可指回 `results/*.json` |
| Oracle Mode 验证结果 | O1–O9 逐项状态，同上 |
| Pool 支持情况 / Timeout 支持情况 | 含 D8 二级证据结论 |
| Parameter Bind 形式 | 两种模式各自的绑定语法与实测结论 |
| 已知问题 | 含 `UNSUPPORTED` 与「无法判定」项 |
| 最终 Runtime 建议 | 满足需求的语言强制约束（TS 若不可用则明确禁止） |

**追加章节**（回填 issue-5）：`validated_compat_modes` 建议、R9 / R10 关闭结论、复现方式与环境清单。

**硬性断言**：未在真实环境执行的项必须显式写「未验证（缺环境）」，不得留空或写推测值。

## 7. 门禁与命令

| 阶段 | 命令 |
|---|---|
| POC 无 DB 自检（L0+L1） | `uv run --frozen pytest experiments/oceanbase_driver_poc/tests` |
| POC 真实验证（L2） | `python experiments/oceanbase_driver_poc/run_poc.py --mode mysql\|oracle --runtime python --out results/` |
| Runtime 评估（L3） | `node experiments/oceanbase_driver_poc/runtimes/typescript/run.mjs`（按需） |
| POC 代码质量 | `make ruff TARGET=experiments/oceanbase_driver_poc` |
| 提交前 | `make detect-secrets-scan`；`make ruff bandit interrogate pylint verify`（对改动文件） |
| 主仓库回归 | **有界**冒烟，见下方警告；**不要**直接跑 `make test` |

> ⚠️ **实施期修正（2026-09-17）：不要在本容器里直接跑 `make test`。**
>
> `make test`（`Makefile:1045`）用 `pytest -n auto`，而 `-n auto` 取的是 `os.cpu_count()`。
> 容器实际资源与这个数不是一回事：实测 cgroup 只给 **1 CPU / 4 GiB**，但
> `os.cpu_count()` 与 `sched_getaffinity()` 都返回宿主机的 **256**，于是 xdist 派生了
> 256 个 worker，每个都加载完整 FastAPI 应用与 SQLAlchemy。结果内存耗尽触发 OOM
> （`oom_kill 13`），**连正在轮询的 agent 进程一起被杀**，已完成的实现未提交即丢失。
>
> 有界回归请显式压住并发。实测 **`-n 4` 全程未触发 OOM**（24543 项跑完），但内存峰值
> 达到 **约 4068 MiB / 4096 MiB**——贴近上限，属可用但吃紧。要留余量请用 `-n 2`。
>
> ```bash
> uv run --frozen --extra plugins pytest -n 4 --maxfail=0 -q \
>   --ignore=tests/playwright --ignore=tests/migration \
>   --ignore=tests/performance --ignore=tests/compliance --ignore=tests/live_gateway
> ```
>
> 另注：该次冒烟有 **41 项失败**，全部位于本次改动**未触及**的文件（如
> `charts/mcp-stack/values.yaml` 缺 `SSRF_ALLOW_LOCALHOST` 键、缺 `xmlschema` 可选依赖的
> 4 个收集错误）。改动只涉及 `experiments/`、`docs/`、`.gitignore` 与 `specs/`，`mcpgateway/`
> 与 `tests/` 零改动，故上述失败为**既有问题**，非本次引入。
>
> 另：`make test` 会经 `uv run` 重新同步依赖，内网 PyPI 镜像限流（HTTP 429）时会直接
> 以 `Failed to unzip wheel` + `Error 2` 失败；`--frozen` 可绕过。该问题与 POC 无关，
> 但会让「跑个回归确认没影响」这件事本身变得不可靠，故一并记录。

## 8. 退出标准

1. 需求 18 项在**两种模式**上各有明确状态，且 `PASS` **不得**由 `SKIP_NO_ENV` 推导；
2. L0 + L1 全绿——这是需求「单元部分仍需通过」的落实；
3. `docs/oceanbase-driver-poc.md` 章节齐全，每条结论可指回 `results/*.json`；
4. 脱敏用例全通过，且 `make detect-secrets-scan` 无新增未审计命中；
5. MySQL / Oracle 两种模式的 Driver 方案各给出唯一推荐，**或明确写出「无法得出结论」及其原因**；
6. Runtime 建议明确，且 TS 结论满足需求的语言强制约束。

## 9. 当前不可执行项（阻塞于环境，禁止伪造）

以下用例在环境补齐前**无法真正运行**，届时先补环境再判定，**不得以替身结果或推测值宣称通过**（需求测试要求原文）：

| 项 | 阻塞原因 | 影响的 AC | 实施状态（2026-09-17） |
|---|---|---|---|
| L2 全部（M1–M9） | **无 MySQL 模式目标实例**；且本 run 环境不可达 OB | A1 A3 | M1/M2 代码就位，待环境；M3–M9 未实现 |
| L2 全部（O1–O9） | 本 run 环境不可达 OB（`10.88.8.29:2883` 实测超时）；无 OBCI 客户端库 | A2 A3 | O1/O2 代码就位，待环境；O3–O9 未实现 |
| D8 二级证据 | 缺服务端观测账号（测试 DBA） | A2 A4 | 判定规则与三态已实现并测试；观测脚本待环境 |
| L3 Runtime 评估 | 缺运行时与采用意向 | A4 | 未开始 |
| 结论文档 §3–§7 实测结论 | 依赖上述全部 | A6 | 未开始；段落生成器已就位 |

**执行方式**：在能访问 OB 的机器上取出本分支、按 `README.md` 注入环境变量后运行 `run_poc.py`，把 `results/*.json` 带回填入结论文档。在此之前，文档对应项只能写「未验证（缺环境）」。

> **可先行部分（已完成）**：L0、L1 与代码骨架不依赖外部环境，已落地并通过 99 项测试，用于锁定脱敏、结果契约、skip 语义与判定归类等确定性语义。

### 9.1 已执行的实施期修正

| 项 | 原规划 | 实际 | 原因 |
|---|---|---|---|
| 结果状态数 | 五种 | **六种**（增 `INDETERMINATE`） | 缺它会让「已执行但服务端是否停止观测不到」挤进 `FAIL`，而 `FAIL` 读作「服务端仍在运行」——后者会误导选型 |
| `--fail-on-skip` 语义 | 仅覆盖 `SKIP_NO_ENV` | 一并覆盖 `UNSUPPORTED` | 两者同属「未验证出结论」，与 `FAIL` 语义不同；退出码策略已由 `common/results.py` 单点定义并测试锁定 |
| 服务端观测要求 | 未标注 | `CheckSpec.requires_server_observation` 显式标记 M6/O6 | 让「需观测通道」成为可查询的属性，而非文档里的一句话 |
| POC 测试夹具 | 使用真实环境标识 | 改用合成标识 | 真实环境事实只留在 `specs/` 的环境事实记录中，不再扩散到测试夹具 |

### 9.2 门禁执行情况

| 门禁 | 结果 |
|---|---|
| POC L0/L1（99 项） | **通过** |
| `make ruff TARGET=experiments/oceanbase_driver_poc` | **通过**（`All checks passed!`） |
| `ruff format` / `make black CHECK=1` | **通过**（23 个文件无需改动） |
| `make detect-secrets-scan` | **未能执行**：该目标需 `--with git+https://github.com/ibm/detect-secrets.git@<sha>`，本 run 的平台 git 包装器拒绝该 fetch（`repo_arg_invalid`）。替代核查：以 PyPI 版 detect-secrets 对 POC 目录做独立扫描，**0 命中**；`.secrets.baseline` 未被改动。⚠️ 扫描器与仓库锁定的 IBM fork 并非同一实现，此替代不等价，正式提交前应在网络不受限的环境补跑该目标 |
| 主仓库回归 | 见下方说明 |


## 10. 与需求正文的对应关系

本计划不重复需求的验收标准与验证项文本，只补充「怎么测、在哪测、用什么断言、什么算通过」。

需求「测试要求」中「如果 CI 环境没有 OceanBase 测试数据库」这一前提，**在当前执行环境下成立**（实测不可达，见设计 §2.3），因此 §9 的阻塞清单是本次规划的必须交付内容之一，而非可选项。
