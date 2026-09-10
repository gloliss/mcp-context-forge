# PR4 — XML/XSD 测试规划

**关联规划**: [pr4-xml-xsd.md](pr4-xml-xsd.md)
**依据**: 设计文档 §23–§29 / §62 / §82 DoD

## 测试范围

| 面 | 覆盖点 |
|---|---|
| XmlCodec | application/xml、text/xml、+xml 后缀解析；XSD 感知 encode/decode + schema-less 兜底；规范映射 `@`/`#text`/preserve_root；repeated element |
| XsdTypeSystem | XSD 1.0/1.1 加载、validate/decode/encode/find_element、roundtrip |
| XsdJsonSchemaMapper | 内置类型、complexType/sequence/choice/attribute、facet（enumeration/pattern/min/max）、namespace |
| XML 安全（§28） | DTD/ENTITY 拒绝、oversized reject、max_depth、forbid_entities 不可关 |
| 集成 | codec registry 接入（application/xml 不再落 Binary）、response decoder 的 response.codec=xml |

## 测试文件

- `tests/unit/mcpgateway/protocols/codecs/test_xml_codec.py`
- `tests/unit/mcpgateway/protocols/contracts/test_xsd_types.py`
- `tests/unit/mcpgateway/protocols/contracts/test_xsd_json_schema.py`
- `tests/unit/mcpgateway/protocols/codecs/test_codec_registry.py`（更新 application/xml 断言）

## 用例清单

1. 编解码：XSD roundtrip、schema-less roundtrip、namespace/attribute/list/required/enumeration
2. 安全：XXE reject、oversized reject、深度限制、forbid_entities 不可关闭
3. mapper：int/date/decimal 映射、complexType object、facet、choice oneOf、missing element 抛错
4. 回归：`tests/integration/test_http_full_chain.py`（待 E2E 环境）

## 需要 E2E 环境的项

- `test_xml_http_full_chain.py`（§60/§62）：真实 HTTP upstream 返回 XML、XSD 校验、XXE reject

## 通过标准

单元/协议测试全绿；XML 安全用例全部拒绝预期 payload；`application/xml`/`text/xml`/`+xml` 在 registry 解析为 XmlCodec。
