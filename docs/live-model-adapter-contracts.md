# Live Model Adapter Contracts（Chat Completions 与 Responses）

Ticket 10（Durable Run PRD User Stories 60–62）为 OpenAI 兼容的
Chat Completions 与 Responses 风格 API 提供清晰的 live Model Adapter
与凭证门控契约测试。本文档说明两个 Adapter 的**如实能力声明**、
与确定性 fake 的区分、如何运行 live 契约测试，以及凭证安全边界。

## 两类 Adapter 与能力声明

两个 Adapter 都位于 `src/m_agent/provider/`（可选 `provider` extra，
依赖 `httpx`，ADR 0038），都是 `m_agent.ModelAdapter` 的 live 实现
（`deterministic` 恒为 False）：

| Adapter | 端点 | streaming | tool calling | native structured output | usage reporting |
| --- | --- | --- | --- | --- | --- |
| `ChatCompletionsModelAdapter` | `{base_url}/chat/completions` | ✅ | ✅ | ✅（`JSON_SCHEMA_STRICT`；显式配置可为 `JSON_OBJECT`） | ✅（`prompt_tokens`/`completion_tokens`） |
| `ResponsesModelAdapter` | `{base_url}{responses_path}`（默认 `/responses`） | ✅ | ✅ | ✅（`JSON_SCHEMA_STRICT`，`text.format` json_schema） | ✅（`input_tokens`/`output_tokens`） |

普通 text、streaming、tool calling 请求不携带 structured-output 参数。只有
冻结 Binding 的 Requirements 显式选择 `structured_output=JSON_SCHEMA_STRICT`
或 `JSON_OBJECT`，才会发送原生 structured output；严格 schema 请求缺 Schema
会在 dispatch 中以 `MODEL_CONTRACT_VIOLATION` 失败。Chat
Completions 默认使用严格的 `json_schema`；仅支持原生 JSON object 的兼容端点必须显式配置
`M_AGENT_OPENAI_CHAT_STRUCTURED_OUTPUT_MODE=json_object`（或构造器的同名
`structured_output_mode`），它只满足 `JSON_OBJECT` Requirement，不能满足
`JSON_SCHEMA_STRICT`，这不是静默降级。

运行时集成者必须在构造 live Adapter 时显式传入实例级
`model_contract=ModelContract(...)`，其中声明真实的 model/deployment
identity、limits、Sizer、serialization、usage guarantee 和非敏感
`configuration_fingerprint`；Adapter 类的 capability 常量只是协议上限，
不能推导这些实例事实。`fingerprint` 是由 Contract identity/version、修订
稳定性、能力组合、Limits、Sizer、serialization 与 usage guarantee 规范化
计算的语义摘要，提供不相等的手写值会被拒绝。未传 Contract 的 Adapter
可以无凭证地构造以配置 HTTP，但 `DefinitionRegistry.register` 会在任何网络
或工具调用前拒绝它。Contract 能力必须是 Adapter 类协议上限的真实交集，可以
比类的能力更窄；Adapter 的 model、净化后的 endpoint、timeout、structured-
output schema 与 Chat mode 形成的非敏感 configuration fingerprint 必须等于
实例 Contract 的 `configuration_fingerprint`。不匹配或之后变化都会在 dispatch
前失败，不能静默改变既有 Run 的模型语义。

能力声明（`capabilities`）是**如实声明**（ADR 0030）：声明为支持的能力
才有契约案例；未声明的能力（本版本两者均无）绝不做静默降级。Runner
只在声明支持时调用对应路径（如 `streaming=DELTA` 才走 `stream()`），
定义注册在**发出任何网络请求前**校验 required capabilities
（`DefinitionRegistry.register` 抛 `ModelCapabilityError`）。同时启用两个
或以上 mode 的 Contract 必须在 `supported_combinations` 中逐项列出允许
并发的组合；仅分别声明 mode 不表示它们可以一起 dispatch。注册和
dispatch 都把未声明组合拒绝为零 provider request。

Definition Snapshot 总是持久化完整的 `PRIMARY`、`CONTEXT_COMPRESSION` 与
`OUTPUT_REPAIR` Model Binding Set。非 PRIMARY purpose 只有以
`source_purpose=PRIMARY` 的显式引用才能复用 primary 的 Contract 和
Requirements；缺少 purpose 不是隐式 reuse。Definition 顶层的 minimum
requirements 会与 PRIMARY binding 的 requirements 取更严格的并集，显式
binding 因此不能绕过 Agent 声明的能力或 Limits。

直接构造 `AgentDefinition` 必须传入完整 `model_bindings`。只想复用同一
Adapter 时，调用方必须在代码中明确选择
`AgentDefinition.for_adapter(...)` 或 `ModelBindingSet.reuse_primary(...)`；
两者都会冻结完整、可检查的 Binding Set。`streaming` 与 native structured
output 都由该冻结 Requirements 决定，Contract 支持某一模式本身不会让
Runner 隐式选择它。

## 与确定性 fake 的区分（禁止混淆）

- `m_agent.DeterministicModelAdapter` / `DeterministicStreamingModelAdapter`
  是确定性 fake：`deterministic=True`，响应由构造参数决定，不访问任何
  网络，只用于测试、演示与离线示例。
- `m_agent.provider.ChatCompletionsModelAdapter` /
  `ResponsesModelAdapter` 是 live 实现：`deterministic=False`，只有
  在调用方显式提供凭证后才会发出真实网络请求。
- 契约测试通过 `m_agent` 公开 `Runner` seam（`create_run` →
  `start_run` → `inspect_run`）驱动真实 provider 行为，绝不把
  deterministic fake 的成功误报为供应商兼容性。

## 凭证边界（ADR 0033）

- live contract 的凭证（API Key）只由**调用方的运行环境**提供
  （`M_AGENT_OPENAI_API_KEY` 或 `OPENAI_API_KEY`）；测试不会读取、回显
  或写入其值。Adapter 构造器不接受 `api_key` 参数。`base_url` / `model`
  亦支持 `OPENAI_*` / `AGENT_*` 变体；base URL 和 endpoint path 的
  userinfo、query、fragment 会在请求记录或错误构造前剥离。
- 凭证**绝不进入** Definition Snapshot、Run Payload、Checkpoint、
  Run Update、Telemetry、fixture、快照或错误文本。
- Adapter 的错误是结构化 `m_agent.ModelFailure`（分类 +
  稳定错误码），消息只含 HTTP 状态 / 传输层类型，**不包含 provider
  错误 body**，从机制上杜绝凭证回显与内容审计。
- 未配置凭证时 Adapter 可以构造；提供显式实例 Contract 后可注册。任何
  网络请求都会以 `ModelFailure(PERMANENT, provider_credentials_missing)`
  失败。

## 安装与运行

安装（含测试依赖）：

```bash
uv pip install -e ".[dev,provider]"   # 或 pip install -e ".[dev,provider]"
```

### 默认离线 CI（不触网）

默认 `pytest`、`pytest -q`、`unittest` discovery 和直接执行测试模块都
不会发出 provider 请求，即使环境意外已有 provider credential。pytest
通过 `conftest.py` 排除 `@pytest.mark.live`；live TestCase 自身的
`setUp` 还要求 `M_AGENT_RUN_LIVE_TESTS=1`，因此不依赖 pytest 才能保持
安全。默认 CI 只运行离线测试。

### 显式运行 live 契约测试

```bash
# 凭证须已由调用方环境提供；此命令不读取或打印凭证值。
M_AGENT_RUN_LIVE_TESTS=1 pytest -m live -v -rP tests/test_live_model_adapters.py
```

仅 `pytest -m live` 仍不足以发出请求：未设置 `M_AGENT_RUN_LIVE_TESTS=1`
时测试以 `OPTED_OUT` 跳过；已设置开关但没有 provider credential 时以
`MISSING_CREDENTIALS` 跳过。两种情况均不会请求 provider。

## 报告语义

| 结果 | 含义 |
| --- | --- |
| `OPTED_OUT` | 没有 `M_AGENT_RUN_LIVE_TESTS=1`；跳过且未请求 provider |
| `MISSING_CREDENTIALS` | 已 opt-in 但缺 provider credential；跳过且未请求 provider |
| `PROVIDER_FAILURE` | 请求已发出但 provider/transport 返回结构化失败 |
| `ASSERTION_FAILURE` | provider 调用成功，但公开 Runner contract 的断言不成立 |
| `VERIFIED` | 真实 provider 的所有已声明能力 contract 都通过 |

`MockTransport`、localhost fixture、health check、capability declaration 与
deterministic fake 都只是离线证据，绝不构成 `VERIFIED` provider
compatibility。pytest 中未带上述前缀的普通 `AssertionError` 属于
`ASSERTION_FAILURE`。

每个已通过的 live capability case 会输出不含 prompt、response 或凭证的
`VERIFIED` 记录（adapter、model、capability）。只有两个 adapter 的所有
已声明 capability case 都为 `VERIFIED`，才可在 Ticket 中记录真实 provider
compatibility 已验证。

## 契约案例覆盖

每个 Adapter 声明支持的能力都有对应契约案例（`tests/test_live_model_adapters.py`）：

- **text completion**：普通生成，断言 `SUCCEEDED` 且最终响应非空；
- **streaming**：订阅 `MODEL_DELTA` Run Update，断言只有完整响应成为
  checkpoint（ADR 0011）且流文本与 checkpoint 一致；
- **tool calling**：定义 `READ_ONLY` 确定性工具，指令要求模型调用，
  断言产生 TOOL Step 且最终响应非空；
- **native structured output**：配置 JSON Schema，断言输出是符合
  Schema 的 JSON；
- **usage reporting**：provider 返回 usage 时透传为 `ModelUsage`
  （Adapter 缺失时显式为 `None`；Runner 将每个缺失 optional 字段持久化为
  `UNAVAILABLE`，绝不伪造）。provider 映射还保留 `raw_unit` 与版本化
  `normalization_source`（包括实际使用的 provider-field alias），使历史
  用量可说明其标准化来源；Provider-reported 值缺少任一项会是
  `MODEL_CONTRACT_VIOLATION`；
- **actual revision**：每次成功响应中 provider 返回的 `model`/revision
  会作为 `ModelResponse.actual_revision` 持久化为可检查的 Run 事实，尤其
  用于标记为 `PROVIDER_ALIAS` 的 Contract；
- **凭证隔离**：离线 `MockTransport` 的成功与 provider-failure case 使用
  sentinel 检查 SQLite 原始 bytes、Snapshot、Attempt、Checkpoint、Run
  Update 与 Telemetry 均不含凭证；真实 live 测试只检查环境是否已配置，
  不读取或回显 credential value。

## 已知表示限制

M-Agent 的 `ToolOutcome`（Model Step checkpoint 的结果）不携带工具
调用参数（`arguments`）。因此多轮工具循环中重建 provider 的
assistant `tool_calls` / `function_call` 消息时使用空参数，以
`call_id` 关联工具结果。这是统一模型契约的已知表示，不影响工具
结果作为数据回流模型的语义（ADR 0017）。

## 不引入的内容（Out of Scope）

本实现**不引入**模型路由平台、fallback chain、默认网络调用、
SFT/RL、推理服务或 Provider 特有行为到核心领域模型：`provider` 子包
是可选扩展，核心 `m_agent` 包不依赖 `httpx`，也不导出 live Adapter。
