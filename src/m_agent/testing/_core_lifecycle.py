"""Deterministic offline public scenario for the Foundation Pack."""

from __future__ import annotations

import hashlib
from importlib.resources import files
import json

from ..adapters import (
    DeterministicModelAdapter,
    InMemoryRunStore,
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
    runner = Runner(
        registry=registry,
        store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
    )
    created = await runner.create_run("core-lifecycle", "1.0", "fixture")
    terminal = await runner.start_run(created.run_id)
    inspection = await runner.inspect_run(created.run_id)
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
    from m_agent import Clock, Runner as RootRunner
    from m_agent import adapters, companion, runtime, testing

    public_layers_available = (
        RootRunner is Runner
        and Clock is runtime.Clock
        and adapters.DeterministicModelAdapter is DeterministicModelAdapter
        and hasattr(companion, "__all__")
        and testing.AcceptanceManifest is not None
    )
    dependency_violations = find_runtime_dependency_violations()
    dependency_direction_passed = not dependency_violations
    evidence_view = {
        "run_succeeded": terminal.status is RunStatus.SUCCEEDED,
        "step_count": len(inspection.steps),
        "attempt_count": len(inspection.attempts),
        "checkpoint_count": len(inspection.checkpoints),
        "unknown_definition_rejected": unknown_definition_rejected,
        "public_layers_available": public_layers_available,
        "runtime_dependency_violation_count": len(dependency_violations),
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
                if RootRunner is Runner and Clock is runtime.Clock
                else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
            reason_code="root_runtime_compatibility_checked",
            evidence_digest=_evidence_digest(
                {
                    "root_runtime_compatibility": RootRunner is Runner
                    and Clock is runtime.Clock
                }
            ),
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
