"""Ticket 18: eval-regression Scenario CONTRACT evidence and bundle tests."""

from __future__ import annotations

import unittest

from m_agent.testing import (
    EVAL_REGRESSION_PACK_VERSION,
    EVAL_REGRESSION_PROFILE,
    EVAL_REGRESSION_SCENARIO,
    AcceptanceCheckStatus,
    BundleIntegrityError,
    EvidenceLevel,
    PackExecution,
    ScenarioEvidenceBundle,
    eval_regression_manifest,
    reconcile_eval_regression,
    run_eval_regression,
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


def _manifest():
    return eval_regression_manifest(
        source_commit="a" * 40,
        artifact_digest="sha256:" + "b" * 64,
        sdist_digest="sha256:" + "c" * 64,
        fixture_digest="sha256:" + "d" * 64,
        environment=_ENVIRONMENT,
    )


class EvalRegressionScenarioTests(unittest.TestCase):
    """CONTRACT evidence for the eval-regression Scenario."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results, cls.evidence_view, cls.independent_evidence = (
            run_eval_regression()
        )
        cls.by_id = {result.check_id: result for result in cls.results}

    def test_required_checks_all_pass_at_contract_level(self) -> None:
        self.assertEqual(len(self.results), 8)
        for result in self.results:
            with self.subTest(check_id=result.check_id):
                self.assertIs(result.status, AcceptanceCheckStatus.PASS)
                self.assertIs(result.evidence_level, EvidenceLevel.CONTRACT)

    def test_durable_recovery_resumes_without_rerunning_completed_units(self) -> None:
        # 三个 item：崩溃前完成 1 个，恢复后仅新增 2 次 subject dispatch，
        # 幂等重跑零新增 dispatch。
        self.assertEqual(self.evidence_view["durable_recovery_items"], 3)
        self.assertEqual(
            self.evidence_view["durable_recovery_resume_dispatches"], 2
        )
        self.assertIs(self.evidence_view["durable_recovery_idempotent"], True)
        self.assertIs(self.evidence_view["durable_recovery_observed"], True)

    def test_judge_isolation_enforced_and_results_reused(self) -> None:
        self.assertIs(self.evidence_view["judge_isolation_enforced"], True)
        self.assertIs(self.evidence_view["judge_isolation_observed"], True)

    def test_baseline_comparison_covers_all_five_states(self) -> None:
        self.assertEqual(self.evidence_view["baseline_comparison_states"], 5)
        self.assertIs(self.evidence_view["baseline_comparison_observed"], True)

    def test_regression_detection_covers_all_paths(self) -> None:
        # 五条语义路径：hard gate（fail-closed，policy 不可关闭）、
        # quality gate（按 policy）、pass^k 恶化、关闭策略被尊重、
        # 干净重跑不误判。
        self.assertEqual(self.evidence_view["regression_detection_paths"], 5)
        self.assertIs(self.evidence_view["regression_detection_observed"], True)

    def test_report_metrics_are_disclosed_with_justification(self) -> None:
        # 20 次 repetition 原样保留且全部通过；样本充足时才报告
        # nearest-rank P95；样本不足（3 个）时 p95 被抑制。
        self.assertEqual(self.evidence_view["report_metrics_repetitions"], 20)
        self.assertIs(self.evidence_view["report_metrics_p95_justified"], True)
        self.assertIs(
            self.evidence_view["report_metrics_insufficient_p95_suppressed"],
            True,
        )
        self.assertIs(self.evidence_view["report_metrics_observed"], True)

    def test_observe_is_read_only_and_projection_minimal(self) -> None:
        # OBSERVE 零新增 model dispatch；Projection 未授权字段绝不泄漏。
        self.assertIs(self.evidence_view["observe_projection_read_only"], True)
        self.assertIs(self.evidence_view["observe_projection_observed"], True)

    def test_recommendation_is_a_read_only_reference(self) -> None:
        # Recommendation 落盘后既有 durable 事实保持原样，篡改确定性冲突。
        self.assertIs(self.evidence_view["recommendation_readonly"], True)
        self.assertIs(self.evidence_view["recommendation_observed"], True)

    def test_all_three_mutations_are_detected(self) -> None:
        self.assertIs(self.evidence_view["mutation_detected"], True)
        self.assertEqual(self.evidence_view["mutation_probes"], 3)

    def test_every_passing_check_binds_dual_source_evidence(self) -> None:
        manifest = _manifest()
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
        # Bundle 约束：字符串值必须是 sha256 引用，键是稳定标识符。
        for key, value in self.evidence_view.items():
            with self.subTest(key=key):
                if isinstance(value, str):
                    self.assertRegex(
                        value, r"sha256:[0-9a-f]{64}\Z"
                    )
                self.assertRegex(key, r"[a-z][a-z0-9_]{0,63}\Z")
        for key, value in self.independent_evidence.items():
            with self.subTest(independent=key):
                self.assertRegex(value, r"sha256:[0-9a-f]{64}\Z")


class ReconcileEvalRegressionTests(unittest.TestCase):
    """The public reconciliation seam detects every controlled mutation."""

    @staticmethod
    def _clean() -> dict[str, bool]:
        return {
            "clean_control_comparable": True,
            "tampered_report_detected": True,
            "baseline_identity_mismatch_detected": True,
            "pass_at_k_degradation_detected": True,
        }

    def test_clean_observation_reconciles_without_problems(self) -> None:
        self.assertEqual(reconcile_eval_regression(self._clean()), [])

    def test_clean_control_misclassified_as_regression_is_detected(self) -> None:
        problems = reconcile_eval_regression(
            {**self._clean(), "clean_control_comparable": False}
        )
        self.assertIn("clean_comparison_not_comparable", problems)

    def test_undetected_report_tamper_is_detected(self) -> None:
        problems = reconcile_eval_regression(
            {**self._clean(), "tampered_report_detected": False}
        )
        self.assertIn("undetected_report_tamper", problems)

    def test_undetected_baseline_identity_mismatch_is_detected(self) -> None:
        problems = reconcile_eval_regression(
            {**self._clean(), "baseline_identity_mismatch_detected": False}
        )
        self.assertIn("undetected_baseline_identity_mismatch", problems)

    def test_undetected_pass_at_k_degradation_is_detected(self) -> None:
        problems = reconcile_eval_regression(
            {**self._clean(), "pass_at_k_degradation_detected": False}
        )
        self.assertIn("undetected_pass_at_k_degradation", problems)

    def test_missing_keys_report_every_problem(self) -> None:
        problems = reconcile_eval_regression({})
        self.assertEqual(len(problems), 4)


class EvalRegressionManifestTests(unittest.TestCase):
    """The frozen eval-regression Scenario Manifest."""

    def test_manifest_freezes_the_scenario_and_eight_required_checks(self) -> None:
        manifest = _manifest()
        self.assertEqual(manifest.pack_version, EVAL_REGRESSION_PACK_VERSION)
        self.assertEqual(manifest.profile, EVAL_REGRESSION_PROFILE)
        self.assertEqual(manifest.scenarios, (EVAL_REGRESSION_SCENARIO,))
        self.assertEqual(
            {check.check_id for check in manifest.required_checks},
            {
                "eval.regression.durable-recovery",
                "eval.regression.judge-isolation",
                "eval.regression.baseline-comparison",
                "eval.regression.regression-detection",
                "eval.regression.report-metrics",
                "eval.regression.observe-projection",
                "eval.regression.recommendation-readonly",
                "eval.regression.mutation",
            },
        )
        for check in manifest.required_checks:
            with self.subTest(check_id=check.check_id):
                self.assertIs(check.scenario, EVAL_REGRESSION_SCENARIO)
                self.assertIs(check.evidence_level, EvidenceLevel.CONTRACT)
                self.assertTrue(check.required)
                for declaration in (
                    check.owner,
                    check.public_seam,
                    check.positive_check,
                    check.negative_check,
                    check.authoritative_evidence,
                    check.independent_evidence,
                    check.milestone,
                    check.non_claim,
                ):
                    self.assertTrue(declaration.strip())

    def test_manifest_declares_nonempty_milestone_and_non_claims(self) -> None:
        manifest = _manifest()
        self.assertTrue(
            all(
                check.milestone == "0_5" and check.non_claim
                for check in manifest.required_checks
            )
        )


class EvalRegressionBundleTests(unittest.TestCase):
    """Dual-source Evidence Bundle for the eval-regression Scenario."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _manifest()
        cls.results, cls.evidence_view, cls.independent_evidence = (
            run_eval_regression()
        )
        execution = PackExecution.create(
            cls.manifest, execution_id="eval-regression-1"
        ).start(cls.manifest)
        cls.completed = execution.complete(cls.manifest, cls.results)
        cls.bundle = ScenarioEvidenceBundle.create(
            manifest=cls.manifest,
            execution=cls.completed,
            execution_checks=cls.results,
            scenario=EVAL_REGRESSION_SCENARIO,
            checks=cls.results,
            evidence_view=cls.evidence_view,
            independent_evidence=cls.independent_evidence,
        )

    def test_all_pass_results_complete_the_pack_as_passed(self) -> None:
        self.assertEqual(self.completed.exit_code, 0)

    def test_bundle_carries_dual_source_evidence_and_verifies(self) -> None:
        self.bundle.verify(self.manifest, self.completed)
        for key in (
            "durable_recovery_authoritative_digest",
            "judge_isolation_authoritative_digest",
            "baseline_comparison_authoritative_digest",
            "regression_detection_authoritative_digest",
            "report_metrics_authoritative_digest",
            "observe_projection_authoritative_digest",
            "recommendation_authoritative_digest",
            "mutation_authoritative_digest",
        ):
            with self.subTest(authoritative=key):
                self.assertIn(key, self.bundle.evidence_view)
        for key in (
            "durable_recovery_sqlite_digest",
            "judge_isolation_sqlite_digest",
            "baseline_comparison_sqlite_digest",
            "regression_detection_sqlite_digest",
            "report_metrics_sqlite_digest",
            "observe_projection_sqlite_digest",
            "recommendation_sqlite_digest",
            "mutation_independent_digest",
        ):
            with self.subTest(independent=key):
                self.assertIn(key, self.bundle.independent_evidence)

    def test_controlled_bundle_mutation_is_detected(self) -> None:
        tampered = self.bundle.model_copy(
            update={
                "evidence_view": {
                    **self.bundle.evidence_view,
                    "mutation_detected": False,
                }
            }
        )
        with self.assertRaises(BundleIntegrityError):
            tampered.verify(self.manifest, self.completed)
        checks_tampered = self.bundle.model_copy(
            update={
                "checks": self.bundle.checks[:-1],
            }
        )
        with self.assertRaises(BundleIntegrityError):
            checks_tampered.verify(self.manifest, self.completed)

    def test_missing_required_check_is_incomplete(self) -> None:
        execution = PackExecution.create(
            self.manifest, execution_id="eval-regression-2"
        ).start(self.manifest)
        missing_one = tuple(
            result
            for result in self.results
            if result.check_id != "eval.regression.mutation"
        )
        completed = execution.complete(self.manifest, missing_one)
        self.assertEqual(completed.exit_code, 4)
        with self.assertRaises(ValueError):
            ScenarioEvidenceBundle.create(
                manifest=self.manifest,
                execution=completed,
                execution_checks=missing_one,
                scenario=EVAL_REGRESSION_SCENARIO,
                checks=missing_one,
                evidence_view=self.evidence_view,
                independent_evidence=self.independent_evidence,
            )


if __name__ == "__main__":
    unittest.main()
