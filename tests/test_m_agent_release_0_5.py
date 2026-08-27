"""release-gate contracts for the 0.5 Runtime Foundation candidate.

这些测试只用公共 seam。冻结内容：

- ``foundation-release-0-5`` Pack profile：同一 RC 身份（Manifest）下
  冻结六个 required Scenario（0.3/0.4 基线原样重跑，新增
  ``model-routing`` 与 ``eval-regression`` 的 CONTRACT/HOST 证据）；
- 聚合规则与退出码：required 全 PASS 才 PASSED；FAIL/ERROR/NOT_RUN/
  INCONCLUSIVE 按既定规则保留；undeclared 的通过结果不能救场
  （optional 通过率不能覆盖 required 失败的结构化表达）；
- 场景隔离与防拼接：每个 Scenario 的 Evidence Bundle 只携带自己的
  required checks；不同 RC 的 Bundle 互不匹配；
- routing/eval 的隔离 HOST 探针观察器（ERROR/FAIL/PASS 三态）；
- 0.5 跨平台矩阵：Linux 3.11–3.14 CONTRACT、Linux 3.11 primary
  HOST、macOS 最低/最高支持 Python secondary HOST；无法覆盖的平台
  以诚实 NOT_RUN 记录并说明证据来源；
- Coverage Matrix 无 required gap 校验；
- 演示可重放性：12–15 分钟完整演示与 3 分钟 recovery/report 短版
  可从已通过 Bundle 渲染，预生成证据明确标注，拒绝失败/被篡改 Bundle；
- Eval workload benchmark 先做正确性/完整性验证再报告环境限定指标；
- 发布材料一致性：0.5 版本号、Coverage Matrix 的 0.5 required 行、
  非目标与文档。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from m_agent.testing import (
    AcceptanceCheckResult,
    AcceptanceCheckStatus,
    BundleIntegrityError,
    CONTEXT_COMPRESSION_SCENARIO,
    CORE_LIFECYCLE_SCENARIO,
    DURABLE_EFFECTS_SCENARIO,
    EVAL_REGRESSION_SCENARIO,
    EvidenceLevel,
    EXIT_HARNESS_ERROR,
    EXIT_INCOMPLETE,
    EXIT_SUBJECT_FAILURE,
    EXIT_SUCCESS,
    FOUNDATION_RELEASE_0_5_PACK_VERSION,
    FOUNDATION_RELEASE_0_5_PROFILE,
    MODEL_ROUTING_SCENARIO,
    PackExecution,
    ScenarioEvidenceBundle,
    SESSION_CONVERSATION_SCENARIO,
    foundation_release_0_5_manifest,
)

ROOT = Path(__file__).parents[1]

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
    return foundation_release_0_5_manifest(
        source_commit="a" * 40,
        artifact_digest="sha256:" + "b" * 64,
        sdist_digest="sha256:" + "c" * 64,
        fixture_digest="sha256:" + "d" * 64,
        environment=_ENVIRONMENT,
    )


def _other_rc_manifest():
    """A different release candidate: same shape, different artifact identity."""
    return foundation_release_0_5_manifest(
        source_commit="a" * 40,
        artifact_digest="sha256:" + "1" * 64,
        sdist_digest="sha256:" + "2" * 64,
        fixture_digest="sha256:" + "d" * 64,
        environment=_ENVIRONMENT,
    )


def _result(check, status=AcceptanceCheckStatus.PASS):
    return AcceptanceCheckResult(
        check_id=check.check_id,
        status=status,
        evidence_level=check.evidence_level,
        reason_code="ticket22_test",
        evidence_digest="sha256:" + "0" * 64,
    )


def _all_pass(manifest):
    return tuple(_result(check) for check in manifest.required_checks)


class FoundationRelease05ManifestTests(unittest.TestCase):
    """The frozen six-Scenario 0.5 release Manifest."""

    def test_profile_freezes_all_six_required_scenarios_in_order(self) -> None:
        manifest = _manifest()
        self.assertEqual(manifest.profile, FOUNDATION_RELEASE_0_5_PROFILE)
        self.assertEqual(manifest.pack_version, FOUNDATION_RELEASE_0_5_PACK_VERSION)
        self.assertEqual(
            manifest.scenarios,
            (
                CORE_LIFECYCLE_SCENARIO,
                DURABLE_EFFECTS_SCENARIO,
                SESSION_CONVERSATION_SCENARIO,
                CONTEXT_COMPRESSION_SCENARIO,
                MODEL_ROUTING_SCENARIO,
                EVAL_REGRESSION_SCENARIO,
            ),
        )
        declared = {check.scenario for check in manifest.required_checks}
        self.assertEqual(declared, set(manifest.scenarios))

    def test_prior_required_scenarios_are_rerun_not_replaced(self) -> None:
        manifest = _manifest()
        check_ids = {check.check_id for check in manifest.required_checks}
        for check_id in (
            # 0.3 baseline
            "core.lifecycle",
            "core.lifecycle.telemetry",
            "core.lifecycle.migration",
            "core.lifecycle.host-wheel",
            "core.lifecycle.telemetry-host",
            "durable.effects.recovery-windows",
            "durable.effects.host-wheel",
            # 0.4 scenarios
            "session.conversation.recovery-windows",
            "session.conversation.host-wheel",
            "context.compression.hard-budget",
            "context.compression.host-wheel",
        ):
            self.assertIn(check_id, check_ids)

    def test_model_routing_and_eval_regression_required_checks_are_frozen(self) -> None:
        manifest = _manifest()
        check_ids = {check.check_id for check in manifest.required_checks}
        for check_id in (
            "model.routing.typed-capability",
            "model.routing.operational-limits",
            "model.routing.usage-cost",
            "model.routing.deployment-constraints",
            "model.routing.six-outcomes",
            "model.routing.fallback",
            "model.routing.zero-side-effect",
            "model.routing.immutable-decision",
            "model.routing.no-in-run-switch",
            "model.routing.explicit-promotion",
            "model.routing.mutation",
            "model.routing.host-wheel",
            "eval.regression.durable-recovery",
            "eval.regression.judge-isolation",
            "eval.regression.baseline-comparison",
            "eval.regression.regression-detection",
            "eval.regression.report-metrics",
            "eval.regression.observe-projection",
            "eval.regression.recommendation-readonly",
            "eval.regression.mutation",
            "eval.regression.host-wheel",
        ):
            self.assertIn(check_id, check_ids)

    def test_every_scenario_requires_contract_and_host_evidence(self) -> None:
        manifest = _manifest()
        for scenario in manifest.scenarios:
            levels = {
                check.evidence_level
                for check in manifest.required_checks
                if check.scenario == scenario
            }
            self.assertIn(EvidenceLevel.CONTRACT, levels, scenario)
            self.assertIn(EvidenceLevel.HOST, levels, scenario)

    def test_routing_and_eval_rows_declare_the_0_5_milestone(self) -> None:
        manifest = _manifest()
        for check in manifest.required_checks:
            if check.scenario in (MODEL_ROUTING_SCENARIO, EVAL_REGRESSION_SCENARIO):
                self.assertEqual(check.milestone, "0_5", check.check_id)
                self.assertTrue(check.non_claim.strip(), check.check_id)

    def test_evidence_slots_do_not_collide_across_scenarios(self) -> None:
        manifest = _manifest()
        authoritative = [
            check.authoritative_evidence for check in manifest.required_checks
        ]
        independent = [
            check.independent_evidence for check in manifest.required_checks
        ]
        self.assertEqual(len(set(authoritative)), len(authoritative))
        self.assertEqual(len(set(independent)), len(independent))

    def test_one_manifest_identity_binds_every_scenario(self) -> None:
        manifest = _manifest()
        other = _other_rc_manifest()
        self.assertNotEqual(manifest.digest, other.digest)
        for scenario in manifest.scenarios:
            execution = PackExecution.create(
                manifest, execution_id="release-1"
            ).start(manifest)
            self.assertRaises(ValueError, execution.assert_matches, other)

    def test_required_cli_commands_are_declared(self) -> None:
        self.assertEqual(
            _manifest().required_cli_commands, ("run", "inspect", "verify", "render")
        )

    def test_offline_cli_dispatches_the_0_5_profile(self) -> None:
        from m_agent.testing.__main__ import _assert_supported_manifest

        _assert_supported_manifest(_manifest())
        tampered = _manifest().model_copy(
            update={"profile": "foundation-release-0-5-tampered"}
        )
        with self.assertRaises(ValueError):
            _assert_supported_manifest(tampered)

    def test_manifest_rejects_non_required_declarations(self) -> None:
        from m_agent.testing import AcceptanceManifest

        base = _manifest()
        with self.assertRaises(ValueError):
            AcceptanceManifest.model_validate(
                {
                    **base.model_dump(mode="json"),
                    "required_checks": [
                        {
                            **check.model_dump(mode="json"),
                            "required": False,
                        }
                        for check in base.required_checks[:1]
                    ]
                    + [
                        check.model_dump(mode="json")
                        for check in base.required_checks[1:]
                    ],
                }
            )


class Release05AggregationTests(unittest.TestCase):
    """One Pack Execution aggregates six isolated scenario bundles per RC."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _manifest()
        cls.results = _all_pass(cls.manifest)
        cls.execution = PackExecution.create(
            cls.manifest, execution_id="foundation-release-0-5-1"
        ).start(cls.manifest)
        cls.completed = cls.execution.complete(cls.manifest, cls.results)

    def test_all_required_pass_completes_the_pack_as_passed(self) -> None:
        self.assertIs(self.completed.status.value, "PASSED")
        self.assertEqual(self.completed.exit_code, EXIT_SUCCESS)

    def test_one_scenario_fail_fails_the_pack_without_rewriting_others(self) -> None:
        results = tuple(
            _result(
                check,
                (
                    AcceptanceCheckStatus.FAIL
                    if check.scenario is MODEL_ROUTING_SCENARIO
                    else AcceptanceCheckStatus.PASS
                ),
            )
            for check in self.manifest.required_checks
        )
        completed = self.execution.complete(self.manifest, results)
        self.assertIs(completed.status.value, "FAILED")
        self.assertEqual(completed.exit_code, EXIT_SUBJECT_FAILURE)
        routing_ids = {
            check.check_id
            for check in self.manifest.required_checks
            if check.scenario is MODEL_ROUTING_SCENARIO
        }
        self.assertEqual(
            {
                result.check_id
                for result in results
                if result.status is AcceptanceCheckStatus.FAIL
            },
            routing_ids,
        )

    def test_error_check_keeps_harness_error_status_and_exit_code(self) -> None:
        results = tuple(
            _result(
                check,
                (
                    AcceptanceCheckStatus.ERROR
                    if check.check_id == "eval.regression.host-wheel"
                    else AcceptanceCheckStatus.PASS
                ),
            )
            for check in self.manifest.required_checks
        )
        completed = self.execution.complete(self.manifest, results)
        self.assertIs(completed.status.value, "ERROR")
        self.assertEqual(completed.exit_code, EXIT_HARNESS_ERROR)

    def test_not_run_check_is_incomplete(self) -> None:
        results = tuple(
            _result(
                check,
                (
                    AcceptanceCheckStatus.NOT_RUN
                    if check.scenario is EVAL_REGRESSION_SCENARIO
                    else AcceptanceCheckStatus.PASS
                ),
            )
            for check in self.manifest.required_checks
        )
        completed = self.execution.complete(self.manifest, results)
        self.assertIs(completed.status.value, "INCOMPLETE")
        self.assertEqual(completed.exit_code, EXIT_INCOMPLETE)

    def test_missing_scenario_check_is_incomplete(self) -> None:
        results = tuple(
            _result(check)
            for check in self.manifest.required_checks
            if check.scenario is not EVAL_REGRESSION_SCENARIO
        )
        completed = self.execution.complete(self.manifest, results)
        self.assertIs(completed.status.value, "INCOMPLETE")
        self.assertEqual(completed.exit_code, EXIT_INCOMPLETE)

    def test_undeclared_passing_results_cannot_rescue_required_failure(self) -> None:
        # optional / 未声明 check 的通过率不能覆盖 required 失败：
        # 传入 undeclared 结果（无论 PASS）被聚合为 ERROR 而不是 PASSED。
        results = tuple(
            _result(
                check,
                (
                    AcceptanceCheckStatus.FAIL
                    if check.scenario is MODEL_ROUTING_SCENARIO
                    else AcceptanceCheckStatus.PASS
                ),
            )
            for check in self.manifest.required_checks
        ) + (
            AcceptanceCheckResult(
                check_id="optional.extra-check",
                status=AcceptanceCheckStatus.PASS,
                evidence_level=EvidenceLevel.CONTRACT,
                reason_code="ticket22_test",
                evidence_digest="sha256:" + "9" * 64,
            ),
        )
        completed = self.execution.complete(self.manifest, results)
        self.assertIs(completed.status.value, "ERROR")
        self.assertEqual(completed.exit_code, EXIT_HARNESS_ERROR)

    def _release_evidence_views(self):
        declared = {
            check.check_id: check for check in self.manifest.required_checks
        }
        evidence_view = {
            declared[result.check_id].authoritative_evidence: result.evidence_digest
            for result in self.results
        }
        independent = {
            check.independent_evidence: "sha256:" + "8" * 64
            for check in self.manifest.required_checks
        }
        return evidence_view, independent

    def test_scenario_bundles_isolate_checks_per_scenario(self) -> None:
        evidence_view, independent = self._release_evidence_views()
        declared_by_id = {check.check_id: check for check in self.manifest.required_checks}
        for scenario in self.manifest.scenarios:
            scenario_checks = tuple(
                result
                for result in self.results
                if declared_by_id[result.check_id].scenario is scenario
            )
            bundle = ScenarioEvidenceBundle.create(
                manifest=self.manifest,
                execution=self.completed,
                execution_checks=self.results,
                scenario=scenario,
                checks=scenario_checks,
                evidence_view=evidence_view,
                independent_evidence=independent,
            )
            bundle.verify(self.manifest, self.completed)
            self.assertEqual(
                {result.check_id for result in bundle.checks},
                {result.check_id for result in scenario_checks},
            )

    def test_scenario_bundle_rejects_foreign_scenario_checks(self) -> None:
        evidence_view, independent = self._release_evidence_views()
        routing_checks = tuple(
            result
            for result in self.results
            if next(
                check
                for check in self.manifest.required_checks
                if check.check_id == result.check_id
            ).scenario
            is MODEL_ROUTING_SCENARIO
        )
        with self.assertRaises(ValueError):
            ScenarioEvidenceBundle.create(
                manifest=self.manifest,
                execution=self.completed,
                execution_checks=self.results,
                scenario=EVAL_REGRESSION_SCENARIO,
                checks=routing_checks,
                evidence_view=evidence_view,
                independent_evidence=independent,
            )

    def test_bundle_from_another_rc_cannot_attest_this_candidate(self) -> None:
        evidence_view, independent = self._release_evidence_views()
        routing_checks = tuple(
            result
            for result in self.results
            if next(
                check
                for check in self.manifest.required_checks
                if check.check_id == result.check_id
            ).scenario
            is MODEL_ROUTING_SCENARIO
        )
        bundle = ScenarioEvidenceBundle.create(
            manifest=self.manifest,
            execution=self.completed,
            execution_checks=self.results,
            scenario=MODEL_ROUTING_SCENARIO,
            checks=routing_checks,
            evidence_view=evidence_view,
            independent_evidence=independent,
        )
        other = _other_rc_manifest()
        with self.assertRaises(BundleIntegrityError):
            bundle.verify(other, self.completed)
        other_execution = PackExecution.create(
            other, execution_id="foundation-release-0-5-2"
        ).start(other)
        with self.assertRaises(BundleIntegrityError):
            bundle.verify(self.manifest, other_execution)

    def test_tampered_bundle_content_fails_verification(self) -> None:
        evidence_view, independent = self._release_evidence_views()
        declared_by_id = {check.check_id: check for check in self.manifest.required_checks}
        routing_checks = tuple(
            result
            for result in self.results
            if declared_by_id[result.check_id].scenario is MODEL_ROUTING_SCENARIO
        )
        bundle = ScenarioEvidenceBundle.create(
            manifest=self.manifest,
            execution=self.completed,
            execution_checks=self.results,
            scenario=MODEL_ROUTING_SCENARIO,
            checks=routing_checks,
            evidence_view=evidence_view,
            independent_evidence=independent,
        )
        tampered = bundle.model_copy(
            update={"evidence_view": {**bundle.evidence_view, "six_outcomes_count": 99}}
        )
        with self.assertRaises(BundleIntegrityError):
            tampered.verify(self.manifest, self.completed)


class RoutingEvalHostProbeObserverTests(unittest.TestCase):
    """The isolated-process HOST probe observer classifies honestly."""

    def _observe(self, source: str, check_id: str):
        from m_agent.testing import observe_isolated_scenario_probe

        return observe_isolated_scenario_probe(
            probe_source=source,
            check_id=check_id,
        )

    def test_passing_routing_probe_observation_is_pass(self) -> None:
        from m_agent.testing import MODEL_ROUTING_HOST_EXPECTATION

        payload = json.dumps(
            {
                "scenario": MODEL_ROUTING_HOST_EXPECTATION["scenario"],
                "checks": dict.fromkeys(
                    MODEL_ROUTING_HOST_EXPECTATION["check_ids"], "PASS"
                ),
                "observation": MODEL_ROUTING_HOST_EXPECTATION["observation"],
            }
        )
        probe = f"import sys; sys.stdout.write({payload!r})"
        result, evidence = self._observe(
            probe, check_id="model.routing.host-wheel"
        )
        self.assertIs(result.status, AcceptanceCheckStatus.PASS)
        self.assertIs(result.evidence_level, EvidenceLevel.HOST)
        self.assertTrue(result.evidence_digest.startswith("sha256:"))
        self.assertTrue(evidence["probe_process_observed"])

    def test_failing_eval_probe_check_is_subject_failure(self) -> None:
        from m_agent.testing import EVAL_REGRESSION_HOST_EXPECTATION

        payload = json.dumps(
            {
                "scenario": EVAL_REGRESSION_HOST_EXPECTATION["scenario"],
                "checks": {
                    **dict.fromkeys(
                        EVAL_REGRESSION_HOST_EXPECTATION["check_ids"], "PASS"
                    ),
                    "eval.regression.durable-recovery": "FAIL",
                },
                "observation": EVAL_REGRESSION_HOST_EXPECTATION["observation"],
            }
        )
        probe = f"import sys; sys.stdout.write({payload!r})"
        result, _ = self._observe(probe, check_id="eval.regression.host-wheel")
        self.assertIs(result.status, AcceptanceCheckStatus.FAIL)

    def test_broken_probe_process_is_harness_error(self) -> None:
        for check_id in ("model.routing.host-wheel", "eval.regression.host-wheel"):
            for probe in (
                "import sys; sys.exit(9)",
                "raise RuntimeError('probe exploded')",
            ):
                with self.subTest(check_id=check_id, probe=probe):
                    result, _ = self._observe(probe, check_id=check_id)
                    self.assertIs(result.status, AcceptanceCheckStatus.ERROR)


class PlatformMatrixTests(unittest.TestCase):
    """The frozen 0.5 cross-platform requirement and honest evidence."""

    def test_required_platform_matrix_declares_linux_and_macos_gates(self) -> None:
        from m_agent.testing import FOUNDATION_PLATFORM_MATRIX_0_5

        entries = {
            (entry.platform, entry.python, entry.level.value, entry.role)
            for entry in FOUNDATION_PLATFORM_MATRIX_0_5
        }
        for python in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(("linux", python, "CONTRACT", "required"), entries)
        self.assertIn(("linux", "3.11", "HOST", "primary"), entries)
        self.assertIn(("darwin", "3.11", "HOST", "secondary"), entries)
        self.assertIn(("darwin", "3.14", "HOST", "secondary"), entries)
        # 没有把 Windows 伪装成支持平台。
        self.assertFalse(any(
            entry.platform == "windows" for entry in FOUNDATION_PLATFORM_MATRIX_0_5
        ))

    def _observed(
        self,
        status=AcceptanceCheckStatus.PASS,
        platform="linux",
        python="3.11",
        level="CONTRACT",
        role="required",
        evidence_source="ticket22-test-harness",
    ):
        from m_agent.testing import PlatformMatrixObservation

        return PlatformMatrixObservation(
            platform=platform,
            python=python,
            level=level,
            role=role,
            status=status,
            evidence_source=evidence_source,
            evidence_digest=(
                "sha256:" + "7" * 64
                if status is not AcceptanceCheckStatus.NOT_RUN
                else ""
            ),
            artifact_digest="sha256:" + "b" * 64,
        )

    def test_complete_matrix_passes_and_gaps_are_empty(self) -> None:
        from m_agent.testing import (
            FOUNDATION_PLATFORM_MATRIX_0_5,
            PlatformMatrixEvidence,
        )

        observations = tuple(
            self._observed(
                platform=entry.platform,
                python=entry.python,
                level=entry.level.value,
                role=entry.role,
            )
            for entry in FOUNDATION_PLATFORM_MATRIX_0_5
        )
        evidence = PlatformMatrixEvidence.create(observations=observations)
        self.assertEqual(evidence.overall_status, "PASS")
        self.assertEqual(evidence.gaps, ())

    def test_honest_not_run_entries_are_gaps_and_make_matrix_incomplete(self) -> None:
        from m_agent.testing import (
            FOUNDATION_PLATFORM_MATRIX_0_5,
            PlatformMatrixEvidence,
        )

        # 只有 darwin secondary HOST 被本机覆盖；Linux 以诚实 NOT_RUN 记录
        # 并说明证据来源。
        observations = tuple(
            self._observed(
                platform=entry.platform,
                python=entry.python,
                level=entry.level.value,
                role=entry.role,
                status=(
                    AcceptanceCheckStatus.PASS
                    if entry.platform == "darwin"
                    else AcceptanceCheckStatus.NOT_RUN
                ),
                evidence_source=(
                    "local secondary HOST run"
                    if entry.platform == "darwin"
                    else "requires a Linux runner; not executable in this offline session"
                ),
            )
            for entry in FOUNDATION_PLATFORM_MATRIX_0_5
        )
        evidence = PlatformMatrixEvidence.create(observations=observations)
        self.assertEqual(evidence.overall_status, "INCOMPLETE")
        self.assertTrue(evidence.gaps)
        self.assertTrue(
            all(entry.platform == "linux" for entry in evidence.gaps)
        )
        # NOT_RUN 记录保留了诚实的证据来源说明。
        not_run = [o for o in evidence.observations if o.status is AcceptanceCheckStatus.NOT_RUN]
        self.assertTrue(not_run)
        self.assertTrue(
            all("Linux runner" in o.evidence_source for o in not_run)
        )

    def test_foreign_artifact_digest_is_rejected(self) -> None:
        from m_agent.testing import PlatformMatrixEvidence

        first = self._observed()
        second = self._observed(platform="linux", python="3.12")
        second = second.model_copy(
            update={"artifact_digest": "sha256:" + "3" * 64}
        )
        with self.assertRaises(ValueError):
            PlatformMatrixEvidence.create(observations=(first, second))

    def test_missing_requirement_entries_are_rejected(self) -> None:
        from m_agent.testing import PlatformMatrixEvidence

        with self.assertRaises(ValueError):
            PlatformMatrixEvidence.create(observations=(self._observed(),))

    def test_not_run_observations_carry_no_fabricated_digest_claim(self) -> None:
        from m_agent.testing import PlatformMatrixObservation

        observation = PlatformMatrixObservation(
            platform="linux",
            python="3.12",
            level="CONTRACT",
            role="required",
            status=AcceptanceCheckStatus.NOT_RUN,
            evidence_source="requires a Linux runner",
            evidence_digest="",
            artifact_digest="sha256:" + "b" * 64,
        )
        self.assertEqual(observation.evidence_digest, "")


class CoverageMatrixGapTests(unittest.TestCase):
    """The frozen Coverage Matrix documents every 0.5 required check."""

    def test_release_documented_matrix_has_no_required_gap(self) -> None:
        from m_agent.testing import coverage_matrix_gaps

        markdown = (ROOT / "docs/acceptance-coverage-matrix.md").read_text()
        self.assertEqual(coverage_matrix_gaps(markdown, _manifest()), ())

    def test_gap_detection_reports_missing_required_rows(self) -> None:
        from m_agent.testing import coverage_matrix_gaps

        markdown = (ROOT / "docs/acceptance-coverage-matrix.md").read_text()
        # 删掉一行 documented row：对应 required check 应成为 gap。
        lines = markdown.splitlines(keepends=True)
        removed = [
            line
            for line in lines
            if line.startswith("| `model.routing.explicit-promotion`")
        ]
        self.assertEqual(len(removed), 1)
        mutated = "".join(
            line for line in lines if line is not removed[0]
        )
        gaps = coverage_matrix_gaps(mutated, _manifest())
        self.assertIn("model.routing.explicit-promotion", gaps)


class DemoReplayTests(unittest.TestCase):
    """The 12-15 minute demo and 3-minute fallback replay from Bundles."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _manifest()
        cls.results = _all_pass(cls.manifest)
        cls.execution = PackExecution.create(
            cls.manifest, execution_id="foundation-release-0-5-demo"
        ).start(cls.manifest)
        cls.completed = cls.execution.complete(cls.manifest, cls.results)
        declared = {check.check_id: check for check in cls.manifest.required_checks}
        cls.evidence_view = {
            declared[result.check_id].authoritative_evidence: result.evidence_digest
            for result in cls.results
        }
        cls.independent = {
            check.independent_evidence: "sha256:" + "8" * 64
            for check in cls.manifest.required_checks
        }
        cls.bundles = tuple(
            ScenarioEvidenceBundle.create(
                manifest=cls.manifest,
                execution=cls.completed,
                execution_checks=cls.results,
                scenario=scenario,
                checks=tuple(
                    result
                    for result in cls.results
                    if declared[result.check_id].scenario == scenario
                ),
                evidence_view=cls.evidence_view,
                independent_evidence=cls.independent,
            )
            for scenario in cls.manifest.scenarios
        )

    def test_full_demo_renders_from_all_six_passed_bundles(self) -> None:
        from m_agent.testing import PRE_GENERATED_EVIDENCE_LABEL, render_release_demo

        transcript = render_release_demo(self.bundles, mode="full")
        for scenario in self.manifest.scenarios:
            self.assertIn(scenario, transcript)
        # 12–15 分钟结构：总时长声明在 12 到 15 分钟之间。
        self.assertIn("12–15", transcript)
        # 预生成证据必须明确标注。
        self.assertIn(PRE_GENERATED_EVIDENCE_LABEL, transcript)
        # RC 身份与 non-claim 呈现。
        self.assertIn(self.manifest.artifact_digest, transcript)
        self.assertIn("non-claim", transcript.lower())
        self.assertIn("PROVIDER", transcript)
        self.assertIn("NOT a live provider", transcript)

    def test_short_demo_renders_recovery_and_report(self) -> None:
        from m_agent.testing import render_release_demo

        transcript = render_release_demo(self.bundles, mode="short")
        self.assertIn("3", transcript)
        self.assertIn(DURABLE_EFFECTS_SCENARIO, transcript)
        self.assertIn("recovery", transcript.lower())

    def test_demo_requires_passed_execution(self) -> None:
        from m_agent.testing import render_release_demo

        failing_results = tuple(
            _result(
                check,
                (
                    AcceptanceCheckStatus.FAIL
                    if check.scenario is MODEL_ROUTING_SCENARIO
                    else AcceptanceCheckStatus.PASS
                ),
            )
            for check in self.manifest.required_checks
        )
        failed = self.execution.complete(self.manifest, failing_results)
        declared = {check.check_id: check for check in self.manifest.required_checks}
        failed_view = {
            declared[result.check_id].authoritative_evidence: result.evidence_digest
            for result in failing_results
        }
        failed_independent = {
            check.independent_evidence: "sha256:" + "8" * 64
            for check in self.manifest.required_checks
        }
        failed_bundle = ScenarioEvidenceBundle.create(
            manifest=self.manifest,
            execution=failed,
            execution_checks=failing_results,
            scenario=CORE_LIFECYCLE_SCENARIO,
            checks=tuple(
                result
                for result in failing_results
                if declared[result.check_id].scenario == CORE_LIFECYCLE_SCENARIO
            ),
            evidence_view=failed_view,
            independent_evidence=failed_independent,
        )
        with self.assertRaises(ValueError):
            render_release_demo(
                (failed_bundle, *self.bundles[1:]), mode="full"
            )

    def test_demo_refuses_tampered_bundle(self) -> None:
        from m_agent.testing import render_release_demo

        tampered = self.bundles[0].model_copy(
            update={
                "evidence_view": {**self.bundles[0].evidence_view, "run_succeeded": False}
            }
        )
        with self.assertRaises(BundleIntegrityError):
            render_release_demo((tampered, *self.bundles[1:]), mode="full")

    def test_full_demo_requires_all_six_scenarios(self) -> None:
        from m_agent.testing import render_release_demo

        with self.assertRaises(ValueError):
            render_release_demo(self.bundles[:5], mode="full")


class EvalWorkloadBenchmarkTests(unittest.TestCase):
    """Correctness-first Eval workload benchmark contracts."""

    def test_public_benchmark_freezes_the_workload(self) -> None:
        from benchmarks.eval_workload import MEASURED_ITEMS

        self.assertGreaterEqual(MEASURED_ITEMS, 10)

    def test_small_workload_validates_before_reporting_metrics(self) -> None:
        from benchmarks.eval_workload import execute_eval_workload

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = asyncio.run(
                execute_eval_workload(
                    database=root / "eval.sqlite3",
                    case_count=3,
                )
            )
        self.assertEqual(report["validation"]["status"], "PASS")
        self.assertEqual(report["validation"]["problems"], [])
        self.assertEqual(report["validation"]["suite_items"], 3)
        self.assertEqual(report["validation"]["observations"], 3)
        self.assertEqual(report["validation"]["evaluator_results"], 3)
        self.assertEqual(report["eval_store"]["integrity_check"], "ok")
        self.assertEqual(report["measurement"]["case_count"], 3)
        for metric in (
            "throughput_items_per_second",
            "item_latency_ms",
            "persistence_overhead_ms_per_item",
        ):
            self.assertIn(metric, report["metrics"])
        self.assertIn("p50", report["metrics"]["item_latency_ms"])
        self.assertIn("p95", report["metrics"]["item_latency_ms"])
        self.assertIn("not production", report["qualification"])
        self.assertIn("environment-qualified", report["qualification"])

    def test_workload_never_overwrites_or_accepts_invalid_arguments(self) -> None:
        from benchmarks.eval_workload import execute_eval_workload

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "eval.sqlite3"
            database.write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                asyncio.run(execute_eval_workload(database=database, case_count=1))
            with self.assertRaises(ValueError):
                asyncio.run(
                    execute_eval_workload(database=root / "other.sqlite3", case_count=0)
                )

    def test_baseline_comparison_stays_environment_qualified(self) -> None:
        from benchmarks.eval_workload import compare_with_baseline

        def report(
            python="3.11.9",
            system="Darwin",
            machine="arm64",
            validation="PASS",
            case_count=3,
            identity=None,
        ):
            return {
                "environment": {
                    "python": {"implementation": "CPython", "version": python},
                    "os": {"system": system, "release": "23.0", "version": "23.0"},
                    "cpu": {"machine": machine, "processor": "arm", "logical_count": 10},
                },
                "baseline_identity": identity
                or {
                    "artifact_digest": "sha256:" + "a" * 64,
                    "manifest_digest": "sha256:" + "b" * 64,
                },
                "measurement": {"case_count": case_count},
                "validation": {"status": validation},
                "metrics": {"throughput_items_per_second": 50.0},
            }

        baseline = report()
        baseline["metrics"]["throughput_items_per_second"] = 40.0
        comparison = compare_with_baseline(report(), baseline)
        self.assertEqual(comparison["status"], "COMPARED")
        self.assertAlmostEqual(
            comparison["metrics"]["throughput_delta_ratio"], 0.25
        )
        self.assertIn("not production", comparison["non_claim"])
        for mutated in (
            report(python="3.12.1"),
            report(system="Linux"),
            report(machine="x86_64"),
            report(validation="FAIL"),
            report(case_count=6),
            report(identity={"artifact_digest": "", "manifest_digest": ""}),
        ):
            with self.subTest(mutated=mutated["environment"]):
                self.assertEqual(
                    compare_with_baseline(report(), mutated)["status"],
                    "INCONCLUSIVE",
                )


class Release05MaterialConsistencyTests(unittest.TestCase):
    """0.5 release material agrees with the actual candidate."""

    def test_distribution_version_is_0_5_0(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["version"], "0.5.0")

    def test_coverage_matrix_documents_the_0_5_release_rows(self) -> None:
        matrix = (ROOT / "docs/acceptance-coverage-matrix.md").read_text()
        for marker in (
            "foundation-release-0-5",
            "model.routing.typed-capability",
            "model.routing.operational-limits",
            "model.routing.usage-cost",
            "model.routing.deployment-constraints",
            "model.routing.six-outcomes",
            "model.routing.fallback",
            "model.routing.zero-side-effect",
            "model.routing.immutable-decision",
            "model.routing.no-in-run-switch",
            "model.routing.explicit-promotion",
            "model.routing.mutation",
            "model.routing.host-wheel",
            "eval.regression.durable-recovery",
            "eval.regression.judge-isolation",
            "eval.regression.baseline-comparison",
            "eval.regression.regression-detection",
            "eval.regression.report-metrics",
            "eval.regression.observe-projection",
            "eval.regression.recommendation-readonly",
            "eval.regression.mutation",
            "eval.regression.host-wheel",
        ):
            self.assertIn(marker, matrix, marker)

    def test_coverage_matrix_records_0_5_platform_and_non_goals(self) -> None:
        matrix = (ROOT / "docs/acceptance-coverage-matrix.md").read_text()
        for non_goal in (
            "automatic promotion",
            "in-run model switching",
            "live provider",
            "production capacity",
            "SLO",
            "exactly-once",
            "0.6",
        ):
            self.assertIn(non_goal, matrix, non_goal)

    def test_readme_documents_the_0_5_candidate(self) -> None:
        readme = (ROOT / "README.md").read_text()
        self.assertIn("0.5.0", readme)
        self.assertIn("model routing", readme.lower())
        self.assertIn("eval", readme.lower())

    def test_eval_benchmark_documented(self) -> None:
        benchmark_readme = (ROOT / "benchmarks/README.md").read_text()
        self.assertIn("eval_workload", benchmark_readme)


if __name__ == "__main__":
    unittest.main()
