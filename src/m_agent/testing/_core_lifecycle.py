"""Deterministic offline public scenario for the Foundation Pack."""

from __future__ import annotations

import hashlib
from importlib.resources import files
import json
from pathlib import Path
import tempfile

from ..adapters import (
    DeterministicModelAdapter,
    InMemoryRunStore,
    JsonlTelemetrySink,
    PlaintextPayloadCodec,
)
from ..runtime import (
    AgentDefinition,
    DefinitionNotFoundError,
    DefinitionRegistry,
    Runner,
    RunStatus,
)
from ._dependencies import find_runtime_dependency_violations
from ._pack import (
    AcceptanceCheckResult,
    AcceptanceCheckStatus,
    EvidenceLevel,
)


_EXPAND_RUNTIME_EXPORTS = (
    "AgentDefinition",
    "ALLOWED_FOR_DEFINITION_UNAVAILABLE",
    "ALLOWED_FOR_UNCERTAIN_NON_IDEMPOTENT",
    "Clock",
    "ContextItem",
    "ContextProvider",
    "ContextRequest",
    "DEFAULT_LEASE_TTL",
    "DEFAULT_TOOL_PARAMETERS",
    "DefinitionConflictError",
    "DefinitionNotFoundError",
    "DefinitionRegistry",
    "DefinitionSnapshot",
    "DuplicateRunError",
    "ERROR_EFFECT_UNCONFIRMED",
    "FailureClassification",
    "IllegalRunTransitionError",
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
    "PayloadCodec",
    "REASON_DEFINITION_UNAVAILABLE",
    "REASON_UNCERTAIN_NON_IDEMPOTENT",
    "ResolutionAction",
    "ResolutionNotAllowedError",
    "RetryPolicy",
    "RunInspection",
    "RunLease",
    "RunNotFoundError",
    "RunRecord",
    "RunResolution",
    "RunStatus",
    "RunStore",
    "RunUpdate",
    "RunUpdateType",
    "Runner",
    "StaleRunVersionError",
    "StepAttempt",
    "StepCheckpoint",
    "StepFailure",
    "StepRecord",
    "StepStatus",
    "StepType",
    "SyncRunner",
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
    "is_terminal",
)
_EXPAND_ADAPTER_EXPORTS = (
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
)
_EXPAND_ONLY_EXPORTS = (
    "CrashPoint",
    "deserialize_model_response",
    "deserialize_tool_outcome",
    "serialize_model_response",
    "serialize_tool_outcome",
)
_EXPAND_ROOT_EXPORTS = frozenset(
    _EXPAND_RUNTIME_EXPORTS + _EXPAND_ADAPTER_EXPORTS + _EXPAND_ONLY_EXPORTS
)


def _evidence_digest(value: dict[str, bool | int]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _fixture_response() -> tuple[str, str]:
    """Read the packaged deterministic fixture that the Manifest identifies."""
    fixture_bytes = files("m_agent.testing").joinpath(
        "fixtures/core_lifecycle.json"
    ).read_bytes()
    fixture = json.loads(fixture_bytes)
    response = fixture.get("response")
    if not isinstance(response, str) or not response:
        raise ValueError("core-lifecycle fixture must define a nonempty response")
    return response, "sha256:" + hashlib.sha256(fixture_bytes).hexdigest()


def _expand_compatibility_observation() -> dict[str, bool | int]:
    """Measure every documented 0.2 root binding through public namespaces."""
    import m_agent
    from m_agent import adapters, runtime

    root_exports = tuple(m_agent.__all__)
    runtime_bindings = all(
        getattr(m_agent, name, None) is getattr(runtime, name)
        for name in _EXPAND_RUNTIME_EXPORTS
    )
    adapter_bindings = all(
        getattr(m_agent, name, None) is getattr(adapters, name)
        for name in _EXPAND_ADAPTER_EXPORTS
    )
    legacy_bindings = all(
        name in m_agent.__all__ and hasattr(m_agent, name)
        for name in _EXPAND_ONLY_EXPORTS
    )
    complete_exports = (
        len(root_exports) == len(set(root_exports))
        and set(root_exports) == _EXPAND_ROOT_EXPORTS
    )
    return {
        "expand_binding_count": len(root_exports),
        "expand_exports_complete": complete_exports,
        "expand_runtime_bindings": runtime_bindings,
        "expand_adapter_bindings": adapter_bindings,
        "expand_legacy_bindings": legacy_bindings,
        "expand_compatibility": (
            complete_exports
            and runtime_bindings
            and adapter_bindings
            and legacy_bindings
        ),
    }


async def run_core_lifecycle(*, fixture_digest: str) -> tuple[
    tuple[AcceptanceCheckResult, ...],
    dict[str, str | int | bool | None],
    dict[str, str | int | bool | None],
]:
    """Exercise public Runtime and Adapter seams without external I/O."""
    response, measured_fixture_digest = _fixture_response()
    if fixture_digest != measured_fixture_digest:
        raise ValueError("core-lifecycle fixture does not match Manifest identity")
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition(
            definition_id="core-lifecycle",
            version="1.0",
            instructions="Use the deterministic fixture.",
            model_adapter=DeterministicModelAdapter((response,)),
        )
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        telemetry_path = Path(temporary_directory) / "core-lifecycle.jsonl"
        sink = JsonlTelemetrySink(telemetry_path)
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
            telemetry_sink=sink,
        )
        created = await runner.create_run("core-lifecycle", "1.0", "fixture")
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)
        sink.close()
        telemetry_events = [
            json.loads(line)
            for line in telemetry_path.read_text().splitlines()
            if line
        ]
    lifecycle_passed = (
        terminal.status is RunStatus.SUCCEEDED
        and len(inspection.steps) == len(inspection.attempts) == len(inspection.checkpoints) == 1
    )
    try:
        registry.resolve("unknown", "1.0")
    except DefinitionNotFoundError:
        unknown_definition_rejected = True
    else:
        unknown_definition_rejected = False
    from m_agent import adapters, companion, runtime, testing

    public_layers_available = (
        getattr(runtime, "Runner") is Runner
        and adapters.DeterministicModelAdapter is DeterministicModelAdapter
        and hasattr(companion, "__all__")
        and testing.AcceptanceManifest is not None
    )
    dependency_violations = find_runtime_dependency_violations()
    dependency_direction_passed = not dependency_violations
    expand_observation = _expand_compatibility_observation()
    telemetry_correlated = bool(telemetry_events) and all(
        event.get("run_id") == created.run_id for event in telemetry_events
    )
    telemetry_lifecycle_observed = {
        "STEP_STARTED",
        "STEP_COMPLETED",
    }.issubset({event.get("event_type") for event in telemetry_events}) and any(
        event.get("run_status") == RunStatus.SUCCEEDED.value
        for event in telemetry_events
    )
    telemetry_payload_absent = all(
        not {"input", "output", "instructions", "payload"}.intersection(event)
        for event in telemetry_events
    )
    telemetry_passed = (
        telemetry_correlated
        and telemetry_lifecycle_observed
        and telemetry_payload_absent
    )
    evidence_view = {
        "run_succeeded": terminal.status is RunStatus.SUCCEEDED,
        "step_count": len(inspection.steps),
        "attempt_count": len(inspection.attempts),
        "checkpoint_count": len(inspection.checkpoints),
        "unknown_definition_rejected": unknown_definition_rejected,
        "public_layers_available": public_layers_available,
        "runtime_dependency_violation_count": len(dependency_violations),
        "telemetry_event_count": len(telemetry_events),
        "telemetry_correlated": telemetry_correlated,
        "telemetry_lifecycle_observed": telemetry_lifecycle_observed,
        "telemetry_payload_absent": telemetry_payload_absent,
        **expand_observation,
    }
    results = (
        AcceptanceCheckResult(
            check_id="core.lifecycle",
            status=(
                AcceptanceCheckStatus.PASS
                if lifecycle_passed
                else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
            reason_code="terminal_lifecycle_observed",
            evidence_digest=_evidence_digest(
                {
                    "run_succeeded": evidence_view["run_succeeded"],
                    "step_count": evidence_view["step_count"],
                    "attempt_count": evidence_view["attempt_count"],
                    "checkpoint_count": evidence_view["checkpoint_count"],
                }
            ),
        ),
        AcceptanceCheckResult(
            check_id="core.lifecycle.telemetry",
            status=(
                AcceptanceCheckStatus.PASS
                if telemetry_passed
                else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
            reason_code="jsonl_telemetry_contract_observed",
            evidence_digest=_evidence_digest(
                {
                    "telemetry_event_count": evidence_view["telemetry_event_count"],
                    "telemetry_correlated": evidence_view["telemetry_correlated"],
                    "telemetry_lifecycle_observed": evidence_view[
                        "telemetry_lifecycle_observed"
                    ],
                    "telemetry_payload_absent": evidence_view[
                        "telemetry_payload_absent"
                    ],
                }
            ),
        ),
        AcceptanceCheckResult(
            check_id="core.lifecycle.public-namespaces",
            status=(
                AcceptanceCheckStatus.PASS
                if public_layers_available
                else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
            reason_code="public_layers_available",
            evidence_digest=_evidence_digest(
                {"public_layers_available": public_layers_available}
            ),
        ),
        AcceptanceCheckResult(
            check_id="core.lifecycle.dependency-direction",
            status=(
                AcceptanceCheckStatus.PASS
                if dependency_direction_passed
                else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
            reason_code="runtime_dependency_direction_checked",
            evidence_digest=_evidence_digest(
                {"runtime_dependency_violation_count": len(dependency_violations)}
            ),
        ),
        AcceptanceCheckResult(
            check_id="core.lifecycle.expand-compatibility",
            status=(
                AcceptanceCheckStatus.PASS
                if expand_observation["expand_compatibility"]
                else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
            reason_code="root_expand_compatibility_checked",
            evidence_digest=_evidence_digest(expand_observation),
        ),
        AcceptanceCheckResult(
            check_id="core.lifecycle.unknown-definition",
            status=(
                AcceptanceCheckStatus.PASS
                if unknown_definition_rejected
                else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
            reason_code="unknown_definition_rejected",
            evidence_digest=_evidence_digest(
                {
                    "unknown_definition_rejected": evidence_view[
                        "unknown_definition_rejected"
                    ]
                }
            ),
        ),
    )
    return (
        results,
        evidence_view,
        {
            "fixture_digest": measured_fixture_digest,
        },
    )
