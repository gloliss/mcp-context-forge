# PR7 — gRPC 四类 RPC + grpc.aio（§46–§55）

**目标**：新增 GrpcProtocolAdapter；支持 unary_unary / unary_stream / stream_unary / stream_stream；业务 Channel 迁移 grpc.aio；cancellation 传播；Status Detail 映射。
**依赖**：PR6（Correctness）
**后继**：PR8（full-chain 集成测试）

## 范围（来自设计文档）

| 设计节 | 内容 |
|---|---|
| §47 | 新增 `protocols/grpc/adapter.py` + `stream.py`；ToolService gRPC branch 逐步迁向 ProtocolAdapterRegistry |
| §48 | 四类 RPC：unary_unary / unary_stream / stream_unary / stream_stream |
| §49/§50 | MCP Stream 输入模型：`{"items": [...]}` → stream_unary/bidi；普通 tools/call 保持 bounded request → bounded result |
| §51 | True streaming 走 Debugger/SSE/Streamable HTTP，不硬塞 MCP Result |
| §52 | StreamLimiter：max_items/max_bytes/idle_timeout/deadline |
| §53 | grpc.Channel → grpc.aio.Channel；保留 RuntimeCache（Channel/DescriptorPool/MessageClass） |
| §54 | Cancellation：MCP caller 取消 → call.cancel() → upstream |
| §55 | grpc-status-details-bin / google.rpc.Status → Error Model |

## 任务清单

| 任务 | 内容 | 依赖 | 状态 |
|---|---|---|---|
| T7.1 | GrpcProtocolAdapter（§47，duck-typed endpoint） | — | ✅ `5ab7bec` |
| T7.2 | unary_unary / unary_stream 适配（§48 前两类） | T7.1 | ✅ `5ab7bec` |
| T7.3 | StreamLimiter（§52） | — | ✅ `5ab7bec` |
| T7.4 | grpc.aio.Channel 迁移（§53，保留 RuntimeCache） | — | ⏳ |
| T7.5 | client-stream / bidi 两类 RPC（§48 后两类） | T7.4 | ⏳ |
| T7.6 | Cancellation 传播（§54） | T7.4 | ⏳ |
| T7.7 | gRPC Status Detail → Error Model（§55） | — | ✅ `a1ecf97` |
| T7.8 | ToolService gRPC branch 迁入 ProtocolAdapterRegistry（§47） | T7.1/T7.5 | ⏳ |
| T7.9 | full chain 四类 RPC 测试（§64：扩展 grpc_test_server 加 ClientStream/BidiStream） | T7.5 + E2E 环境 | ⏳ |

## 验收标准（§64/§82 DoD）

- 四类 RPC 均可用（Unary/ServerStream/ClientStream/Bidi）
- Reflection metadata、Deadline、Cancellation、maxItems/maxBytes 生效
- grpc-status-details-bin 映射到 canonical Error Model

## 测试要求

- `tests/unit/mcpgateway/protocols/grpc/test_grpc_adapter.py`
- `tests/unit/mcpgateway/protocols/grpc/test_stream_limiter.py`
- `tests/unit/mcpgateway/services/test_grpc_status_details.py`
- `tests/integration/test_grpc_streaming_full_chain.py`（待 E2E 环境）

## 交付信息（§80）

1. Changed files：`protocols/grpc/__init__.py`、`services/grpc_service.py`
2. New files：`protocols/grpc/adapter.py`、`protocols/grpc/stream.py`
3. DB migration：无
4. Behavior change：gRPC 调用经 adapter 层；stream 截断语义化
5. Backward compat：unary 路径经 endpoint 兼容
6. Security：status detail 不泄露内部 stack
7. Tests added：若干
8. Tests executed：protocols/grpc + services/grpc
9. Known limitations：grpc.aio 迁移（T7.4）、client-stream/bidi（T7.5）、ToolService 接线（T7.8）待续
10. Next PR dependency：PR8
