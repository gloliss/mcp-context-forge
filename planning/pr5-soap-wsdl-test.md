# PR5 — SOAP/WSDL 测试规划

**关联规划**: [pr5-soap-wsdl.md](pr5-soap-wsdl.md)
**依据**: 设计文档 §30–§35 / §63 / §82 DoD

## 测试范围

| 面 | 覆盖点 |
|---|---|
| SoapCodec | SOAP 1.1（text/xml + SOAPAction）/ 1.2（application/soap+xml）、Envelope/Body/Fault、namespace |
| WsdlContractProvider | WSDL 解析 → OperationCatalog、key=service:port:binding:operation、错误（非 WSDL/坏 payload） |
| SOAP runtime | soap_request_headers（1.1 SOAPAction / 1.2 无）、map_soap_fault（Client→INVALID_ARGUMENT、Server→UPSTREAM_ERROR） |
| 集成 | `application/soap+xml` 在 registry 解析为 SoapCodec、response.codec=soap |

## 测试文件

- `tests/unit/mcpgateway/protocols/codecs/test_soap_codec.py`
- `tests/unit/mcpgateway/protocols/contracts/test_wsdl_provider.py`
- `tests/unit/mcpgateway/protocols/http/test_soap_runtime.py`
- `tests/unit/mcpgateway/protocols/codecs/test_codec_registry.py`（application/soap+xml → SoapCodec）

## 用例清单

1. 编解码：1.1/1.2 信封、Body 提取、Fault 抛出（code/string/detail）、DTD reject
2. WSDL：单操作发现、source_hash、非 WSDL 拒绝、坏 payload 报 ContractProviderError
3. runtime：SOAPAction 头、Fault 映射到 canonical categories
4. 回归：registry 接入

## 需要 E2E 环境的项

- `test_soap_full_chain.py`（§60/§63）：本地 WSDL server → 服务发现 → 工具生成 → SOAP 1.1/1.2 调用 → Fault

## 通过标准

单元/协议测试全绿；SOAP 1.1/1.2 编解码与 Fault 用例通过；WSDL 编译错误被规范化。
