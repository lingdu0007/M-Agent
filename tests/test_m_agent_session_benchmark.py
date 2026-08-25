"""contracts for the durable Session conversation benchmark."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from benchmarks.session_workload import (
    MEASURED_SESSIONS,
    TURNS_PER_SESSION,
    SessionWorkloadValidationError,
    compare_with_baseline,
    execute_session_workload,
    format_report,
    validate_session_history,
)
from m_agent.companion import SessionSnapshot, SessionTurn


def _turn(run_id: str, user_input: str, *, definition_id: str = "session-workload") -> SessionTurn:
    return SessionTurn(
        turn_id=f"turn-{run_id}",
        session_id="session-1",
        run_id=run_id,
        definition_id=definition_id,
        definition_version="1.0",
        user_input=user_input,
        assistant_output=f"acknowledged:{user_input}",
        created_at=datetime(2026, 8, 25, 12, 0, 0),
    )


def _snapshot(turns, *, version=None) -> SessionSnapshot:
    return SessionSnapshot(
        session_id="session-1",
        version=len(turns) if version is None else version,
        turns=tuple(turns),
        next_cursor=None,
    )


class SessionBenchmarkContractTests(unittest.TestCase):
    def test_public_benchmark_is_fixed_at_exactly_100_turns(self) -> None:
        self.assertEqual(MEASURED_SESSIONS, 10)
        self.assertEqual(TURNS_PER_SESSION, 10)
        self.assertEqual(MEASURED_SESSIONS * TURNS_PER_SESSION, 100)

    def test_clean_history_validates_without_problems(self) -> None:
        snapshot = _snapshot(
            [_turn("run-1", "turn-000-00"), _turn("run-2", "turn-000-01")]
        )
        self.assertEqual(
            validate_session_history(
                snapshot, expected_inputs=["turn-000-00", "turn-000-01"]
            ),
            [],
        )

    def test_version_mismatch_is_a_validation_problem(self) -> None:
        snapshot = _snapshot([_turn("run-1", "turn-000-00")], version=7)
        problems = validate_session_history(
            snapshot, expected_inputs=["turn-000-00"]
        )
        self.assertTrue(any("version" in problem for problem in problems))

    def test_duplicate_run_identity_is_a_validation_problem(self) -> None:
        snapshot = _snapshot(
            [_turn("run-1", "turn-000-00"), _turn("run-1", "turn-000-01")]
        )
        problems = validate_session_history(
            snapshot, expected_inputs=["turn-000-00", "turn-000-01"]
        )
        self.assertTrue(any("duplicate" in problem for problem in problems))

    def test_mutated_conversation_text_is_a_validation_problem(self) -> None:
        snapshot = _snapshot(
            [
                _turn("run-1", "turn-000-00"),
                _turn("run-2", "tampered input"),
            ]
        )
        problems = validate_session_history(
            snapshot, expected_inputs=["turn-000-00", "turn-000-01"]
        )
        self.assertTrue(any("mismatch" in problem for problem in problems))

    def test_unexpected_definition_binding_is_a_validation_problem(self) -> None:
        snapshot = _snapshot([_turn("run-1", "turn-000-00", definition_id="other")])
        problems = validate_session_history(
            snapshot, expected_inputs=["turn-000-00"]
        )
        self.assertTrue(any("definition" in problem for problem in problems))


class SessionWorkloadExecutionTests(unittest.TestCase):
    """Correctness-first execution of a small public workload."""

    def test_workload_validates_history_version_and_claim_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = asyncio.run(
                execute_session_workload(
                    session_database=root / "sessions.sqlite3",
                    run_database=root / "runs.sqlite3",
                    session_count=3,
                    turns_per_session=2,
                )
            )
        self.assertEqual(report["validation"]["status"], "PASS")
        self.assertEqual(report["validation"]["problems"], [])
        self.assertEqual(report["validation"]["terminal_runs"], 6)
        self.assertEqual(report["measurement"]["turn_count"], 6)
        self.assertEqual(
            report["session_database"]["row_counts"],
            {"sessions": 3, "session_claims": 0, "session_turns": 6},
        )
        self.assertEqual(report["run_database"]["row_counts"], {"runs": 6})
        self.assertEqual(report["session_database"]["integrity_check"], "ok")
        self.assertEqual(report["session_database"]["foreign_scope_turns"], 0)
        self.assertIn("throughput_turns_per_second", report["metrics"])
        self.assertIn("turn_latency_ms", report["metrics"])
        self.assertIn("persistence_overhead_ms_per_turn", report["metrics"])
        self.assertIn("not production", report["qualification"])
        rendered = format_report(report)
        self.assertIn("VALIDATION: PASS", rendered)
        self.assertIn("Durable Session conversation SQLite benchmark", rendered)

    def test_existing_database_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_database = root / "sessions.sqlite3"
            session_database.write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                asyncio.run(
                    execute_session_workload(
                        session_database=session_database,
                        run_database=root / "runs.sqlite3",
                        session_count=1,
                        turns_per_session=1,
                    )
                )

    def test_invalid_arguments_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                asyncio.run(
                    execute_session_workload(
                        session_database=root / "sessions.sqlite3",
                        run_database=root / "runs.sqlite3",
                        session_count=0,
                        turns_per_session=1,
                    )
                )


class SessionBenchmarkBaselineTests(unittest.TestCase):
    """Correctness-first benchmark evidence stays environment-qualified."""

    @staticmethod
    def _report(
        python: str = "3.11.9",
        system: str = "Darwin",
        machine: str = "arm64",
        validation: str = "PASS",
        turn_count: int = 100,
        identity: dict | None = None,
    ) -> dict:
        return {
            "environment": {
                "python": {"implementation": "CPython", "version": python},
                "os": {"system": system, "release": "23.0.0", "version": "23.0.0"},
                "cpu": {"machine": machine, "processor": "arm", "logical_count": 10},
            },
            "baseline_identity": identity
            or {
                "artifact_digest": "sha256:" + "a" * 64,
                "manifest_digest": "sha256:" + "b" * 64,
            },
            "measurement": {"turn_count": turn_count},
            "validation": {"status": validation},
            "metrics": {"throughput_turns_per_second": 120.0},
        }

    def test_compatible_baseline_reports_relative_comparison(self) -> None:
        baseline = self._report()
        baseline["metrics"]["throughput_turns_per_second"] = 100.0
        comparison = compare_with_baseline(self._report(), baseline)
        self.assertEqual(comparison["status"], "COMPARED")
        self.assertAlmostEqual(
            comparison["metrics"]["throughput_delta_ratio"], 0.2
        )
        self.assertIn("non_claim", comparison)

    def test_incompatible_baseline_is_inconclusive(self) -> None:
        for baseline in (
            self._report(python="3.12.1"),
            self._report(system="Linux"),
            self._report(machine="x86_64"),
            self._report(turn_count=50),
            self._report(validation="FAIL"),
            self._report(identity={"artifact_digest": "", "manifest_digest": ""}),
        ):
            with self.subTest(baseline=baseline["baseline_identity"]):
                comparison = compare_with_baseline(self._report(), baseline)
                self.assertEqual(comparison["status"], "INCONCLUSIVE")
                self.assertIn("incompatible", comparison["reason"].lower())
                self.assertNotIn("metrics", comparison)

    def test_comparison_never_claims_production_numbers(self) -> None:
        comparison = compare_with_baseline(self._report(), self._report())
        self.assertIn("not production", comparison["non_claim"])
        self.assertIn("environment-qualified", comparison["qualification"])

    def test_validation_failure_payload_is_machine_readable(self) -> None:
        error = SessionWorkloadValidationError(
            {"status": "FAIL", "problems": ["session-1: version != turn count"]}
        )
        payload = json.loads(str(error))
        self.assertEqual(payload["validation"]["status"], "FAIL")
        self.assertTrue(payload["validation"]["problems"])


if __name__ == "__main__":
    unittest.main()
