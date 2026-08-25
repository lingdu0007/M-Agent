"""Ticket 13 acceptance contracts for the session-conversation Scenario.

这些测试只用公共 seam。冻结内容：

- ``session-conversation`` Reference Scenario：三个已决议的跨 Store
  crash window（Claim 后 Run 创建前 / Core 成功后 Session 提交前 /
  Turn 追加与 Claim 清理原子边界）各连续重复三次，真实子进程在公共
  边界硬退出后由父进程只经公开 API reopen 对账，恢复后分别得到
  CONFLICT、PENDING、COMMITTED 且不改写 Core 终态；
- claim 无 TTL：重启后 claim 原样存活，第二个 Run 不得静默占用；
- Session Payload 独立保护：可搜索 metadata 与原始库文件均不含对话
  正文，错误 key fail closed，正确 key 历史完整；
- 公开对账 seam（``reconcile_session_recovery`` /
  ``reconcile_session_protection``）必须检出篡改、duplicate-submit、
  wrong-scope、wrong-key 四类受控变异；
- ``session-conversation`` Manifest 与双源 Evidence Bundle：全部
  required 检查 PASS 时 Pack 为 PASSED，缺一为 INCOMPLETE，Bundle
  篡改被检出，且 Bundle 不泄露对话正文。
"""

from __future__ import annotations

import unittest

from m_agent.testing import (
    AcceptanceCheckStatus,
    BundleIntegrityError,
    EvidenceLevel,
    PackExecution,
    SESSION_CONVERSATION_PACK_VERSION,
    SESSION_CONVERSATION_PROFILE,
    SESSION_CONVERSATION_SCENARIO,
    ScenarioEvidenceBundle,
    reconcile_session_protection,
    reconcile_session_recovery,
    run_session_conversation,
    session_conversation_manifest,
)

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

_CANARY = "SESSION-SECRET-CANARY-T13"


def _manifest():
    return session_conversation_manifest(
        source_commit="a" * 40,
        artifact_digest="sha256:" + "b" * 64,
        sdist_digest="sha256:" + "c" * 64,
        fixture_digest="sha256:" + "d" * 64,
        environment=_ENVIRONMENT,
    )


class SessionConversationScenarioTests(unittest.TestCase):
    """CONTRACT evidence for the durable Session recovery Scenario."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results, cls.evidence_view, cls.independent_evidence = (
            run_session_conversation()
        )
        cls.by_id = {result.check_id: result for result in cls.results}

    def test_required_checks_all_pass_at_contract_level(self) -> None:
        self.assertEqual(len(self.results), 5)
        for result in self.results:
            with self.subTest(check_id=result.check_id):
                self.assertIs(result.status, AcceptanceCheckStatus.PASS)
                self.assertIs(result.evidence_level, EvidenceLevel.CONTRACT)

    def test_three_windows_repeat_three_times_with_correct_outcomes(self) -> None:
        # 三个窗口 × 三次重复（ADR 0042），每窗口恢复后产生
        # 正确的 CONFLICT / PENDING / COMMITTED。
        self.assertEqual(
            self.evidence_view["recovery_window_repetitions"], 9
        )
        self.assertIs(self.evidence_view["recovery_windows_clean"], True)
        self.assertIs(
            self.evidence_view["recovery_window_outcomes_correct"], True
        )
        self.assertIs(
            self.evidence_view["recovery_window_conflict_observed"], True
        )
        self.assertIs(
            self.evidence_view["recovery_window_pending_observed"], True
        )
        self.assertIs(
            self.evidence_view["recovery_window_committed_observed"], True
        )
        self.assertIs(
            self.evidence_view["recovery_windows_core_terminal_preserved"],
            True,
        )

    def test_claim_has_no_ttl_across_restart(self) -> None:
        self.assertIs(self.evidence_view["claim_no_ttl_observed"], True)
        self.assertEqual(self.evidence_view["claim_no_ttl_repetitions"], 9)

    def test_payload_protection_and_scope_isolation_observed(self) -> None:
        self.assertIs(self.evidence_view["payload_protection_observed"], True)
        self.assertIs(self.evidence_view["scope_isolation_observed"], True)

    def test_all_four_mutations_are_detected(self) -> None:
        self.assertIs(self.evidence_view["mutation_detected"], True)
        for key in (
            "mutation_tamper_detected",
            "mutation_duplicate_submit_detected",
            "mutation_wrong_scope_detected",
            "mutation_wrong_key_detected",
        ):
            with self.subTest(mutation=key):
                self.assertIs(self.evidence_view[key], True)

    def test_every_passing_check_binds_dual_source_evidence(self) -> None:
        manifest = _manifest()
        for check in manifest.required_checks:
            result = self.by_id[check.check_id]
            with self.subTest(check_id=check.check_id):
                self.assertEqual(
                    self.evidence_view[check.authoritative_evidence],
                    result.evidence_digest,
                )
                independent = self.independent_evidence[
                    check.independent_evidence
                ]
                self.assertTrue(independent.startswith("sha256:"))


class ReconcileSessionRecoveryTests(unittest.TestCase):
    """The public reconciliation seam detects every controlled mutation."""

    def _w1(self, **overrides):
        observation = {
            "window": "after_claim_before_run_creation",
            "repetition": 0,
            "claim_survived_restart": True,
            "second_run_blocked": True,
            "reconciled_missing_run": True,
            "claim_released_after_reconciliation": True,
            "followup_run_id": "followup-1",
            "followup_commit_status": "COMMITTED",
            "turn_run_ids": ["followup-1"],
            "version": 1,
            "observed_status": "CONFLICT",
        }
        observation.update(overrides)
        return observation

    def _w1_journal(self):
        return [
            {"kind": "session_created"},
            {"kind": "claim_created", "run_id": "ghost-1"},
        ]

    def _w2(self, **overrides):
        observation = {
            "window": "after_core_success_before_commit",
            "repetition": 0,
            "claim_survived_restart": True,
            "core_status_after_crash": "SUCCEEDED",
            "commit_status_after_crash": "PENDING",
            "recovery_commit_status": "COMMITTED",
            "turn_run_ids": ["run-1"],
            "version": 1,
            "claim_cleared": True,
            "core_status_after_recovery": "SUCCEEDED",
            "observed_status": "PENDING",
        }
        observation.update(overrides)
        return observation

    def _w2_journal(self):
        return [
            {"kind": "session_created"},
            {"kind": "claim_created", "run_id": "run-1"},
            {"kind": "run_created", "run_id": "run-1"},
            {"kind": "run_succeeded", "run_id": "run-1"},
        ]

    def _w3(self, **overrides):
        observation = {
            "window": "at_commit_atomic_boundary",
            "repetition": 0,
            "claim_survived_restart": True,
            "sentinel_reached": True,
            "partial_state_after_crash": False,
            "commit_status_after_crash": "PENDING",
            "recovery_commit_status": "COMMITTED",
            "turn_run_ids": ["run-1"],
            "version": 1,
            "replay_status": "COMMITTED",
            "replay_turn_count": 1,
            "core_status_after_recovery": "SUCCEEDED",
            "observed_status": "COMMITTED",
        }
        observation.update(overrides)
        return observation

    def _w3_journal(self):
        return [
            {"kind": "session_created"},
            {"kind": "claim_created", "run_id": "run-1"},
            {"kind": "run_created", "run_id": "run-1"},
            {"kind": "run_succeeded", "run_id": "run-1"},
            {"kind": "commit_boundary_entered"},
        ]

    def test_clean_observations_reconcile_without_problems(self) -> None:
        for observation, journal in (
            (self._w1(), self._w1_journal()),
            (self._w2(), self._w2_journal()),
            (self._w3(), self._w3_journal()),
        ):
            with self.subTest(window=observation["window"]):
                self.assertEqual(
                    reconcile_session_recovery(
                        observation, journal_events=journal
                    ),
                    [],
                )

    def test_tamper_mutation_is_detected(self) -> None:
        # 篡改：历史里出现 journal 从未背书的 phantom turn。
        tampered = self._w2(turn_run_ids=["run-1", "phantom-run"])
        problems = reconcile_session_recovery(
            tampered, journal_events=self._w2_journal()
        )
        self.assertIn("unattested_turn_in_history", problems)

    def test_w1_ghost_turn_in_history_is_detected(self) -> None:
        # W1 篡改：历史中出现 ghost run（claimed 但从未 created）的 Turn。
        ghost_tampered = self._w1(turn_run_ids=["ghost-1", "followup-1"])
        problems = reconcile_session_recovery(
            ghost_tampered, journal_events=self._w1_journal()
        )
        self.assertIn("phantom_turn_from_never_created_run", problems)

    def test_duplicate_submit_mutation_is_detected(self) -> None:
        duplicated = self._w3(
            turn_run_ids=["run-1", "run-1"], replay_turn_count=2
        )
        problems = reconcile_session_recovery(
            duplicated, journal_events=self._w3_journal()
        )
        self.assertIn("duplicate_turn_identity", problems)
        self.assertIn("duplicate_turn_after_replay", problems)

    def test_core_terminal_rewrite_mutation_is_detected(self) -> None:
        rewritten = self._w2(core_status_after_recovery="FAILED")
        problems = reconcile_session_recovery(
            rewritten, journal_events=self._w2_journal()
        )
        self.assertIn("core_terminal_rewritten", problems)

    def test_partial_commit_mutation_is_detected(self) -> None:
        torn = self._w3(partial_state_after_crash=True)
        problems = reconcile_session_recovery(
            torn, journal_events=self._w3_journal()
        )
        self.assertIn("partial_commit_state", problems)

    def test_second_run_admission_mutation_is_detected(self) -> None:
        admitted = self._w1(second_run_blocked=False, observed_status="")
        problems = reconcile_session_recovery(
            admitted, journal_events=self._w1_journal()
        )
        self.assertIn("second_run_silently_admitted", problems)

    def test_wrong_window_is_rejected(self) -> None:
        problems = reconcile_session_recovery(
            {"window": "unexpected"}, journal_events=()
        )
        self.assertIn("unknown_window", problems)

    def test_protection_mutations_are_detected(self) -> None:
        clean = {
            "wrong_scope_access_blocked": True,
            "wrong_key_decode_failed": True,
            "searchable_metadata_clean": True,
            "raw_database_free_of_plaintext": True,
            "correct_key_history_intact": True,
            "metadata_readable_without_payload_key": True,
            "error_messages_free_of_conversation_text": True,
        }
        self.assertEqual(reconcile_session_protection(clean), [])
        for key, expected_problem in (
            ("wrong_scope_access_blocked", "wrong_scope_access_not_closed"),
            ("wrong_key_decode_failed", "wrong_key_decode_succeeded"),
            ("searchable_metadata_clean", "conversation_text_in_searchable_metadata"),
            ("raw_database_free_of_plaintext", "plaintext_history_on_disk"),
            ("correct_key_history_intact", "history_corrupted_with_correct_key"),
            ("error_messages_free_of_conversation_text", "error_message_leaks_conversation_text"),
        ):
            with self.subTest(mutated=key):
                problems = reconcile_session_protection(
                    {**clean, key: False}
                )
                self.assertIn(expected_problem, problems)


class SessionConversationManifestTests(unittest.TestCase):
    """The frozen durable Session recovery Scenario Manifest."""

    def test_manifest_freezes_the_scenario_and_five_required_checks(self) -> None:
        manifest = _manifest()
        self.assertEqual(manifest.pack_version, SESSION_CONVERSATION_PACK_VERSION)
        self.assertEqual(manifest.profile, SESSION_CONVERSATION_PROFILE)
        self.assertEqual(manifest.scenarios, (SESSION_CONVERSATION_SCENARIO,))
        self.assertEqual(
            {check.check_id for check in manifest.required_checks},
            {
                "session.conversation.recovery-windows",
                "session.conversation.claim-no-ttl",
                "session.conversation.payload-protection",
                "session.conversation.scope-isolation",
                "session.conversation.mutation",
            },
        )
        for check in manifest.required_checks:
            with self.subTest(check_id=check.check_id):
                self.assertIs(check.scenario, SESSION_CONVERSATION_SCENARIO)
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
                check.milestone == "0_4" and check.non_claim
                for check in manifest.required_checks
            )
        )


class SessionConversationBundleTests(unittest.TestCase):
    """Dual-source Evidence Bundle for the session-conversation Scenario."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _manifest()
        cls.results, cls.evidence_view, cls.independent_evidence = (
            run_session_conversation()
        )
        execution = PackExecution.create(
            cls.manifest, execution_id="session-conversation-1"
        ).start(cls.manifest)
        cls.completed = execution.complete(cls.manifest, cls.results)
        cls.bundle = ScenarioEvidenceBundle.create(
            manifest=cls.manifest,
            execution=cls.completed,
            execution_checks=cls.results,
            scenario=SESSION_CONVERSATION_SCENARIO,
            checks=cls.results,
            evidence_view=cls.evidence_view,
            independent_evidence=cls.independent_evidence,
        )

    def test_all_pass_results_complete_the_pack_as_passed(self) -> None:
        self.assertEqual(self.completed.exit_code, 0)

    def test_bundle_carries_dual_source_evidence_and_verifies(self) -> None:
        self.bundle.verify(self.manifest, self.completed)
        for key in (
            "recovery_windows_authoritative_digest",
            "claim_no_ttl_authoritative_digest",
            "payload_protection_authoritative_digest",
            "scope_isolation_authoritative_digest",
            "mutation_authoritative_digest",
        ):
            with self.subTest(authoritative=key):
                self.assertIn(key, self.bundle.evidence_view)
        for key in (
            "recovery_windows_journal_digest",
            "claim_no_ttl_journal_digest",
            "payload_protection_independent_digest",
            "scope_isolation_independent_digest",
            "mutation_independent_digest",
        ):
            with self.subTest(independent=key):
                self.assertIn(key, self.bundle.independent_evidence)

    def test_bundle_does_not_leak_conversation_text(self) -> None:
        bundle_text = self.bundle.model_dump_json()
        self.assertNotIn(_CANARY, bundle_text)
        self.assertNotIn("crash window message", bundle_text)
        self.assertNotIn("recovered follow up message", bundle_text)

    def test_controlled_bundle_mutation_is_detected(self) -> None:
        tampered = self.bundle.model_copy(
            update={
                "evidence_view": {
                    **self.bundle.evidence_view,
                    "recovery_windows_clean": False,
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
            self.manifest, execution_id="session-conversation-2"
        ).start(self.manifest)
        missing_one = tuple(
            result
            for result in self.results
            if result.check_id != "session.conversation.mutation"
        )
        completed = execution.complete(self.manifest, missing_one)
        self.assertEqual(completed.exit_code, 4)
        with self.assertRaises(ValueError):
            ScenarioEvidenceBundle.create(
                manifest=self.manifest,
                execution=completed,
                execution_checks=missing_one,
                scenario=SESSION_CONVERSATION_SCENARIO,
                checks=missing_one,
                evidence_view=self.evidence_view,
                independent_evidence=self.independent_evidence,
            )


if __name__ == "__main__":
    unittest.main()
