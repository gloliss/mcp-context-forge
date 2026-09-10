# PR6 — gRPC Correctness（§36–§45）

**目标**：优先修协议正确性，不做 streaming 大改。ProtoJSON 正确、WKT 完整、Reflection metadata 统一、可配置 stream 限制。
**依赖**：PR1–PR3（gRPC Registry）
**后继**：PR7（四类 RPC + grpc.aio）

## 范围（来自设计文档）

| 设计节 | 内容 |
|---|---|
| §37 | 新增 `protocols/grpc/protojson.py` + `schema_mapper.py`；GrpcSchemaService._field_schema/_message_schema 迁入 Mapper |
| §38 | int64/uint64/fixed64/sfixed64/sint64 → JSON string + 数值 pattern |
| §39 | WKT 完整：Timestamp/Duration/Any/Struct/Value/ListValue/包装类型/FieldMask/Empty |
| §40 | oneof/optional：保留 x-protobuf-oneof；不简单对全部 containing_oneof 生成 oneOf |
| §41 | Reflection request / Health Check / Business RPC 统一 metadata resolver；复用 metadata_env，不新增 reflection_metadata |
| §42 | GrpcService.runtime_config JSON NULL 列 + 迁移 |
| §43 | grpc-service.yaml 支持 runtime 字段（proto_scan_service） |
| §44 | Server Stream 限制：硬编码 100 → maxItems/maxBytes/idleTimeout |
| §45 | 测试：mapper/int64/WKT/reflection_metadata/stream_limits/proto_scan_runtime_config |

## 任务清单

| 任务 | 内容 | 依赖 | 状态 |
|---|---|---|---|
| T6.1 | ProtoJsonSchemaMapper 迁移（§37） | — | ✅ `8efe715` |
| T6.2 | 64-bit integer → string（§38） | T6.1 | ✅ `8efe715` |
| T6.3 | WKT 完整映射（§39） | T6.1 | ✅ `8efe715` |
| T6.4 | oneof/optional 保留 vendor extension（§40） | T6.1 | ✅ `8efe715` |
| T6.5 | Reflection/Health/Business metadata 统一 resolver（§41） | — | ✅ `c7c90cd` |
| T6.6 | GrpcService.runtime_config 列 + 迁移 `5c6d7e8f9a0b`（§42） | — | ✅ `8efe715` |
| T6.7 | grpc-service.yaml runtime 字段（§43） | T6.6 | ✅ `8efe715` |
| T6.8 | Server stream 限制（§44） | T6.6 | ✅ `8efe715` |
| T6.9 | 测试（mapper/migration/stream/proto_scan） | T6.1–6.8 | ✅ `8efe715`/`c7c90cd` |

## 验收标准（§45/§82 DoD）

- int64 等以 JSON string 呈现（ProtoJSON canonical）
- WKT 按 ProtoJSON 语义映射
- reflection/health/business 共用同一 metadata 源
- stream 达到 maxItems/maxBytes → `{"items": [...], "truncated": true}`

## 测试要求

- `tests/unit/mcpgateway/protocols/grpc/test_schema_mapper.py`
- `tests/unit/mcpgateway/db/test_grpc_runtime_config_migration.py`
- `tests/unit/mcpgateway/services/test_grpc_stream_limits.py`
- `tests/unit/mcpgateway/services/test_proto_scan_runtime_config.py`
- `tests/unit/mcpgateway/services/test_grpc_metadata_resolver.py`
- `tests/integration/test_grpc_full_chain.py`（待 E2E 环境）

## 交付信息（§80）

1. Changed files：`services/grpc_schema_service.py`、`services/grpc_service.py`、`services/proto_scan_service.py`、`schemas.py`、`db.py`
2. New files：`protocols/grpc/schema_mapper.py`、`protocols/grpc/__init__.py`
3. DB migration：`5c6d7e8f9a0b_add_grpc_runtime_config`
4. Behavior change：int64 schema 从 integer 改为 string+pattern；stream 上限可配置
5. Backward compat：runtime_config 为 NULL 时走默认策略
6. Security：无明文；metadata 统一边界解密
7. Tests added：若干
8. Tests executed：protocols/grpc + services/grpc + db migration
9. Known limitations：运行时 ProtoJSON 序列化转换留待 PR7
10. Next PR dependency：PR7
