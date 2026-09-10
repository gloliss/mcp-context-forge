# PR4 — XML / XSD 支持（§23–§29）

**目标**：HTTP 消息支持 XML 编解码、XSD 1.0/1.1 校验、XSD→JSON Schema 映射。
**依赖**：PR1–PR3（Phase 1 HTTP MVP）
**后继**：PR5（SOAP/WSDL，依赖本 PR 的 XML codec）

## 范围（来自设计文档）

| 设计节 | 内容 |
|---|---|
| §23 | 新增 `protocols/codecs/xml.py`、`protocols/contracts/xsd_types.py`、`protocols/contracts/xsd_json_schema.py` |
| §24 | XmlCodec：namespace/attribute/text/root/repeated element/encoding；规范映射 `@`/`#text`/preserve_root；用 xmlschema converter |
| §25 | XsdTypeSystem：load/validate/decode/encode/find_element；XMLSchema + XMLSchema11 |
| §26 | XsdJsonSchemaMapper：内置类型/complexType/facet/choice/attribute/namespace；XSD 不产生 Operation |
| §27 | Manual XML HTTP：YAML `discovery.mode=manual` + XSD artifact/element |
| §28 | XML 安全底线：DTD/ENTITY 禁用、maxBytes/maxDepth/maxNodes、forbidEntities 不可关 |
| §29 | 依赖：`xmlschema` 进 `xml` extra，不与 SOAP 混装 |

## 任务清单

| 任务 | 内容 | 依赖 | 状态 |
|---|---|---|---|
| T4.1 | XmlCodec（application/xml、text/xml、+xml 后缀；XSD 感知 + schema-less 兜底） | — | ✅ `88d6d0d` |
| T4.2 | XsdTypeSystem（XSD 1.0/1.1） | — | ✅ `88d6d0d` |
| T4.3 | XsdJsonSchemaMapper | T4.2 | ✅ `88d6d0d` |
| T4.4 | Manual XML HTTP：XSD 绑定到 XML 操作并在运行时生效（§27） | — | ✅ 见下「T4.4 实施结果」 |
| T4.5 | XML 安全底线（§28） | T4.1 | ✅ `88d6d0d` |
| T4.6 | 依赖 `xmlschema` → xml extra（§29） | — | ✅ `88d6d0d` |
| T4.7 | 测试（codec/XSD/mapper/安全/registry 接入） | T4.1–4.3/4.5 | ✅ `88d6d0d` |

## 验收标准（§62/§82 DoD）

- encode/decode、namespace、attribute、list、required、enumeration
- XXE reject、oversized reject
- `test_xml_http_full_chain.py`（§60/§62，待 E2E 环境）

## 测试要求

- `tests/unit/mcpgateway/protocols/codecs/test_xml_codec.py`
- `tests/unit/mcpgateway/protocols/contracts/test_xsd_types.py`
- `tests/unit/mcpgateway/protocols/contracts/test_xsd_json_schema.py`
- registry/response-decoder 接入回归

## 交付信息（§80）

1. Changed files：`codecs/__init__.py`、`codecs/base.py`（CodecContext 增 xsd_type_system）、`http/response_decoder.py`
2. New files：`protocols/codecs/xml.py`、`protocols/contracts/xsd_types.py`、`protocols/contracts/xsd_json_schema.py`
3. DB migration：无
4. Behavior change：application/xml/text/xml 从 Binary/Text 兜底改为 XmlCodec 解码
5. Backward compat：schema-less XML 走 loose 路径；旧 REST 不变
6. Security：DTD/ENTITY 拒绝、大小/深度/节点限制
7. Tests added：38 个
8. Tests executed：protocols 套件
9. Known limitations：见「T4.4 实施结果」——运行时 XSD 绑定已完成；YAML manifest 声明 manual 操作留待后续
10. Next PR dependency：PR5

## T4.4 实施结果（§27 XML/XSD 运行时绑定）

**调研结论**：`XsdTypeSystem`（T4.2）与 XSD 感知的 `XmlCodec`（T4.1）都已实现，`CodecContext.xsd_type_system` 也已就位，但**运行时从不填充它** —— 于是每个 XML 工具都走 schema-less 的 loose 路径，XSD 纯属装饰。这是 §27 缺失的那一环。

**实施**
1. 新增 `mcpgateway/protocols/http/xsd_binding.py`：
   - `xsd_binding(protocol_config, side=...)` —— 读请求侧（`request.body.xsd`）或响应侧（`response.xsd`）的绑定；两侧**独立**（请求文档与响应文档是不同的元素）；
   - `build_xsd_type_system(...)` —— 构建/复用（LRU 缓存）`XsdTypeSystem`；未声明时返回 `None`，非 XML 与 schemaless XML 工具完全不受影响；
   - schema 以**内联文本**随工具配置存储，不引入新的抓取路径与 SSRF 面。
2. 接线：`RequestBuilder._encode_body`（出站编码）与 `HttpProtocolAdapter` 的响应 `CodecContext`（入站解码）分别按 `side` 注入。
3. 顺带修复一个通用缺陷：`_invoke_protocol_config` 此前**丢弃 `EncodedBody.content_type`**（仅 SOAP 分支单独处理），导致 XML/form/multipart 请求体不带声明的 Content-Type。现改为 codec 声明优先、调用方显式头不被覆盖。

**测试**
- 单元 `tests/unit/mcpgateway/protocols/http/test_xsd_binding.py`（16 例）
- 集成 `tests/integration/test_xml_http_full_chain.py`（5 例，真实本地 HTTP 端点）：conforming 往返、**请求体按 XSD 校验（不合规不发车）**、**响应按 XSD 校验（不合规不静默解码）**、conforming 响应带类型解码（`total` → int 7）、无绑定工具仍走 loose 路径（`total` → str "7"，对照说明绑定的价值）

**范围边界**：本次交付「XSD 绑定 → 运行时生效」这一实质链路。YAML manifest 中**显式声明** manual XML 操作（`spec.operations` 扩展 + 扫描建工具）尚未实现——现有 manifest 仅支持 `operations.include` 过滤 OpenAPI 发现的操作，属于独立的一段工作。
