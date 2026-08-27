"""Correctness-first benchmark for the durable Eval regression workload.

The measured interval covers one public ``EvalExecutionEngine.run_suite``
call for a frozen offline Suite: every Suite item executes exactly one
deterministic subject Run and one hard evaluator. Correctness validation —
execution identity preservation, observation/result completeness, PASS
outcomes, reopen comparison, and SQLite integrity/row counts — runs after
the interval and is never included in the reported performance metrics.

The subject RunStore is an ``InMemoryRunStore`` (documented: the measured
persistence boundary is the append-only ``SQLiteEvalStore``, mirroring how
the eval-regression acceptance scenario drives the engine). No credentials,
provider clients, network services, or external databases are used.
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
from pathlib import Path
from statistics import fmean
from typing import Any

from m_agent.adapters import DeterministicModelAdapter, InMemoryRunStore, PlaintextPayloadCodec
from m_agent.companion.eval import (
    AgentVariant,
    EvalCase,
    EvalExecutionEngine,
    EvalExecutionRecord,
    EvalObservation,
    EvalSuite,
    EvaluatorResultRecord,
    EvidenceField,
    EvaluatorRef,
    ExecutionProtocol,
    FixtureBundle,
    ObservationProjectionPolicy,
    OutputMatchesEvaluator,
    SQLiteEvalStore,
)
from m_agent.runtime import AgentDefinition, DefinitionRegistry


MEASURED_ITEMS = 20
DEFINITION_ID = "eval-workload-assistant"
DEFINITION_VERSION = "1.0"
SUITE_ID = "eval-workload-suite"
SUITE_VERSION = "1.0"
VARIANT_ID = "primary"
EVALUATOR_ID = "eval-workload-hard-match"
_EXPECTED_OUTPUT = "ok"

_POLICY = ObservationProjectionPolicy(
    policy_id="eval-workload-projection",
    version="1.0",
    allowed_fields=frozenset({EvidenceField.RUN_OUTPUT}),
)


class EvalWorkloadValidationError(RuntimeError):
    """Correctness failed, so no performance metrics may be reported."""

    def __init__(self, validation: dict[str, Any]) -> None:
        self.payload = {"validation": validation}
        super().__init__(json.dumps(self.payload, sort_keys=True))


class TimedSQLiteEvalStore(SQLiteEvalStore):
    """SQLiteEvalStore that times the writes forming each Suite item."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.write_seconds: list[float] = []
        self.result_completions: list[float] = []

    async def record_execution(self, execution: EvalExecutionRecord) -> EvalExecutionRecord:
        started = time.perf_counter()
        try:
            return await super().record_execution(execution)
        finally:
            self.write_seconds.append(time.perf_counter() - started)

    async def record_observation(self, observation: EvalObservation) -> EvalObservation:
        started = time.perf_counter()
        try:
            return await super().record_observation(observation)
        finally:
            self.write_seconds.append(time.perf_counter() - started)

    async def record_evaluator_result(
        self, result: EvaluatorResultRecord
    ) -> EvaluatorResultRecord:
        started = time.perf_counter()
        try:
            return await super().record_evaluator_result(result)
        finally:
            elapsed = time.perf_counter() - started
            self.write_seconds.append(elapsed)
            self.result_completions.append(started + elapsed)


class EvalWorkloadModel(DeterministicModelAdapter):
    """Stateless deterministic adapter echoing the expected output."""

    deterministic: bool = True

    def __init__(self) -> None:
        super().__init__(responses=(_EXPECTED_OUTPUT,))


def _variant() -> AgentVariant:
    return AgentVariant(
        variant_id=VARIANT_ID,
        definition_id=DEFINITION_ID,
        definition_version=DEFINITION_VERSION,
    )


def _case(case_id: str) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        input=f"eval-workload-input-{case_id}",
        variant=_variant(),
        fixture_bundle=FixtureBundle.build(
            bundle_id=f"eval-workload-bundle-{case_id}",
            facts=(),
            declared_external_effects=(),
            expected_evidence_ids=(),
        ),
        execution_protocol=ExecutionProtocol(deterministic=True),
        evaluators=(EvaluatorRef(evaluator_id=EVALUATOR_ID, version="1.0"),),
    )


def _suite(case_count: int) -> EvalSuite:
    return EvalSuite(
        suite_id=SUITE_ID,
        version=SUITE_VERSION,
        cases=tuple(_case(f"case-{index:03d}") for index in range(case_count)),
        variants=(_variant(),),
    )


def _registry() -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id=DEFINITION_ID,
            version=DEFINITION_VERSION,
            instructions="eval workload subject",
            model_adapter=EvalWorkloadModel(),
            tools=[],
        )
    )
    return registry


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


def _eval_database_evidence(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"  # table names are fixed below
            ).fetchone()[0]
            for table in (
                "eval_executions",
                "eval_observations",
                "eval_execution_observations",
                "eval_evaluator_results",
            )
        }
    return {
        "path": str(path),
        "journal_mode": journal_mode,
        "synchronous": synchronous,
        "integrity_check": integrity,
        "row_counts": counts,
    }


async def execute_eval_workload(
    *, database: Path, case_count: int
) -> dict[str, Any]:
    """Execute the measured Suite, validate correctness, then report metrics."""
    if case_count < 1:
        raise ValueError("case_count must be positive")
    if database.exists():
        raise FileExistsError(
            f"refusing to overwrite existing benchmark database: {database}"
        )
    database.parent.mkdir(parents=True, exist_ok=True)

    suite = _suite(case_count)
    items = suite.expand()
    execution_id = EvalExecutionEngine.execution_identity(suite)
    store = TimedSQLiteEvalStore(database)
    engine = EvalExecutionEngine(
        registry=_registry(),
        run_store=InMemoryRunStore(PlaintextPayloadCodec()),
        eval_store=store,
        evaluators={EVALUATOR_ID: OutputMatchesEvaluator(
            evaluator_id=EVALUATOR_ID,
            version="1.0",
            expected=_EXPECTED_OUTPUT,
            hard=True,
        )},
        projection_policy=_POLICY,
    )
    try:
        measured_started = time.perf_counter()
        run = await engine.run_suite(suite)
        measured_seconds = time.perf_counter() - measured_started
        item_completions = list(store.result_completions)
        write_seconds = list(store.write_seconds)
    finally:
        store.close()

    problems: list[str] = []
    if run.execution.execution_id != execution_id:
        problems.append("execution_identity_not_preserved")
    if len(run.observations) != case_count:
        problems.append("observation_count_mismatch")
    if len(run.results) != case_count:
        problems.append("evaluator_result_count_mismatch")
    if any(result.outcome.value != "PASS" for result in run.results):
        problems.append("evaluator_results_not_pass")
    if len(item_completions) != case_count:
        problems.append("recorded_result_completions_mismatch")
    if not all(
        completion > measured_started for completion in item_completions
    ):
        problems.append("item_completions_outside_measured_interval")

    database_evidence = _eval_database_evidence(database)
    expected_rows = {
        "eval_executions": 1,
        "eval_observations": case_count,
        "eval_execution_observations": case_count,
        "eval_evaluator_results": case_count,
    }
    if database_evidence["row_counts"] != expected_rows:
        problems.append("eval SQLite row counts do not match the public views")
    if database_evidence["integrity_check"] != "ok":
        problems.append("eval SQLite integrity_check did not return ok")

    validation = {
        "status": "FAIL" if problems else "PASS",
        "problems": problems,
        "suite_items": len(items),
        "observations": len(run.observations),
        "evaluator_results": len(run.results),
        "execution_id": execution_id,
    }
    if problems:
        raise EvalWorkloadValidationError(validation)

    # Sequential engine: per-item latency is the completion interval between
    # consecutive recorded evaluator results (first item: from measurement
    # start). Persistence overhead is the summed wall time of the durable
    # writes attributed to each item.
    boundaries = [measured_started, *item_completions]
    item_latency_ms = [
        (boundaries[index + 1] - boundaries[index]) * 1000
        for index in range(case_count)
    ]
    persistence_ms = [
        seconds * 1000
        for seconds in write_seconds[1:]  # execution write is amortized setup
    ]
    if not persistence_ms:
        persistence_ms = [0.0]
    report = {
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
            "item_count": 0,
            "definition": "SQLite schema initialization before measurement",
        },
        "measurement": {
            "case_count": case_count,
            "evaluators_per_case": 1,
            "definition": (
                "wall clock of one public EvalExecutionEngine.run_suite call"
                " for the frozen Suite (sequential items); validation and"
                " inspection excluded"
            ),
            "elapsed_seconds": measured_seconds,
        },
        "eval_store": database_evidence,
        "validation": validation,
        "metrics": {
            "throughput_items_per_second": case_count / measured_seconds,
            "item_latency_ms": {
                "p50": _percentile(item_latency_ms, 0.50),
                "p95": _percentile(item_latency_ms, 0.95),
            },
            "persistence_overhead_ms_per_item": {
                "mean": fmean(persistence_ms),
                "p50": _percentile(persistence_ms, 0.50),
                "p95": _percentile(persistence_ms, 0.95),
                "definition": (
                    "wall time of record_observation and"
                    " record_evaluator_result per completed item"
                ),
            },
        },
        "qualification": (
            "Comparative environment-qualified local evidence only; not"
            " production QPS, latency, scalability, or capacity proof."
        ),
    }
    return report


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
        and report.get("measurement", {}).get("case_count")
        == baseline.get("measurement", {}).get("case_count")
        and bool(report_identity.get("artifact_digest"))
        and bool(report_identity.get("manifest_digest"))
        and report_identity == baseline_identity
    )
    result: dict[str, Any] = {
        "status": "COMPARED" if compatible else "INCONCLUSIVE",
        "qualification": "environment-qualified local evidence only",
        "non_claim": (
            "This comparison is not production QPS, SLA, or capacity"
            " evidence."
        ),
    }
    if not compatible:
        result["reason"] = "incompatible environment or correctness validation"
        return result
    current = report["metrics"]["throughput_items_per_second"]
    previous = baseline["metrics"]["throughput_items_per_second"]
    result["metrics"] = {
        "throughput_delta_ratio": (current - previous) / previous
        if previous
        else None,
    }
    return result


def format_report(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    latency = metrics["item_latency_ms"]
    persistence = metrics["persistence_overhead_ms_per_item"]
    environment = report["environment"]
    eval_store = report["eval_store"]
    measurement = report["measurement"]
    return "\n".join(
        (
            "Durable Eval regression SQLite benchmark",
            f"VALIDATION: {report['validation']['status']}",
            f"Suite items: {measurement['case_count']}",
            f"Throughput: {metrics['throughput_items_per_second']:.2f} items/s",
            f"Item latency P50/P95: {latency['p50']:.2f}/{latency['p95']:.2f} ms",
            f"Persistence overhead per item mean/P50/P95: "
            f"{persistence['mean']:.3f}/{persistence['p50']:.3f}/"
            f"{persistence['p95']:.3f} ms",
            f"Python: {environment['python']['implementation']} "
            f"{environment['python']['version']}",
            f"OS/CPU: {environment['os']['system']} "
            f"{environment['os']['release']} / "
            f"{environment['cpu']['machine']} "
            f"({environment['cpu']['logical_count']} logical CPUs)",
            f"SQLite: {eval_store['path']} journal={eval_store['journal_mode']} "
            f"synchronous={eval_store['synchronous']}",
            f"Warmup: {report['warmup']['item_count']} items "
            f"({report['warmup']['definition']})",
            f"Measurement: {measurement['definition']}",
            report["qualification"],
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the durable Eval regression workload against SQLite."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="new Eval SQLite database path (never overwritten)",
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
            execute_eval_workload(
                database=args.database,
                case_count=MEASURED_ITEMS,
            )
        )
    except (EvalWorkloadValidationError, FileExistsError, ValueError) as exc:
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
