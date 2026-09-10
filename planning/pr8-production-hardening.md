# PR8 — Production Hardening（§56–§64）

**目标**：不增加新的协议类型，只做生产化：监控、契约测试、Activation Gate、full-chain 集成、外部引用安全、Auth/Secret 审计、Retry/Limits。
**依赖**：PR4–PR7 全部
**后继**：DoD（§82）收尾

## 范围（来自设计文档）

| 设计节 | 内容 |
|---|---|
| §57 | HttpMonitoringService + `http_health_samples` 表 + 迁移；不建 http_metrics_hourly，复用 ToolMetric |
| §58 | HTTP Contract Testing：schemathesis，`tests/contracts/http/` |
| §59 | Activation Gate：off/warn/strict；默认只测 GET/HEAD/OPTIONS，mutating 需显式 allow |
| §60–§64 | 4 个 full-chain 集成测试（http/xml/soap/grpc_streaming），真实本地 upstream |
| §66 | SafeReferenceFetcher + ContractArtifactResolver：外部 $ref/WSDL import/XSD include 走 SSRF 策略 |
| §67 | Auth/Secret：protocol_config/runtime_config/YAML 拒绝明文 secret |
| §68 | HTTP Retry：GET/HEAD/OPTIONS 默认自动；PUT/DELETE 结合幂等；POST/PATCH 不盲试 |
| §69 | HTTP Limits：connect/read/write timeout、max request/response bytes、max redirect hops |
| §73 | Error Mapping 表（HTTP/XML/SOAP/gRPC → canonical categories） |

## 任务清单

| 任务 | 内容 | 依赖 | 状态 |
|---|---|---|---|
| T8.1 | HttpMonitoringService + `http_health_samples` 表 + 迁移 `6d7e8f9a0b1c`（§57） | — | ✅ `208d896` |
| T8.2 | schemathesis contract test 基建（§58，`tests/contracts/http/`） | — | ✅ `c88d33a`（见下「T8.2 细化」） |
| T8.3 | Activation Gate off/warn/strict（§59） | T8.2 | ✅ `e3508e2`（Gate 配置+决策；已被 T8.2 契约套件消费） |
| T8.4a | `test_http_full_chain` 最小集扩展（§61） | — | ⏳（依赖 E2E 环境） |
| T8.4b | `test_xml_http_full_chain`（§62） | T4.x | ✅ `tests/integration/test_xml_http_full_chain.py`（5 例） |
| T8.4c | `test_soap_full_chain`（§63） | T5.x | ✅ `tests/integration/test_soap_full_chain.py`（3 例） |
| T8.4d | `test_grpc_streaming_full_chain`（§64） | T7.5 | ✅ 四类 RPC + 取消：`tests/integration/test_grpc_full_chain.py`（43 例） |
| T8.5a | SafeReferenceFetcher（§66） | — | ✅ 已在 **PR3** 实现：`services/safe_reference_fetcher.py`（含 `SafeReferenceFetcher` + `ContractArtifactResolver`） |
| T8.5b | ContractArtifactResolver 完整接线（外部引用物化） | T8.5a | ✅ 已在 **PR3** 接线：`contract_artifact_service.prepare_artifact` → `resolve_and_bundle` |
| T8.6 | Auth/Secret 审计：拒绝明文 secret（§67） | — | ✅ `(secret_policy)` |
| T8.7 | HTTP Retry / Limits / Error Mapping（§68/§69/§73） | — | ✅ `a399092` |

## 验收标准（§82 DoD）

- Health 样本落库、Metrics 复用 ToolMetric
- Contract test 检测 5xx / schema mismatch / invalid input / response contract mismatch
- Activation Gate 默认不 fuzz mutating operations
- 四个 full-chain 集成测试用真实 upstream 通过
- 外部引用获取受 SSRF 策略约束、大小受限
- 明文 secret 被 schema/API 拒绝
- Retry 仅对安全方法生效；HTTP/gRPC/SOAP/XML 错误映射到 canonical categories

## 测试要求

- `tests/unit/mcpgateway/db/test_http_health_samples_migration.py`
- `tests/unit/mcpgateway/utils/test_safe_reference_fetcher.py`
- `tests/unit/mcpgateway/utils/test_secret_policy.py`
- `tests/unit/mcpgateway/schemas/test_runtime_config_secret_policy.py`
- `tests/unit/mcpgateway/protocols/http/test_error_mapping.py`
- `tests/contracts/http/` + `tests/integration/test_*_full_chain.py`（待 E2E 环境）

## 交付信息（§80）

1. Changed files：`services/http_monitoring_service.py`、`schemas.py`、`services/proto_scan_service.py`、`protocols/http/adapter.py`
2. New files：`utils/secret_policy.py`
3. DB migration：`6d7e8f9a0b1c_add_http_health_samples`
4. Behavior change：400→INVALID_ARGUMENT（§73）；runtime_config 拒绝明文 secret；健康样本落库
5. Backward compat：旧工具 runtime_config 为 NULL 走默认
6. Security：SSRF 策略 + 明文拒绝
7. Tests added：若干
8. Tests executed：utils/schemas/protocols/db migration
9. Known limitations：T8.2–8.4（schemathesis/full-chain）依赖 E2E 环境；T8.3 待续
10. Next PR dependency：无（收尾 DoD §82）

## 更正记录（2026-09-10）

- §66（SafeReferenceFetcher / ContractArtifactResolver / 外部 `$ref` 物化）**在 PR3 已完整实现**于 `mcpgateway/services/safe_reference_fetcher.py`，并由 `contract_artifact_service.prepare_artifact` 调用 `resolve_and_bundle`。补建 `utils/safe_reference_fetcher.py` 属重复实现，已删除（连同其测试）。T8.5a/T8.5b 标记为「PR3 已实现」，不重复做。

## T8.3 Activation Gate 细化（§59）

**目标**：第一版 Activation Gate 支持 `off / warn / strict`；默认只对安全方法自动跑契约测试，mutating 需显式开启。

**接口/配置**
- `http-service.yaml`：
  - `spec.validation.activationGate: off | warn | strict`（默认 `warn`）
  - `spec.testing.allowMutatingOperations: bool`（默认 `false`）
- 新增 `mcpgateway/protocols/http/activation_gate.py`：
  - `ACTIVATION_GATES = ("off", "warn", "strict")`
  - `SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}`
  - `evaluate_activation_gate(gate, method, allow_mutating) -> str`：返回 `"off" | "skip" | "run" | "warn"`（`strict` 下 mutating 被禁则 `skip`；`warn` 下标注告警；`off` 直接 `off`）

**范围边界**：本任务只做「Gate 配置解析 + 决策函数」；schemathesis 实际执行（T8.2）与其 full-chain（T8.4）待 E2E 环境。

**验收**
- YAML 中 `activationGate` 非 `off/warn/strict` 报 `HttpServiceError`
- `allowMutatingOperations` 非 bool 报错
- 决策：safe 方法在 `warn/strict` 下 `run`；mutating 在无 `allowMutatingOperations` 时 `skip`，在 `off` 时 `off`

**测试文件**
- `tests/unit/mcpgateway/protocols/http/test_activation_gate.py`
- `tests/unit/mcpgateway/services/test_http_yaml_service.py`（新增 activationGate/testing 解析用例）

## T8.2 细化（§58 HTTP Contract Testing 基建）

**目标**：建立 `tests/contracts/http/` 契约测试基建：从 OpenAPI 文档派生操作用例，按 Activation Gate（T8.3）挑选可跑操作，并对响应做契约判定（5xx / 未声明状态码 / 响应 schema 不匹配 / 非法输入未被拒）。**本地可单测的部分不依赖 live gateway**；真正对网关跑 schemathesis 的用例在无 `CONTRACT_BASE_URL` 时跳过（标 待 E2E）。

**实施项**

1. `tests/contracts/__init__.py`、`tests/contracts/http/__init__.py`
2. `tests/contracts/http/contract_checks.py`（纯逻辑，无 gateway 依赖）：
   - `OperationCase`（method / path / operation_id / 声明的 responses / parameters）
   - `collect_operations(spec) -> list[OperationCase]`：遍历 `paths`，跳过非 HTTP 方法与无 `responses` 的条目
   - `select_operations(cases, *, gate, allow_mutating) -> (selected, skipped)`：委托生产代码 `protocols.http.activation_gate.evaluate_activation_gate`，`skip`/`off` 的操作不跑（§59：默认只跑 GET/HEAD/OPTIONS）
   - `ContractViolation` + `classify_response(case, status_code, body, content_type) -> list[ContractViolation]`：
     - `server_error`：5xx
     - `undeclared_status`：状态码不在 `responses` 声明中（`default` 视为声明）
     - `response_schema_mismatch`：声明的 JSON Schema 与响应体不符（jsonschema 校验）
     - `invalid_input_not_rejected`：由 `check_invalid_input_rejected(status_code)` 单独判定（非法输入应 4xx）
3. `tests/contracts/http/test_contract_checks.py`：上述纯逻辑单测（本地全跑）
4. `tests/contracts/http/test_gateway_contract.py`：schemathesis 驱动的 live 套件；`CONTRACT_BASE_URL` 未设置则 `pytest.skip`（待 E2E）

**范围边界**：本任务只建基建与判定逻辑；**不**新增生产代码（Gate 逻辑已在 T8.3）。

**本地验证结论（2026-09-10）**
- 纯逻辑单测 38 个通过（`tests/contracts/http/test_contract_checks.py`），含 `load_openapi_document` 对本地 HTTP server 的真实加载。
- live 套件用**本地 stand-in 网关**实测通过（合规网关 3 passed / 1 skipped），并确认能**真实捕获**违约：对返回 `{"items":"NOT-AN-ARRAY"}` 的 200 报 `response_schema_mismatch`、对 500 报 `server_error`（非空跑）。
- 无 `CONTRACT_BASE_URL` 时全程 skip、不触网（collection 阶段也不加载文档）。

**待 E2E 的增量**：schema 驱动的 hypothesis 模糊测试用例生成（schemathesis 4.x 的 `get_case_strategy` 集成）留作 E2E 阶段扩展；当前以「有效请求 + 缺参请求」两类用例覆盖 §58 的四项判定。

**验收**
- `collect_operations` 正确枚举 OpenAPI paths（含 `default` 响应）
- 默认 gate 下 mutating 操作被排除；`allowMutatingOperations: true` 时纳入
- `classify_response` 对 5xx / 未声明状态码 / schema 不匹配分别产出对应 violation；`default` 声明不误报
- 无 `CONTRACT_BASE_URL` 时 live 套件整体 skip，不报错

**测试文件**
- `tests/contracts/http/test_contract_checks.py`（本地）
- `tests/contracts/http/test_gateway_contract.py`（待 E2E）
