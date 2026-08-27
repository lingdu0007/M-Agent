"""Ticket 20: model-routing Scenario CONTRACT evidence and bundle tests."""

from __future__ import annotations

import unittest

from m_agent.testing import (
    MODEL_ROUTING_PACK_VERSION,
    MODEL_ROUTING_PROFILE,
    MODEL_ROUTING_SCENARIO,
    AcceptanceCheckStatus,
    EvidenceLevel,
    reconcile_model_routing,
    run_model_routing,
)

_ENVIRONMENT = {
    "distribution": "m-agent",
    "version": "0.5.0",
    "python": "3.11.9",
    "os": "linux",
    "architecture": "x86_64",
    "installation": "wheel",
    "source_state": "clean",
    "build_tool": "uv==0.5.0",
    "dependency_summary": "sha256:" + "e" * 64,
    "installed_distribution_summary": "sha256:" + "f" * 64,
}

_OBSERVATION_KEYS = (
    "typed_capability_observed",
    "operational_limits_observed",
    "usage_cost_observed",
    "deployment_constraints_observed",
    "six_outcomes_observed",
    "fallback_observed",
    "zero_side_effect_observed",
    "immutable_decision_observed",
    "no_in_run_switch_observed",
    "explicit_promotion_observed",
)


class ModelRoutingScenarioTests(unittest.TestCase):
    """CONTRACT evidence for the model-routing Scenario."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results, cls.evidence_view, cls.independent_evidence = (
            run_model_routing()
        )
        cls.by_id = {result.check_id: result for result in cls.results}

    def test_required_checks_all_pass_at_contract_level(self) -> None:
        self.assertEqual(len(self.results), 11)
        for result in self.results:
            with self.subTest(check_id=result.check_id):
                self.assertIs(result.status, AcceptanceCheckStatus.PASS)
                self.assertIs(result.evidence_level, EvidenceLevel.CONTRACT)

    def test_typed_capability_and_limits_observed(self) -> None:
        self.assertIs(self.evidence_view["typed_capability_observed"], True)

    def test_operational_limits_paths_observed(self) -> None:
        self.assertIs(self.evidence_view["operational_limits_observed"], True)

    def test_usage_cost_declared_without_fabricated_precision(self) -> None:
        self.assertIs(self.evidence_view["usage_cost_observed"], True)

    def test_deployment_constraints_observed(self) -> None:
        self.assertIs(self.evidence_view["deployment_constraints_observed"], True)

    def test_all_six_outcomes_observed(self) -> None:
        self.assertEqual(self.evidence_view["six_outcomes_count"], 6)
        self.assertIs(self.evidence_view["six_outcomes_observed"], True)

    def test_fallback_sequence_bounded_and_inspectable(self) -> None:
        self.assertIs(self.evidence_view["fallback_observed"], True)

    def test_routing_is_zero_side_effect(self) -> None:
        self.assertIs(self.evidence_view["zero_side_effect_observed"], True)

    def test_decisions_are_immutable_in_the_store(self) -> None:
        self.assertIs(self.evidence_view["immutable_decision_observed"], True)

    def test_no_in_run_switch_is_structural(self) -> None:
        self.assertIs(self.evidence_view["no_in_run_switch_observed"], True)

    def test_promotion_is_explicit(self) -> None:
        self.assertIs(self.evidence_view["explicit_promotion_observed"], True)

    def test_every_flipped_observation_is_detected(self) -> None:
        self.assertIs(self.evidence_view["mutation_detected"], True)
        self.assertEqual(self.evidence_view["mutation_probes"], 10)

    def test_every_passing_check_binds_dual_source_evidence(self) -> None:
        from m_agent.testing import model_routing_manifest

        manifest = model_routing_manifest(
            source_commit="a" * 40,
            artifact_digest="sha256:" + "b" * 64,
            sdist_digest="sha256:" + "c" * 64,
            fixture_digest="sha256:" + "d" * 64,
            environment=_ENVIRONMENT,
        )
        self.assertEqual(manifest.pack_version, MODEL_ROUTING_PACK_VERSION)
        self.assertEqual(manifest.profile, MODEL_ROUTING_PROFILE)
        self.assertEqual(manifest.scenarios, (MODEL_ROUTING_SCENARIO,))
        self.assertEqual(len(manifest.required_checks), 11)
        for check in manifest.required_checks:
            result = self.by_id[check.check_id]
            with self.subTest(check_id=check.check_id):
                self.assertEqual(
                    self.evidence_view[check.authoritative_evidence],
                    result.evidence_digest,
                )
                independent = self.independent_evidence[check.independent_evidence]
                self.assertTrue(independent.startswith("sha256:"))

    def test_evidence_values_are_minimal_and_stable(self) -> None:
        for key, value in self.evidence_view.items():
            with self.subTest(key=key):
                if isinstance(value, str):
                    self.assertRegex(value, r"sha256:[0-9a-f]{64}\Z")
                self.assertRegex(key, r"[a-z][a-z0-9_]{0,63}\Z")
        for key, value in self.independent_evidence.items():
            with self.subTest(independent=key):
                self.assertRegex(value, r"sha256:[0-9a-f]{64}\Z")

    def test_scenario_replays_deterministically(self) -> None:
        _, replay_view, replay_independent = run_model_routing()
        self.assertEqual(
            {
                key: value
                for key, value in replay_view.items()
                if not isinstance(value, bool)
            },
            {
                key: value
                for key, value in self.evidence_view.items()
                if not isinstance(value, bool)
            },
        )
        self.assertEqual(replay_independent, self.independent_evidence)


class ReconcileModelRoutingTests(unittest.TestCase):
    """The public reconciliation seam detects every controlled mutation."""

    def test_clean_observation_has_no_problems(self) -> None:
        observation = {key: True for key in _OBSERVATION_KEYS}
        self.assertEqual(reconcile_model_routing(observation), [])

    def test_every_flipped_boolean_is_detected(self) -> None:
        for key in _OBSERVATION_KEYS:
            with self.subTest(key=key):
                observation = {item: True for item in _OBSERVATION_KEYS}
                observation[key] = False
                self.assertNotEqual(reconcile_model_routing(observation), [])

    def test_missing_keys_are_reported_not_ignored(self) -> None:
        self.assertEqual(
            len(reconcile_model_routing({})), len(_OBSERVATION_KEYS)
        )


if __name__ == "__main__":
    unittest.main()
