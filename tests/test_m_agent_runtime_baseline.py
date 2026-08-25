"""release-gate contracts for the 0.3 Runtime Baseline.

These tests use only public seams. They freeze:

- the ``durable-effects-recovery`` Reference Scenario: real subprocess hard
  exits at named public lifecycle windows, SQLite reopen recovery, three
  consecutive repetitions per required window without duplicate external
  effects, without Model Execution Budget reset, with correct
  WAITING/Resolution semantics, and a controlled mutation that must fail;
- the ``runtime-baseline`` Pack profile freezing both required Scenarios on
  one Acceptance Manifest, including the Windows-style case fail-closed
  platform gate (a P3 check that is not a Windows support claim);
- the 0.3 public import reset: the root facade shrinks to the high-frequency
  subset and every removed 0.2 import fails with a directional migration
  error;
- the correctness-first benchmark baseline comparison that never upgrades an
  incompatible environment into a comparison.
"""

from __future__ import annotations

import importlib
import unittest

import m_agent
from m_agent.testing import (
    AcceptanceCheckStatus,
    EvidenceLevel,
    PackExecution,
    RUNTIME_BASELINE_PACK_VERSION,
    RUNTIME_BASELINE_PROFILE,
    CORE_LIFECYCLE_SCENARIO,
    DURABLE_EFFECTS_SCENARIO,
    reconcile_recovery_window,
    runtime_baseline_manifest,
    run_durable_effects_recovery,
)

_ENVIRONMENT = {
    "distribution": "m-agent",
    "version": "0.3.0",
    "python": "3.11.9",
    "os": "linux",
    "architecture": "x86_64",
    "installation": "wheel",
    "source_state": "clean",
    "build_tool": "uv==0.5.0",
    "dependency_summary": "sha256:" + "e" * 64,
    "installed_distribution_summary": "sha256:" + "f" * 64,
}


def _manifest() -> object:
    return runtime_baseline_manifest(
        source_commit="a" * 40,
        artifact_digest="sha256:" + "b" * 64,
        sdist_digest="sha256:" + "c" * 64,
        fixture_digest="sha256:" + "d" * 64,
        environment=_ENVIRONMENT,
    )


def _result(check_id: str, evidence_level: object, status: object) -> object:
    from m_agent.testing import AcceptanceCheckResult

    return AcceptanceCheckResult(
        check_id=check_id,
        status=status,
        evidence_level=evidence_level,
        reason_code="ticket11_test",
        evidence_digest="sha256:" + "0" * 64,
    )


class DurableEffectsRecoveryScenarioTests(unittest.TestCase):
    """CONTRACT evidence for the second required 0.3 Scenario."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results, cls.evidence_view, cls.independent_evidence = (
            run_durable_effects_recovery()
        )
        cls.by_id = {result.check_id: result for result in cls.results}

    def test_required_window_repetitions_have_no_duplicate_effect(self) -> None:
        result = self.by_id["durable.effects.recovery-windows"]
        self.assertIs(result.status, AcceptanceCheckStatus.PASS)
        self.assertEqual(self.evidence_view["recovery_window_repetitions"], 9)
        self.assertIs(self.evidence_view["recovery_windows_clean"], True)
        self.assertIs(self.evidence_view["recovery_windows_no_budget_reset"], True)
        self.assertIs(self.evidence_view["recovery_windows_succeeded"], True)

    def test_budget_reservation_survives_recovery_fail_closed(self) -> None:
        result = self.by_id["durable.effects.budget-fail-closed"]
        self.assertIs(result.status, AcceptanceCheckStatus.PASS)
        self.assertEqual(self.evidence_view["budget_fail_closed_repetitions"], 3)
        self.assertIs(self.evidence_view["budget_fail_closed_observed"], True)

    def test_waiting_resolution_semantics_repeat_three_times(self) -> None:
        result = self.by_id["durable.effects.waiting-resolution"]
        self.assertIs(result.status, AcceptanceCheckStatus.PASS)
        self.assertEqual(self.evidence_view["waiting_resolution_repetitions"], 3)
        self.assertIs(self.evidence_view["waiting_semantics_observed"], True)

    def test_controlled_mutation_makes_reconciliation_fail(self) -> None:
        result = self.by_id["durable.effects.mutation"]
        self.assertIs(result.status, AcceptanceCheckStatus.PASS)
        self.assertIs(self.evidence_view["mutation_detected"], True)

    def test_dual_source_evidence_present_for_every_pass(self) -> None:
        for result in self.results:
            self.assertTrue(result.evidence_digest.startswith("sha256:"))
            self.assertNotEqual(result.reason_code, "")
        self.assertIn("recovery_windows_journal_digest", self.independent_evidence)
        self.assertIn("budget_fail_closed_journal_digest", self.independent_evidence)
        self.assertIn("waiting_resolution_journal_digest", self.independent_evidence)
        self.assertIn("mutation_independent_digest", self.independent_evidence)

    def test_reconciliation_detects_duplicate_external_effect(self) -> None:
        observation = {
            "status": "SUCCEEDED",
            "attempt_statuses": ["FAILED", "SUCCEEDED", "SUCCEEDED", "SUCCEEDED"],
            "model_attempts": 3,
            "tool_attempts": 1,
        }
        problems = reconcile_recovery_window(
            observation,
            effects=["effect:call-1", "effect:call-1"],
            model_dispatches=["model:1", "model:2"],
        )
        self.assertTrue(problems)

    def test_reconciliation_detects_budget_reset(self) -> None:
        observation = {
            "status": "SUCCEEDED",
            "attempt_statuses": ["SUCCEEDED"],
            "model_attempts": 1,
            "tool_attempts": 1,
        }
        # The crashed reservation is absent from the authoritative view:
        # recovery silently regained the consumed Model Execution Budget.
        problems = reconcile_recovery_window(
            observation,
            effects=["effect:call-1"],
            model_dispatches=["model:1", "model:2"],
        )
        self.assertTrue(problems)

    def test_reconciliation_accepts_a_clean_window(self) -> None:
        observation = {
            "status": "SUCCEEDED",
            "attempt_statuses": ["FAILED", "SUCCEEDED", "SUCCEEDED", "SUCCEEDED"],
            "model_attempts": 3,
            "tool_attempts": 1,
        }
        problems = reconcile_recovery_window(
            observation,
            effects=["effect:call-1"],
            model_dispatches=["model:1", "model:2"],
        )
        self.assertEqual(problems, [])


class RuntimeBaselineManifestTests(unittest.TestCase):
    """The frozen two-Scenario 0.3 release Manifest."""

    def test_manifest_freezes_both_required_scenarios(self) -> None:
        manifest = _manifest()
        self.assertEqual(
            manifest.scenarios,
            (CORE_LIFECYCLE_SCENARIO, DURABLE_EFFECTS_SCENARIO),
        )
        self.assertEqual(manifest.profile, RUNTIME_BASELINE_PROFILE)
        self.assertEqual(manifest.pack_version, RUNTIME_BASELINE_PACK_VERSION)
        scenarios = {check.scenario for check in manifest.required_checks}
        self.assertEqual(
            scenarios, {CORE_LIFECYCLE_SCENARIO, DURABLE_EFFECTS_SCENARIO}
        )

    def test_manifest_declares_the_0_3_migration_check_not_expand(self) -> None:
        manifest = _manifest()
        check_ids = {check.check_id for check in manifest.required_checks}
        self.assertIn("core.lifecycle.migration", check_ids)
        self.assertNotIn("core.lifecycle.expand-compatibility", check_ids)
        durable_ids = {
            check.check_id
            for check in manifest.required_checks
            if check.scenario == DURABLE_EFFECTS_SCENARIO
        }
        self.assertEqual(
            durable_ids,
            {
                "durable.effects.recovery-windows",
                "durable.effects.budget-fail-closed",
                "durable.effects.waiting-resolution",
                "durable.effects.mutation",
                "durable.effects.host-wheel",
            },
        )

    def test_manifest_requires_contract_and_host_evidence_per_scenario(self) -> None:
        manifest = _manifest()
        for scenario in manifest.scenarios:
            levels = {
                check.evidence_level
                for check in manifest.required_checks
                if check.scenario == scenario
            }
            self.assertIn(EvidenceLevel.CONTRACT, levels, scenario)
            self.assertIn(EvidenceLevel.HOST, levels, scenario)

    def test_manifest_rejects_windows_style_case_collisions(self) -> None:
        manifest = _manifest()
        with self.assertRaises(ValueError):
            manifest.model_copy(
                update={"scenarios": (CORE_LIFECYCLE_SCENARIO, "Core-Lifecycle")}
            )
        collided = manifest.required_checks[0].model_copy(
            update={"check_id": "CORE.LIFECYCLE"}
        )
        with self.assertRaises(ValueError):
            manifest.model_copy(
                update={"required_checks": (*manifest.required_checks, collided)}
            )

    def test_manifest_rejects_windows_style_environment_case(self) -> None:
        manifest = _manifest()
        for os_name in ("Windows", "WINDOWS", "Darwin"):
            with self.subTest(os=os_name):
                with self.assertRaises(ValueError):
                    manifest.model_copy(
                        update={
                            "environment": {
                                **dict(manifest.environment),
                                "os": os_name,
                            }
                        }
                    )

    def test_two_scenario_execution_completes_over_every_required_check(self) -> None:
        manifest = _manifest()
        execution = PackExecution.create(manifest, execution_id="baseline-1").start(
            manifest
        )
        all_pass = tuple(
            _result(check.check_id, check.evidence_level, AcceptanceCheckStatus.PASS)
            for check in manifest.required_checks
        )
        completed = execution.complete(manifest, all_pass)
        self.assertEqual(completed.exit_code, 0)
        one_fail = tuple(
            _result(
                check.check_id,
                check.evidence_level,
                (
                    AcceptanceCheckStatus.FAIL
                    if check.check_id == "durable.effects.budget-fail-closed"
                    else AcceptanceCheckStatus.PASS
                ),
            )
            for check in manifest.required_checks
        )
        completed = execution.complete(manifest, one_fail)
        self.assertEqual(completed.exit_code, 1)
        missing_one = tuple(
            _result(check.check_id, check.evidence_level, AcceptanceCheckStatus.PASS)
            for check in manifest.required_checks
            if check.check_id != "durable.effects.mutation"
        )
        completed = execution.complete(manifest, missing_one)
        self.assertEqual(completed.exit_code, 4)


class PublicImportResetTests(unittest.TestCase):
    """The 0.3 contract reset removes the 0.2 root surface directionally."""

    ROOT_FACADE = (
        "AgentDefinition",
        "DefinitionRegistry",
        "Runner",
        "SyncRunner",
        "RunStatus",
        "RunRecord",
        "RunInspection",
    )

    RUNTIME_MOVED = (
        "Clock",
        "ContextItem",
        "ContextProvider",
        "ContextRequest",
        "DefinitionSnapshot",
        "RetryPolicy",
        "ModelAdapter",
        "ModelCapabilities",
        "ModelRequest",
        "ModelResponse",
        "ModelUsage",
        "PayloadCodec",
        "TelemetrySink",
        "TelemetryEvent",
        "TelemetryEventType",
        "Tool",
        "ToolCall",
        "ToolEffect",
        "ToolOutcome",
        "ToolRequest",
        "ToolSpec",
        "ToolDeclaration",
        "RunPolicy",
        "RunResolution",
        "RunStore",
        "RunLease",
        "RunUpdate",
        "StepRecord",
        "StepAttempt",
        "StepCheckpoint",
        "StepStatus",
        "StepType",
        "FailureClassification",
        "MAgentError",
        "DefinitionNotFoundError",
        "ModelFailure",
        "ToolFailure",
    )

    ADAPTER_MOVED = (
        "DeterministicModelAdapter",
        "DeterministicStreamingModelAdapter",
        "DeterministicContextProvider",
        "DeterministicTool",
        "InMemoryRunStore",
        "SQLiteRunStore",
        "JsonlTelemetrySink",
        "PlaintextPayloadCodec",
        "FakeClock",
        "SystemClock",
    )

    REMOVED_HELPERS = (
        "CrashPoint",
        "serialize_model_response",
        "deserialize_model_response",
        "serialize_tool_outcome",
        "deserialize_tool_outcome",
    )

    def test_root_facade_retains_only_the_high_frequency_subset(self) -> None:
        self.assertEqual(set(m_agent.__all__), set(self.ROOT_FACADE))
        from m_agent import runtime

        for name in self.ROOT_FACADE:
            self.assertIs(getattr(m_agent, name), getattr(runtime, name))

    def test_removed_runtime_imports_fail_with_directional_error(self) -> None:
        for name in self.RUNTIME_MOVED:
            with self.subTest(name=name):
                self.assertFalse(hasattr(m_agent, name))
                with self.assertRaises(AttributeError) as caught:
                    getattr(m_agent, name)
                self.assertIn("m_agent.runtime", str(caught.exception))

    def test_removed_adapter_imports_fail_with_directional_error(self) -> None:
        for name in self.ADAPTER_MOVED:
            with self.subTest(name=name):
                self.assertFalse(hasattr(m_agent, name))
                with self.assertRaises(AttributeError) as caught:
                    getattr(m_agent, name)
                self.assertIn("m_agent.adapters", str(caught.exception))

    def test_removed_helpers_fail_with_a_removed_direction(self) -> None:
        for name in self.REMOVED_HELPERS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(m_agent, name))
                with self.assertRaises(AttributeError) as caught:
                    getattr(m_agent, name)
                self.assertIn("removed", str(caught.exception).lower())

    def test_every_removed_name_points_at_the_migration_table(self) -> None:
        for name in (*self.RUNTIME_MOVED, *self.ADAPTER_MOVED, *self.REMOVED_HELPERS):
            with self.subTest(name=name):
                with self.assertRaises(AttributeError) as caught:
                    getattr(m_agent, name)
                self.assertIn("migrating-to-0.3", str(caught.exception))

    def test_unknown_attribute_is_not_a_migration_error(self) -> None:
        with self.assertRaises(AttributeError) as caught:
            getattr(m_agent, "definitely_not_an_export")
        self.assertNotIn("migrating-to-0.3", str(caught.exception))

    def test_legacy_provider_module_fails_directionally(self) -> None:
        with self.assertRaises(ImportError) as caught:
            importlib.import_module("m_agent.provider")
        self.assertIn("m_agent.adapters.provider", str(caught.exception))

    def test_legacy_agent_framework_fails_directionally(self) -> None:
        with self.assertRaises(ImportError) as caught:
            importlib.import_module("agent_framework")
        self.assertIn("migrating-from-0.1", str(caught.exception))

    def test_adapters_provider_is_the_public_home(self) -> None:
        from m_agent.adapters.provider import (  # noqa: F401
            ChatCompletionsModelAdapter,
            ResponsesModelAdapter,
        )


class BenchmarkBaselineComparisonTests(unittest.TestCase):
    """Correctness-first benchmark evidence stays environment-qualified."""

    @staticmethod
    def _report(python: str = "3.11.9", system: str = "Darwin",
                machine: str = "arm64") -> dict:
        return {
            "schema_version": 1,
            "environment": {
                "python": {
                    "implementation": "CPython",
                    "version": python,
                },
                "os": {"system": system, "release": "24.0", "version": "24.0"},
                "cpu": {
                    "machine": machine,
                    "processor": "arm",
                    "logical_count": 10,
                },
            },
            "measurement": {"run_count": 100},
            "baseline_identity": {
                "artifact_digest": "sha256:" + "a" * 64,
                "manifest_digest": "sha256:" + "b" * 64,
            },
            "metrics": {
                "throughput_runs_per_second": 100.0,
                "run_latency_ms": {"p50": 5.0, "p95": 9.0},
            },
        }

    def test_compatible_baseline_reports_relative_comparison(self) -> None:
        from benchmarks.durable_run_sqlite import compare_with_baseline

        comparison = compare_with_baseline(self._report(), self._report())
        self.assertEqual(comparison["status"], "COMPARED")
        self.assertIn("throughput_delta_ratio", comparison["metrics"])
        self.assertIn("non_claim", comparison)

    def test_incompatible_baseline_is_inconclusive(self) -> None:
        from benchmarks.durable_run_sqlite import compare_with_baseline

        for baseline in (
            self._report(python="3.12.1"),
            self._report(system="Linux"),
            self._report(machine="x86_64"),
        ):
            with self.subTest(baseline=baseline["environment"]):
                comparison = compare_with_baseline(self._report(), baseline)
                self.assertEqual(comparison["status"], "INCONCLUSIVE")
                self.assertIn("incompatible", comparison["reason"].lower())
                self.assertNotIn("metrics", comparison)

    def test_comparison_never_claims_production_numbers(self) -> None:
        from benchmarks.durable_run_sqlite import compare_with_baseline

        comparison = compare_with_baseline(self._report(), self._report())
        self.assertIn("not production", comparison["non_claim"])
        self.assertIn("environment-qualified", comparison["qualification"])


if __name__ == "__main__":
    unittest.main()
