# OceanBase MySQL / Oracle 双模式 Driver POC（OB-00）

验证 OceanBase 两种兼容模式对应 Driver 的真实可用性，为 Database Runtime 选型提供依据。

- 关联需求：GitLab issue #6《[OB-00] OceanBase MySQL / Oracle 双模式 Driver POC》
- 设计文档：`specs/designs/issue-6-ob-mysql-oracle-driver-poc.md`
- 测试计划：`specs/tests/issue-6-ob-mysql-oracle-driver-poc.md`

这是 **spike（可行性验证）**，不是产品功能：产物是实验代码与结论文档，不进入
`mcpgateway/`。目录刻意放在 `experiments/` 下——它不在 `make ruff` 的默认目标
（`DEFAULT_TARGETS := mcpgateway`）与 pytest 的 `testpaths` 之内，因此既不污染主仓库
的依赖锁，也不会因默认门禁失败而阻塞 CI；代价是 POC 自带门禁入口（见下）。

## 目录结构

```
oceanbase_driver_poc/
├── README.md               本文件
├── .env.example            环境变量样例（仅占位符）
├── requirements-poc.txt    POC 专用依赖，不写入主 pyproject/uv.lock
├── run_poc.py              可执行测试脚本，统一入口
├── pytest.ini              POC 自带 pytest 配置
├── conftest.py             把 POC 根加入 sys.path
├── common/                 配置读取、脱敏、结果契约、错误码、判定规则、编排、报告
├── mysql_mode/             MySQL 兼容模式驱动适配
├── oracle_mode/            Oracle 兼容模式驱动适配
├── runtimes/               跨运行时评估：typescript/ 已可运行（限定范围），.NET 未开始
├── scripts/                开发用辅助脚本（MariaDB 自检后端）
├── tests/                  L0 无 DB 自检 + L1 stub 单元
└── results/                每次运行的机器可读结果（不提交）
```

## 结果状态词表

结果契约的核心是：**未执行不等于通过**。每一检查项落在六个状态之一：

| 状态 | 含义 | 算通过？ |
|---|---|---|
| `PASS` | 已执行且通过 | 是 |
| `FAIL` | 已执行且不达标 | 否 |
| `SKIP_NO_ENV` | 缺环境变量，**未执行** | **否** |
| `UNSUPPORTED` | 该驱动无法提供此能力（未安装，或 POC 尚未接通） | **否** |
| `INDETERMINATE` | 已执行但无法判定（例如服务端行为观测不到） | **否** |
| `ERROR` | 执行中抛出异常 | 否 |

只有 `PASS` 计为通过。汇总措辞由 `common/results.py::aggregate()` 统一生成，
全部跳过时输出「未验证（缺环境）」，绝不出现在「通过」的表述里。这条由
`tests/test_skip_semantics.py` 锁定。

`UNSUPPORTED` 的 `evidence.unimplemented` 区分两种情形：驱动未安装
（`reason: driver-not-installed`）与 POC 尚未接通该检查（`reason: l2-pending`）。

## 配置

连接信息**只从环境变量读取**，没有任何硬编码。变量名见 `.env.example`：

- MySQL 模式：`OB_MYSQL_HOST` / `OB_MYSQL_PORT` / `OB_MYSQL_USER` / `OB_MYSQL_PASSWORD`
- Oracle 模式：`OB_ORACLE_HOST` / `OB_ORACLE_PORT` / `OB_ORACLE_USER` / `OB_ORACLE_PASSWORD` / `OB_ORACLE_SERVICE_NAME`

四个必填项（`HOST`/`PORT`/`USER`/`PASSWORD`）缺一即视为该模式没有目标，全部检查记为
`SKIP_NO_ENV`。**端口没有默认值**：OBProxy 与直连使用不同端口，默认值会静默指向错误端点。

经 OBProxy 连接时用户名形如 `user@tenant#cluster`，**原样填写不做改写**。

禁止：把口令写进源码、把真实账号密码提交仓库、在日志或结果中打印完整口令或 DSN。

## 运行

```bash
# 依赖（POC 独立环境）
pip install -r requirements-poc.txt

# L0 + L1 自检（不需要数据库，CI 可跑）
python -m pytest -q

# 真实验证（需要能访问 OceanBase 的机器 + 已注入环境变量）
python run_poc.py --mode all
python run_poc.py --mode oracle --out results/

# 开发用：没有 OceanBase 时，用 MariaDB 验证 harness 代码本身（见「关于 MariaDB 自检」）
scripts/mariadb_smoke.sh --install
```

`run_poc.py` 退出码：`0` 无失败；`2` 有检查失败/异常/无法判定；`1` 用法或配置错误。
只有 `SKIP_NO_ENV` 不会让退出码非零；加 `--fail-on-skip` 可让未执行项也返回非零。

可选参数：`--md-out <path>` 把结论文档的实测段落按结果 JSON 生成，`--print-json` 同时打印结果。

### Oracle 模式的两个前提

`python-oracledb` 有 thin 与 thick 两种客户端模式，**运行前必须明确用的是哪一种**，
两者的部署代价不同：

- **thin**（默认）：纯 Python，无需客户端库。与 OceanBase Oracle 模式的兼容性正是本 POC
  要回答的首要问题——未证实前不得假定可用。
- **thick**：需要 `libobclient`/OBCI 客户端库，并设置 `LD_LIBRARY_PATH`；运行前需
  `oracledb.init_oracle_client(lib_dir=...)`。容器镜像需额外携带该库。

`run_poc.py` 会把实际使用的客户端模式记入结果（`evidence.client_mode`）。

## 如何读结果

每次运行写入 `results/<mode>-<runtime>-<timestamp>.json`，并在终端打印检查表与汇总。
结论文档 `docs/oceanbase-driver-poc.md` 的实测段落应当由此 JSON 支撑，而不是手写：

```bash
python run_poc.py --mode all --md-out /tmp/poc-sections.md
```

## 当前实现状态

18 项检查全部实现；L2（真实验证）依赖外部环境，尚未接通。

| 项 | 状态 |
|---|---|
| 目录骨架、入口、依赖隔离 | 已完成 |
| 配置读取、脱敏、结果契约、错误码映射 | 已完成，L0/L1 覆盖 |
| 判定规则（含 Query Timeout 证据规则） | 已完成并为 harness 强制执行，L1 覆盖 |
| 两种模式的连接参数映射与超时单位换算 | 已完成，L1 覆盖 |
| `M1`–`M9` / `O1`–`O9`（18 项） | **已实现**，等待真实 OceanBase 实测 |
| MariaDB 自检后端（仅验证 harness 代码本身） | 已完成，见 `scripts/mariadb_smoke.sh`，同时跑 Python 与 TS 两侧 |
| TypeScript 侧（限定范围，见 `runtimes/README.md`） | **已可运行**；Oracle 决定性结论待环境 |
| .NET 侧 | 未开始（本环境无 `dotnet`；需求为「如需要」） |

**实现完整不等于结论成立。** 18 项检查的代码都已写好并经过 L1 与 MariaDB 自检，
但**没有任何一项在 OceanBase 上跑过**，因此结论文档中的实测结论仍然为空。
未接通 L2 时运行 `run_poc.py`，全部检查记为 `SKIP_NO_ENV`，不会给出任何形式的通过。

### 关于 MariaDB 自检

`scripts/mariadb_smoke.sh` 用 MariaDB 当后端把 MySQL 模式的检查真跑一遍，用来发现
「SQL 写错、控制流写错、驱动 API 用错」这类问题——这些问题在只有真实 OceanBase 才能
跑的前提下，原本要等拿到环境才暴露。它已经抓到过两个真实缺陷（错误码 1698 未映射、
不存在的库会返回 1044 而非 1049）。

**它验证的是 harness 写对了没有，不是 OceanBase 兼容不兼容。** MariaDB 与 OceanBase 的
差异（没有 `ob_query_timeout`、没有 `ob_compatibility_mode`、元数据视图不同）恰恰是 POC
要验证的对象，所以该后端的结果**不得**写入结论文档，运行时也一律标 `--runtime mariadb-smoke`。

Oracle 模式**没有**对应的自检后端（MariaDB 说的是 MySQL 协议），因此没有任何真实服务器
验证过 O1–O9。其中 **O3、O6 的代码路径由脚本化游标覆盖**（`tests/test_bind_checks.py`、
`tests/test_query_timeout_check.py`：在 Python API 层伪造驱动模块，驱动真实的 `_check_*`
方法），其余七项只有 L1 纯函数测试，**函数体本身从未执行过**。

## 门禁

```bash
python -m pytest -q                                              # POC 自检（161 项）
make ruff TARGET=experiments/oceanbase_driver_poc                # 默认 TARGET 是 mcpgateway，必须显式传
make detect-secrets-scan                                         # 提交前
```

`make bandit TARGET=experiments/oceanbase_driver_poc` 会**以退出码 1 结束**，但其中没有真问题：
210 条全部是 Low —— 199 条 `B101`（测试里的 `assert`）与 11 条 `B105`/`B106`（`tests/` 中
为验证脱敏而写的合成口令，如 `sup3r-s3cret`、`pw`、`p`，不是真实凭据）。本目录不在仓库默认
bandit 目标（`DEFAULT_TARGETS := mcpgateway`）内，因此不影响既有门禁。

**JS 侧同理**：仓库的 `make eslint` 只扫 `mcpgateway/static/**/*.js`，`runtimes/typescript/`
不在其内，因此不会因本目录失败，也不会被自动检查。代码按仓库风格手写（2 空格缩进、分号、
JSDoc），如需本地校验可自行 `npx prettier --check`。`runtimes/typescript/node_modules/` 由
仓库既有的 `node_modules/` 规则忽略。

主仓库回归 `make test` 不会收集本目录，但建议在提交前跑一次确认未受影响。

> ⚠️ **在受限容器里不要直接跑 `make test`。** 该目标用 `pytest -n auto`，而 `-n auto` 取的是
> `os.cpu_count()`。容器里的 CPU 配额与这个数不是一回事：曾观察到 cgroup 只给 1 CPU / 4 GiB，
> 却因为 `os.cpu_count()` 返回宿主机的 256 而派生 256 个 worker，内存耗尽触发 OOM（`oom_kill 13`），
> 把正在轮询的 agent 进程一并杀掉，导致已完成的实现**未提交就丢失**。
>
> 冒烟请显式压住并发：`pytest -n 4` 实测可跑完（24543 项、无 OOM），但内存峰值约
> **4068 MiB / 4096 MiB**，贴着上限，吃紧；想留余量用 `-n 2` 或 `-n 1`。另注意 `make test`
> 会经 `uv run` 重新同步依赖，在内网镜像限流（HTTP 429）时会直接失败；`uv run --frozen` 更稳。
