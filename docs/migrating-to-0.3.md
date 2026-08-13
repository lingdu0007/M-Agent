# Migrating To 0.3

## Expand Window

The Runtime Foundation uses an **expand** window while the distribution is
still `0.2.x`: every existing `m_agent` root import remains supported and has
the same behavior. New integrations should use the four semantic namespaces
below. This is deliberately additive; it does not change Runner semantics or
silently replace an Adapter.

## 0.3 Import Table

At `0.3.0`, the root package becomes a small high-frequency facade:
`AgentDefinition`, `DefinitionRegistry`, `Runner`, `SyncRunner`, `RunStatus`,
`RunRecord`, and `RunInspection`. The following table lists every 0.2 root
export scheduled to move or be removed, together with its 0.3 replacement.

| 0.2 import | 0.3 entry | Status |
| --- | --- | --- |
| `m_agent.AgentDefinition`, `DefinitionRegistry`, `DefinitionSnapshot`, `RetryPolicy`, `Runner`, `SyncRunner`, `RunRecord`, `RunInspection`, `RunStatus`, `RunStore`, `RunLease` | `m_agent.runtime` | Root facade retains only the high-frequency subset above. |
| `m_agent.ModelAdapter`, `ModelCapabilities`, `ModelRequest`, `ModelResponse`, `ModelDelta`, `ModelUsage`, `ContextProvider`, `ContextRequest`, `ContextItem`, `PayloadCodec`, `TelemetrySink`, `TelemetryEvent`, `TelemetryEventType`, `Tool`, `ToolCall`, `ToolRequest`, `ToolSpec`, `ToolDeclaration`, `ToolEffect`, `ToolOutcome`, `ToolOutcomeStatus` | `m_agent.runtime` | Core contracts and ports move to the Runtime namespace. |
| `m_agent.StepRecord`, `StepAttempt`, `StepCheckpoint`, `StepStatus`, `StepType`, `FailureClassification`, `RunUpdate`, `RunUpdateType`, `ResolutionAction`, `RunResolution`, `allowed_resolutions`, `ALLOWED_FOR_DEFINITION_UNAVAILABLE`, `ALLOWED_FOR_UNCERTAIN_NON_IDEMPOTENT`, `REASON_DEFINITION_UNAVAILABLE`, `REASON_UNCERTAIN_NON_IDEMPOTENT`, `ERROR_EFFECT_UNCONFIRMED`, `DEFAULT_LEASE_TTL`, `DEFAULT_TOOL_PARAMETERS`, `is_terminal` | `m_agent.runtime` | Core lifecycle and explicit resolution contracts. |
| `m_agent.MAgentError`, `DefinitionConflictError`, `DefinitionNotFoundError`, `DuplicateRunError`, `IllegalRunTransitionError`, `LeaseNotHeldError`, `ModelCapabilityError`, `ResolutionNotAllowedError`, `RunNotFoundError`, `StaleRunVersionError`, `ModelFailure`, `StepFailure`, `ToolFailure` | `m_agent.runtime` | Core errors and failure contracts. |
| `m_agent.DeterministicModelAdapter`, `DeterministicStreamingModelAdapter`, `DeterministicContextProvider`, `DeterministicTool`, `InMemoryRunStore`, `SQLiteRunStore`, `JsonlTelemetrySink`, `PlaintextPayloadCodec`, `FakeClock`, `SystemClock` | `m_agent.adapters` | Concrete Adapter implementations move out of Core. |
| `m_agent.provider` | `m_agent.adapters.provider` | The old provider namespace is removed; install `m-agent[provider]`. |
| `m_agent.serialize_model_response`, `deserialize_model_response`, `serialize_tool_outcome`, `deserialize_tool_outcome` | removed | These checkpoint serialization helpers are not a stable integration seam. Use public inspection and Adapter contracts. |
| `m_agent.CrashPoint` | removed | Test-only fault injection is replaced by public Reference Acceptance scenarios. |
| `agent_framework` | removed | Follow [the 0.1 migration table](migrating-from-0.1.md); the compatibility shim does not continue into 0.3. |

`m_agent.companion` is intentionally public but empty in the Foundation. Future
Session, Context, Eval, and Routing capabilities enter through this namespace
and compose only through public Runtime ports. `m_agent.testing` provides the
offline Acceptance Manifest, Bundle, dependency check, and CLI; it does not
belong to Runtime Core.
