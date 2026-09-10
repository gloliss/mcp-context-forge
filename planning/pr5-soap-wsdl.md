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
| T5.3 | SOAP runtime 完整接线：HttpAdapter 集成 SoapCodec + soap headers + Fault 映射 | T5.2 | ⏳（映射已完，adapter 接线待） |
| T5.4 | SOAP Fault → Error Model（§34） | T5.2 | ✅ `da5883f` |
| T5.5 | 依赖 `zeep` → soap extra（§35） | — | ✅ `da5883f` |
| T5.6 | SOAP full chain 测试（§63：本地 WSDL server → 生成工具 → SOAP 调用） | T5.1–5.5 + E2E 环境 | ⏳ |

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

1. Changed files：`codecs/__init__.py`、`http/response_decoder.py`
2. New files：`protocols/codecs/soap.py`、`protocols/contracts/wsdl.py`、`protocols/http/soap.py`
3. DB migration：无
4. Behavior change：`application/soap+xml` 解析为 SoapCodec；response.codec=soap 支持
5. Backward compat：text/xml 仍归 XmlCodec；SOAP 仅显式 codec/soap+xml 命中
6. Security：复用 XML 安全底线；Fault 不透传内部 stack
7. Tests added：若干单测
8. Tests executed：protocols 套件
9. Known limitations：HttpAdapter 完整接线（T5.3）与 SOAP full-chain（T5.6）待续
10. Next PR dependency：PR8
