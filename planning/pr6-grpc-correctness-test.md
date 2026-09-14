# PR6 — gRPC Correctness 测试规划

**关联规划**: [pr6-grpc-correctness.md](pr6-grpc-correctness.md)
**依据**: 设计文档 §36–§45 / §45 / §82 DoD

## 测试范围

| 面 | 覆盖点 |
|---|---|
| ProtoJsonSchemaMapper | int64/uint64/fixed64/sfixed64/sint64 → string+pattern（§38）；WKT 完整映射（§39）；oneof 保留（§40）；递归 $defs |
| runtime_config | GrpcService.runtime_config 列迁移幂等（§42）；grpc-service.yaml runtime 字段（§43） |
| Stream 限制 | maxItems/maxBytes/idleTimeout 截断（§44） |
| Reflection metadata | Reflection/Health/Business 统一 `_resolve_grpc_metadata`（§41） |

## 测试文件

- `tests/unit/mcpgateway/protocols/grpc/test_schema_mapper.py`
- `tests/unit/mcpgateway/db/test_grpc_runtime_config_migration.py`
- `tests/unit/mcpgateway/services/test_grpc_stream_limits.py`
- `tests/unit/mcpgateway/services/test_proto_scan_runtime_config.py`
- `tests/unit/mcpgateway/services/test_grpc_metadata_resolver.py`

## 用例清单

1. mapper：全部 64-bit 类型 → string pattern；WKT 各类型映射；递归 message → $defs；oneof 提示
2. migration：grpc_services.runtime_config 建列/幂等/降级
3. stream：items/bytes 截断、idle/deadline
4. proto_scan：runtime 字段放行、未知字段仍拒绝
5. metadata resolver：解密、override 合并、空/tolerant

## 需要 E2E 环境的项

- `tests/integration/test_grpc_full_chain.py`（§45 要求继续通过）：Reflection metadata 在真实 upstream 生效

## 通过标准

单元/协议测试全绿；int64 schema 为 string 形态；stream 截断语义正确；metadata 三面统一。
