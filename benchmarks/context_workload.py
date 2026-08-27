"""Correctness-first benchmark for explicit Context compression workloads.

The measured interval covers public ``Runner.start_run`` calls for every
workload Run: each Run freezes a RUN_INPUT ``ContextPlan`` whose PROVIDE
stage yields two compressible articles plus one protected item, then an
explicit ``ModelPurpose.CONTEXT_COMPRESSION`` Model Step, then the business
``PRIMARY`` Model Step. Integrity validation (terminal status, checkpoint
order, compression provenance), SQLite validation, and reopen inspection run
after the interval and are never included in the reported metrics.
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

from m_agent.adapters import (
    DeterministicContextProvider,
    DeterministicModelAdapter,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent.runtime import (
    AgentDefinition,
    CompressionContract,
    CompressionResult,
    ContextItem,
    ContextPlan,
    ContextScope,
    ContextStage,
    ContextStageIdentity,
    ContextTransformType,
    DefinitionRegistry,
    ModelBinding,
    ModelBindingSet,
    ModelCapabilities,
    ModelContract,
    ModelLimits,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
    RevisionStability,
    Runner,
    compression_step_id,
)


MEASURED_RUNS = 30
DEFINITION_ID = "context-workload"
DEFINITION_VERSION = "1.0"
ALLOWED_SOURCE = "docs:articles"
PROTECTED_SOURCE = "internal:protected"

WORKLOAD_CONTRACT = CompressionContract(
    contract_id="workload-summarize",
    version="1",
    instructions="Summarize the workload articles and retain key facts only.",
    allowed_sources=(ALLOWED_SOURCE,),
    retained_categories=("facts", "entities"),
    omitted_categories=("verbatim-text",),
    derived_categories=("summary",),
    max_output_items=1,
)
EXPECTED_CHECKPOINT_LABELS = [
    ["CONTEXT", "PROVIDER"],
    ["MODEL", "CONTEXT_COMPRESSION"],
    ["MODEL", "PRIMARY"],
]


class ContextWorkloadValidationError(RuntimeError):
    """Correctness failed, so no performance metrics may be reported."""

    def __init__(self, validation: dict[str, Any]) -> None:
        self.payload = {"validation": validation}
        super().__init__(json.dumps(self.payload, sort_keys=True))


class WorkloadCompressionAdapter(DeterministicModelAdapter):
    """Deterministic compressor emitting one derived summary Item."""

    deterministic: bool = True

    def __init__(self, *, model_contract: ModelContract | None = None) -> None:
        super().__init__(responses=("",), model_contract=model_contract)

    def _fingerprint_excluded_state(self) -> frozenset[str]:
        return frozenset({"_last_request", "call_count"})

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        payload = {
            "items": [
                {
                    "item_id": "workload-summary-1",
                    "content": "condensed workload summary",
                    "source_item_ids": [
                        item.item_id for item in request.context_items
                    ],
                }
            ]
        }
        return ModelResponse(content=json.dumps(payload))


class WorkloadBusinessAdapter(DeterministicModelAdapter):
    """Deterministic business model producing the final answer."""

    deterministic: bool = True

    def __init__(self, *, model_contract: ModelContract | None = None) -> None:
        super().__init__(
            responses=("workload final answer",), model_contract=model_contract
        )

    def _fingerprint_excluded_state(self) -> frozenset[str]:
        return frozenset({"_last_request", "call_count"})


class TimedSQLiteRunStore(SQLiteRunStore):
    """SQLiteRunStore that attributes every write to one workload Run."""

    def __init__(self, path: Path) -> None:
        super().__init__(path, payload_codec=PlaintextPayloadCodec())
        self.write_seconds: dict[str, float] = defaultdict(float)

    def _timed(self, run_id: str, started: float) -> None:
        self.write_seconds[run_id] += time.perf_counter() - started

    async def create_run(self, run):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            return await super().create_run(run)
        finally:
            self._timed(run.run_id, started)

    async def transition_run(self, run_id, *args, **kwargs):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            return await super().transition_run(run_id, *args, **kwargs)
        finally:
            self._timed(run_id, started)

    async def record_step(self, step, *args, **kwargs):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            return await super().record_step(step, *args, **kwargs)
        finally:
            self._timed(step.run_id, started)

    async def record_attempt(self, attempt, *args, **kwargs):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            return await super().record_attempt(attempt, *args, **kwargs)
        finally:
            self._timed(attempt.run_id, started)

    async def record_checkpoint(self, checkpoint, *args, **kwargs):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            return await super().record_checkpoint(checkpoint, *args, **kwargs)
        finally:
            self._timed(checkpoint.run_id, started)

    async def record_policy_decision(self, record, *args, **kwargs):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        try:
            return await super().record_policy_decision(record, *args, **kwargs)
        finally:
            self._timed(record.run_id, started)


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


def _model_contract(contract_id: str) -> ModelContract:
    return ModelContract(
        contract_id=contract_id,
        version="1",
        revision_stability=RevisionStability.PINNED,
        model_identity=f"deterministic:{contract_id}",
        capabilities=ModelCapabilities(),
        limits=ModelLimits(context_window_tokens=1_000_000, max_output_tokens=100_000),
        input_sizer_id="deterministic-v1",
        serialization_id="deterministic-text-v1",
    )


def _workload_items() -> tuple[ContextItem, ...]:
    return (
        ContextItem(
            item_id="doc-1", content="first workload article " * 24,
            source=ALLOWED_SOURCE,
        ),
        ContextItem(
            item_id="doc-2", content="second workload article " * 24,
            source=ALLOWED_SOURCE,
        ),
        ContextItem(
            item_id="prot-1", content="protected workload evidence " * 6,
            source=PROTECTED_SOURCE,
        ),
    )


def _run_input_plan(index: int) -> ContextPlan:
    """One deterministic RUN_INPUT PROVIDE plan per workload Run.

    与 compression 契约 revision 同理：stage step id 由
    ``ctx:{stage_id}:{scope}:{boundary}`` 决定，同一数据库内多次 Run
    需要各自的 stage_id 才能落盘各自的 Stage checkpoint。
    """
    return ContextPlan(
        stages=(
            ContextStage(
                identity=ContextStageIdentity(
                    stage_id=f"workload-provide-{index:03d}",
                    scope=ContextScope.RUN_INPUT,
                    transform_type=ContextTransformType.PROVIDE,
                )
            ),
        )
    )


def _workload_contract_revision(index: int) -> CompressionContract:
    """One deterministic contract revision per workload Run.

    ``step_checkpoints`` 以 step id 为主键，而 compression step id 由契约
    身份决定（``compression:{contract_id}:{version}``）；同一数据库内
    多次 Run 必须使用不同 revision 才能各自落盘 compression checkpoint。
    """
    return WORKLOAD_CONTRACT.model_copy(update={"version": str(index)})


def _workload_definition(index: int) -> AgentDefinition:
    contract = _workload_contract_revision(index)
    compression = WorkloadCompressionAdapter(
        model_contract=_model_contract(f"workload-compression-{index:03d}")
    )
    business = WorkloadBusinessAdapter(
        model_contract=_model_contract(f"workload-business-{index:03d}")
    )
    primary = ModelBinding(
        purpose=ModelPurpose.PRIMARY,
        contract=business.model_contract,
    )
    definition = AgentDefinition(
        definition_id=f"{DEFINITION_ID}-{index:03d}",
        version=DEFINITION_VERSION,
        instructions="Answer using the provided workload context.",
        model_bindings=ModelBindingSet(
            bindings=(
                primary,
                ModelBinding(
                    purpose=ModelPurpose.CONTEXT_COMPRESSION,
                    contract=compression.model_contract,
                ),
                primary.model_copy(
                    update={
                        "purpose": ModelPurpose.OUTPUT_REPAIR,
                        "source_purpose": ModelPurpose.PRIMARY,
                    }
                ),
            )
        ),
        model_adapter=business,
        model_adapters={ModelPurpose.CONTEXT_COMPRESSION: compression},
        context_provider=DeterministicContextProvider(items=_workload_items()),
        compression_contract=contract,
        context_plan=_run_input_plan(index),
    )
    return definition


def _registry(run_count: int) -> DefinitionRegistry:
    registry = DefinitionRegistry()
    for index in range(run_count):
        registry.register(_workload_definition(index))
    return registry


def _provenance_intact(record: dict[str, Any]) -> bool:
    sources = set(record.get("compression_source_ids") or ())
    derived = record.get("derived_source_ids") or ()
    return (
        bool(derived)
        and set(derived) <= sources
        and record.get("compression_contract_id") == WORKLOAD_CONTRACT.contract_id
    )


def _checkpoint_order_clean(record: dict[str, Any]) -> bool:
    return record.get("checkpoint_labels") == EXPECTED_CHECKPOINT_LABELS


def validate_run_integrity(record: dict[str, Any]) -> list[str]:
    """Validate one workload Run's terminal status, order, and provenance."""
    problems: list[str] = []
    if record.get("status") != "SUCCEEDED":
        problems.append("run_status_not_succeeded")
    if not _checkpoint_order_clean(record):
        problems.append("checkpoint_order_violated")
    if record.get("compression_contract_id") != WORKLOAD_CONTRACT.contract_id:
        problems.append("compression_contract_mismatch")
    sources = set(record.get("compression_source_ids") or ())
    derived = record.get("derived_source_ids") or ()
    if not derived or not set(derived) <= sources:
        problems.append("compression_provenance_violated")
    return problems


async def _run_record(
    runner: Runner, run_id: str, compression_step: str
) -> dict[str, Any]:
    """Read one Run's public integrity record via ``inspect_run``."""
    inspection = await runner.inspect_run(run_id)
    attempts = {attempt.attempt_id: attempt for attempt in inspection.attempts}
    labels: list[list[str]] = []
    compression: CompressionResult | None = None
    source_ids: list[str] = []
    for checkpoint in inspection.checkpoints:
        attempt = attempts.get(checkpoint.attempt_id)
        purpose = (
            attempt.model_purpose.value
            if attempt is not None and attempt.model_purpose is not None
            else "PROVIDER"
        )
        labels.append([checkpoint.step_type.value, purpose])
        if checkpoint.step_id == compression_step:
            envelope = json.loads(checkpoint.output)
            compression = CompressionResult.deserialize(envelope["content"])
    if compression is not None:
        source_ids = list(compression.source_item_ids)
        derived = [
            list(item.provenance.source_item_ids)
            for item in compression.output_items
        ]
        derived_ids = sorted({value for values in derived for value in values})
    else:
        derived_ids = []
    return {
        "status": inspection.run.status.value,
        "checkpoint_labels": labels,
        "compression_contract_id": compression.contract_id
        if compression is not None
        else None,
        "compression_source_ids": source_ids,
        "derived_source_ids": derived_ids,
    }


def _run_database_evidence(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        runs = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    return {"path": str(path), "integrity_check": integrity, "row_counts": {"runs": runs}}


async def execute_context_workload(
    *,
    database: Path,
    run_count: int,
) -> dict[str, Any]:
    """Execute measured compression Runs, validate correctness, then metrics."""
    if run_count < 1:
        raise ValueError("run_count must be positive")
    if database.exists():
        raise FileExistsError(
            f"refusing to overwrite existing benchmark database: {database}"
        )
    database.parent.mkdir(parents=True, exist_ok=True)

    registry = _registry(run_count)
    store = TimedSQLiteRunStore(database)
    runner = Runner(registry=registry, store=store)
    latencies: dict[str, float] = {}
    run_steps: dict[str, str] = {}
    measured_started = time.perf_counter()
    for index in range(run_count):
        definition_id = f"{DEFINITION_ID}-{index:03d}"
        compression_step = compression_step_id(_workload_contract_revision(index))
        created = await runner.create_run(
            definition_id,
            DEFINITION_VERSION,
            input=f"workload run input {index}",
        )
        run_steps[created.run_id] = compression_step
        started = time.perf_counter()
        await runner.start_run(created.run_id)
        latencies[created.run_id] = time.perf_counter() - started
    measured_seconds = time.perf_counter() - measured_started

    records = {
        run_id: await _run_record(runner, run_id, run_steps[run_id])
        for run_id in latencies
    }
    store.close()

    problems: list[str] = []
    for run_id, record in records.items():
        for problem in validate_run_integrity(record):
            problems.append(f"{run_id}: {problem}")
    succeeded_runs = sum(
        record["status"] == "SUCCEEDED" for record in records.values()
    )
    provenance_intact_runs = sum(
        _provenance_intact(record) for record in records.values()
    )
    checkpoint_clean_runs = sum(
        _checkpoint_order_clean(record) for record in records.values()
    )
    run_database_evidence = _run_database_evidence(database)
    if run_database_evidence["integrity_check"] != "ok":
        problems.append("run SQLite integrity_check did not return ok")
    if run_database_evidence["row_counts"]["runs"] != run_count:
        problems.append("run SQLite row counts do not match the public views")

    validation = {
        "status": "FAIL" if problems else "PASS",
        "problems": problems,
        "runs": run_count,
        "succeeded_runs": succeeded_runs,
        "compression_provenance_intact_runs": provenance_intact_runs,
        "checkpoint_order_clean_runs": checkpoint_clean_runs,
    }
    if problems:
        raise ContextWorkloadValidationError(validation)

    latency_ms = [seconds * 1000 for seconds in latencies.values()]
    persistence_ms = [
        seconds * 1000
        for run_id, seconds in store.write_seconds.items()
        if run_id in latencies
    ]
    return {
        "schema_version": 1,
        "environment": _environment(),
        "baseline_identity": {
            "artifact_digest": os.environ.get(
                "M_AGENT_BENCHMARK_ARTIFACT_DIGEST", ""
            ),
            "manifest_digest": os.environ.get(
                "M_AGENT_BENCHMARK_MANIFEST_DIGEST", ""
            ),
        },
        "warmup": {
            "run_count": 0,
            "definition": "SQLite schema initialization before measurement",
        },
        "measurement": {
            "run_count": run_count,
            "definition": (
                "wall clock covering public Runner.start_run for every "
                "workload Run; validation and inspection excluded"
            ),
            "elapsed_seconds": measured_seconds,
        },
        "run_database": run_database_evidence,
        "validation": validation,
        "metrics": {
            "throughput_runs_per_second": run_count / measured_seconds,
            "run_latency_ms": {
                "p50": _percentile(latency_ms, 0.50),
                "p95": _percentile(latency_ms, 0.95),
            },
            "persistence_overhead_ms_per_run": {
                "mean": fmean(persistence_ms),
                "p50": _percentile(persistence_ms, 0.50),
                "p95": _percentile(persistence_ms, 0.95),
                "definition": (
                    "summed create/transition/step/attempt/checkpoint/"
                    "policy-decision write wall time for each workload Run"
                ),
            },
        },
        "qualification": (
            "Comparative, environment-qualified local evidence only; not "
            "production QPS, latency, scalability, or availability proof."
        ),
    }


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
        "non_claim": (
            "This comparison is not production QPS, SLA, or capacity evidence."
        ),
    }
    if not compatible:
        result["reason"] = "incompatible environment or correctness validation"
        return result
    current = report["metrics"]["throughput_runs_per_second"]
    previous = baseline["metrics"]["throughput_runs_per_second"]
    result["metrics"] = {
        "throughput_delta_ratio": (current - previous) / previous if previous else None
    }
    return result


def format_report(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    latency = metrics["run_latency_ms"]
    persistence = metrics["persistence_overhead_ms_per_run"]
    environment = report["environment"]
    measurement = report["measurement"]
    database = report["run_database"]
    return "\n".join(
        (
            "Explicit Context compression SQLite benchmark",
            f"VALIDATION: {report['validation']['status']}",
            f"Runs: {measurement['run_count']}",
            f"Throughput: {metrics['throughput_runs_per_second']:.2f} Runs/s",
            f"Run latency P50/P95: {latency['p50']:.2f}/{latency['p95']:.2f} ms",
            f"Persistence overhead per Run mean/P50/P95: "
            f"{persistence['mean']:.3f}/{persistence['p50']:.3f}/"
            f"{persistence['p95']:.3f} ms",
            f"Python: {environment['python']['implementation']} "
            f"{environment['python']['version']}",
            f"OS/CPU: {environment['os']['system']} "
            f"{environment['os']['release']} / "
            f"{environment['cpu']['machine']} "
            f"({environment['cpu']['logical_count']} logical CPUs)",
            f"SQLite: {database['path']} runs={database['row_counts']['runs']}",
            f"Warmup: {report['warmup']['run_count']} Runs "
            f"({report['warmup']['definition']})",
            f"Measurement: {measurement['definition']}",
            report["qualification"],
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run explicit Context compression workload against SQLite."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="new Run SQLite database path (never overwritten)",
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
            execute_context_workload(
                database=args.database,
                run_count=MEASURED_RUNS,
            )
        )
    except (ContextWorkloadValidationError, FileExistsError, ValueError) as exc:
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
