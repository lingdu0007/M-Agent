"""Correctness-first benchmark for 100 concurrent sessionless Agent Runs.

The measured interval covers public ``Runner.create_run`` and
``Runner.start_run`` calls for every Run. Inspection and SQLite validation run
after the interval and are never included in the reported performance metrics.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

from m_agent.runtime import (
    AgentDefinition,
    DefinitionRegistry,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Runner,
    RunInspection,
    RunStatus,
    StepAttempt,
    StepCheckpoint,
    StepRecord,
    StepStatus,
    StepType,
    ToolCall,
    ToolEffect,
    ToolOutcome,
)
from m_agent.adapters import (
    DeterministicModelAdapter,
    DeterministicTool,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent.runtime import ModelRequirements, ToolCallingMode


MEASURED_RUNS = 100
DEFINITION_ID = "sqlite-benchmark"
DEFINITION_VERSION = "1.0"
EXPECTED_STEP_TYPES = (StepType.MODEL, StepType.TOOL, StepType.MODEL)


def compare_with_baseline(
    report: dict[str, Any], baseline: dict[str, Any]
) -> dict[str, Any]:
    """Compare only compatible correctness-validated local environments."""
    report_env = report.get("environment", {})
    baseline_env = baseline.get("environment", {})
    report_identity = report.get("baseline_identity", {})
    baseline_identity = baseline.get("baseline_identity", {})
    compatible = (
        report_env.get("python", {}).get("implementation")
        == baseline_env.get("python", {}).get("implementation")
        and report_env.get("python", {}).get("version")
        == baseline_env.get("python", {}).get("version")
        and report_env.get("os", {}).get("system")
        == baseline_env.get("os", {}).get("system")
        and report_env.get("cpu", {}).get("machine")
        == baseline_env.get("cpu", {}).get("machine")
        and report.get("validation", {}).get("status", "PASS") == "PASS"
        and baseline.get("validation", {}).get("status", "PASS") == "PASS"
        and report.get("measurement", {}).get("run_count")
        == baseline.get("measurement", {}).get("run_count")
        and bool(report_identity.get("artifact_digest"))
        and bool(report_identity.get("manifest_digest"))
        and report_identity == baseline_identity
    )
    result: dict[str, Any] = {
        "status": "COMPARED" if compatible else "INCONCLUSIVE",
        "qualification": "environment-qualified local evidence only",
        "non_claim": "This comparison is not production QPS, SLA, or capacity evidence.",
    }
    if not compatible:
        result["reason"] = "incompatible environment or correctness validation"
        return result
    current = report["metrics"]["throughput_runs_per_second"]
    previous = baseline["metrics"]["throughput_runs_per_second"]
    result["metrics"] = {
        "throughput_delta_ratio": (current - previous) / previous
        if previous
        else None,
    }
    return result


class BenchmarkValidationError(RuntimeError):
    """Correctness failed, so no performance metrics may be reported."""

    def __init__(self, validation: dict[str, Any]) -> None:
        self.payload = {"validation": validation}
        super().__init__(json.dumps(self.payload, sort_keys=True))


class BenchmarkModel(DeterministicModelAdapter):
    """Stateless deterministic adapter: one Tool Step, then final output."""

    def __init__(self) -> None:
        super().__init__(
            capabilities=ModelCapabilities(tool_calling=ToolCallingMode.NATIVE)
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if not request.tool_outcomes:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id=f"effect-{request.input}",
                        tool_name="record_effect",
                        arguments=json.dumps(
                            {"run_input": request.input}, sort_keys=True
                        ),
                    ),
                )
            )
        outcome = request.tool_outcomes[0]
        return ModelResponse(content=f"completed:{outcome.result}")


class CountingEffectTool(DeterministicTool):
    """Deterministic idempotent adapter with an inspectable effect ledger."""

    def __init__(self) -> None:
        super().__init__(
            name="record_effect",
            description="Record one deterministic benchmark effect.",
            effect=ToolEffect.IDEMPOTENT,
        )
        self.effects: dict[str, int] = defaultdict(int)

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        run_input = json.loads(request.arguments)["run_input"]
        self.effects[run_input] += 1
        return ToolOutcome.success(
            request.call_id,
            request.tool_name,
            result=f"effect-recorded:{run_input}",
        )


class TimedSQLiteRunStore(SQLiteRunStore):
    """SQLiteRunStore that times the three writes forming each Step."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, payload_codec=PlaintextPayloadCodec())
        self.step_write_seconds: dict[str, float] = defaultdict(float)

    async def record_step(
        self,
        step: StepRecord,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> StepRecord:
        started = time.perf_counter()
        try:
            return await super().record_step(
                step,
                expected_version=expected_version,
                lease_owner=lease_owner,
            )
        finally:
            self.step_write_seconds[step.step_id] += (
                time.perf_counter() - started
            )

    async def record_attempt(
        self,
        attempt: StepAttempt,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> StepAttempt:
        started = time.perf_counter()
        try:
            return await super().record_attempt(
                attempt,
                expected_version=expected_version,
                lease_owner=lease_owner,
            )
        finally:
            self.step_write_seconds[attempt.step_id] += (
                time.perf_counter() - started
            )

    async def record_checkpoint(
        self,
        checkpoint: StepCheckpoint,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> StepCheckpoint:
        started = time.perf_counter()
        try:
            return await super().record_checkpoint(
                checkpoint,
                expected_version=expected_version,
                lease_owner=lease_owner,
            )
        finally:
            self.step_write_seconds[checkpoint.step_id] += (
                time.perf_counter() - started
            )


class AsyncStartBarrier:
    """Release all benchmark workers only after every worker is ready."""

    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._ready = 0
        self._condition = asyncio.Condition()
        self._release = asyncio.Event()

    async def wait(self) -> None:
        async with self._condition:
            self._ready += 1
            self._condition.notify_all()
        await self._release.wait()

    async def release_when_ready(self) -> None:
        async with self._condition:
            await self._condition.wait_for(
                lambda: self._ready == self._parties
            )
        self._release.set()


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _environment() -> dict[str, Any]:
    uname = platform.uname()
    return {
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "os": {
            "system": uname.system,
            "release": uname.release,
            "version": uname.version,
        },
        "cpu": {
            "machine": uname.machine,
            "processor": uname.processor or platform.processor() or "unknown",
            "logical_count": os.cpu_count(),
        },
    }


def _database_evidence(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_violations = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"  # table names are fixed below
            ).fetchone()[0]
            for table in (
                "runs",
                "steps",
                "step_attempts",
                "step_checkpoints",
            )
        }
        orphan_counts = {
            "steps_without_run": connection.execute(
                "SELECT COUNT(*) FROM steps s LEFT JOIN runs r "
                "ON r.run_id=s.run_id WHERE r.run_id IS NULL"
            ).fetchone()[0],
            "attempts_without_step": connection.execute(
                "SELECT COUNT(*) FROM step_attempts a LEFT JOIN steps s "
                "ON s.step_id=a.step_id WHERE s.step_id IS NULL"
            ).fetchone()[0],
            "checkpoints_without_step": connection.execute(
                "SELECT COUNT(*) FROM step_checkpoints c LEFT JOIN steps s "
                "ON s.step_id=c.step_id WHERE s.step_id IS NULL"
            ).fetchone()[0],
        }
    return {
        "path": str(path),
        "journal_mode": journal_mode,
        "synchronous": synchronous,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_key_violations,
        "row_counts": counts,
        "orphan_counts": orphan_counts,
    }


def validate_inspection(inspection: RunInspection) -> list[str]:
    """Validate the one-to-one Step, Attempt, and Checkpoint relationships."""
    run = inspection.run
    problems: list[str] = []
    if run.status is not RunStatus.SUCCEEDED:
        problems.append(f"{run.run_id}: terminal status {run.status.value}")
    if tuple(step.step_type for step in inspection.steps) != EXPECTED_STEP_TYPES:
        problems.append(f"{run.run_id}: unexpected Step trajectory")

    steps_by_id = {step.step_id: step for step in inspection.steps}
    attempts_by_id = {attempt.attempt_id: attempt for attempt in inspection.attempts}
    checkpoints_by_step = {
        checkpoint.step_id: checkpoint for checkpoint in inspection.checkpoints
    }
    if len(steps_by_id) != len(inspection.steps):
        problems.append(f"{run.run_id}: duplicate Step identity")
    if len(attempts_by_id) != len(inspection.attempts):
        problems.append(f"{run.run_id}: duplicate Attempt identity")
    if len(checkpoints_by_step) != len(inspection.checkpoints):
        problems.append(f"{run.run_id}: duplicate checkpoint Step identity")

    attempts_by_step: dict[str, list[StepAttempt]] = defaultdict(list)
    for attempt in inspection.attempts:
        attempts_by_step[attempt.step_id].append(attempt)
        if attempt.run_id != run.run_id:
            problems.append(f"{run.run_id}: Attempt has wrong Run identity")
        if attempt.status is not StepStatus.SUCCEEDED:
            problems.append(f"{run.run_id}: non-succeeded Attempt")

    for step in inspection.steps:
        if step.run_id != run.run_id:
            problems.append(f"{run.run_id}: Step has wrong Run identity")
        if step.status is not StepStatus.SUCCEEDED:
            problems.append(f"{run.run_id}: non-succeeded Step")
        step_attempts = attempts_by_step.get(step.step_id, [])
        checkpoint = checkpoints_by_step.get(step.step_id)
        if len(step_attempts) != 1:
            problems.append(f"{run.run_id}: Step does not have one Attempt")
            continue
        if checkpoint is None:
            problems.append(f"{run.run_id}: Step does not have one Checkpoint")
            continue
        attempt = step_attempts[0]
        if checkpoint.run_id != run.run_id:
            problems.append(f"{run.run_id}: Checkpoint has wrong Run identity")
        if checkpoint.step_type is not step.step_type:
            problems.append(f"{run.run_id}: Checkpoint has wrong Step type")
        if checkpoint.attempt_id != attempt.attempt_id:
            problems.append(f"{run.run_id}: Checkpoint maps to wrong Attempt")

    if len(inspection.attempts) != len(EXPECTED_STEP_TYPES):
        problems.append(f"{run.run_id}: unexpected Attempt count")
    if len(inspection.checkpoints) != len(EXPECTED_STEP_TYPES):
        problems.append(f"{run.run_id}: unexpected checkpoint count")
    if set(attempts_by_step) != set(steps_by_id):
        problems.append(f"{run.run_id}: Attempt set does not match Step set")
    if set(checkpoints_by_step) != set(steps_by_id):
        problems.append(f"{run.run_id}: Checkpoint set does not match Step set")
    return problems


async def execute_benchmark(
    *,
    database_path: Path,
    run_count: int,
    expected_side_effects: int | None = None,
) -> dict[str, Any]:
    """Execute measured Runs, validate correctness, then calculate metrics."""
    if run_count < 1:
        raise ValueError("run_count must be positive")
    if database_path.exists():
        raise FileExistsError(
            f"refusing to overwrite existing benchmark database: {database_path}"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)

    store = TimedSQLiteRunStore(database_path)
    model = BenchmarkModel()
    tool = CountingEffectTool()
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id=DEFINITION_ID,
            version=DEFINITION_VERSION,
            instructions="Execute the deterministic benchmark tool once.",
            model_requirements=ModelRequirements(
                capabilities=ModelCapabilities(
                    tool_calling=ToolCallingMode.NATIVE
                )
            ),
            model_adapter=model,
            tools=(tool,),
        )
    )
    runner = Runner(registry=registry, store=store)
    barrier = AsyncStartBarrier(run_count)
    latencies: dict[str, float] = {}
    run_ids: list[str] = []

    async def run_one(index: int) -> None:
        run_input = f"benchmark-run-{index:03d}"
        await barrier.wait()
        started = time.perf_counter()
        created = await runner.create_run(
            DEFINITION_ID, DEFINITION_VERSION, input=run_input
        )
        run_ids.append(created.run_id)
        await runner.start_run(created.run_id)
        latencies[created.run_id] = time.perf_counter() - started

    tasks = [asyncio.create_task(run_one(index)) for index in range(run_count)]
    await barrier.release_when_ready()
    measured_started = time.perf_counter()
    await asyncio.gather(*tasks)
    measured_seconds = time.perf_counter() - measured_started

    problems: list[str] = []
    inspections = [await runner.inspect_run(run_id) for run_id in run_ids]
    for inspection in inspections:
        problems.extend(validate_inspection(inspection))

    database = _database_evidence(database_path)
    expected_effects = (
        run_count if expected_side_effects is None else expected_side_effects
    )
    actual_effects = sum(tool.effects.values())
    if len(tool.effects) != run_count or any(value != 1 for value in tool.effects.values()):
        problems.append("side effects were not exactly one per Run")
    if actual_effects != expected_effects:
        problems.append(
            f"expected {expected_effects} side effects, observed {actual_effects}"
        )
    expected_rows = {
        "runs": run_count,
        "steps": run_count * len(EXPECTED_STEP_TYPES),
        "step_attempts": run_count * len(EXPECTED_STEP_TYPES),
        "step_checkpoints": run_count * len(EXPECTED_STEP_TYPES),
    }
    if database["row_counts"] != expected_rows:
        problems.append("SQLite row counts do not match the public inspections")
    if database["integrity_check"] != "ok":
        problems.append("SQLite integrity_check did not return ok")
    if database["foreign_key_violations"] != 0:
        problems.append("SQLite foreign_key_check reported violations")
    if any(database["orphan_counts"].values()):
        problems.append("SQLite contains orphaned lifecycle records")

    validation = {
        "status": "FAIL" if problems else "PASS",
        "problems": problems,
        "terminal_runs": sum(
            inspection.run.status is RunStatus.SUCCEEDED
            for inspection in inspections
        ),
        "steps": sum(len(inspection.steps) for inspection in inspections),
        "attempts": sum(
            len(inspection.attempts) for inspection in inspections
        ),
        "checkpoints": sum(
            len(inspection.checkpoints) for inspection in inspections
        ),
        "side_effect_count": actual_effects,
    }
    if problems:
        store.close()
        raise BenchmarkValidationError(validation)

    latency_ms = [seconds * 1000 for seconds in latencies.values()]
    persistence_ms = [
        seconds * 1000 for seconds in store.step_write_seconds.values()
    ]
    report = {
        "schema_version": 1,
        "environment": _environment(),
        "baseline_identity": {
            "artifact_digest": os.environ.get("M_AGENT_BENCHMARK_ARTIFACT_DIGEST", ""),
            "manifest_digest": os.environ.get("M_AGENT_BENCHMARK_MANIFEST_DIGEST", ""),
        },
        "warmup": {
            "run_count": 0,
            "definition": "SQLite schema initialization before measurement",
        },
        "measurement": {
            "run_count": run_count,
            "concurrency": run_count,
            "definition": (
                "wall clock from barrier release through concurrent public "
                "Runner.create_run + Runner.start_run completion; validation "
                "and inspection excluded"
            ),
            "elapsed_seconds": measured_seconds,
        },
        "database": database,
        "validation": validation,
        "metrics": {
            "throughput_runs_per_second": run_count / measured_seconds,
            "run_latency_ms": {
                "p50": _percentile(latency_ms, 0.50),
                "p95": _percentile(latency_ms, 0.95),
            },
            "persistence_overhead_ms_per_step": {
                "mean": fmean(persistence_ms),
                "p50": _percentile(persistence_ms, 0.50),
                "p95": _percentile(persistence_ms, 0.95),
                "definition": (
                    "sum of record_step, record_attempt, and "
                    "record_checkpoint wall time for each completed Step"
                ),
            },
        },
        "qualification": (
            "Comparative local evidence only; not production QPS, latency, "
            "scalability, or availability proof."
        ),
    }
    store.close()
    return report


def format_report(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    latency = metrics["run_latency_ms"]
    persistence = metrics["persistence_overhead_ms_per_step"]
    database = report["database"]
    environment = report["environment"]
    return "\n".join(
        (
            "Durable Run SQLite benchmark",
            f"VALIDATION: {report['validation']['status']}",
            f"Runs/concurrency: {report['measurement']['run_count']}/"
            f"{report['measurement']['concurrency']}",
            f"Throughput: {metrics['throughput_runs_per_second']:.2f} Runs/s",
            f"Run latency P50/P95: {latency['p50']:.2f}/"
            f"{latency['p95']:.2f} ms",
            f"Persistence overhead per Step mean/P50/P95: "
            f"{persistence['mean']:.3f}/{persistence['p50']:.3f}/"
            f"{persistence['p95']:.3f} ms",
            f"Python: {environment['python']['implementation']} "
            f"{environment['python']['version']}",
            f"OS/CPU: {environment['os']['system']} "
            f"{environment['os']['release']} / "
            f"{environment['cpu']['machine']} "
            f"({environment['cpu']['logical_count']} logical CPUs)",
            f"SQLite: {database['path']} journal={database['journal_mode']} "
            f"synchronous={database['synchronous']}",
            f"Warmup: {report['warmup']['run_count']} Runs "
            f"({report['warmup']['definition']})",
            f"Measurement: {report['measurement']['definition']}",
            report["qualification"],
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run exactly 100 concurrent sessionless Agent Runs against SQLite."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="new SQLite database path (existing files are never overwritten)",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        required=True,
        help="path for the environment-qualified JSON result",
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(
            execute_benchmark(
                database_path=args.database,
                run_count=MEASURED_RUNS,
            )
        )
    except (BenchmarkValidationError, FileExistsError) as exc:
        print(f"BENCHMARK FAILED: {exc}", file=sys.stderr)
        return 1
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(format_report(report))
    print(f"JSON evidence: {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
