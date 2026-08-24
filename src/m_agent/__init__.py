"""Small 0.3 facade for the M-Agent Runtime Core.

Stable contracts live in :mod:`m_agent.runtime`; concrete integrations live
in :mod:`m_agent.adapters`; acceptance facilities live in
:mod:`m_agent.testing`. See ``docs/migrating-to-0.3.md`` for the one-time
public import reset.
"""

from . import runtime as _runtime
from .runtime import (
    AgentDefinition,
    DefinitionRegistry,
    RunInspection,
    RunRecord,
    RunStatus,
    Runner,
    SyncRunner,
)

__all__ = [
    "AgentDefinition",
    "DefinitionRegistry",
    "Runner",
    "SyncRunner",
    "RunStatus",
    "RunRecord",
    "RunInspection",
]

_RUNTIME_MOVED = frozenset(_runtime.__all__) - frozenset(__all__)
_ADAPTER_MOVED = frozenset({
    "DeterministicContextProvider", "DeterministicModelAdapter",
    "DeterministicStreamingModelAdapter", "DeterministicTool", "FakeClock",
    "InMemoryRunStore", "InMemorySpanExporter", "JsonlTelemetrySink",
    "OpenTelemetrySpan", "OpenTelemetrySpanExporter", "OpenTelemetrySpanScope",
    "OpenTelemetryTelemetrySink", "OpenTelemetryTraceContext", "OpenTelemetryTracer",
    "OpenTelemetryWritableSpan", "PlaintextPayloadCodec", "SQLiteRunStore",
    "SystemClock",
})
_REMOVED = frozenset({
    "CrashPoint", "serialize_model_response", "deserialize_model_response",
    "serialize_tool_outcome", "deserialize_tool_outcome",
})


def __getattr__(name: str) -> object:
    if name in _REMOVED:
        raise AttributeError(
            f"m_agent.{name} was removed; see docs/migrating-to-0.3.md"
        )
    if name in _RUNTIME_MOVED:
        raise AttributeError(
            f"m_agent.{name} moved to m_agent.runtime; see docs/migrating-to-0.3.md"
        )
    if name in _ADAPTER_MOVED:
        raise AttributeError(
            f"m_agent.{name} moved to m_agent.adapters; see docs/migrating-to-0.3.md"
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
