# PR8 — Production Hardening 测试规划

**关联规划**: [pr8-production-hardening.md](pr8-production-hardening.md)
**依据**: 设计文档 §56–§64 / §66 / §82 DoD

## 测试范围

| 面 | 覆盖点 |
|---|---|
| 健康样本（§57） | `http_health_samples` 表迁移幂等；HttpMonitoringService 每次检查写样本 |
| 外部引用（§66） | 已有测试覆盖：`services/safe_reference_fetcher.py`（PR3 实现）+ `tests/unit/mcpgateway/services/` 下相关用例 |
| Auth/Secret（§67） | `secret_policy` 检测明文；Http/Grpc Create/Update runtime_config 拒绝明文；encrypted(v2:) 放行 |
| Error Mapping/Retry（§68/§73） | map_http_status_to_category 全表；is_retryable_http_method 矩阵 |
| Contract Test（§58/§59，待实施） | schemathesis 对真实 upstream 跑 OpenAPI；Activation Gate off/warn/strict |

## 测试文件

- `tests/unit/mcpgateway/db/test_http_health_samples_migration.py`
- `tests/unit/mcpgateway/utils/test_secret_policy.py`
- `tests/unit/mcpgateway/schemas/test_runtime_config_secret_policy.py`
- `tests/unit/mcpgateway/protocols/http/test_error_mapping.py`
- §66 外部引用：由 PR3 的 `services/safe_reference_fetcher.py` 覆盖（测试随 PR3）

## 用例清单

1. migration：http_health_samples 建表/幂等/降级/列集合
2. fetcher：refuse remote、file:// 拒绝、SSRF metadata-IP 拒绝、transport 失败映射、max_bytes
3. secret policy：明文 password/nested token 检出、encrypted 放行、非字符串放行
4. schemas：4 个 Create/Update 拒绝明文、接受 clean/encrypted
5. error mapping：400/401/403/404/409/429/503 映射；GET/HEAD/OPTIONS 恒重试、PUT/DELETE 瞬时重试、POST/PATCH 不重试

## 需要 E2E 环境的项

- `tests/contracts/http/`（§58 schemathesis）
- 4 个 full-chain：`test_http_full_chain`（§61 最小集）、`test_xml_http_full_chain`（§62）、`test_soap_full_chain`（§63）、`test_grpc_streaming_full_chain`（§64）

## 通过标准

单元/协议测试全绿；明文 secret 被 schema/API 拒绝；HTTP 状态错误映射到 canonical categories；Retry 仅对安全方法生效。
