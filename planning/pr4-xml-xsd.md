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
| T4.4 | Manual XML HTTP：manifest 声明 manual XML 操作 + XSD 绑定（§27） | — | ✅ 见下「T4.4 实施结果」 |
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
9. Known limitations：无（T4.4 两段均已完成，见「T4.4 实施结果」）
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
- 集成 `tests/integration/test_xml_http_full_chain.py`（8 例，真实本地 HTTP 端点）：conforming 往返、**请求体按 XSD 校验（不合规不发车）**、**响应按 XSD 校验（不合规不静默解码）**、conforming 响应带类型解码（`total` → int 7）、无绑定工具仍走 loose 路径（`total` → str "7"，对照说明绑定的价值）；`TestOpenApiXmlBinding` 另 3 例覆盖「OpenAPI 文档声明的 XML 操作同样携带 XSD 并端到端生效」

**范围边界（本段）**：本次交付「XSD 绑定 → 运行时生效」这一实质链路。YAML manifest 中**显式声明** manual XML 操作（`spec.operations` 扩展 + 扫描建工具）在本段结束时尚未实现——现有 manifest 仅支持 `operations.include` 过滤 OpenAPI 发现的操作，属于独立的一段工作。**该缺口已在下一段（方案 B）关闭**，详见下文。

## T4.4 实施结果（第二段：manifest 声明 manual XML 操作，方案 B）

**选定方案 B**：manual 声明合成一份 OpenAPI 文档，喂给现有 `import_schema`，从而完整复用 artifact 哈希 / 漂移 / candidate-then-activate / 工具同步——**不新增第二条工具创建路径**。

**manifest 语法**
```yaml
spec:
  baseUrl: http://report.internal
  # discovery 整段可省略——仅当声明了 manual 操作时
  operations:
    manual:
      - name: queryReport
        method: POST
        path: /query
        body:
          mediaType: application/xml
          xsd: {schema: "<xs:schema …>"}      # 或 xsd: {file: query.xsd}
        response:
          mediaType: application/xml
          xsd: {file: query_response.xsd}
```
- `discovery` 仅在**没有** manual 操作时必填；有 manual 即可省略（既有 manifest 不受影响）
- `xsd` 必须**恰好**声明 `schema`（内联）或 `file`（相对 manifest、须留在 scan root 内）之一；`file` 走与 OpenAPI 源相同的逃逸守卫
- 严格校验：name 非空且唯一、method 属白名单、path 以 `/` 开头、mediaType 非空、每个条目至少声明 body/response 之一

**合成与落地**
- `_synthesize_manual_openapi()` 把声明转成 OpenAPI 3.0 文档，XSD 走 `x-contextforge-xsd` 扩展
- 扫描时若存在 manual 声明，payload 取合成文档（文件名 `manual-operations.json`），`source_url` 记为 None

**过程中修掉的三个缺陷**
1. **验证逻辑自身**：`has_schema == bool(has_file)` 拿真值字符串与 bool 比较（`'<x/>' == True` 为 False），导致「同时给 schema 和 file」漏过校验 → 改为两端显式 `bool()`
2. **catalog 往返丢字段**：`http_schema_service._operation_to_dict` 是**字段白名单**，新加的 `xsd` 未列入，工具同步从 catalog 重建时静默丢失。已在写入与重建两侧补齐（模型的类型化字段必须同步进 catalog 白名单，否则只是「看起来通了」）
3. 前一段的 OpenAPI XML codec/响应 schema 两处断链（见 `de60e18`）

**验证**：单元（yaml 服务 29 例含新增 20 例、schema 服务、protocols、contracts）全绿；集成 **72 passed**，其中 `TestManualXmlManifestFullChain` 三例覆盖 manifest → 扫描 → 工具 → **真实 HTTP 上游**：protocol_config 同时带 xml codec 与两侧 XSD、调用往返且响应类型被 XSD 强制（`total` → int 7）、不合规请求体在网关侧即被拒（上游零请求）。`interrogate` 保持 100%。
