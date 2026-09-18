# OceanBase MySQL / Oracle 双模式 Driver POC 结论

- 关联需求：GitLab issue #6《[OB-00] OceanBase MySQL / Oracle 双模式 Driver POC》
- 设计文档：`specs/designs/issue-6-ob-mysql-oracle-driver-poc.md`
- 测试计划：`specs/tests/issue-6-ob-mysql-oracle-driver-poc.md`
- 实验代码：`experiments/oceanbase_driver_poc/`

> **本文档当前状态：结论未达成。**
> 骨架、配置契约、脱敏与判定规则已实现并通过 L0/L1 自检；**真实验证（L2）尚未执行**——
> 执行环境不可达 OceanBase，且 MySQL 模式没有目标实例。因此本文档中所有「某驱动在
> OceanBase 上可用」的结论**一律为空**，只有「未验证（缺环境）」。
>
> 按需求「如果 CI 环境没有 OceanBase 测试数据库：不允许伪造成功结果」，下方未实测项
> 一律显式标注，不以推测值或替身结果填充。

## 1. 使用 Driver

| 兼容模式 | 候选驱动 | 机制 | 客户端库依赖 | 状态 |
|---|---|---|---|---|
| MySQL | `PyMySQL` | MySQL 线协议，纯 Python | **无** | 已接入，未实测 |
| Oracle | `python-oracledb`（thin） | 纯 Python，直连 Oracle 线协议 | **无** | 已接入，未实测 |
| Oracle | `python-oracledb`（thick） | 包装 OCI | **需要** `libobclient`/OBCI + `LD_LIBRARY_PATH` | 已接入，未实测 |
| Oracle | `cx_Oracle` | 包装 OCI | 需要 OBCI | **已淘汰**，见下 |

**`cx_Oracle` 在可安装性筛选阶段即被淘汰**，未进入兼容性验证：它是 OceanBase 官方
Oracle 模式 Python 路径所指向的驱动，但 8.3.0 是末版，**不支持 Python 3.12+**，而本仓库
锁定 `requires-python = ">=3.12,<3.14"`（实测 3.12.13）。这一条是**安装层面的结论**，与
「它在 OceanBase 上能不能用」是两回事——不得混为一谈，否则会误导下游把「装不上」记成
「不兼容」。

`python-oracledb` 的 thin 与 thick 是**两种不同结论**，部署代价差别很大（thick 会把原生
客户端库拖进镜像）。运行结果通过 `evidence.client_mode` 记录本次实际使用的是哪一种，不以
「能跑通」代替说明用了哪种。

## 2. Driver 版本

| 模式 | 驱动 | 锁定版本 | 来源 |
|---|---|---|---|
| MySQL | `PyMySQL` | `1.2.0` | `experiments/oceanbase_driver_poc/requirements-poc.txt` |
| Oracle | `oracledb` | `26.0.0` | 同上 |

版本由 `common/versions.py::resolve_version()` 从 **distribution 元数据**读取，而非模块的
`__version__` 属性——原因见 §8「已知问题」第 1 条。

**以上是 POC 声明的版本，不是「在 OceanBase 上验证过的版本」。** 实测记录须以
`results/*.json` 中 `driver.version` 字段为准，并注明当时的 OceanBase 版本与客户端模式
（thin/thick）。

## 3. MySQL Mode 验证结果

**汇总结论：未验证（缺环境）（0/9 通过，9 未执行）**

| 检查 | 项目 | 状态 | 说明 |
|---|---|---|---|
| M1 | 建立连接 | 未验证（缺环境） | 未执行：缺少 `OB_MYSQL_HOST`、`OB_MYSQL_PORT`、`OB_MYSQL_USER`、`OB_MYSQL_PASSWORD` |
| M2 | SELECT 1 / 简单查询 | 未验证（缺环境） | 同上 |
| M3 | 参数绑定 | 未验证（缺环境） | 同上；且检查项本身尚未接通（见下） |
| M4 | Connection Pool | 未验证（缺环境） | 同上；且检查项本身尚未接通 |
| M5 | Connection Timeout | 未验证（缺环境） | 同上；且检查项本身尚未接通 |
| M6 | Query Timeout | 未验证（缺环境） | 同上；且检查项本身尚未接通 |
| M7 | Metadata 查询 | 未验证（缺环境） | 同上；且检查项本身尚未接通 |
| M8 | Connection close / reconnect | 未验证（缺环境） | 同上；且检查项本身尚未接通 |
| M9 | 异常处理 | 未验证（缺环境） | 同上；且检查项本身尚未接通 |

**需要区分两件事**，否则会高估完成度：

- **已实现的只有 M1、M2**（连接、简单查询），它们等待环境即可执行；
- **M3–M9 尚未接通**，在没有环境时报告为 `SKIP_NO_ENV`，一旦注入环境变量则会报告为
  `UNSUPPORTED`（`evidence.reason = "l2-pending"`）——即「POC 还没写这段」，不是
  「驱动不支持」。

### 目标实例

**MySQL 模式当前没有目标实例。** 已登记的环境事实只有一个 **Oracle 兼容模式**租户
`rptdb`。OceanBase 单集群可承载多种兼容模式的租户，但本集群是否存在或可否创建 MySQL
模式租户尚未确认。在该输入补齐前，M1–M9 无法得出任何结论。

## 4. Oracle Mode 验证结果

**汇总结论：未验证（缺环境）（0/9 通过，9 未执行）**

| 检查 | 项目 | 状态 | 说明 |
|---|---|---|---|
| O1 | 建立连接 | 未验证（缺环境） | 未执行：缺少 `OB_ORACLE_HOST`、`OB_ORACLE_PORT`、`OB_ORACLE_USER`、`OB_ORACLE_PASSWORD` |
| O2 | 简单 SELECT | 未验证（缺环境） | 同上 |
| O3 | Named Bind Parameter | 未验证（缺环境） | 同上；且检查项本身尚未接通 |
| O4 | Connection Pool | 未验证（缺环境） | 同上；且检查项本身尚未接通 |
| O5 | Connection Timeout | 未验证（缺环境） | 同上；且检查项本身尚未接通 |
| O6 | Query Timeout | 未验证（缺环境） | 同上；且检查项本身尚未接通 |
| O7 | Schema / Table / Column Metadata | 未验证（缺环境） | 同上；且检查项本身尚未接通 |
| O8 | Connection close / reconnect | 未验证（缺环境） | 同上；且检查项本身尚未接通 |
| O9 | 异常处理 | 未验证（缺环境） | 同上；且检查项本身尚未接通 |

已实现 O1、O2；O3–O9 尚未接通，语义同 §3。

**Oracle 模式是本次选型的决定性未知项。** 需求 §「技术选型要求」点名的那一个问题——
「TypeScript 方案能否稳定支持 OceanBase Oracle Mode」——以及 issue-5 登记的 R9/R10，
都还没有答案。

### 已登记的目标实例（未连通）

| 项 | 值 |
|---|---|
| OceanBase 版本 | 4.3.5.6 |
| 兼容模式 | Oracle |
| 租户 / 服务名 / Schema | `rptdb` / `rtd6` / `RTD6` |
| 接入方式 | 经 OBProxy |
| 用户名形式 | `askquery@rptdb#hldw`（原样使用，不追加租户或集群名） |
| TLS | 不要求 |

该实例在本次执行环境**不可达**（连接超时）。凭据不入库、不进日志，只经环境变量注入。

## 5. Pool 支持情况

**结论：未验证。** M4 / O4 尚未接通，因此没有任何实测数据。

已就绪的部分是配置契约：`OB_{MYSQL,ORACLE}_POOL_MIN` / `_POOL_MAX` 可读入
（默认 `0` / `5`），但**当前驱动适配器尚未据其建立连接池**——`_connect()` 走的是单连接
路径。这是骨架阶段的刻意取舍：池语义只能在真实实例上验证，提前写死会掩盖真实行为。

待验证的关键点（对应测试计划 §2.3）：借出/归还/复用是否可达、池满时是否有界等待并返回
可辨识错误、失效连接是否被剔除而不是还给调用方。

## 6. Timeout 支持情况

**结论：未验证。** M5 / M6 / O5 / O6 尚未接通。

已就绪的是**参数映射**（纯函数，L1 已覆盖）与一处**必须被显式处理的单位差**：

| 参数 | MySQL 模式 | Oracle 模式 |
|---|---|---|
| 连接超时 | PyMySQL `connect_timeout`（**秒**） | oracledb `tcp_connect_timeout`（**秒**） |
| 客户端查询超时 | PyMySQL `read_timeout`（**秒**，socket 级） | `connection.call_timeout`（**毫秒**） |
| 服务端查询超时 | `SET SESSION ob_query_timeout`（**微秒**） | `ALTER SESSION SET ob_query_timeout`（**微秒**） |

三种单位并存是真实的缺陷来源，因此换算被测试锁定而不是靠记忆：OceanBase 自身的
`ob_query_timeout` 用微秒，PyMySQL 的 socket 超时用秒，oracledb 的 `call_timeout` 用毫秒。

**客户端超时不等于服务端停止。** `read_timeout` / `call_timeout` 只让调用方不再等待，服务端
查询可能仍在跑——这正是 issue-5 记录的既有缺口（`tool_service.py:7273-7284`）。因此
Query Timeout 一项要求两级证据，且**二级证据（服务端确实停止）是选型的一票否决项**；
「已执行但观测不到服务端行为」记为 `INDETERMINATE`，**不得**记为通过。

Oracle 方面另有两个待验证点：`ALTER SESSION SET ob_query_timeout` 在 OB Oracle 模式下是否
被接受，以及 `connection.cancel()` 是否真能停下服务端查询（对应 issue-5 R10）。

## 7. Parameter Bind 形式

**结论：未验证。** M3 / O3 尚未接通。

按驱动语法，预期的绑定形式为：

| 模式 | 形式 | 示例 |
|---|---|---|
| MySQL | `%s` 位置占位符，或 `%(name)s` 命名占位符 | `SELECT ... WHERE id = %s` |
| Oracle | `:name` 命名绑定 | `SELECT ... FROM T WHERE ID = :id` |

**约束（不因未验证而放宽）**：参数一律走驱动绑定，禁止拼接进 SQL；动态表名/字段名必须
来自已授权标识符集合。待验证项包含「特殊字符与类型边界参数不改变 SQL 结构」——引号、
分号、注释符与 Unicode 参数是必测集。

## 8. 已知问题

1. **PyMySQL 的 `__version__` 与发行版不一致。** 模块内声明 `__version__ = "2.2.8"`，而
   发行版是 `1.2.0`（`2.2.8` 是其中继的 MySQL 协议版本）。若直接读属性，结论文档会写下一个
   **没有任何发行版可以安装**的版本号。已通过 `resolve_version()` 优先读 distribution
   元数据修正，并由 `tests/test_versions.py` 锁定。
2. **`cx_Oracle` 无法在当前运行时安装**（§1），因此它的候选路径需要第二套 Python（≤3.10）。
   仅在 thin 与 thick 都不通时才会启用，成本须计入选型结论。
3. **O1 的版本探测用 `v$version`，可能因权限失败。** 若 POC 运行账号是需求要求的最小权限
   查询账号，该视图可能不可读，O1 会失败在**权限**而非**驱动兼容性**上——这是一次
   **假阴性**，会把权限问题误判成驱动问题。联网执行前应先在目标实例确认该视图可用性；
   不可用时需改用 `SELECT * FROM v$instance` 之外的降级探测，或明确记录该失败成因。
4. **MySQL 模式无目标实例**（§3），该模式整体无法得出结论。
5. **M3–M9 / O3–O9 尚未接通**，注入环境变量后会报 `UNSUPPORTED`（`l2-pending`），而非
   给出结果。当前完成度是骨架 + 2/9 检查实现。
6. **`runtimes/` 为空。** TypeScript / .NET 的横向评估（需求点名 TS 能否稳定支持 OB Oracle
   Mode）尚未开始，属 L3。
7. **POC 依赖不进主锁是刻意的。** `requirements-poc.txt` 独立存在，因为 `cx_Oracle` 在
   Python 3.12 上装不上，一旦进入 `pyproject.toml` / `uv.lock` 会让 `uv lock` 与 CI 直接
   失败。
8. **端口无默认值，是刻意的。** 经 OBProxy 与直连使用不同端口，写死默认值会让 POC 静默指向
   错误端点并给出看似可信的错误结论。
9. **用极短口令做测试会看到「乱码」般的输出，这是设计取舍而非缺陷。** 脱敏器把已注册口令
   按字面量全局替换，不设最小长度、也不区分「这处出现是不是真口令」。用单字符口令（如
   `p`）时，报告里的 `pymysql` 会被遮成 `***ymysql`。该行为在 `common/redaction.py` 的类
   文档中已明确记录并刻意保留：**被遮花一个词是外观缺陷，而口令落进提交物是安全失败**，
   且没有可靠办法判断「这次出现的口令才是真的」。测试时请使用接近真实长度的口令；要改的
   不是脱敏强度。

## 9. 最终 Runtime 建议

**当前无法给出最终的驱动与 Runtime 选型结论。** 原因不是分析不足，而是缺三样外部输入：
可访问 OceanBase 的执行环境、MySQL 模式目标实例、以及 Oracle 模式「服务端确实停止」的
观测手段。在补齐之前给出推荐会违反需求「不允许伪造成功结果」。

可以确定的部分：

- **Python 应作为主评估对象**，两种模式的全矩阵都在 Python 侧（需求亦将其列为首位）；
- **TypeScript 的取舍已被需求预先约束**——「如果 TypeScript 方案不能稳定支持 OceanBase
  Oracle Mode，不允许为了统一语言强行采用 TypeScript」。因此 TS 的结论**只能由 OB Oracle
  Mode 的实测决定**，不得以 MySQL 侧可用或生态成熟为理由放行；
- **`cx_Oracle` 路径的代价已经明确**（§1/§8.2），不构成默认选择；
- **Oracle 模式若无法证明服务端查询可停止，不得进入稳定版**——这是 issue-5 的 A07 硬要求，
  一票否决。

选型判据（按优先级，来自设计 §5.4）：① 能否通过 Query Timeout 二级证据；② 是否受维护、
能否在当前 Python 3.12 上安装运行；③ 能力覆盖度（池、超时、绑定、元数据、类型保真）；
④ 引入成本（是否新增运行时 / 客户端库 / 部署依赖）。

## 10. 对 issue-5 的回填

| issue-5 项 | 当前状态 |
|---|---|
| R9（Oracle 模式 Python 驱动硬冲突） | **部分收敛**：`cx_Oracle` 已在可安装性层面淘汰（Python 3.12 无法安装）；`python-oracledb` thin/thick 与 OB Oracle 模式的兼容性**仍未证实**，R9 未关闭 |
| R10（Oracle 模式缺语句级超时） | **未收敛**：`ob_query_timeout` 与 `connection.cancel()` 两条路径均待实测，R10 未关闭 |
| §11.2 驱动候选表 | 候选 A（thin）/ B（thick）已接入待测；候选 C（`cx_Oracle` + 旧 Python）成本已明确；Java Connector/J 未评估 |
| `validated_compat_modes` 建议值 | **建议为空集**——当前没有任何模式被实测通过。放行集合必须由 L2 实测结果填充，不得凭「探测到某模式」或「驱动能装」写入 |

## 11. 复现方式与环境清单

### 复现

```bash
# 1) POC 自检（不需要数据库）
python -m pytest -q                                    # 在 experiments/oceanbase_driver_poc/ 下

# 2) 注入连接信息（只在执行环境注入，不写入任何提交物）
export OB_MYSQL_HOST=... OB_MYSQL_PORT=... OB_MYSQL_USER=... OB_MYSQL_PASSWORD=...
export OB_ORACLE_HOST=... OB_ORACLE_PORT=... OB_ORACLE_USER=... OB_ORACLE_PASSWORD=...
export OB_ORACLE_SERVICE_NAME=...

# 3) 真实验证（在能访问 OceanBase 的机器上）
python run_poc.py --mode all --out results/

# 4) 由结果 JSON 生成本文档实测段落，避免手写
python run_poc.py --mode all --md-out /tmp/poc-sections.md
```

退出码：`0` 无失败；`2` 有失败/异常/无法判定；`1` 用法或配置错误。仅 `SKIP_NO_ENV`
不让退出码非零（它表示「没验证出结论」），加 `--fail-on-skip` 可让未执行项也返回非零。

### 环境清单（须随实测结果一并记录）

| 项 | 说明 |
|---|---|
| 运行时 | Python 版本（本仓库 `>=3.12,<3.14`） |
| 驱动 | 名称 + **发行版版本**（取自 `results/*.json`，非模块属性） |
| Oracle 客户端模式 | `thin` / `thick`（记入 `evidence.client_mode`） |
| 客户端库 | thick 模式下的 `libobclient`/OBCI 版本与 `LD_LIBRARY_PATH` |
| OceanBase | 版本、租户、兼容模式 |
| 接入 | OBProxy 或直连、地址端口、服务名 |
| 凭据 | **只记注入方式，不记值** |

## 12. 如何读结果

每次运行写入 `results/<mode>-<runtime>-<timestamp>.json`，其中 `aggregate.verdict` 是唯一
的汇总结论来源。状态词表的核心是**未执行不等于通过**：

| 状态 | 含义 | 算通过？ |
|---|---|---|
| `PASS` | 已执行且通过 | 是 |
| `FAIL` | 已执行且不达标 | 否 |
| `SKIP_NO_ENV` | 缺环境变量，未执行 | **否** |
| `UNSUPPORTED` | 该驱动无法提供此能力（未安装，或 POC 尚未接通） | **否** |
| `INDETERMINATE` | 已执行但无法判定 | **否** |
| `ERROR` | 执行中抛出异常 | 否 |

只有 `PASS` 计为通过；全部跳过时汇总措辞固定为「未验证（缺环境）」，绝不出现「通过」字样。
`results/` 下的文件**不提交仓库**（已由 `.gitignore` 覆盖），需留证时附到需求时间线。
