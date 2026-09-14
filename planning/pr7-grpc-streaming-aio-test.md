# PR7 — gRPC 四类 RPC + grpc.aio 测试规划

**关联规划**: [pr7-grpc-streaming-aio.md](pr7-grpc-streaming-aio.md)
**依据**: 设计文档 §46–§55 / §64 / §82 DoD

## 测试范围

| 面 | 覆盖点 |
|---|---|
| GrpcProtocolAdapter | unary→unary 走 endpoint.invoke；unary→server stream 走 invoke_streaming + StreamLimiter；client-stream/bidi 明确 UNSUPPORTED |
| StreamLimiter | max_items/max_bytes/idle_timeout/deadline；StreamLimitError 不被 async for 吞掉 |
| Status Detail | map_grpc_status_to_category（§55/§73 全表）；parse_grpc_status_details（wire base64 / client 已解码两形态） |
| Cancellation（待实施） | MCP 取消 → call.cancel() → upstream 传播 |

## 测试文件

- `tests/unit/mcpgateway/protocols/grpc/test_grpc_adapter.py`
- `tests/unit/mcpgateway/protocols/grpc/test_stream_limiter.py`
- `tests/unit/mcpgateway/services/test_grpc_status_details.py`

## 用例清单

1. adapter：unary 委托、server-stream bounded 收集、截断标志、client-stream UNSUPPORTED、缺字段 INVALID_ARGUMENT
2. stream limiter：items/bytes/idle/deadline、0 禁用
3. status details：category 全表、details-bin 解析/缺失/垃圾输入
4. cancellation（T7.6 完成后）：取消传播到 endpoint

## 需要 E2E 环境的项

- `tests/integration/test_grpc_streaming_full_chain.py`（§64）：四类 RPC、Metadata Reflection、Deadline、Cancellation、maxItems/maxBytes
- 扩展 `tests/grpc_test_server` 增加 `ClientStream`/`BidiStream` RPC

## 通过标准

单元/协议测试全绿；client-stream/bidi 在 grpc.aio 落地前返回 UNSUPPORTED 而非静默错误；status-details 映射到 canonical categories。
