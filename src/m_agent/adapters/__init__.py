"""Official concrete Runtime Adapter implementations.

Adapters implement one Runtime Core port. They may depend on
:mod:`m_agent.runtime`; Runtime Core never imports this namespace.
"""

from .._clock import FakeClock, SystemClock
from .._codec import PlaintextPayloadCodec
from .._context import DeterministicContextProvider
from .._model import DeterministicModelAdapter, DeterministicStreamingModelAdapter
from ._in_memory_store import InMemoryRunStore
from ._sqlite_store import SQLiteRunStore
from .._telemetry import JsonlTelemetrySink
from ._opentelemetry import (
    InMemorySpanExporter,
    OpenTelemetrySpan,
    OpenTelemetrySpanExporter,
    OpenTelemetrySpanScope,
    OpenTelemetryTelemetrySink,
    OpenTelemetryTraceContext,
    OpenTelemetryTracer,
    OpenTelemetryWritableSpan,
)
from .._tools import DeterministicTool

__all__ = [
    "DeterministicContextProvider",
    "DeterministicModelAdapter",
    "DeterministicStreamingModelAdapter",
    "DeterministicTool",
    "FakeClock",
    "InMemoryRunStore",
    "InMemorySpanExporter",
    "JsonlTelemetrySink",
    "OpenTelemetrySpan",
    "OpenTelemetrySpanExporter",
    "OpenTelemetrySpanScope",
    "OpenTelemetryTelemetrySink",
    "OpenTelemetryTraceContext",
    "OpenTelemetryTracer",
    "OpenTelemetryWritableSpan",
    "PlaintextPayloadCodec",
    "SQLiteRunStore",
    "SystemClock",
]
