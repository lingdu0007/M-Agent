# Migrating From 0.1

`agent_framework` is removed in 0.3.0. The table below is a directional
historical migration guide; imports from the old namespace fail closed.
mappings preserve old synchronous behavior and emit `DeprecationWarning`; the
shim is removed in 0.3.0.

| 0.1 import/concept | 0.2 mapping | Notes |
| --- | --- | --- |
| `agent_framework.Agent` | `m_agent.SyncRunner` plus `AgentDefinition` | The shim preserves only the synchronous tool loop; new code should use the durable Run lifecycle. |
| `agent_framework.AgentResult` | `m_agent.RunRecord` and `RunInspection` | Read terminal state and execution detail through the public runtime queries. |
| `agent_framework.EchoModel` / `RuleBasedDemoModel` | `m_agent.adapters.DeterministicModelAdapter` | Deterministic fake behavior only. |
| `agent_framework.ModelClient` | `m_agent.runtime.ModelAdapter` | Implement the async adapter contract at the application boundary. |
| `agent_framework.OpenAICompatibleClient` | `m_agent.adapters.provider.ChatCompletionsModelAdapter` | Install `m-agent[provider]`; credentials remain application-owned. |
| `agent_framework.OpenAIResponsesClient` | `m_agent.adapters.provider.ResponsesModelAdapter` | Install `m-agent[provider]`; credentials remain application-owned. |
| `agent_framework.Message` / `ModelResponse` / `ToolCall` | `m_agent.runtime.ModelRequest` / `ModelResponse` / `ToolCall` | Adapt the data at the application seam. |
| `agent_framework.Tool` / `ToolRegistry` / `tool` | `m_agent.runtime.Tool` / `AgentDefinition.tools` | Declare an effect and return `ToolOutcome`; make registration explicit. |
| `agent_framework.JsonlTracer` | `m_agent.adapters.JsonlTelemetrySink` | Telemetry is non-authoritative and payload-redacted. |
| `agent_framework` memory/session helpers | No core mapping | Supply context explicitly; Session is outside the 0.2 Durable Run core. |
| `agent_framework` RAG helpers | `ContextProvider` or ordinary `Tool` | Keep retrieval implementation in the application. |
| `agent_framework` Workflow/MultiAgent | Compose multiple Runs in the application | No hosted orchestrator is added to 0.2. |
| `agent_framework` Eval helpers | External evaluation code | Evaluation remains a runtime companion. |

Imports without an accurate mapping raise `LegacyMigrationError` with a
recommended direction, including direct imports such as
`agent_framework.workflow`. Read the [Durable Run guide](durable-run.md) before
moving a production integration.
