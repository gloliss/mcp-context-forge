# PR5 — SOAP / WSDL 支持（§30–§35）

**目标**：HTTP 之上支持 SOAP 1.1/1.2。Zeep 只负责解析，业务运行时走 Tool → HttpProtocolAdapter → SoapCodec → HTTPX（复用 SSRF/TLS/Auth/Plugin/Audit/Metrics）。
**依赖**：PR4（XML codec）
**后继**：PR8（SOAP full-chain 测试）

## 范围（来自设计文档）

| 设计节 | 内容 |
|---|---|
| §30 | 新增 `protocols/contracts/wsdl.py`、`protocols/codecs/soap.py`、`protocols/http/soap.py` |
| §31 | WsdlContractProvider：WSDL → service/port/binding/operation/message/type → OperationDefinition → OperationCatalog |
| §32 | Zeep 仅解析；禁止 `zeep.Client(...).service.xxx()` 作为生产调用主链 |
| §33 | SoapCodec：SOAP 1.1（text/xml + SOAPAction）/ 1.2（application/soap+xml）、Envelope/Header/Body/namespace/Fault |
| §34 | SOAP Fault → ProtocolError：Client→INVALID_ARGUMENT/UPSTREAM_ERROR、Server→UPSTREAM_ERROR；保留 code/string/safe detail |
| §35 | 依赖：`zeep` 进 `soap` extra（optional） |

## 任务清单

| 任务 | 内容 | 依赖 | 状态 |
|---|---|---|---|
| T5.1 | WsdlContractProvider（§31） | — | ✅ `da5883f` |
| T5.2 | SoapCodec（§33） | T4.x | ✅ `da5883f` |
| T5.3 | SOAP runtime 完整接线：HttpAdapter 集成 SoapCodec + soap headers + Fault 映射 | T5.2 | ✅ `1487619`（见下「T5.3 细化」） |
| T5.4 | SOAP Fault → Error Model（§34） | T5.2 | ✅ `da5883f` |
| T5.5 | 依赖 `zeep` → soap extra（§35） | — | ✅ `da5883f` |
| T5.6 | SOAP full chain 测试（§63：本地 WSDL server → 生成工具 → SOAP 调用） | T5.1–5.5 | ✅ `tests/integration/test_soap_full_chain.py`（见下「T5.6 实施结果」） |

## 验收标准（§63/§82 DoD）

- WSDL import、service discovery、operation generation
- SOAP 1.1 / 1.2、SOAPAction、SOAP Fault
- `test_soap_full_chain.py`（§60/§63，待 E2E 环境）

## 测试要求

- `tests/unit/mcpgateway/protocols/codecs/test_soap_codec.py`
- `tests/unit/mcpgateway/protocols/contracts/test_wsdl_provider.py`
- `tests/unit/mcpgateway/protocols/http/test_soap_runtime.py`
- registry/response-decoder 接入回归

## 交付信息（§80）

1. Changed files：`codecs/__init__.py`、`http/response_decoder.py`、`http/adapter.py`、`http/request_builder.py`、`codecs/registry.py`
2. New files：`protocols/codecs/soap.py`、`protocols/contracts/wsdl.py`、`protocols/http/soap.py`
3. DB migration：无
4. Behavior change：`application/soap+xml` 解析为 SoapCodec；response.codec=soap 支持；SOAP 请求体按 `body.codec` 名解析并带 Envelope；SOAP Fault → ProtocolError
5. Backward compat：text/xml 仍归 XmlCodec（非 soap 配置时）；SOAP 仅显式 codec/soap+xml 命中；`RequestBuilder.build` 第三参为可选新增
6. Security：复用 XML 安全底线；Fault 不透传内部 stack
7. Tests added：若干单测
8. Tests executed：protocols 套件、operation_tool_compiler、http registry/schema/yaml、proto_scan
9. Known limitations：无（T5.6 已完成，见下「T5.6 实施结果」）
10. Next PR dependency：PR8

## T5.3 细化（§32–§34 SOAP runtime 接线）

**目标**：让 `Tool → HttpProtocolAdapter → SoapCodec → HTTPX` 这条链真正跑通（§32：zeep 只解析 WSDL，不参与调用）。

**现状调研（2026-09-10）**——WSDL provider 已产出正确的 `protocol_config`，但运行时 4 处断链：

| # | 断链 | 后果 |
|---|---|---|
| G1 | `RequestBuilder._encode_body` 只按 `mediaType` 解析 codec，忽略 `body.codec` | SOAP 1.1 的 `mediaType=text/xml` 被 XmlCodec 编码 → **没有 Envelope** |
| G2 | `_encode_body` 用裸 `CodecContext(preferred_content_type=…)`，丢掉 `protocol_config` | `SoapCodec.encode` 看不到 `request.soap` → 无 operation/namespace、version 恒为 1.1 |
| G3 | `EncodedBody.content_type` 未被 `_body_kwargs` 使用 | 请求缺 `Content-Type`（§33 要求 1.1 `text/xml` / 1.2 `application/soap+xml`） |
| G4 | `soap_request_headers` / `map_soap_fault` 已实现但无调用点 | 无 `SOAPAction`；Fault 被当成功数据返回（protocol_config 路径对非 2xx 不报错） |

**实施项**

1. `protocols/codecs/registry.py`：新增 `resolve_name(name) -> Optional[MessageCodec]`，把 `response_decoder._CODEC_NAME_MEDIA_TYPES` 提升为注册表的**单一策略点**（`name → canonical media type`），`response_decoder` 改为委托（行为不变）。
2. `protocols/http/request_builder.py`：`build(arguments, request_config, protocol_config=None)`（**可选第三参，向后兼容**）；
   - `_encode_body` 先按 `body.codec` 名解析（`resolve_name`），未命中再按 `mediaType`；
   - `CodecContext` 带上 `protocol_config`，使 SOAP 绑定生效。
3. `protocols/http/soap.py`：新增
   - `is_soap_config(protocol_config) -> bool`（`request.body.codec` 或 `response.codec` == `soap`）；
   - `soap_content_type(protocol_config, codec_content_type=None) -> Optional[str]`：1.2 追加 `; charset=utf-8; action="…"` 参数。
   - `soap_request_headers` 契约不变（1.2 仍返回 `{}`，action 走 Content-Type）。
4. `protocols/http/adapter.py`（`_invoke_protocol_config`）：
   - 构造 `RequestBuilder(...).build(arguments, request_config, config)`；
   - 请求头在 `built.headers` 之后合并 `soap_request_headers(config)`，并用 `soap_content_type(...)` 覆盖 `Content-Type`（SOAP 绑定优先于通用头）；
   - 响应解码包 `try/except SoapFaultError` → `map_soap_fault`（§34）；
   - SOAP 配置下非 2xx 且无 Fault → `ProtocolError(REST_HTTP_STATUS_ERROR, protocol_status=…)`，交由 ToolService 渲染为 is_error 结果（与 legacy 路径一致）。

**范围边界**：本任务只做 runtime 接线；WSDL operation → `OperationToolCompiler`（当前 compiler 要求 `HttpRequestContract`，WSDL 的 request 是 dict）与 `test_soap_full_chain`（§63）仍待 **T5.6**。

**验收**
- SOAP 1.1 请求体是带 `soap:Envelope` 的 XML，头含 `SOAPAction`、`Content-Type: text/xml`
- SOAP 1.2 请求头无 `SOAPAction`，`Content-Type: application/soap+xml; charset=utf-8; action="…"`
- `soap:Client` Fault → `INVALID_ARGUMENT`；`soap:Server` Fault → `UPSTREAM_ERROR`；均保留 code/string/detail
- 非 SOAP 请求回归不变（既有 `test_request_builder.py` / `test_response_decoder.py` 全绿）

**测试文件**
- `tests/unit/mcpgateway/protocols/http/test_soap_runtime.py`（扩展：envelope 编码、headers、content-type、fault 端到端映射）
- `tests/unit/mcpgateway/protocols/http/test_request_builder.py`（新增 `body.codec` 名解析 / SOAP 上下文用例）

## T5.6 已识别缺口（调研 T5.3 时发现，留待 T5.6）

T5.3 只打通了「已有 protocol_config 的 SOAP 工具 → 运行时」；**WSDL → 工具**这一上游仍是断的，T5.6 需补齐：

1. **WSDL operation 无法进入 `OperationToolCompiler`**：`compile()` 要求 `operation.request` 是 `HttpRequestContract`（带 `parameters`/`bodies`），而 `WsdlContractProvider` 产出的是普通 dict。需要为 SOAP operation 增加编译分支，或让 WSDL provider 产出 `HttpRequestContract`。
2. **字段命名不一致**：WSDL provider 用 snake（`path_template`、`base_url`、`response.preferred_media_types`），而运行时/decoder 读 camel（`pathTemplate`、`response.preferredMediaTypes`）。T5.3 已让 SOAP 绑定块（`request.soap` / `extensions.soap`）双读兼容，但 path/响应媒体类型两处尚未统一。
3. **`test_soap_full_chain`（§60/§63）**：本地 WSDL server → 注册 → 生成工具 → SOAP 调用，依赖 E2E 环境。


## T5.6 实施结果

三项已识别缺口全部关闭：

1. **WSDL operation 进入 `OperationToolCompiler`**：`WsdlContractProvider` 不再产出普通 dict，改为产出**类型化 HTTP 契约**（`HttpRequestContract` / `HttpResponseContract` / `HttpBodyVariant` / `HttpResponseVariant`），因此 SOAP operation 与 OpenAPI operation 走**同一个** `OperationToolCompiler`（§31），无需新增编译分支。
2. **命名与载体统一**：字段命名不一致随类型化契约消失；SOAP 绑定的载体按 `OperationDefinition` 自身规则（「runtime-required 信息必须放类型化字段，不得放 extensions」）新增类型化字段 **`soap_binding`**，由编译器写入 `protocol_config.request.soap`，运行时 `resolve_soap_config` 直接命中。
3. **响应 codec 可声明**：`HttpResponseContract` 新增 `codec`，编译器在声明时用它替代 `"auto"`。这一点是 SOAP 1.1 的**必需项**——1.1 响应是 `text/xml`，若不钉住 `soap`，响应会被 XmlCodec 解码、Fault 被当作成功数据返回。

**新增 full-chain 集成测试**：`tests/integration/test_soap_full_chain.py` —— 真实本地 HTTP SOAP 端点（非 mock），覆盖 WSDL → provider → compiler → `LegacyRestContractBuilder`（复刻 ToolService 的真实交接）→ adapter → SoapCodec → 真实 HTTP：
- 成功调用：上游实际收到 `SOAPAction` 头、`text/xml` Content-Type、含 `soap:Envelope`/`QueryRequest`/参数的请求体；响应解码为 `{"QueryResponse": {"status": "OK"}}`
- Fault：`soap:Client` → `INVALID_ARGUMENT`，保留 code/faultstring/safe detail，不透传 stack

**验证**：单元 protocols 套件 + 编译器 + http registry/schema/yaml 全绿；集成 `--with-integration` 20 passed（SOAP 3 + HTTP full chain 17）。

**T5.6 测试过程中发现的真实问题**：初版测试直接用类型化契约构造运行时 operation，暴露出运行时实际经 `LegacyRestContractBuilder` 重建 **dict 形状**的 operation（类型化契约只是编译器的输入）。已按真实交接路径重写测试，避免了一个「测的不是生产路径」的假验证。
