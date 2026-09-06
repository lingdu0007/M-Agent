"""Validate the four frozen release workloads, not arbitrary benchmark reports."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from m_agent.testing import AcceptanceManifest

WORKLOADS = {
    "durable-run": "durable_run_sqlite.py",
    "session": "session_workload.py",
    "context": "context_workload.py",
    "eval": "eval_workload.py",
}

# Independent expectations for the existing scripts' frozen default workloads.
COUNTS = {
    "durable-run": {
        "measurement": {"run_count": 100, "concurrency": 100},
        "validation": {
            "terminal_runs": 100,
            "steps": 300,
            "attempts": 300,
            "checkpoints": 300,
            "side_effect_count": 100,
        },
        "database": {
            "integrity_check": "ok",
            "foreign_key_violations": 0,
            "orphan_counts": {
                "attempts_without_step": 0,
                "checkpoints_without_step": 0,
                "steps_without_run": 0,
            },
            "row_counts": {
                "runs": 100,
                "steps": 300,
                "step_attempts": 300,
                "step_checkpoints": 300,
            },
        },
    },
    "session": {
        "measurement": {
            "concurrency": 10,
            "session_count": 10,
            "turn_count": 100,
            "turns_per_session": 10,
        },
        "validation": {"sessions": 10, "terminal_runs": 100, "turns": 100},
        "session_database": {
            "integrity_check": "ok",
            "foreign_scope_turns": 0,
            "orphan_counts": {"claims_without_session": 0, "turns_without_session": 0},
            "row_counts": {"session_claims": 0, "session_turns": 100, "sessions": 10},
        },
        "run_database": {"integrity_check": "ok", "row_counts": {"runs": 100}},
    },
    "context": {
        "measurement": {"run_count": 30},
        "validation": {
            "runs": 30,
            "succeeded_runs": 30,
            "checkpoint_order_clean_runs": 30,
            "compression_provenance_intact_runs": 30,
        },
        "run_database": {"integrity_check": "ok", "row_counts": {"runs": 30}},
    },
    "eval": {
        "measurement": {"case_count": 20, "evaluators_per_case": 1},
        "validation": {"suite_items": 20, "observations": 20, "evaluator_results": 20},
        "eval_store": {
            "integrity_check": "ok",
            "row_counts": {
                "eval_evaluator_results": 20,
                "eval_execution_observations": 20,
                "eval_executions": 1,
                "eval_observations": 20,
            },
        },
    },
}
UNITS = {
    "durable-run": ("run", "step"),
    "session": ("turn", "turn"),
    "context": ("run", "run"),
    "eval": ("item", "item"),
}


def require_fields(actual: Any, expected: Any, location: str) -> None:
    if type(actual) is not type(expected):
        raise ValueError(f"benchmark field has wrong type: {location}")
    if isinstance(expected, dict):
        for key, value in expected.items():
            if key not in actual:
                raise ValueError(f"benchmark field missing: {location}.{key}")
            require_fields(actual[key], value, f"{location}.{key}")
    elif actual != expected:
        raise ValueError(f"benchmark field has unexpected value: {location}")


def number(value: Any, *, positive: bool = False) -> float:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        raise ValueError("benchmark metric must be finite and nonnegative/positive")
    return float(value)


def text(value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("benchmark description or environment field is missing")


def validate_report(
    name: str, report: dict[str, Any], manifest: AcceptanceManifest
) -> None:
    require_fields(report, COUNTS[name], name)
    require_fields(
        report,
        {"schema_version": 1, "validation": {"status": "PASS", "problems": []}},
        name,
    )
    if report["baseline_identity"] != {
        "artifact_digest": manifest.artifact_digest,
        "manifest_digest": manifest.digest,
    }:
        raise ValueError("benchmark candidate binding failed")
    environment = report["environment"]
    if (
        environment["python"]["version"] != manifest.environment["python"]
        or environment["os"]["system"].lower() != manifest.environment["os"]
        or environment["cpu"]["machine"].lower() != manifest.environment["architecture"]
    ):
        raise ValueError("benchmark environment differs from the primary host")
    text(environment["python"]["implementation"])
    text(environment["os"]["release"])
    text(environment["os"]["version"])
    number(environment["cpu"]["logical_count"], positive=True)
    text(report["qualification"])
    if "not production" not in report["qualification"]:
        raise ValueError("benchmark lacks its capacity non-claim")
    text(report["measurement"]["definition"])
    number(report["measurement"]["elapsed_seconds"], positive=True)
    unit, persistence_unit = UNITS[name]
    require_fields(report["warmup"], {f"{unit}_count": 0}, f"{name}.warmup")
    text(report["warmup"]["definition"])
    metrics = report["metrics"]
    number(metrics[f"throughput_{unit}s_per_second"], positive=True)
    latency = metrics[f"{unit}_latency_ms"]
    overhead = metrics[f"persistence_overhead_ms_per_{persistence_unit}"]
    for distribution in (latency, overhead):
        if number(distribution["p95"]) < number(distribution["p50"]):
            raise ValueError("benchmark percentiles are out of order")
    number(overhead["mean"])
    text(overhead["definition"])
    if name == "eval":
        text(report["validation"]["execution_id"])


def benchmark_index(directory: Path, manifest: AcceptanceManifest) -> dict[str, Any]:
    expected_files = {f"{name}.json" for name in WORKLOADS}
    if {path.name for path in directory.glob("*.json")} - {
        "index.json"
    } != expected_files:
        raise ValueError("benchmark attachment set is incomplete or unexpected")
    digests = {}
    for name in WORKLOADS:
        path = directory / f"{name}.json"
        content = path.read_bytes()
        validate_report(name, json.loads(content), manifest)
        digests[path.name] = "sha256:" + hashlib.sha256(content).hexdigest()
    return {
        "schema_version": 1,
        "artifact_digest": manifest.artifact_digest,
        "manifest_digest": manifest.digest,
        "reports": digests,
    }


def verify_benchmarks(directory: Path, manifest: AcceptanceManifest) -> None:
    recorded = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    if recorded != benchmark_index(directory, manifest):
        raise ValueError("benchmark attachment digest index does not match")
