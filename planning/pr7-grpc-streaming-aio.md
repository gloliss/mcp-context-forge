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
| T7.4 | grpc.aio.Channel 迁移（§53，保留 RuntimeCache） | — | ⏳ 见下「T7.4 细化」 |
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

## T7.4 细化（§53 grpc.aio.Channel 迁移）

**目标**：把业务 Channel 从同步 `grpc.Channel` 迁到 `grpc.aio.Channel`，使 RPC 原生化（可被 asyncio 取消），同时**保留** RuntimeCache 的 Channel / DescriptorPool / MessageClass 复用。

**现状调研（2026-09-10）**

| 位置 | 现状 | 迁移影响 |
|---|---|---|
| `translate_grpc.GrpcEndpoint.start` | `grpc.secure_channel` / `grpc.insecure_channel` | 改 `grpc.aio.*` |
| `GrpcEndpoint.invoke` | 同步 `unary.with_call` 经 `_run_bounded_blocking_call` 丢到 executor | 改 `await call(req, timeout=, metadata=)`；`initial_metadata()`/`trailing_metadata()` 改 await |
| `GrpcEndpoint.invoke_streaming` | 同步迭代器丢到 executor | 改 `async for resp in call(...)` |
| `_discover_services` / `_discover_service_details` | 同步反射 stub + executor | 反射 `ServerReflectionInfo` 是 **bidi**；改 aio 泛型 `stream_stream` |
| `grpc_runtime_cache._build_channel` | 建同步 channel，跨请求/跨事件循环复用 | **关键风险**：aio channel 绑定创建时的 event loop |

**关键设计决策：Channel 的 event-loop 亲和性**

`grpc.aio.Channel` 绑定其创建时的 running loop，跨 loop 复用会静默失效或报错。RuntimeCache 是进程级、跨请求的，因此：

- 缓存键在 `(service_id, schema_hash, connection fingerprint)` 之外**追加 `id(running_loop)`**，不同 loop 各自持有自己的 channel；
- 缓存条目的 `close()` 是协程：LRU 淘汰发生在同步锁内，改为把待关闭 channel 收集起来、由调用方在锁外 `await` 关闭（或对已死 loop 直接跳过）；
- `_build_channel` 接收 loop 上下文（在 async `acquire` 路径内创建）。

**分阶段**（一次 PR 内完成，便于评审与回滚）

- **T7.4a**：`GrpcEndpoint` 内部改用 aio channel（`invoke` 一元 + `invoke_streaming` 服务端流）；
- **T7.4b**：反射发现改 aio（bidi `stream_stream`）；
- **T7.4c**：RuntimeCache loop 亲和 + 淘汰时的异步关闭。

**验收**
- 一元 / 服务端流经 aio channel 完成，`_last_call_metadata` 的 headers/trailers/status 仍被填充
- 反射发现服务与 `_discover_service_details` 仍产出相同的 `_services` / `_descriptors` 结构
- RuntimeCache 命中复用不重建 channel；不同 event loop 不共享同一 channel
- 既有 `tests/unit/mcpgateway/services/test_grpc_*` 与 translate_grpc 相关测试全绿

**范围边界**：T7.4 只做「同步 → aio」的等价迁移 + 缓存 loop 亲和；**不**新增 client-stream/bidi 业务能力（T7.5），也**不**改 ToolService 接线（T7.8）。取消传播（§54）依赖本任务的 aio 化，落地于 T7.6。

**测试文件**
- `tests/unit/mcpgateway/protocols/grpc/test_grpc_adapter.py`（扩展）
- `tests/unit/mcpgateway/services/test_grpc_runtime_cache.py`（loop 亲和新增用例）

