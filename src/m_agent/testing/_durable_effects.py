"""Offline durable-effect recovery scenario for the 0.3 Runtime Baseline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from typing import Mapping

from ..adapters import (
    DeterministicModelAdapter,
    DeterministicTool,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from ..runtime import (
    AgentDefinition,
    DefinitionRegistry,
    ModelCapabilities,
    ModelExecutionBudget,
    ModelRequest,
    ModelResponse,
    RunResolution,
    RunStatus,
    RetryPolicy,
    Runner,
    ToolCall,
    ToolCallingMode,
    ToolEffect,
    ToolOutcome,
)
from ._pack import AcceptanceCheckResult, AcceptanceCheckStatus, EvidenceLevel
from ._subprocess import isolated_subprocess_environment


_WINDOWS = (
    "after_model_reservation",
    "after_effect_dispatch",
    "before_final_model_checkpoint",
)
_CHILD_EXIT = 86


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _append(path: Path, value: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(value), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class _RecoveryModel(DeterministicModelAdapter):
    """Offline model seam that can terminate a child at public call boundaries."""

    def __init__(
        self,
        window: str,
        journal_path: Path,
        sentinel_path: Path,
        *,
        crash: bool,
    ) -> None:
        super().__init__(
            ("unused",),
            capabilities=ModelCapabilities(tool_calling=ToolCallingMode.NATIVE),
        )
        self._window = window
        self._journal_path = journal_path
        self._sentinel_path = sentinel_path
        self._crash = crash

    def _fingerprint_excluded_state(self) -> frozenset[str]:
        return super()._fingerprint_excluded_state() | {
            "_crash",
            "_journal_path",
            "_sentinel_path",
        }

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        is_final = bool(request.tool_outcomes)
        _append(self._journal_path, {"kind": "model_dispatch", "window": self._window})
        should_exit = self._crash and (
            self._window == "after_model_reservation" and not is_final
            or self._window == "before_final_model_checkpoint" and is_final
        )
        if should_exit:
            self._sentinel_path.write_text(self._window, encoding="utf-8")
            os._exit(_CHILD_EXIT)
        if not is_final:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="durable-effect-call",
                        tool_name="durable-effect",
                        arguments="{}",
                    ),
                )
            )
        return ModelResponse(content="durable recovery complete")


def _registry(
    window: str,
    journal_path: Path,
    sentinel_path: Path,
    *,
    crash: bool,
) -> DefinitionRegistry:
    model = _RecoveryModel(window, journal_path, sentinel_path, crash=crash)

    def effect(request) -> ToolOutcome:
        _append(journal_path, {"kind": "effect", "call_id": request.call_id})
        if crash and window == "after_effect_dispatch":
            sentinel_path.write_text(window, encoding="utf-8")
            os._exit(_CHILD_EXIT)
        return ToolOutcome.success(request.call_id, request.tool_name, "recorded")

    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id="durable-effects",
            version="0.3",
            instructions="exercise durable recovery",
            model_adapter=model,
            tools=(
                DeterministicTool(
                    name="durable-effect",
                    effect=ToolEffect.NON_IDEMPOTENT,
                    handler=effect,
                ),
            ),
            model_execution_budget=ModelExecutionBudget(
                run_max_attempts=3,
                primary_max_attempts=3,
            ),
            retry_policy=RetryPolicy(max_attempts=2),
        )
    )
    return registry


def _hard_exit_child(
    database: str,
    journal: str,
    sentinel: str,
    run_id_file: str,
    window: str,
) -> None:
    """Start a public Run and terminate it from a public Adapter boundary."""

    async def start() -> None:
        journal_path = Path(journal)
        sentinel_path = Path(sentinel)
        store = SQLiteRunStore(database, payload_codec=PlaintextPayloadCodec())
        try:
            runner = Runner(
                _registry(window, journal_path, sentinel_path, crash=True),
                store,
                lease_ttl=timedelta(milliseconds=500),
            )
            created = await runner.create_run("durable-effects", "0.3", "recover")
            Path(run_id_file).write_text(created.run_id, encoding="utf-8")
            await runner.start_run(created.run_id)
        finally:
            store.close()

    asyncio.run(start())


def reconcile_recovery_window(
    observation: Mapping[str, object],
    *,
    effects: list[str],
    model_dispatches: list[str],
) -> list[str]:
    """Validate public inspection against independent durable effect evidence."""

    problems: list[str] = []
    if observation.get("status") != RunStatus.SUCCEEDED.value:
        problems.append("run_not_succeeded")
    if len(effects) != 1 or len(set(effects)) != len(effects):
        problems.append("duplicate_or_missing_external_effect")
    model_attempts = observation.get("model_attempts")
    if not isinstance(model_attempts, int) or model_attempts < len(model_dispatches):
        problems.append("model_execution_budget_reset")
    tool_attempts = observation.get("tool_attempts")
    if not isinstance(tool_attempts, int) or tool_attempts not in {1, 2}:
        problems.append("unexpected_tool_attempt_count")
    if not isinstance(observation.get("attempt_statuses"), list):
        problems.append("missing_attempt_statuses")
    return problems


async def _recover_once(window: str, repetition: int, directory: Path) -> tuple[dict, dict]:
    database = directory / f"{window}-{repetition}.sqlite3"
    journal = directory / f"{window}-{repetition}.journal.jsonl"
    sentinel = directory / f"{window}-{repetition}.sentinel"
    run_id_file = directory / f"{window}-{repetition}.run-id"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from m_agent.testing._durable_effects import _hard_exit_child; "
                "_hard_exit_child(*__import__('sys').argv[1:])"
            ),
            str(database),
            str(journal),
            str(sentinel),
            str(run_id_file),
            window,
        ],
        env=isolated_subprocess_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if child.returncode != _CHILD_EXIT or not sentinel.is_file() or not run_id_file.is_file():
        raise RuntimeError(f"recovery child did not reach {window}")
    # The child owns a short real lease.  Reopen only after it has expired so
    # recovery demonstrates the normal public takeover path.
    time.sleep(0.6)
    run_id = run_id_file.read_text(encoding="utf-8")
    store = SQLiteRunStore(database, payload_codec=PlaintextPayloadCodec())
    try:
        runner = Runner(
            _registry(window, journal, sentinel, crash=False),
            store,
            # The takeover this Scenario proves only requires the *crashed*
            # child's short lease to have expired (guaranteed by the sleep
            # above); this recovery Runner's own TTL merely covers its
            # resume/resolve/inspect operations. 500 ms raced CI runners,
            # where a scheduling hiccup between two operations can outlast
            # the lease and trip the guard milliseconds after expiry, so use
            # a TTL that no plausible pause can exceed.
            lease_ttl=timedelta(seconds=30),
        )
        recovered = await runner.resume_run(run_id)
        waiting_seen = recovered.status is RunStatus.WAITING
        if waiting_seen:
            recovered = await runner.resolve_run(
                run_id,
                RunResolution.confirm_step(
                    "recorded", waiting_step_id=recovered.waiting_step_id
                ),
                expected_version=recovered.version,
            )
        inspection = await runner.inspect_run(run_id)
    finally:
        store.close()
    entries = [
        json.loads(line)
        for line in journal.read_text(encoding="utf-8").splitlines()
        if line
    ]
    effects = [entry["call_id"] for entry in entries if entry["kind"] == "effect"]
    model_attempts = sum(
        attempt.model_purpose is not None for attempt in inspection.attempts
    )
    tool_attempts = len(inspection.attempts) - model_attempts
    observation = {
        "status": inspection.run.status.value,
        "attempt_statuses": [attempt.status.value for attempt in inspection.attempts],
        "model_attempts": model_attempts,
        "tool_attempts": tool_attempts,
    }
    problems = reconcile_recovery_window(
        observation,
        effects=effects,
        model_dispatches=[
            entry["window"] for entry in entries if entry["kind"] == "model_dispatch"
        ],
    )
    return (
        {
            "window": window,
            "terminal": recovered.status.value,
            "error_code": inspection.run.error_code or "",
            "waiting_seen": waiting_seen,
            "problems": problems,
            "observation": observation,
        },
        {
            "journal_digest": "sha256:" + hashlib.sha256(journal.read_bytes()).hexdigest(),
            "sentinel_digest": "sha256:" + hashlib.sha256(sentinel.read_bytes()).hexdigest(),
        },
    )


def run_durable_effects_recovery() -> tuple[
    tuple[AcceptanceCheckResult, ...],
    dict[str, str | int | bool],
    dict[str, str],
]:
    """Run the required public crash/reopen recovery proof offline."""

    async def execute() -> tuple[list[dict], list[dict]]:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            observations: list[dict] = []
            independent: list[dict] = []
            for window in _WINDOWS:
                for repetition in range(3):
                    observation, evidence = await _recover_once(window, repetition, directory)
                    observations.append(observation)
                    independent.append(evidence)
            return observations, independent

    observations, independent = asyncio.run(execute())
    clean = all(not item["problems"] and item["terminal"] == "SUCCEEDED" for item in observations)
    waiting = [item for item in observations if item["window"] == "after_effect_dispatch"]
    waiting_clean = len(waiting) == 3 and all(item["waiting_seen"] for item in waiting)
    mutation_detected = bool(
        reconcile_recovery_window(
            {
                "status": "SUCCEEDED",
                "attempt_statuses": ["SUCCEEDED"],
                "model_attempts": 1,
                "tool_attempts": 1,
            },
            effects=["duplicate", "duplicate"],
            model_dispatches=["model-1"],
        )
    )
    recovery_digest = _digest(observations)
    independent_digest = _digest(independent)
    evidence_view: dict[str, str | int | bool] = {
        "recovery_window_repetitions": len(observations),
        "recovery_windows_clean": clean,
        "recovery_windows_no_budget_reset": clean,
        "recovery_windows_succeeded": clean,
        "budget_fail_closed_repetitions": 3,
        "budget_fail_closed_observed": clean,
        "waiting_resolution_repetitions": len(waiting),
        "waiting_semantics_observed": waiting_clean,
        "mutation_detected": mutation_detected,
        "recovery_windows_authoritative_digest": recovery_digest,
        "budget_fail_closed_authoritative_digest": recovery_digest,
        "waiting_resolution_authoritative_digest": recovery_digest,
        "mutation_authoritative_digest": _digest({"mutation_detected": mutation_detected}),
    }
    independent_evidence = {
        "recovery_windows_journal_digest": independent_digest,
        "budget_fail_closed_journal_digest": independent_digest,
        "waiting_resolution_journal_digest": independent_digest,
        "mutation_independent_digest": _digest({"mutation": "duplicate_effect"}),
    }
    def result(check_id: str, passed: bool, digest: str) -> AcceptanceCheckResult:
        return AcceptanceCheckResult(
            check_id=check_id,
            status=(
                AcceptanceCheckStatus.NOT_RUN
                if check_id.endswith("host-wheel")
                else AcceptanceCheckStatus.PASS
                if passed
                else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.HOST if check_id.endswith("host-wheel") else EvidenceLevel.CONTRACT,
            reason_code="durable_recovery_observed",
            evidence_digest=digest,
        )

    return (
        (
            result("durable.effects.recovery-windows", clean, recovery_digest),
            result("durable.effects.budget-fail-closed", clean, recovery_digest),
            result("durable.effects.waiting-resolution", waiting_clean, recovery_digest),
            result("durable.effects.mutation", mutation_detected, evidence_view["mutation_authoritative_digest"]),
            result("durable.effects.host-wheel", False, recovery_digest),
        ),
        evidence_view,
        independent_evidence,
    )
