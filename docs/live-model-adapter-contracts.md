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
| `ChatCompletionsModelAdapter` | `{base_url}/chat/completions` | ✅ | ✅ | ✅（`response_format` json_schema / json_object） | ✅（`prompt_tokens`/`completion_tokens`） |
| `ResponsesModelAdapter` | `{base_url}{responses_path}`（默认 `/responses`） | ✅ | ✅ | ✅（`text.format` json_schema） | ✅（`input_tokens`/`output_tokens`） |

普通 text、streaming、tool calling 请求不携带 structured-output 参数。只有
构造 Adapter 时显式提供 JSON Schema 才请求原生 structured output。Chat
Completions 默认使用严格的 `json_schema`；仅支持原生 JSON object 的兼容
端点必须显式配置
`M_AGENT_OPENAI_CHAT_STRUCTURED_OUTPUT_MODE=json_object`（或构造器的同名
`structured_output_mode`），这不是静默降级。

Adapter 的 model、净化后的 endpoint、timeout、structured-output schema 与
Chat mode 只会形成不可逆的 configuration fingerprint，随
`DefinitionSnapshot` 冻结。恢复时重新注册同一 definition/version 若
fingerprint 不一致，会在任何网络或工具调用前失败，不能静默改变既有 Run
的模型语义；fingerprint 不保存凭证、URL 或 schema 正文。所有 live
adapter 都必须声明非空、稳定的 fingerprint，注册时会被校验。

能力声明（`capabilities`）是**如实声明**（ADR 0030）：声明为支持的能力
才有契约案例；未声明的能力（本版本两者均无）绝不做静默降级。Runner
只在声明支持时调用对应路径（如 `streaming=True` 才走 `stream()`），
定义注册在**发出任何网络请求前**校验 required capabilities
（`DefinitionRegistry.register` 抛 `ModelCapabilityError`）。

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
- 未配置凭证时 Adapter 可以构造与注册；任何网络请求都会以
  `ModelFailure(PERMANENT, provider_credentials_missing)` 失败。

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
  （缺失时显式为 `None`，绝不伪造）；
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
