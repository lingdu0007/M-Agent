"""OpenTelemetry Telemetry adapter with no Runtime Core SDK dependency.

The Runtime Core exposes only ``TelemetrySink``.  This Adapter maps that
public event contract to immutable span evidence for local CONTRACT tests and,
when supplied, invokes the standard ``Tracer.start_span(...); Span.end(...)``
shape of an application-owned OpenTelemetry integration.  The Core never
imports an OpenTelemetry SDK, owns a provider, or contacts a Collector.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
import threading
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

from .._telemetry import TelemetryEvent, TelemetryEventType


@dataclass(frozen=True)
class OpenTelemetryTraceContext:
    """Portable parent trace context supplied by the embedding application.

    The Core deliberately does not import an OpenTelemetry SDK.  An embedding
    which already owns one can therefore convert its active span context into
    this small, immutable public value and retain trace / parent relationships
    in local exporter evidence.  A configured SDK's native context may be
    carried opaquely to an application-owned ``Tracer``.  ``trace_state`` and
    baggage are excluded because they are caller-specific transport metadata
    and may contain sensitive data.
    """

    trace_id: str | None = None
    parent_span_id: str | None = None
    native_context: object | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not self.trace_id and self.native_context is None:
            raise ValueError(
                "OpenTelemetry trace context needs trace_id or native_context"
            )
        if self.parent_span_id == "":
            raise ValueError("OpenTelemetry parent_span_id must not be empty")


class OpenTelemetrySpanScope(str, enum.Enum):
    """Runtime entity represented by one exported event span."""

    RUN = "RUN"
    STEP = "STEP"
    ATTEMPT = "ATTEMPT"


@dataclass(frozen=True)
class OpenTelemetrySpan:
    """A minimal immutable span projection of one public telemetry event."""

    name: str
    started_at: datetime
    ended_at: datetime
    attributes: Mapping[str, str | int | float | bool]
    scope: OpenTelemetrySpanScope
    trace_context: OpenTelemetryTraceContext | None = None


@runtime_checkable
class OpenTelemetrySpanExporter(Protocol):
    """Local span-export seam used by deterministic CONTRACT evidence."""

    def export(self, span: OpenTelemetrySpan) -> None: ...

    def shutdown(self) -> None: ...


@runtime_checkable
class OpenTelemetryWritableSpan(Protocol):
    """The small standard ``Span.end`` surface used by the SDK bridge."""

    def end(self, end_time: int | None = None) -> None: ...


@runtime_checkable
class OpenTelemetryTracer(Protocol):
    """Application-owned OpenTelemetry ``Tracer.start_span`` bridge.

    This deliberately mirrors the public SDK call shape without importing its
    types.  A real ``opentelemetry.trace.Tracer`` can satisfy it; no provider
    lifecycle is owned by this Adapter.
    """

    def start_span(
        self,
        name: str,
        *,
        context: object | None = None,
        attributes: Mapping[str, str | int | float | bool] | None = None,
        start_time: int | None = None,
    ) -> OpenTelemetryWritableSpan: ...


class InMemorySpanExporter:
    """Local deterministic exporter for offline OpenTelemetry CONTRACT checks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: list[OpenTelemetrySpan] = []
        self._closed = False

    @property
    def spans(self) -> tuple[OpenTelemetrySpan, ...]:
        with self._lock:
            return tuple(self._spans)

    def export(self, span: OpenTelemetrySpan) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("InMemorySpanExporter is closed")
            self._spans.append(span)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True


class OpenTelemetryTelemetrySink:
    """Map ``TelemetryEvent`` to OpenTelemetry-compatible span attributes.

    The adapter deliberately accepts only the public event object.  Run input,
    checkpoints, protected payload and provider raw bodies cannot enter the
    projection because those fields are absent from the source contract.
    """

    def __init__(
        self,
        exporter: OpenTelemetrySpanExporter | None = None,
        *,
        tracer: OpenTelemetryTracer | None = None,
        trace_context: OpenTelemetryTraceContext | None = None,
    ) -> None:
        if exporter is None and tracer is None:
            raise ValueError("OpenTelemetryTelemetrySink needs exporter or tracer")
        self._exporter = exporter
        self._tracer = tracer
        self._trace_context = trace_context
        self._lock = threading.Lock()
        self._closed = False

    def emit(self, event: TelemetryEvent) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("OpenTelemetryTelemetrySink is closed")
            span = _span_from_event(event, trace_context=self._trace_context)
            if self._exporter is not None:
                self._exporter.export(span)
            if self._tracer is not None:
                tracer_span = self._tracer.start_span(
                    span.name,
                    context=(
                        self._trace_context.native_context
                        if self._trace_context is not None
                        else None
                    ),
                    attributes=span.attributes,
                    start_time=_unix_nanos(span.started_at),
                )
                tracer_span.end(end_time=_unix_nanos(span.ended_at))

    def flush(self) -> None:
        """The exporter seam is synchronous; accepted spans are already handed off."""

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                if self._exporter is not None:
                    self._exporter.shutdown()
                self._closed = True

    def __enter__(self) -> "OpenTelemetryTelemetrySink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_SPAN_NAMES = {
    TelemetryEventType.RUN_STATUS_CHANGED: "m_agent.run.status_changed",
    TelemetryEventType.STEP_STARTED: "m_agent.step.started",
    TelemetryEventType.STEP_COMPLETED: "m_agent.step.completed",
    TelemetryEventType.ATTEMPT_FAILED: "m_agent.attempt.failed",
}

_SPAN_SCOPES = {
    TelemetryEventType.RUN_STATUS_CHANGED: OpenTelemetrySpanScope.RUN,
    TelemetryEventType.STEP_STARTED: OpenTelemetrySpanScope.STEP,
    TelemetryEventType.STEP_COMPLETED: OpenTelemetrySpanScope.ATTEMPT,
    TelemetryEventType.ATTEMPT_FAILED: OpenTelemetrySpanScope.ATTEMPT,
}


def _span_from_event(
    event: TelemetryEvent,
    *,
    trace_context: OpenTelemetryTraceContext | None,
) -> OpenTelemetrySpan:
    scope = _SPAN_SCOPES[event.event_type]
    attributes: dict[str, str | int | float | bool] = {
        "m_agent.event_type": event.event_type.value,
        "m_agent.run_id": event.run_id,
        "m_agent.span_scope": scope.value,
    }
    for name, value in (
        ("step_id", event.step_id),
        ("attempt_id", event.attempt_id),
        ("step_type", event.step_type.value if event.step_type else None),
        (
            "model_purpose",
            event.model_purpose.value if event.model_purpose else None,
        ),
        ("run_status", event.run_status.value if event.run_status else None),
        ("step_status", event.step_status.value if event.step_status else None),
        (
            "classification",
            event.classification.value if event.classification else None,
        ),
        ("error_code", event.error_code),
        ("duration_ms", event.duration_ms),
    ):
        if value is not None:
            attributes[f"m_agent.{name}"] = value
    if event.usage is not None:
        for name, value in event.usage.model_dump(mode="json").items():
            if value is not None:
                attributes[f"m_agent.usage.{name}"] = value
    if trace_context is not None:
        if trace_context.trace_id is not None:
            attributes["m_agent.trace_id"] = trace_context.trace_id
        if trace_context.parent_span_id is not None:
            attributes["m_agent.parent_span_id"] = trace_context.parent_span_id
    return OpenTelemetrySpan(
        name=_SPAN_NAMES[event.event_type],
        started_at=event.created_at,
        ended_at=event.created_at,
        attributes=MappingProxyType(attributes),
        scope=scope,
        trace_context=trace_context,
    )


def _unix_nanos(value: datetime) -> int:
    """Convert an event timestamp to the OpenTelemetry epoch-nanosecond form."""
    return int(value.timestamp() * 1_000_000_000)
