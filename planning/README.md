# ContextForge HTTP / XML / SOAP / gRPC — 实施规划

**Requirement**: ContextForge HTTP / XML / SOAP / gRPC 完整支持
**依据**: 平台 authoritative 设计文档《Implementation Design v1》（§1–§83）
**开发方式**: 增量 PR（§77），每个 PR 拆成可独立实施的原子任务（T<PR>.<N>），**先规划后实施**
**执行约定**: 每个任务完成门槛 = 实现 → 定向测试（不跑全量 pytest）→ ruff → `git commit -s`；PR 内任务全过后按 fork 流程走「双远端 → 镜像 → 容器 → E2E」

## 依赖图

```text
Phase 0  HTTP MVP      [PR1] → [PR2] → [PR3]     ✅ 已完成
                          ┌────────────┴────────────┐
Phase 2  Protocol      [PR4 XML/XSD]        [PR6 gRPC Correctness]   ← 可并行
Complete                   ↓                         ↓
Phase 3  Protocol      [PR5 SOAP/WSDL]     [PR7 gRPC Streaming+grpc.aio]
Complete                   └────────────┬────────────┘
                                        ↓
Phase 4  Production    [PR8 Production Hardening]
```

依赖：PR4 ∥ PR6（并行）→ PR5（依赖 PR4）+ PR7（依赖 PR6）→ PR8（依赖全部）。

## PR 规划文档索引

| PR | 规划文档 | 状态 |
|---|---|---|
| PR4 | [pr4-xml-xsd.md](pr4-xml-xsd.md) | 主体完成，T4.4 待办 |
| PR5 | [pr5-soap-wsdl.md](pr5-soap-wsdl.md) | 主体完成，T5.3/T5.6 待办 |
| PR6 | [pr6-grpc-correctness.md](pr6-grpc-correctness.md) | 主体完成，T6.5 已完，全量完成 |
| PR7 | [pr7-grpc-streaming-aio.md](pr7-grpc-streaming-aio.md) | 部分完成（T7.1/7.2/7.3/7.7） |
| PR8 | [pr8-production-hardening.md](pr8-production-hardening.md) | 部分完成（T8.1/8.5a/8.6/8.7） |

## 总任务状态（截至 2026-09-10）

- **已完成**：T4.1–4.3/4.5–4.7、T5.1/5.2/5.4/5.5、T6.1–6.9、T7.1–7.3/7.7、T8.1/8.5a/8.6/8.7
- **待办**：T4.4、T5.3/5.6、T7.4–7.6/7.8/7.9、T8.2–8.4/8.5b

## 横切约束（所有 PR 必须遵守）

- 架构边界（§2–§4）：XML/SOAP 不是 integration_type；保留 `integration_type="REST"`；不重写 gRPC Registry（GrpcService/GrpcSchemaService/GrpcRegistryService/GrpcMonitoringService/GrpcSchemaArtifact/grpc_runtime_cache）；Core 与 Protocol Runtime 分离。
- 禁止本阶段建模（§65）：不建 `api_services/api_operations/api_parameters/api_media_types/xml_schema_elements/soap_bindings`。
- 依赖纪律（§74–§76）：新增只允许 xmlschema（xml extra）、zeep（soap extra）、httpx-sse（optional）；不加 xsdata / Protovalidate。
- migration 纪律（Rule 8）：upgrade 幂等、downgrade 安全、SQLite+PostgreSQL 兼容、保留旧数据、`down_revision` 指向实际当前 head。
- 开发规则（§79）：一次一个 PR；禁止「REST→HTTP」重命名；禁止删除旧 REST DB 列；新代码 typed、不静默 catch、不泄露 secret、不向 client 返回 raw stack trace。
- 交付信息（§80）：每个 PR 输出「Changed/New files、DB migration、Behavior change、Backward compat、Security、Tests added/executed、Known limitations、Next PR dependency」。
