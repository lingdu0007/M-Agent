"""M-Agent 公共包：可嵌入 Python 应用的 Agent Application Runtime。

Durable Run 是 M-Agent 的第一个生产形态垂直切片。本包向
Runtime Integrator 提供：

- 不可变、版本化的 :class:`AgentDefinition` 注册与精确解析
  （:class:`DefinitionRegistry`），注册时校验 Model Capabilities；
- async-first 的 :class:`Runner`：创建（CREATED）、启动（冻结
  Definition Snapshot，执行模型-工具循环：先执行声明的 Context Step，
  再执行 Model Step；模型请求工具时严格顺序执行 Tool Step 并把
  Tool Outcome 作为数据回到模型，直到最终响应）、恢复（复用已确认
  的 Context / Model / Tool checkpoint）、检查（inspect）Agent Run；
- 流式观察：:meth:`Runner.subscribe_run` 订阅稳定但**非权威**的
  Run Update（模型流式 delta 携带 run_id / step_id / attempt_id，
  只有完整响应 checkpoint；订阅者断开不影响 Run，重连后从 RunStore
  重建事实）；:meth:`Runner.cancel_run` 提交协作式取消（只在安全
  边界终止，不假装中断或撤销已发出的调用）；
- 观测：可选 :class:`TelemetrySink` 在构造 Runner 时附加（Ticket 09），
  Runner 在 Run / Step / Attempt 生命周期事件上发射带 run_id /
  step_id / attempt_id、时间 / 耗时、状态、错误分类与可用 usage 的
  :class:`TelemetryEvent`；官方 :class:`JsonlTelemetrySink` 把事件
  写成本地 JSONL（默认不含 Run Payload）。Telemetry 只用于观测，
  RunStore 仍是唯一权威；Sink 失败被隔离，绝不覆盖或伪造权威状态。
- 权威的 :class:`RunStore` 契约与 :class:`InMemoryRunStore` 实现。

执行职责边界（ADR 0009 / PRD）：

- Runner 只执行调用方显式交给它的 Run（``start_run(run_id)``）。
- Runner 不承担 worker、queue、scheduler 或自动扫描职责；进程部署、
  任务调度、租约接管完全由上层应用控制。
- Run Store 是权威状态来源；Telemetry 只用于观测（可采样、脱敏、
  丢弃），本包不提供 Session / RAG / Workflow / MultiAgent /
  OpenTelemetry Adapter / Dashboard 等运行时子系统。

最小示例（Runtime Integrator）：

    import asyncio
    from m_agent import (
        AgentDefinition,
        DefinitionRegistry,
        DeterministicModelAdapter,
        InMemoryRunStore,
        Runner,
    )

    async def main() -> None:
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="assistant",
                version="1.0",
                instructions="Answer deterministically.",
                model_adapter=DeterministicModelAdapter(
                    responses=("hello from m_agent",),
                ),
            )
        )
        runner = Runner(registry=registry, store=InMemoryRunStore())
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)
        print(terminal.status, inspection.checkpoints[0].output)
"""

from ._clock import Clock, FakeClock, SystemClock
from ._codec import PayloadCodec, PlaintextPayloadCodec
from ._context import (
    ContextItem,
    ContextProvider,
    ContextRequest,
    DeterministicContextProvider,
)
from ._definition import (
    AgentDefinition,
    DefinitionRegistry,
    DefinitionSnapshot,
    RetryPolicy,
)
from ._output import OutputContract, OutputFallback, OutputRepairPolicy
from ._policy import (
    AllowAllRunPolicy,
    PolicyAction,
    PolicyDecision,
    PolicyDecisionRecord,
    PolicyGate,
    PolicyIdentity,
    PolicyRequest,
    RunPolicy,
    StaticRunPolicy,
)
from ._errors import (
    DefinitionConflictError,
    DefinitionNotFoundError,
    DuplicateRunError,
    IllegalRunTransitionError,
    LeaseNotHeldError,
    MAgentError,
    ModelCapabilityError,
    ResolutionNotAllowedError,
    RunNotFoundError,
    StaleRunVersionError,
)
from ._failure import ModelFailure, StepFailure, ToolFailure
from ._model import (
    DeterministicModelAdapter,
    DeterministicStreamingModelAdapter,
    ModelAdapter,
    ModelCapabilities,
    ModelDelta,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    deserialize_model_response,
    serialize_model_response,
)
from ._resolution import (
    ALLOWED_FOR_DEFINITION_UNAVAILABLE,
    ALLOWED_FOR_UNCERTAIN_NON_IDEMPOTENT,
    ResolutionAction,
    RunResolution,
    allowed_resolutions,
)
from ._run import RunInspection, RunRecord
from ._status import RunStatus, is_terminal
from ._steps import (
    FailureClassification,
    StepAttempt,
    StepCheckpoint,
    StepRecord,
    StepStatus,
    StepType,
)
from ._store import RunLease, RunStore
from ._telemetry import (
    JsonlTelemetrySink,
    TelemetryEvent,
    TelemetryEventType,
    TelemetrySink,
)
from ._tools import (
    DEFAULT_TOOL_PARAMETERS,
    DeterministicTool,
    Tool,
    ToolCall,
    ToolDeclaration,
    ToolEffect,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolRequest,
    ToolSpec,
    deserialize_tool_outcome,
    serialize_tool_outcome,
)
from ._updates import RunUpdate, RunUpdateType
from ._runner import (
    DEFAULT_LEASE_TTL,
    ERROR_EFFECT_UNCONFIRMED,
    ERROR_OUTPUT_VALIDATION_FAILED,
    ERROR_POLICY_ERROR,
    CrashPoint,
    REASON_DEFINITION_UNAVAILABLE,
    REASON_POLICY_RESOLUTION_REQUIRED,
    REASON_UNCERTAIN_NON_IDEMPOTENT,
    Runner,
)
from ._sync import SyncRunner

__all__ = [
    "AgentDefinition",
    "AllowAllRunPolicy",
    "ALLOWED_FOR_DEFINITION_UNAVAILABLE",
    "ALLOWED_FOR_UNCERTAIN_NON_IDEMPOTENT",
    "Clock",
    "ContextItem",
    "ContextProvider",
    "ContextRequest",
    "CrashPoint",
    "DEFAULT_LEASE_TTL",
    "DEFAULT_TOOL_PARAMETERS",
    "DefinitionConflictError",
    "DefinitionNotFoundError",
    "DefinitionRegistry",
    "DefinitionSnapshot",
    "DeterministicContextProvider",
    "DeterministicModelAdapter",
    "DeterministicStreamingModelAdapter",
    "DeterministicTool",
    "DuplicateRunError",
    "ERROR_EFFECT_UNCONFIRMED",
    "ERROR_OUTPUT_VALIDATION_FAILED",
    "ERROR_POLICY_ERROR",
    "FakeClock",
    "FailureClassification",
    "IllegalRunTransitionError",
    "InMemoryRunStore",
    "JsonlTelemetrySink",
    "LeaseNotHeldError",
    "MAgentError",
    "ModelAdapter",
    "ModelCapabilities",
    "ModelCapabilityError",
    "ModelDelta",
    "ModelFailure",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
    "OutputContract",
    "OutputFallback",
    "OutputRepairPolicy",
    "PayloadCodec",
    "PolicyAction",
    "PolicyDecision",
    "PolicyDecisionRecord",
    "PolicyGate",
    "PolicyIdentity",
    "PolicyRequest",
    "PlaintextPayloadCodec",
    "REASON_DEFINITION_UNAVAILABLE",
    "REASON_POLICY_RESOLUTION_REQUIRED",
    "REASON_UNCERTAIN_NON_IDEMPOTENT",
    "ResolutionAction",
    "ResolutionNotAllowedError",
    "RetryPolicy",
    "RunInspection",
    "RunLease",
    "RunNotFoundError",
    "RunRecord",
    "RunPolicy",
    "Runner",
    "SyncRunner",
    "RunResolution",
    "RunStatus",
    "RunStore",
    "RunUpdate",
    "RunUpdateType",
    "SQLiteRunStore",
    "StaleRunVersionError",
    "StepAttempt",
    "StepCheckpoint",
    "StepFailure",
    "StepRecord",
    "StepStatus",
    "StepType",
    "StaticRunPolicy",
    "SystemClock",
    "TelemetryEvent",
    "TelemetryEventType",
    "TelemetrySink",
    "Tool",
    "ToolCall",
    "ToolDeclaration",
    "ToolEffect",
    "ToolFailure",
    "ToolOutcome",
    "ToolOutcomeStatus",
    "ToolRequest",
    "ToolSpec",
    "allowed_resolutions",
    "deserialize_model_response",
    "deserialize_tool_outcome",
    "is_terminal",
    "serialize_model_response",
    "serialize_tool_outcome",
]


_LAZY_ADAPTER_FACADE = frozenset({"InMemoryRunStore", "SQLiteRunStore"})


def __getattr__(name: str) -> object:
    """Keep 0.2 root Store imports without loading Adapters for Runtime imports."""
    if name not in _LAZY_ADAPTER_FACADE:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import adapters

    value = getattr(adapters, name)
    globals()[name] = value
    return value
