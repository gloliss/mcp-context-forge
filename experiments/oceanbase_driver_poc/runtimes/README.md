# 跨运行时评估

本目录预留给需求的「技术选型要求」中 Python 之外的运行时评估：

- `typescript/` —— 限定范围评估。`mysql2` 覆盖 MySQL 模式的基本连通与查询；
  `node-oracledb` 的 thin 与 thick 两条路径负责回答需求点名的那一个决定性问题：
  **TypeScript 能否稳定支持 OceanBase Oracle Mode**。若结论是不能，按需求
  「不允许为了统一语言强行采用 TypeScript」，不得据 MySQL 侧的可用性放行。
- `dotnet/` —— 仅在确认存在采用意向时创建。候选为 MySQL 模式的 `MySqlConnector`
  与 Oracle 模式的 `Oracle.ManagedDataAccess.Core`（纯托管，无需客户端库）。

**归属层：L3。** 本目录当前为空，属于骨架阶段刻意不做的部分——它依赖尚未确认的
运行时采用意向，且需要与 L2 相同的外部环境才能得出可信结论。
