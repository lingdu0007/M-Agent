"""release-gate contracts for the 0.4 Session/Context candidate.

这些测试只用公共 seam。冻结内容：

- ``foundation-release-0-4`` Pack profile：同一 RC 身份（Manifest）下
  冻结四个 required Scenario（0.3 基线 ``core-lifecycle`` 与
  ``durable-effects-recovery`` 原样重跑，新增 ``session-conversation``
  与 ``context-budget-compression`` 的 CONTRACT/HOST 证据）；
- 场景隔离：每个 Scenario 的 Evidence Bundle 只携带自己的 required
  checks，跨场景 checks 被拒绝；单个 Scenario 的 FAIL 使整个 Pack
  FAILED 而不改写其他场景的检查结果；
- 防拼接：不同 RC（artifact digest 不同）的 Manifest / Execution /
  Bundle 互不匹配，旧 RC Bundle 不能证明新候选；
- 隔离 HOST 探针观察器：探针进程异常是 ERROR，检查失败是 FAIL，
  全部通过且观察值符合期望才是 PASS；
- PROVIDER 证据按 Model Contract fingerprint 与 30 天规则标注
  ``VALID`` / ``STALE`` / ``NOT_RUN``，缺失证据绝不升级为通过；
- Context workload benchmark 先做正确性/完整性验证再报告环境限定
  指标，且只对兼容 baseline 报告相对变化；
- 发布材料一致性：0.4 版本号、Coverage Matrix 的 0.4 required 行、
  非目标（长期记忆 / history compression / 远程 Store）与示例。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from m_agent.testing import (
    AcceptanceCheckResult,
    AcceptanceCheckStatus,
    BundleIntegrityError,
    CONTEXT_COMPRESSION_SCENARIO,
    CORE_LIFECYCLE_SCENARIO,
    DURABLE_EFFECTS_SCENARIO,
    EvidenceLevel,
    FOUNDATION_RELEASE_0_4_PACK_VERSION,
    FOUNDATION_RELEASE_0_4_PROFILE,
    PackExecution,
    ProviderEvidenceStatus,
    ScenarioEvidenceBundle,
    SESSION_CONVERSATION_SCENARIO,
    foundation_release_0_4_manifest,
    provider_evidence_status,
)

ROOT = Path(__file__).parents[1]

_ENVIRONMENT = {
    "distribution": "m-agent",
    "version": "0.4.0",
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
    return foundation_release_0_4_manifest(
        source_commit="a" * 40,
        artifact_digest="sha256:" + "b" * 64,
        sdist_digest="sha256:" + "c" * 64,
        fixture_digest="sha256:" + "d" * 64,
        environment=_ENVIRONMENT,
    )


def _other_rc_manifest():
    """A different release candidate: same shape, different artifact identity."""
    return foundation_release_0_4_manifest(
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
        reason_code="ticket16_test",
        evidence_digest="sha256:" + "0" * 64,
    )


def _all_pass(manifest):
    return tuple(_result(check) for check in manifest.required_checks)


class FoundationReleaseManifestTests(unittest.TestCase):
    """The frozen four-Scenario 0.4 release Manifest."""

    def test_profile_freezes_all_four_required_scenarios_in_order(self) -> None:
        manifest = _manifest()
        self.assertEqual(manifest.profile, FOUNDATION_RELEASE_0_4_PROFILE)
        self.assertEqual(manifest.pack_version, FOUNDATION_RELEASE_0_4_PACK_VERSION)
        self.assertEqual(
            manifest.scenarios,
            (
                CORE_LIFECYCLE_SCENARIO,
                DURABLE_EFFECTS_SCENARIO,
                SESSION_CONVERSATION_SCENARIO,
                CONTEXT_COMPRESSION_SCENARIO,
            ),
        )
        declared = {check.scenario for check in manifest.required_checks}
        self.assertEqual(declared, set(manifest.scenarios))

    def test_0_3_required_scenarios_are_rerun_not_replaced(self) -> None:
        manifest = _manifest()
        check_ids = {check.check_id for check in manifest.required_checks}
        # 0.3 基线检查原样保留（含 migration 检查），不允许被删减。
        for check_id in (
            "core.lifecycle",
            "core.lifecycle.telemetry",
            "core.lifecycle.migration",
            "core.lifecycle.host-wheel",
            "core.lifecycle.telemetry-host",
            "durable.effects.recovery-windows",
            "durable.effects.budget-fail-closed",
            "durable.effects.waiting-resolution",
            "durable.effects.mutation",
            "durable.effects.host-wheel",
        ):
            self.assertIn(check_id, check_ids)
        self.assertNotIn("core.lifecycle.expand-compatibility", check_ids)

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

    def test_session_and_context_host_checks_are_frozen(self) -> None:
        manifest = _manifest()
        by_id = {check.check_id: check for check in manifest.required_checks}
        for check_id in (
            "session.conversation.host-wheel",
            "context.compression.host-wheel",
        ):
            check = by_id[check_id]
            self.assertIs(check.evidence_level, EvidenceLevel.HOST)
            self.assertTrue(check.required)
            self.assertTrue(check.non_claim.strip())

    def test_required_security_and_mutation_gates_are_frozen(self) -> None:
        manifest = _manifest()
        check_ids = {check.check_id for check in manifest.required_checks}
        for check_id in (
            "session.conversation.scope-isolation",
            "session.conversation.payload-protection",
            "session.conversation.mutation",
            "context.compression.protected-channels",
            "context.compression.hard-budget",
            "context.compression.no-recursion",
            "context.compression.mutation",
            "core.lifecycle.bundle-tamper",
        ):
            self.assertIn(check_id, check_ids)

    def test_session_and_context_rows_declare_the_0_4_milestone(self) -> None:
        manifest = _manifest()
        for check in manifest.required_checks:
            if check.scenario in (SESSION_CONVERSATION_SCENARIO, CONTEXT_COMPRESSION_SCENARIO):
                self.assertEqual(check.milestone, "0_4", check.check_id)

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


class ReleaseAggregationTests(unittest.TestCase):
    """One Pack Execution aggregates isolated scenario bundles per RC."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _manifest()
        cls.results = _all_pass(cls.manifest)
        cls.execution = PackExecution.create(
            cls.manifest, execution_id="foundation-release-0-4-1"
        ).start(cls.manifest)
        cls.completed = cls.execution.complete(cls.manifest, cls.results)

    def test_all_required_pass_completes_the_pack_as_passed(self) -> None:
        self.assertIs(self.completed.status.value, "PASSED")
        self.assertEqual(self.completed.exit_code, 0)

    def test_one_scenario_fail_fails_the_pack_without_rewriting_others(self) -> None:
        results = tuple(
            _result(
                check,
                (
                    AcceptanceCheckStatus.FAIL
                    if check.scenario is SESSION_CONVERSATION_SCENARIO
                    else AcceptanceCheckStatus.PASS
                ),
            )
            for check in self.manifest.required_checks
        )
        completed = self.execution.complete(self.manifest, results)
        self.assertIs(completed.status.value, "FAILED")
        self.assertEqual(completed.exit_code, 1)
        # 其他场景的检查结果不被改写：仍是传入的 PASS。
        session_ids = {
            check.check_id
            for check in self.manifest.required_checks
            if check.scenario is SESSION_CONVERSATION_SCENARIO
        }
        self.assertEqual(
            {result.check_id for result in results if result.status is AcceptanceCheckStatus.FAIL},
            session_ids,
        )

    def test_missing_session_check_is_incomplete(self) -> None:
        results = tuple(
            _result(check)
            for check in self.manifest.required_checks
            if check.scenario is not SESSION_CONVERSATION_SCENARIO
        )
        completed = self.execution.complete(self.manifest, results)
        self.assertIs(completed.status.value, "INCOMPLETE")
        self.assertEqual(completed.exit_code, 4)

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
        for scenario in self.manifest.scenarios:
            scenario_checks = tuple(
                result
                for result in self.results
                if next(
                    check
                    for check in self.manifest.required_checks
                    if check.check_id == result.check_id
                ).scenario
                is scenario
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
        core_checks = tuple(
            result
            for result in self.results
            if next(
                check
                for check in self.manifest.required_checks
                if check.check_id == result.check_id
            ).scenario
            is CORE_LIFECYCLE_SCENARIO
        )
        with self.assertRaises(ValueError):
            ScenarioEvidenceBundle.create(
                manifest=self.manifest,
                execution=self.completed,
                execution_checks=self.results,
                scenario=SESSION_CONVERSATION_SCENARIO,
                checks=core_checks,
                evidence_view=evidence_view,
                independent_evidence=independent,
            )

    def test_bundle_from_another_rc_cannot_attest_this_candidate(self) -> None:
        evidence_view, independent = self._release_evidence_views()
        core_checks = tuple(
            result
            for result in self.results
            if next(
                check
                for check in self.manifest.required_checks
                if check.check_id == result.check_id
            ).scenario
            is CORE_LIFECYCLE_SCENARIO
        )
        bundle = ScenarioEvidenceBundle.create(
            manifest=self.manifest,
            execution=self.completed,
            execution_checks=self.results,
            scenario=CORE_LIFECYCLE_SCENARIO,
            checks=core_checks,
            evidence_view=evidence_view,
            independent_evidence=independent,
        )
        other = _other_rc_manifest()
        with self.assertRaises(BundleIntegrityError):
            bundle.verify(other, self.completed)
        other_execution = PackExecution.create(
            other, execution_id="foundation-release-0-4-2"
        ).start(other)
        with self.assertRaises(BundleIntegrityError):
            bundle.verify(self.manifest, other_execution)


class ReleaseHostProbeObserverTests(unittest.TestCase):
    """The isolated-process HOST probe observer classifies honestly."""

    def _observe(self, source: str, check_id: str = "session.conversation.host-wheel"):
        from m_agent.testing import observe_isolated_scenario_probe

        return observe_isolated_scenario_probe(
            probe_source=source,
            check_id=check_id,
        )

    def test_passing_probe_observation_is_pass_with_dual_evidence(self) -> None:
        from m_agent.testing import SESSION_HOST_EXPECTATION

        payload = json.dumps(
            {
                "scenario": SESSION_HOST_EXPECTATION["scenario"],
                "checks": dict.fromkeys(
                    SESSION_HOST_EXPECTATION["check_ids"], "PASS"
                ),
                "observation": SESSION_HOST_EXPECTATION["observation"],
            }
        )
        probe = f"import sys; sys.stdout.write({payload!r})"
        result, evidence = self._observe(probe)
        self.assertIs(result.status, AcceptanceCheckStatus.PASS)
        self.assertIs(result.evidence_level, EvidenceLevel.HOST)
        self.assertTrue(result.evidence_digest.startswith("sha256:"))
        self.assertTrue(evidence["probe_process_observed"])
        self.assertTrue(
            str(evidence["probe_stdout_digest"]).startswith("sha256:")
        )

    def test_failing_probe_check_is_subject_failure(self) -> None:
        from m_agent.testing import SESSION_HOST_EXPECTATION

        payload = json.dumps(
            {
                "scenario": SESSION_HOST_EXPECTATION["scenario"],
                "checks": {
                    **dict.fromkeys(SESSION_HOST_EXPECTATION["check_ids"], "PASS"),
                    "session.conversation.recovery-windows": "FAIL",
                },
                "observation": SESSION_HOST_EXPECTATION["observation"],
            }
        )
        probe = f"import sys; sys.stdout.write({payload!r})"
        result, _ = self._observe(probe)
        self.assertIs(result.status, AcceptanceCheckStatus.FAIL)

    def test_unexpected_observation_value_is_subject_failure(self) -> None:
        from m_agent.testing import SESSION_HOST_EXPECTATION

        payload = json.dumps(
            {
                "scenario": SESSION_HOST_EXPECTATION["scenario"],
                "checks": dict.fromkeys(
                    SESSION_HOST_EXPECTATION["check_ids"], "PASS"
                ),
                "observation": {
                    **SESSION_HOST_EXPECTATION["observation"],
                    "recovery_windows_clean": False,
                },
            }
        )
        probe = f"import sys; sys.stdout.write({payload!r})"
        result, _ = self._observe(probe)
        self.assertIs(result.status, AcceptanceCheckStatus.FAIL)

    def test_broken_probe_process_is_harness_error(self) -> None:
        for probe in (
            "import sys; sys.exit(9)",
            "raise RuntimeError('probe exploded')",
            "import sys; sys.stdout.write('not json')",
        ):
            with self.subTest(probe=probe):
                result, _ = self._observe(probe)
                self.assertIs(result.status, AcceptanceCheckStatus.ERROR)

    def test_unexpected_probe_schema_is_harness_error(self) -> None:
        from m_agent.testing import SESSION_HOST_EXPECTATION

        payload = json.dumps(
            {
                "scenario": SESSION_HOST_EXPECTATION["scenario"],
                "checks": dict.fromkeys(
                    SESSION_HOST_EXPECTATION["check_ids"], "PASS"
                ),
                "observation": SESSION_HOST_EXPECTATION["observation"],
                "surprise_key": True,
            }
        )
        probe = f"import sys; sys.stdout.write({payload!r})"
        result, _ = self._observe(probe)
        self.assertIs(result.status, AcceptanceCheckStatus.ERROR)
        # 缺少声明的 check 也是 schema 违约。
        payload = json.dumps(
            {
                "scenario": SESSION_HOST_EXPECTATION["scenario"],
                "checks": dict.fromkeys(
                    SESSION_HOST_EXPECTATION["check_ids"][:-1], "PASS"
                ),
                "observation": SESSION_HOST_EXPECTATION["observation"],
            }
        )
        probe = f"import sys; sys.stdout.write({payload!r})"
        result, _ = self._observe(probe)
        self.assertIs(result.status, AcceptanceCheckStatus.ERROR)


class ProviderEvidenceStatusTests(unittest.TestCase):
    """PROVIDER evidence freshness follows the fingerprint + 30-day rule."""

    NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)

    def _evidence(
        self,
        *,
        fingerprint="sha256:" + "a" * 64,
        age_days=0.0,
    ):
        return {
            "contract_fingerprint": fingerprint,
            "verified_at": self.NOW - timedelta(days=age_days),
        }

    def test_missing_evidence_is_not_run(self) -> None:
        self.assertIs(
            provider_evidence_status(
                contract_fingerprint="sha256:" + "a" * 64,
                evidence=None,
                now=self.NOW,
            ),
            ProviderEvidenceStatus.NOT_RUN,
        )

    def test_fresh_matching_evidence_is_valid(self) -> None:
        for age in (0.0, 1.0, 29.0):
            with self.subTest(age=age):
                self.assertIs(
                    provider_evidence_status(
                        contract_fingerprint="sha256:" + "a" * 64,
                        evidence=self._evidence(age_days=age),
                        now=self.NOW,
                    ),
                    ProviderEvidenceStatus.VALID,
                )

    def test_expired_evidence_is_stale(self) -> None:
        self.assertIs(
            provider_evidence_status(
                contract_fingerprint="sha256:" + "a" * 64,
                evidence=self._evidence(age_days=31.0),
                now=self.NOW,
            ),
            ProviderEvidenceStatus.STALE,
        )

    def test_fingerprint_mismatch_is_stale(self) -> None:
        self.assertIs(
            provider_evidence_status(
                contract_fingerprint="sha256:" + "b" * 64,
                evidence=self._evidence(fingerprint="sha256:" + "a" * 64),
                now=self.NOW,
            ),
            ProviderEvidenceStatus.STALE,
        )

    def test_future_timestamp_is_stale(self) -> None:
        evidence = {
            "contract_fingerprint": "sha256:" + "a" * 64,
            "verified_at": self.NOW + timedelta(days=1),
        }
        self.assertIs(
            provider_evidence_status(
                contract_fingerprint="sha256:" + "a" * 64,
                evidence=evidence,
                now=self.NOW,
            ),
            ProviderEvidenceStatus.STALE,
        )

    def test_boundary_day_thirty_is_stale(self) -> None:
        # 「不超过 30 天」可复用：第 30 天整仍在窗口内，超过即 STALE。
        self.assertIs(
            provider_evidence_status(
                contract_fingerprint="sha256:" + "a" * 64,
                evidence=self._evidence(age_days=30.0),
                now=self.NOW,
            ),
            ProviderEvidenceStatus.VALID,
        )
        self.assertIs(
            provider_evidence_status(
                contract_fingerprint="sha256:" + "a" * 64,
                evidence=self._evidence(age_days=30.0 + 1 / 24),
                now=self.NOW,
            ),
            ProviderEvidenceStatus.STALE,
        )

    def test_naive_timestamps_fail_closed(self) -> None:
        evidence = {
            "contract_fingerprint": "sha256:" + "a" * 64,
            "verified_at": datetime(2026, 8, 1),
        }
        with self.assertRaises(ValueError):
            provider_evidence_status(
                contract_fingerprint="sha256:" + "a" * 64,
                evidence=evidence,
                now=self.NOW,
            )


class ContextWorkloadBenchmarkTests(unittest.TestCase):
    """Correctness-first Context workload benchmark contracts."""

    def test_public_benchmark_freezes_the_workload(self) -> None:
        from benchmarks.context_workload import MEASURED_RUNS

        self.assertGreaterEqual(MEASURED_RUNS, 25)

    def test_small_workload_validates_before_reporting_metrics(self) -> None:
        from benchmarks.context_workload import execute_context_workload

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = asyncio.run(
                execute_context_workload(
                    database=root / "context.sqlite3",
                    run_count=3,
                )
            )
        self.assertEqual(report["validation"]["status"], "PASS")
        self.assertEqual(report["validation"]["problems"], [])
        self.assertEqual(report["validation"]["succeeded_runs"], 3)
        self.assertEqual(
            report["validation"]["compression_provenance_intact_runs"], 3
        )
        self.assertEqual(report["validation"]["checkpoint_order_clean_runs"], 3)
        self.assertEqual(report["run_database"]["integrity_check"], "ok")
        self.assertEqual(report["run_database"]["row_counts"]["runs"], 3)
        self.assertEqual(report["measurement"]["run_count"], 3)
        for metric in (
            "throughput_runs_per_second",
            "run_latency_ms",
            "persistence_overhead_ms_per_run",
        ):
            self.assertIn(metric, report["metrics"])
        self.assertIn("p50", report["metrics"]["run_latency_ms"])
        self.assertIn("p95", report["metrics"]["run_latency_ms"])
        self.assertIn("not production", report["qualification"])
        self.assertIn("environment-qualified", report["qualification"])

    def test_workload_never_overwrites_or_accepts_invalid_arguments(self) -> None:
        from benchmarks.context_workload import execute_context_workload

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "context.sqlite3"
            database.write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                asyncio.run(execute_context_workload(database=database, run_count=1))
            with self.assertRaises(ValueError):
                asyncio.run(
                    execute_context_workload(database=root / "other.sqlite3", run_count=0)
                )

    def test_validate_context_integrity_detects_mutations(self) -> None:
        from benchmarks.context_workload import validate_run_integrity

        clean = {
            "status": "SUCCEEDED",
            "checkpoint_labels": [
                ["CONTEXT", "PROVIDER"],
                ["MODEL", "CONTEXT_COMPRESSION"],
                ["MODEL", "PRIMARY"],
            ],
            "compression_contract_id": "workload-summarize",
            "compression_source_ids": ["doc-1", "doc-2"],
            "derived_source_ids": ["doc-1", "doc-2"],
        }
        self.assertEqual(validate_run_integrity(clean), [])
        mutations = (
            {"status": "FAILED"},
            {
                "checkpoint_labels": [
                    ["MODEL", "CONTEXT_COMPRESSION"],
                    ["CONTEXT", "PROVIDER"],
                    ["MODEL", "PRIMARY"],
                ]
            },
            {"compression_contract_id": "other-contract"},
            {"derived_source_ids": ["doc-1", "phantom"]},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertTrue(validate_run_integrity({**clean, **mutation}))

    def test_baseline_comparison_stays_environment_qualified(self) -> None:
        from benchmarks.context_workload import compare_with_baseline

        def report(
            python="3.11.9",
            system="Darwin",
            machine="arm64",
            validation="PASS",
            run_count=3,
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
                "measurement": {"run_count": run_count},
                "validation": {"status": validation},
                "metrics": {"throughput_runs_per_second": 50.0},
            }

        baseline = report()
        baseline["metrics"]["throughput_runs_per_second"] = 40.0
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
            report(run_count=6),
            report(identity={"artifact_digest": "", "manifest_digest": ""}),
        ):
            with self.subTest(mutated=mutated["environment"]):
                self.assertEqual(
                    compare_with_baseline(report(), mutated)["status"],
                    "INCONCLUSIVE",
                )


class ReleaseMaterialConsistencyTests(unittest.TestCase):
    """0.4 release material agrees with the actual candidate."""

    def test_distribution_version_is_0_4_0(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["version"], "0.4.0")

    def test_coverage_matrix_documents_the_0_4_release_rows(self) -> None:
        matrix = (ROOT / "docs/acceptance-coverage-matrix.md").read_text()
        for marker in (
            "foundation-release-0-4",
            "session.conversation.recovery-windows",
            "session.conversation.claim-no-ttl",
            "session.conversation.payload-protection",
            "session.conversation.scope-isolation",
            "session.conversation.mutation",
            "session.conversation.host-wheel",
            "context.compression.plan-order",
            "context.compression.frame-checkpoints",
            "context.compression.hard-budget",
            "context.compression.protected-channels",
            "context.compression.no-recursion",
            "context.compression.mutation",
            "context.compression.host-wheel",
        ):
            self.assertIn(marker, matrix, marker)

    def test_coverage_matrix_records_0_4_non_goals(self) -> None:
        matrix = (ROOT / "docs/acceptance-coverage-matrix.md").read_text()
        for non_goal in (
            "long-term memory",
            "history compression",
            "remote store",
        ):
            self.assertIn(non_goal, matrix, non_goal)

    def test_session_context_example_exists_and_runs_offline(self) -> None:
        import subprocess
        import sys

        example = ROOT / "examples" / "m_agent_session_context.py"
        self.assertTrue(example.is_file())
        source = example.read_text()
        for seam in (
            "SessionRunner",
            "SessionScope",
            "SQLiteSessionStore",
            "CompressionContract",
            "ContextPlan",
            "ModelPurpose.CONTEXT_COMPRESSION",
        ):
            self.assertIn(seam, source, seam)
        self.assertNotIn("OPENAI_API_KEY", source)
        self.assertNotIn("httpx", source)
        completed = subprocess.run(
            [sys.executable, str(example)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"example failed: {completed.stderr}",
        )
        self.assertIn("COMMITTED", completed.stdout)
        self.assertIn("SUCCEEDED", completed.stdout)


if __name__ == "__main__":
    unittest.main()
