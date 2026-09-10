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
| T7.4 | grpc.aio.Channel 迁移（§53，保留 RuntimeCache） | — | ✅ 见下「T7.4 细化」 |
| T7.5 | client-stream / bidi 两类 RPC（§48 后两类） | T7.4 | ✅ 见下「T7.5/T7.6/T7.9 实施结果」 |
| T7.6 | Cancellation 传播（§54） | T7.4 | ✅ 见下「T7.5/T7.6/T7.9 实施结果」 |
| T7.7 | gRPC Status Detail → Error Model（§55） | — | ✅ `a1ecf97` |
| T7.8 | ToolService gRPC branch 迁入 ProtocolAdapterRegistry（§47） | T7.1/T7.5 | ✅ 见下「T7.8 实施结果」 |
| T7.9 | full chain 四类 RPC 测试（§64：扩展 grpc_test_server 加 ClientStream/BidiStream） | T7.5 + E2E 环境 | ✅ 见下 |

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

**实测波及面（2026-09-10 试做后回退，供专项实施参考）**

按本规格实施 T7.4a/b（`translate_grpc.py` 改 aio channel + invoke + 流 + 反射）后实测：

- `tests/unit/mcpgateway/test_translate_grpc.py` **16 个用例失败**（断言同步 channel / 同步 stub 的实现细节）：`test_start_insecure_channel`、`test_start_secure_channel_with_certs/without_certs`、`test_start_trusted_local_skips_validation`、`test_discover_services_success/skip_reflection_service/error`、`test_endpoint_start_without_reflection`、`test_endpoint_start_with_tls_and_reflection`、`test_discover_services_success_no_grpc`、`test_discover_services_ignores_non_list_services_response`、`test_discover_service_details_success/ignores_non_descriptor_response/skips_unrelated_service`、`test_invoke_and_invoke_streaming_without_grpc`、`test_invoke_streaming_rpc_error`。这些断言的是 §53 **刻意要改掉**的同步行为，需按「保持原测试意图、改写断言目标」更新。
- **一致性硬约束**：`GrpcEndpoint` 改为 aio 后，`GrpcRuntimeCache._build_channel` 仍建**同步** channel 并注入，属未定义行为；`grpc_service.py` 仍用同步 `ServerReflectionStub`（line 289）与自带反射 executor（line 322）。因此 T7.4a/b/c 必须**一次性成套**落地，不可只提交 `translate_grpc.py`。
- **决策（已修订）**：该专项已一次性完成（见下「T7.4 实施结果」）。


**测试文件**
- `tests/unit/mcpgateway/protocols/grpc/test_grpc_adapter.py`（扩展）
- `tests/unit/mcpgateway/services/test_grpc_runtime_cache.py`（loop 亲和新增用例）

## T7.4 实施结果

**成套落地**（a+b+c 一次提交，避免「aio endpoint + sync channel」的不一致中间态）：

- `translate_grpc.GrpcEndpoint`：`grpc.aio.secure_channel` / `insecure_channel`；一元调用改为 `call = unary(...)` → `await call`（元数据/状态码挂在 **call 对象**上，不在可调用体上）；`invoke_streaming` 改原生 `async for`；`close()` 兼容协程。
- 反射发现：新增 `_collect_reflection_responses(channel, requests, timeout, metadata)` —— 反射 `ServerReflectionInfo` 是 **bidi**，用泛型 `stream_stream` 完成 write→done_writing→drain；`_discover_services` / `_discover_service_details` 改用它，移除 executor 卸载（`_run_bounded_blocking_call` 删除）。
- `grpc_service`：反射改用 aio channel 与上述收集器；`key_for` 追加 `loop_id`（channel 绑定创建时的 event loop）。
- `grpc_runtime_cache`：`_build_channel` 建 aio channel；`_CacheEntry` 记录 `loop`；`close()` 把协程 `run_coroutine_threadsafe` 回宿主 loop（无活动 loop 时丢弃协程，不泄漏、不阻塞缓存锁）；缓存键折入 loop 身份。

**测试更新**：`test_translate_grpc.py`（16 例）、`test_grpc_service.py`、`test_grpc_service_no_grpc.py` 改为断言 aio 形状（aio channel 构造、`_collect_reflection_responses` 替身、awaitable call 对象、`await code()`）；`test_grpc_runtime_cache.py` 新增 loop 亲和 9 例。全部**保留原测试意图**，只改断言目标。

**验证**
- 单元：286 passed（translate_grpc + services/test_grpc_* + protocols/grpc）
- **集成（真实 gRPC server）**：`tests/integration/test_grpc_full_chain.py --with-integration` **35 passed** —— 覆盖反射全链、一元/服务端流、deadline、metadata 鉴权、无反射 proto 导入、schema v1→v2、并发（同方法/跨方法/跨服务/流式混合/channel 池压力）、大消息（1MB/4MB/批量/并发）。
- **集成测试捕获到 2 个 mock 无法发现的真实缺陷**：① `unary.code()` 误用在可调用体而非 call 对象上；② grpc.aio 的 `Call.code()` 是**协程**（与同步 API 不同）。均已修复。


## T7.5/T7.6/T7.9 实施结果

**T7.5 四类 RPC（§48–§50）**
- `GrpcEndpoint` 新增 `invoke_client_stream(service, method, items, timeout)`（stream→unary：写完全部消息→`done_writing()`→等单个响应）与 `invoke_bidi_stream(service, method, items, timeout)`（stream→stream）。
- 抽出 `_require_method()` / `_resolve_message_classes()` 复用反射校验与消息类解析。
- 适配器：新增 `_request_items()` 实现 **MCP Stream 输入模型**（§49/§50）——`{"items": [...]}`（裸 list 亦接受），空/缺失报 `grpc-stream-items-required`（INVALID_ARGUMENT）；`stream_unary` 返回单个响应，`stream_stream` 用同一 `StreamLimiter` 收敛为 `{"items":…, "truncated":…}`（§52）。
- `grpc_service.invoke_method`：client-streaming / bidi 不再直接拒绝，改走上述两条路径（§50：普通 tools/call 仍是 bounded request → bounded result）。
- `_sync_tools_from_reflection`：移除「client-streaming 方法强制禁用/废弃」的特例——能力已具备，这些方法现在与其它模式一样发布为可用工具。

**T7.6 Cancellation（§54）**
- 一元、服务端流、客户端流、bidi 四条路径均显式捕获 `asyncio.CancelledError` → `call.cancel()` → 重新抛出，确保取消真正抵达上游 RPC 而非仅仅放弃协程。

**T7.9 集成测试（§64）**
- `tests/grpc_test_server` 扩展：`echo.proto` 新增 `EchoClientStream` / `EchoBidiStream` / `EchoSlowStream`（每秒一块，供取消测试），重新生成 stub（protobuf 7.35.1，与 `pyproject.toml` 声明一致），并在 `server.py` 实现（含 `context.is_active()` 短路）。
- `tests/integration/test_grpc_full_chain.py` 新增：`TestFourRpcModes`（四类各一例 + 缺 items 拒绝 + 流式方法已发布为可用工具）、`TestCancellationPropagation`（一元与流式各一例，断言取消后**远早于**上游 3s/5s 延迟返回）。

**验证**：单元 300+ passed；集成 `--with-integration` **43 passed**（真实 gRPC server）。

## T7.8 实施结果（§47 Skill gRPC 分支迁入 ProtocolAdapterRegistry）

**四类 RPC 分派收敛到一处**：`GrpcService.invoke_method` 不再自己实现模式分派，改为构造 `OperationDefinition` 并调用新增的 `GrpcProtocolAdapter.invoke_via_endpoint(operation, arguments, timeout=, stream_callback=)`。该入口专为「调用方已持有 endpoint 生命周期」的场景设计（endpoint 的构建/缓存/关闭仍归 `GrpcService`），避免为了复用适配器而伪造一个 `InvocationContext`。

由此消除了 T7.5 引入的重复：两处各自实现的「四类 RPC 怎么走 + 流怎么限」合并为一处。

**流式字节统计统一**：`_collect_bounded_stream` 与 `StreamLimiter` 是同一语义的两份实现，且存在一个边界差异（首个超限项的取舍）。统一时**完整保留了原有统计语义**——`serialize_item` 返回序列化字符串、`item_size` 返回其长度（即原 `len(_serialize_item(item))`），`max_bytes` 的行为逐字不变；`test_grpc_stream_limits.py` 的 7 个用例（含字节上限、零上限、逐项回调）全部改为针对统一实现并保持通过。

**Registry 注册**：`ProtocolRegistry` 新增 `register_factory` / `get_factory` / `protocols`，`build_default_protocol_registry()` 现注册 `"http"`（共享实例）与 `"grpc"`（工厂——适配器绑定单个存活 endpoint，必须按调用构造）。

**删除的重复代码**：`grpc_service._collect_bounded_stream`、`grpc_service._serialize_item`。

**验证**：单元（protocols + translate_grpc + services/test_grpc_* + tool_service + contracts）全绿；集成 `--with-integration` **68 passed**（HTTP 17 + XML 5 + SOAP 3 + gRPC 43），其中 gRPC 43 例覆盖四类 RPC、取消、并发、大消息、schema 迁移——本次重构是在该真实 server 套件守护下完成的。
