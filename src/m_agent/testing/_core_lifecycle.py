"""Deterministic offline public scenario for the Foundation Pack."""

from __future__ import annotations

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
from ._pack import (
    AcceptanceCheckResult,
    AcceptanceCheckStatus,
    EvidenceLevel,
)


async def run_core_lifecycle() -> tuple[
    tuple[AcceptanceCheckResult, ...],
    dict[str, str | int | bool | None],
    dict[str, str | int | bool | None],
]:
    """Exercise public Runtime and Adapter seams without external I/O."""
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition(
            definition_id="core-lifecycle",
            version="1.0",
            instructions="Use the deterministic fixture.",
            model_adapter=DeterministicModelAdapter(("accepted",)),
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
    results = (
        AcceptanceCheckResult(
            check_id="core.lifecycle",
            status=(
                AcceptanceCheckStatus.PASS
                if lifecycle_passed
                else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
        ),
        AcceptanceCheckResult(
            check_id="core.lifecycle.unknown-definition",
            status=(
                AcceptanceCheckStatus.PASS
                if unknown_definition_rejected
                else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
        ),
    )
    return (
        results,
        {
            "run_succeeded": terminal.status is RunStatus.SUCCEEDED,
            "step_count": len(inspection.steps),
            "attempt_count": len(inspection.attempts),
            "checkpoint_count": len(inspection.checkpoints),
            "unknown_definition_rejected": unknown_definition_rejected,
        },
        {
            "model_digest": "sha256:" + "d" * 64,
            "network_disabled": True,
        },
    )
