"""Deterministic offline public scenario for the Foundation Pack."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import subprocess
import sys
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
    FailureClassification,
    ModelCapabilities,
    ModelFailure,
    ModelResponse,
    ModelUsage,
    Runner,
    RunStatus,
)


_TELEMETRY_CANARIES = (
    "T07-INPUT-CANARY",
    "T07-INSTRUCTION-CANARY",
    "T07-SECRET-CANARY",
)


class _UsageReportingModel(DeterministicModelAdapter):
    """Deterministic public Adapter seam with explicit usage provenance."""

    def __init__(self, response: str) -> None:
        super().__init__(
            (response,),
            capabilities=ModelCapabilities(usage_reporting="PROVIDER_REPORTED"),
        )

    async def generate(self, request):
        response = await super().generate(request)
        return ModelResponse(
            content=response.content,
            usage=ModelUsage(input_tokens=11, output_tokens=7),
        )


class _FailingModel(DeterministicModelAdapter):
    """Deterministic public failure seam for structured telemetry evidence."""

    def __init__(self) -> None:
        super().__init__(("unused",))

    async def generate(self, request):
        raise ModelFailure(
            FailureClassification.PERMANENT,
            "rate_limited",
            _TELEMETRY_CANARIES[2],
        )


_CROSS_PROCESS_TELEMETRY_PROBE = r'''
import asyncio
import json
from pathlib import Path
import sys

from m_agent.adapters import (
    DeterministicModelAdapter,
    InMemoryRunStore,
    JsonlTelemetrySink,
    PlaintextPayloadCodec,
)
from m_agent.runtime import (
    AgentDefinition,
    DefinitionRegistry,
    ModelCapabilities,
    ModelResponse,
    ModelUsage,
    Runner,
)


class UsageModel(DeterministicModelAdapter):
    def __init__(self):
        super().__init__(
            ("child-response",),
            capabilities=ModelCapabilities(usage_reporting="PROVIDER_REPORTED"),
        )

    async def generate(self, request):
        response = await super().generate(request)
        return ModelResponse(content=response.content, usage=ModelUsage(input_tokens=11, output_tokens=7))


async def main():
    path = Path(sys.argv[1])
    sink = JsonlTelemetrySink(path)
    registry = DefinitionRegistry()
    registry.register(AgentDefinition(
        definition_id="telemetry-child",
        version="1.0",
        instructions="child",
        model_adapter=UsageModel(),
    ))
    runner = Runner(
        registry=registry,
        store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        telemetry_sink=sink,
    )
    created = await runner.create_run("telemetry-child", "1.0", "child")
    terminal = await runner.start_run(created.run_id)
    sink.close()
    sink.close()
    print(json.dumps({"run_id": created.run_id, "succeeded": terminal.status.value == "SUCCEEDED"}))


asyncio.run(main())
'''
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
    "ERROR_MODEL_EXECUTION_BUDGET_EXCEEDED",
    "FailureClassification",
    "IllegalRunTransitionError",
    "LeaseNotHeldError",
    "MAgentError",
    "ModelAdapter",
    "ModelBinding",
    "ModelBindingSet",
    "ModelCapabilityCombination",
    "ModelCapabilities",
    "ModelCapabilityError",
    "ModelContract",
    "ModelContractViolationError",
    "ModelDelta",
    "ModelExecutionBudget",
    "ModelFailure",
    "ModelLimits",
    "ModelPurpose",
    "ModelRequest",
    "ModelRequirementMatch",
    "ModelRequirementReason",
    "ModelRequirements",
    "ModelResponse",
    "ModelUsage",
    "ModelUsageGuarantees",
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
    "RevisionStability",
    "Runner",
    "StaleRunVersionError",
    "StepAttempt",
    "StepCheckpoint",
    "StepFailure",
    "StepRecord",
    "StepStatus",
    "StepType",
    "StreamingMode",
    "StructuredOutputMode",
    "SyncRunner",
    "TelemetryEvent",
    "TelemetryEventType",
    "TelemetrySink",
    "Tool",
    "ToolCall",
    "ToolCallingMode",
    "ToolDeclaration",
    "ToolEffect",
    "ToolFailure",
    "ToolOutcome",
    "ToolOutcomeStatus",
    "ToolRequest",
    "ToolSpec",
    "allowed_resolutions",
    "is_terminal",
    "UsageFieldGuarantee",
    "UsageProvenance",
    "UsageReportingMode",
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


def _telemetry_sequence(events: list[dict], run_id: str) -> bool:
    """Check the public lifecycle ordering for one correlated Run."""
    return (
        bool(events)
        and all(event.get("run_id") == run_id for event in events)
        and [event.get("event_type") for event in events]
        == [
            "RUN_STATUS_CHANGED",
            "RUN_STATUS_CHANGED",
            "STEP_STARTED",
            "STEP_COMPLETED",
            "RUN_STATUS_CHANGED",
        ]
        and [
            event.get("run_status")
            for event in events
            if event.get("event_type") == "RUN_STATUS_CHANGED"
        ]
        == ["CREATED", "RUNNING", "SUCCEEDED"]
    )


def _telemetry_inspection_reconciled(events: list[dict], inspection) -> bool:
    """Bind exported step events to the public authoritative inspection."""
    if (
        len(inspection.steps) != 1
        or len(inspection.attempts) != 1
        or len(inspection.checkpoints) != 1
    ):
        return False
    step = inspection.steps[0]
    attempt = inspection.attempts[0]
    checkpoint = inspection.checkpoints[0]
    step_events = [
        event
        for event in events
        if event.get("event_type") in {"STEP_STARTED", "STEP_COMPLETED"}
    ]
    return (
        len(step_events) == 2
        and attempt.step_id == step.step_id == checkpoint.step_id
        and attempt.attempt_id == checkpoint.attempt_id
        and all(
            event.get("step_id") == step.step_id
            and event.get("attempt_id") == attempt.attempt_id
            and event.get("step_type") == step.step_type.value
            for event in step_events
        )
    )


def _telemetry_failure_observed(events: list[dict], inspection) -> bool:
    if len(inspection.steps) != 1 or len(inspection.attempts) != 1:
        return False
    step = inspection.steps[0]
    attempt = inspection.attempts[0]
    failed = [
        event for event in events if event.get("event_type") == "ATTEMPT_FAILED"
    ]
    return (
        len(failed) == 1
        and failed[0].get("step_id") == step.step_id
        and failed[0].get("attempt_id") == attempt.attempt_id
        and failed[0].get("step_type") == step.step_type.value
        and failed[0].get("step_status") == "FAILED"
        and failed[0].get("classification") == FailureClassification.PERMANENT.value
        and failed[0].get("error_code") == "rate_limited"
        and isinstance(failed[0].get("duration_ms"), (int, float))
        and failed[0]["duration_ms"] >= 0
    )


def _telemetry_usage_provenance(events: list[dict]) -> bool:
    completions = [
        event
        for event in events
        if event.get("event_type") == "STEP_COMPLETED"
    ]
    usage = completions[0].get("usage") if len(completions) == 1 else None
    return (
        isinstance(usage, dict)
        and usage.get("input_tokens") == 11
        and usage.get("output_tokens") == 7
        and usage.get("provenance") == "PROVIDER_REPORTED"
        and all(
            event.get("usage") is None
            for event in events
            if event is not completions[0]
        )
    )


def _telemetry_concurrent(events: list[dict], run_ids: set[str]) -> bool:
    grouped = {run_id: [] for run_id in run_ids}
    for event in events:
        run_id = event.get("run_id")
        if run_id not in grouped:
            return False
        grouped[run_id].append(event)
    return len(grouped) == 4 and all(
        _telemetry_sequence(run_events, run_id)
        for run_id, run_events in grouped.items()
    )


def _telemetry_digest(*payloads: bytes) -> str:
    return "sha256:" + hashlib.sha256(b"".join(payloads)).hexdigest()


def _run_concurrent_telemetry(
    sink: JsonlTelemetrySink, response: str, index: int
) -> str:
    async def execute() -> str:
        registry = DefinitionRegistry()
        definition_id = f"telemetry-concurrent-{index}"
        registry.register(
            AgentDefinition(
                definition_id=definition_id,
                version="1.0",
                instructions="concurrent",
                model_adapter=_UsageReportingModel(response),
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
            telemetry_sink=sink,
        )
        created = await runner.create_run(definition_id, "1.0", "concurrent")
        terminal = await runner.start_run(created.run_id)
        if terminal.status is not RunStatus.SUCCEEDED:
            raise RuntimeError("concurrent telemetry Run did not succeed")
        return created.run_id

    return asyncio.run(execute())


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
            instructions=(
                "Use the deterministic fixture. "
                + _TELEMETRY_CANARIES[1]
            ),
            model_adapter=_UsageReportingModel(response),
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
        created = await runner.create_run(
            "core-lifecycle",
            "1.0",
            "fixture " + _TELEMETRY_CANARIES[0] + " " + _TELEMETRY_CANARIES[2],
        )
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)
        sink.close()
        sink.close()
        telemetry_bytes = telemetry_path.read_bytes()
        telemetry_lines = telemetry_bytes.decode("utf-8").splitlines()
        telemetry_events = [json.loads(line) for line in telemetry_lines if line]
        failure_path = Path(temporary_directory) / "failure.jsonl"
        failure_sink = JsonlTelemetrySink(failure_path)
        failure_registry = DefinitionRegistry()
        failure_registry.register(
            AgentDefinition(
                definition_id="telemetry-failure",
                version="1.0",
                instructions=_TELEMETRY_CANARIES[1],
                model_adapter=_FailingModel(),
            )
        )
        failure_runner = Runner(
            registry=failure_registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
            telemetry_sink=failure_sink,
        )
        failed_created = await failure_runner.create_run(
            "telemetry-failure",
            "1.0",
            "failure " + _TELEMETRY_CANARIES[0] + " " + _TELEMETRY_CANARIES[2],
        )
        failed_terminal = await failure_runner.start_run(failed_created.run_id)
        failed_inspection = await failure_runner.inspect_run(failed_created.run_id)
        failure_sink.close()
        failure_sink.close()
        failure_bytes = failure_path.read_bytes()
        failure_events = [
            json.loads(line)
            for line in failure_bytes.decode("utf-8").splitlines()
            if line
        ]

        cross_process_path = Path(temporary_directory) / "cross-process.jsonl"
        cross_process = subprocess.run(
            [sys.executable, "-c", _CROSS_PROCESS_TELEMETRY_PROBE, str(cross_process_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            cross_process_result = json.loads(cross_process.stdout)
        except json.JSONDecodeError:
            cross_process_result = {}
        cross_process_bytes = (
            cross_process_path.read_bytes() if cross_process_path.is_file() else b""
        )
        cross_process_events = [
            json.loads(line)
            for line in cross_process_bytes.decode("utf-8").splitlines()
            if line
        ]

        concurrent_path = Path(temporary_directory) / "concurrent.jsonl"
        concurrent_sink = JsonlTelemetrySink(concurrent_path)
        with ThreadPoolExecutor(max_workers=4) as executor:
            concurrent_futures = [
                executor.submit(_run_concurrent_telemetry, concurrent_sink, response, index)
                for index in range(4)
            ]
            concurrent_run_ids = {future.result() for future in concurrent_futures}
        concurrent_sink.close()
        concurrent_sink.close()
        concurrent_bytes = concurrent_path.read_bytes()
        concurrent_events = [
            json.loads(line)
            for line in concurrent_bytes.decode("utf-8").splitlines()
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
        for event in (*telemetry_events, *failure_events)
    )
    telemetry_ordered = _telemetry_sequence(telemetry_events, created.run_id)
    telemetry_inspection_reconciled = _telemetry_inspection_reconciled(
        telemetry_events, inspection
    )
    telemetry_usage_provenance = _telemetry_usage_provenance(telemetry_events)
    completions = [
        event
        for event in telemetry_events
        if event.get("event_type") == "STEP_COMPLETED"
    ]
    telemetry_model_purpose_observed = (
        len(completions) == 1 and completions[0].get("step_type") == "MODEL"
    )
    telemetry_duration_observed = (
        telemetry_model_purpose_observed
        and isinstance(completions[0].get("duration_ms"), (int, float))
        and completions[0]["duration_ms"] >= 0
    )
    telemetry_error_observed = (
        failed_terminal.status is RunStatus.FAILED
        and _telemetry_failure_observed(failure_events, failed_inspection)
    )
    telemetry_closed = (
        len(telemetry_events) == 5
        and telemetry_bytes.endswith(b"\n")
        and all(line for line in telemetry_lines)
    )
    telemetry_cross_process = (
        cross_process.returncode == 0
        and cross_process_result.get("succeeded") is True
        and _telemetry_sequence(
            cross_process_events, str(cross_process_result.get("run_id"))
        )
    )
    telemetry_concurrent = _telemetry_concurrent(
        concurrent_events, concurrent_run_ids
    )
    telemetry_redacted = all(
        canary.encode("utf-8") not in payload
        for canary in _TELEMETRY_CANARIES
        for payload in (
            telemetry_bytes,
            failure_bytes,
            cross_process_bytes,
            concurrent_bytes,
        )
    )
    telemetry_jsonl_digest = _telemetry_digest(
        telemetry_bytes, failure_bytes, cross_process_bytes, concurrent_bytes
    )
    telemetry_passed = (
        telemetry_correlated
        and telemetry_lifecycle_observed
        and telemetry_payload_absent
        and telemetry_ordered
        and telemetry_inspection_reconciled
        and telemetry_usage_provenance
        and telemetry_error_observed
        and telemetry_model_purpose_observed
        and telemetry_duration_observed
        and telemetry_closed
        and telemetry_cross_process
        and telemetry_concurrent
        and telemetry_redacted
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
        "telemetry_ordered": telemetry_ordered,
        "telemetry_inspection_reconciled": telemetry_inspection_reconciled,
        "telemetry_usage_provenance": telemetry_usage_provenance,
        "telemetry_error_observed": telemetry_error_observed,
        "telemetry_model_purpose_observed": telemetry_model_purpose_observed,
        "telemetry_duration_observed": telemetry_duration_observed,
        "telemetry_closed": telemetry_closed,
        "telemetry_cross_process": telemetry_cross_process,
        "telemetry_concurrent": telemetry_concurrent,
        "telemetry_redacted": telemetry_redacted,
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
                    "telemetry_ordered": evidence_view["telemetry_ordered"],
                    "telemetry_inspection_reconciled": evidence_view[
                        "telemetry_inspection_reconciled"
                    ],
                    "telemetry_usage_provenance": evidence_view[
                        "telemetry_usage_provenance"
                    ],
                    "telemetry_error_observed": evidence_view[
                        "telemetry_error_observed"
                    ],
                    "telemetry_model_purpose_observed": evidence_view[
                        "telemetry_model_purpose_observed"
                    ],
                    "telemetry_duration_observed": evidence_view[
                        "telemetry_duration_observed"
                    ],
                    "telemetry_closed": evidence_view["telemetry_closed"],
                    "telemetry_cross_process": evidence_view[
                        "telemetry_cross_process"
                    ],
                    "telemetry_concurrent": evidence_view[
                        "telemetry_concurrent"
                    ],
                    "telemetry_redacted": evidence_view["telemetry_redacted"],
                    "telemetry_jsonl_digest": telemetry_jsonl_digest,
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
            "telemetry_jsonl_digest": telemetry_jsonl_digest,
        },
    )
