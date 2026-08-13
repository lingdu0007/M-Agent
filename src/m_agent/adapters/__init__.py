"""Official concrete Runtime Adapter implementations.

Adapters implement one Runtime Core port. They may depend on
:mod:`m_agent.runtime`; Runtime Core never imports this namespace.
"""

from .._clock import FakeClock, SystemClock
from .._codec import PlaintextPayloadCodec
from .._context import DeterministicContextProvider
from .._model import DeterministicModelAdapter, DeterministicStreamingModelAdapter
from .._sqlite_store import SQLiteRunStore
from .._store import InMemoryRunStore
from .._telemetry import JsonlTelemetrySink
from .._tools import DeterministicTool

__all__ = [
    "DeterministicContextProvider",
    "DeterministicModelAdapter",
    "DeterministicStreamingModelAdapter",
    "DeterministicTool",
    "FakeClock",
    "InMemoryRunStore",
    "JsonlTelemetrySink",
    "PlaintextPayloadCodec",
    "SQLiteRunStore",
    "SystemClock",
]
