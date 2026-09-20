# 跨运行时评估

本目录承载需求的「技术选型要求」中 Python 之外的运行时评估。

## 当前状态

| 运行时 | 状态 | 说明 |
|---|---|---|
| Python | 全矩阵（18 项） | 见 `../../` 与结论文档 §3–§7 |
| TypeScript | **限定范围，已可运行** | `typescript/`，见下 |
| .NET | **未开始** | 本环境没有 `dotnet`；需求原文是「.NET Runtime（**如需要**）」，尚待采用意向 |

## TypeScript（`typescript/`）

范围按设计 D5 收敛：只回答需求点名的那一个问题——**「TypeScript 能否稳定支持 OceanBase
Oracle Mode」**——外加 MySQL 侧的基本连通与查询。不做第二个全矩阵。

```bash
cd runtimes/typescript
npm install
node run.mjs --mode all --out ../../results/ts
```

环境变量与 Python 侧**完全一致**（`OB_MYSQL_*` / `OB_ORACLE_*`），同一套环境驱动两个运行时；
结果 JSON 用同一套 schema 与同一套六态词表，因此两边可以直接并排读。

### 已经确定的事（不需要 OceanBase）

`node run.mjs --mode oracle` 在没有任何连接的情况下就能给出两条真实结论：

| 检查 | 含义 |
|---|---|
| `TS-PRE-THIN` | `node-oracledb` 能否加载、是否处于 thin 模式（**无需客户端库**） |
| `TS-PRE-THICK` | thick 模式能否启用（**需要** Oracle Instant Client / `libclntsh.so`） |

这与 Python 侧对 `python-oracledb` thin/thick 的区分是同一个问题，答案同样影响部署代价：
选 thin 不往镜像里拖原生库，选 thick 就必须带。

### 还不能确定的事

**「TypeScript 能否稳定支持 OB Oracle Mode」仍未回答。** 上面两条只说明驱动装得上、加载得起来，
不说明它能连上 OceanBase 的 Oracle 兼容模式。那需要真实实例，与 Python 侧 O1–O9 是同一个阻塞。

在拿到实例之前，任何「TS 可用/不可用」的结论都不能下——这正是需求那句「不允许为了统一语言
强行采用 TypeScript」要防的事，反过来也一样：不能因为 TS 生态成熟就默认它能用。

### 与 Python 侧的一处实质差异

MySQL 模式下 mysql2 提供**两条**绑定路径，而 PyMySQL 只有一条：

| 路径 | 绑定方式 |
|---|---|
| `mysql2.execute()` | **服务端预编译**（`lib/commands/prepare.js` + `execute.js`，连接级语句缓存） |
| `mysql2.query()` | 客户端转义后拼接，与 PyMySQL 的 `cursor.execute(sql, args)` 同性质 |

因此 TS-M3 两条都跑，并把结果分别记入 `evidence.modes`。服务端预编译是更强的绑定模式，
它在 OceanBase 上是否可用是一个**兼容性问题**，不是可以假定的前提。

## 目录为什么叫 `shared/` 而不是 `lib/`

仓库根的 `.gitignore` 有一条**裸的 `lib/`** 规则（为 Python 打包产物而设），它会静默吞掉任何
名为 `lib/` 的目录。第一次提交这份 TS 代码时正是如此：`run.mjs` 与两个 check 进了暂存区，
四个 `lib/*.mjs` 却一个都没进——**代码会在 import 处直接崩，而 `git status` 看上去一切正常**。

改成 `shared/` 是刻意的：与其在 `.gitignore` 里为这个路径开一个否定规则（脆弱，且下一个人加
文件时还会再踩），不如不撞这条规则。请勿改回 `lib/`。

## 边界

`runtimes/` 下的东西一律**不写入结论文档的 §3–§7**（那是 Python 侧 OceanBase 实测结果的
位置）。TS 的结论写到结论文档 §9 与本节。以 MariaDB 为后端的运行只能验证 harness 代码本身，
标 `--runtime mariadb-smoke`，同样不入 §3–§7。
