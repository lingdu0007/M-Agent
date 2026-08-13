"""Ticket 12 contracts for the offline CI gate and SQLite benchmark."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.durable_run_sqlite import (
    MEASURED_RUNS,
    BenchmarkValidationError,
    execute_benchmark,
    format_report,
    validate_inspection,
)
from m_agent import (
    RunInspection,
    RunRecord,
    RunStatus,
    StepAttempt,
    StepCheckpoint,
    StepRecord,
    StepStatus,
    StepType,
)


ROOT = Path(__file__).resolve().parents[1]


class OfflineCIContractTests(unittest.TestCase):
    def test_offline_matrix_and_explicit_live_job_are_separate(self) -> None:
        offline = (ROOT / ".github/workflows/offline-ci.yml").read_text()
        live = (
            ROOT / ".github/workflows/live-provider-contracts.yml"
        ).read_text()

        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(f'"{version}"', offline)
        self.assertIn('M_AGENT_RUN_LIVE_TESTS: "0"', offline)
        self.assertIn("pytest -m \"not live\"", offline)
        self.assertIn("python -m unittest discover", offline)
        self.assertIn("python tests/test_live_model_adapters.py", offline)
        self.assertNotIn("secrets.", offline)

        self.assertIn("workflow_dispatch:", live)
        self.assertNotIn("pull_request:", live)
        self.assertNotIn("push:", live)
        self.assertIn('M_AGENT_RUN_LIVE_TESTS: "1"', live)
        self.assertIn("pytest -m live", live)

    def test_offline_gate_names_every_required_behavior_module(self) -> None:
        workflow = (ROOT / ".github/workflows/offline-ci.yml").read_text()
        for module in (
            "tests/test_m_agent_resume.py",
            "tests/test_m_agent_lease.py",
            "tests/test_m_agent_retry.py",
            "tests/test_m_agent_stream_cancel.py",
            "tests/test_m_agent_telemetry.py",
            "tests/test_m_agent_resolution.py",
            "tests/test_m_agent_tool_recovery.py",
            "tests/test_durable_support_agent_example.py",
        ):
            self.assertIn(module, workflow)


class SQLiteBenchmarkContractTests(unittest.TestCase):
    def test_public_benchmark_is_fixed_at_exactly_100_runs(self) -> None:
        self.assertEqual(MEASURED_RUNS, 100)

    def test_validation_rejects_mismatched_checkpoint_relationships(self) -> None:
        run = RunRecord(
            run_id="run-1",
            definition_id="benchmark",
            definition_version="1.0",
            input="input",
            status=RunStatus.SUCCEEDED,
        )
        steps = [
            StepRecord(
                step_id=f"step-{index}",
                run_id=run.run_id,
                step_type=step_type,
                status=StepStatus.SUCCEEDED,
            )
            for index, step_type in enumerate(
                (StepType.MODEL, StepType.TOOL, StepType.MODEL)
            )
        ]
        attempts = [
            StepAttempt(
                attempt_id=f"attempt-{index}",
                step_id=step.step_id,
                run_id=run.run_id,
                status=StepStatus.SUCCEEDED,
            )
            for index, step in enumerate(steps)
        ]
        checkpoints = [
            StepCheckpoint(
                step_id=steps[0].step_id,
                run_id=run.run_id,
                attempt_id=attempts[1].attempt_id,
                step_type=steps[0].step_type,
                output="output",
            ),
            StepCheckpoint(
                step_id=steps[1].step_id,
                run_id=run.run_id,
                attempt_id=attempts[1].attempt_id,
                step_type=steps[1].step_type,
                output="output",
            ),
            StepCheckpoint(
                step_id=steps[2].step_id,
                run_id=run.run_id,
                attempt_id=attempts[2].attempt_id,
                step_type=steps[2].step_type,
                output="output",
            ),
        ]
        problems = validate_inspection(
            RunInspection(
                run=run,
                steps=steps,
                attempts=attempts,
                checkpoints=checkpoints,
            )
        )
        self.assertIn("Checkpoint maps to wrong Attempt", " ".join(problems))

    def test_benchmark_validates_before_returning_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = asyncio.run(
                execute_benchmark(
                    database_path=Path(tmp) / "benchmark.sqlite",
                    run_count=4,
                )
            )

        self.assertEqual(report["validation"]["status"], "PASS")
        self.assertEqual(report["measurement"]["run_count"], 4)
        self.assertEqual(report["measurement"]["concurrency"], 4)
        self.assertEqual(report["validation"]["terminal_runs"], 4)
        self.assertEqual(report["validation"]["side_effect_count"], 4)
        self.assertEqual(report["validation"]["steps"], 12)
        self.assertEqual(report["validation"]["attempts"], 12)
        self.assertEqual(report["validation"]["checkpoints"], 12)
        self.assertEqual(report["database"]["integrity_check"], "ok")
        self.assertEqual(report["database"]["foreign_key_violations"], 0)
        self.assertGreater(report["metrics"]["throughput_runs_per_second"], 0)
        self.assertGreaterEqual(report["metrics"]["run_latency_ms"]["p95"], 0)
        self.assertGreaterEqual(
            report["metrics"]["persistence_overhead_ms_per_step"]["mean"],
            0,
        )
        self.assertEqual(report["warmup"]["run_count"], 0)
        self.assertIn("python", report["environment"])
        self.assertIn("os", report["environment"])
        self.assertIn("cpu", report["environment"])
        self.assertIn("journal_mode", report["database"])

        rendered = format_report(report)
        self.assertIn("VALIDATION: PASS", rendered)
        self.assertIn("Run latency P50/P95", rendered)
        self.assertIn("Persistence overhead per Step", rendered)

    def test_failed_validation_exposes_no_performance_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BenchmarkValidationError) as caught:
                asyncio.run(
                    execute_benchmark(
                        database_path=Path(tmp) / "benchmark.sqlite",
                        run_count=3,
                        expected_side_effects=99,
                    )
                )

        payload = json.loads(str(caught.exception))
        self.assertEqual(payload["validation"]["status"], "FAIL")
        self.assertNotIn("metrics", payload)


if __name__ == "__main__":
    unittest.main()
