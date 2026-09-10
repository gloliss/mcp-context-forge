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
| T4.4 | Manual XML HTTP：YAML manual 操作 + XSD artifact 绑定（§27） | T8.5b artifact resolver | ⏳ |
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
9. Known limitations：Manual XML HTTP（T4.4）依赖 PR8 resolver
10. Next PR dependency：PR5
