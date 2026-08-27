"""Ticket 20: pre-Run Fallback 与显式 Replacement Run 的契约测试。

- Fallback 只在任何 Run / Session Claim 创建之前按冻结候选序列执行：
  纯函数 Router 重放，零模型 dispatch、零状态突变；
- 尝试次数由序列结构性约束（max_attempts 覆盖序列长度、身份唯一）；
- 每次尝试的 outcome / reason / decision 都进入可检查记录；
- Run 创建后的 provider failure 不自动换模：Replacement 必须显式
  注册——predecessor 必须终态，successor 必须绑定新 Decision。
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from m_agent.runtime import RunStatus
from m_agent._definition import DefinitionSnapshot
from m_agent._run import RunRecord
from m_agent.companion.routing import (
    FALLBACK_EXHAUSTED,
    FALLBACK_SELECTED,
    FallbackSequence,
    HardRoutingGates,
    ModelRouter,
    REPLACEMENT_REASON_PROVIDER_FAILURE,
    RoutingOutcome,
    RoutingPolicyIdentity,
    RoutingReplacementError,
    bind_decision_to_run,
    execute_pre_run_fallback,
    register_replacement_run,
)

from routing_fixtures import (
    AS_OF,
    POLICY_IDENTITY,
    make_catalog,
    make_contract,
    make_entry,
    make_evidence,
    make_policy,
    make_variant,
)


SECONDARY_IDENTITY = RoutingPolicyIdentity(
    policy_id="finance-default", version="4"
)


def _variants():  # noqa: ANN202 - test helper
    identities = (POLICY_IDENTITY, SECONDARY_IDENTITY)
    economy = make_variant(
        "variant-economy",
        make_contract("contract-economy"),
        policy_identities=identities,
    )
    premium = make_variant(
        "variant-premium",
        make_contract("contract-premium"),
        policy_identities=identities,
    )
    return economy, premium


def _run(variant, run_id: str, status: RunStatus) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        definition_id=variant.definition_id,
        definition_version=variant.definition_version,
        input="route me",
        status=status,
        snapshot=DefinitionSnapshot(
            definition_id=variant.definition_id,
            version=variant.definition_version,
            instructions="offline agent.",
            model_bindings=variant.model_bindings,
        ),
    )


class FallbackSequenceContractTests(unittest.TestCase):
    """冻结候选序列的结构性约束。"""

    def test_sequence_requires_policies_and_bound(self) -> None:
        with self.assertRaises(ValueError):
            FallbackSequence(policies=(), max_attempts=1)
        economy, _ = _variants()
        policy = make_policy()
        with self.assertRaises(ValueError):
            FallbackSequence(policies=(policy,), max_attempts=0)

    def test_sequence_length_bounded_and_identities_unique(self) -> None:
        policy = make_policy()
        duplicate = make_policy()
        with self.assertRaises(ValueError):
            FallbackSequence(policies=(policy, duplicate), max_attempts=1)
        with self.assertRaises(ValueError):
            FallbackSequence(policies=(policy, duplicate), max_attempts=2)
        different = RoutingPolicyIdentity(policy_id="finance-default", version="4")
        sequence = FallbackSequence(
            policies=(policy, make_policy(identity=different)),
            max_attempts=2,
        )
        self.assertEqual(sequence.attempt_count, 2)

class PreRunFallbackExecutionTests(unittest.TestCase):
    """fallback 执行语义：冻结序列、有限次数、可检查原因。"""

    def test_first_policy_selecting_stops_the_sequence(self) -> None:
        economy, premium = _variants()
        catalog = make_catalog(make_entry(economy), make_entry(premium))
        sequence = FallbackSequence(
            policies=(
                make_policy(),
                make_policy(
                    identity=RoutingPolicyIdentity(
                        policy_id="finance-default", version="4"
                    )
                ),
            ),
            max_attempts=2,
        )
        result = execute_pre_run_fallback(
            catalog=catalog,
            sequence=sequence,
            evidence=make_evidence((economy, premium)),
            as_of=AS_OF,
        )
        self.assertEqual(len(result.attempts), 1)
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(result.reason_code, FALLBACK_SELECTED)
        self.assertFalse(result.exhausted)
        self.assertIsNotNone(result.selected)
        self.assertIsNotNone(result.attempts[0].decision_id)

    def test_failed_attempt_falls_through_with_inspectable_reason(self) -> None:
        economy, premium = _variants()
        catalog = make_catalog(make_entry(economy), make_entry(premium))
        # strict 策略价格上限 1.00 全体不满足；relaxed 3.00 可选。
        strict = make_policy(
            hard_gates=HardRoutingGates(
                max_input_price_per_mtok=Decimal("1.00")
            )
        )
        relaxed = make_policy(
            identity=RoutingPolicyIdentity(
                policy_id="finance-default", version="4"
            ),
            hard_gates=HardRoutingGates(
                max_input_price_per_mtok=Decimal("5.00")
            ),
        )
        sequence = FallbackSequence(
            policies=(strict, relaxed), max_attempts=2
        )
        result = execute_pre_run_fallback(
            catalog=catalog,
            sequence=sequence,
            evidence=make_evidence((economy, premium)),
            as_of=AS_OF,
        )
        self.assertEqual(len(result.attempts), 2)
        first = result.attempts[0]
        self.assertIs(first.outcome, RoutingOutcome.POLICY_UNSATISFIED)
        self.assertEqual(first.reason_code, "POLICY_UNSATISFIED")
        self.assertEqual(first.policy_identity.version, "3")
        self.assertIsNone(first.decision_id)
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(result.reason_code, FALLBACK_SELECTED)
        self.assertEqual(
            result.selected.decision.selected_variant.variant_id,
            "variant-economy",
        )

    def test_exhausted_sequence_records_every_attempt_reason(self) -> None:
        economy, premium = _variants()
        catalog = make_catalog(make_entry(economy), make_entry(premium))
        strict_a = make_policy(
            hard_gates=HardRoutingGates(
                max_input_price_per_mtok=Decimal("1.00")
            )
        )
        strict_b = make_policy(
            identity=RoutingPolicyIdentity(
                policy_id="finance-default", version="4"
            ),
            hard_gates=HardRoutingGates(
                max_input_price_per_mtok=Decimal("2.00")
            ),
        )
        sequence = FallbackSequence(
            policies=(strict_a, strict_b), max_attempts=2
        )
        result = execute_pre_run_fallback(
            catalog=catalog,
            sequence=sequence,
            evidence=make_evidence((economy, premium)),
            as_of=AS_OF,
        )
        self.assertTrue(result.exhausted)
        self.assertEqual(result.reason_code, FALLBACK_EXHAUSTED)
        self.assertIsNone(result.selected)
        self.assertEqual(
            [attempt.outcome for attempt in result.attempts],
            [RoutingOutcome.POLICY_UNSATISFIED, RoutingOutcome.POLICY_UNSATISFIED],
        )
        self.assertEqual(
            [attempt.reason_code for attempt in result.attempts],
            ["POLICY_UNSATISFIED", "POLICY_UNSATISFIED"],
        )

    def test_fallback_is_a_pure_replay_without_input_mutation(self) -> None:
        """fallback 全程零副作用：输入 evidence/catalog 不被改写。"""
        economy, premium = _variants()
        catalog = make_catalog(make_entry(economy), make_entry(premium))
        evidence = make_evidence((economy, premium))
        digest_before = evidence.evidence_digest()
        catalog_digest_before = catalog.catalog_digest()
        sequence = FallbackSequence(policies=(make_policy(),), max_attempts=1)
        execute_pre_run_fallback(
            catalog=catalog,
            sequence=sequence,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertEqual(evidence.evidence_digest(), digest_before)
        self.assertEqual(catalog.catalog_digest(), catalog_digest_before)


class ReplacementRunTests(unittest.TestCase):
    """Run 创建后的显式 Replacement 语义（不自动换模）。"""

    def _route_and_bind(
        self,
        run_id: str,
        status: RunStatus,
        identity: RoutingPolicyIdentity | None = None,
    ):
        economy, premium = _variants()
        catalog = make_catalog(make_entry(economy), make_entry(premium))
        result = ModelRouter().select(
            catalog=catalog,
            policy=make_policy(
                identity=identity,
                hard_gates=HardRoutingGates(
                    max_input_price_per_mtok=Decimal("5.00")
                ),
            ),
            evidence=make_evidence((economy, premium)),
            as_of=AS_OF,
        )
        decision = result.decision
        run = _run(decision.selected_variant, run_id, status)
        return decision, run, bind_decision_to_run(decision, run)

    def test_explicit_replacement_run_freezes_the_relation(self) -> None:
        predecessor_decision, predecessor_run, predecessor_binding = (
            self._route_and_bind("run-1", RunStatus.FAILED)
        )
        successor_decision, successor_run, successor_binding = (
            self._route_and_bind("run-2", RunStatus.CREATED, SECONDARY_IDENTITY)
        )
        record = register_replacement_run(
            predecessor=predecessor_run,
            successor=successor_run,
            predecessor_binding=predecessor_binding,
            successor_binding=successor_binding,
            reason_code=REPLACEMENT_REASON_PROVIDER_FAILURE,
        )
        self.assertEqual(record.predecessor_run_id, "run-1")
        self.assertEqual(record.successor_run_id, "run-2")
        self.assertEqual(
            record.reason_code, REPLACEMENT_REASON_PROVIDER_FAILURE
        )
        self.assertNotEqual(
            record.predecessor_decision_id, record.successor_decision_id
        )

    def test_running_predecessor_cannot_be_replaced(self) -> None:
        """Run 未终止绝不换模：in-run switching 结构性禁止。"""
        _, predecessor_run, predecessor_binding = self._route_and_bind(
            "run-1", RunStatus.RUNNING
        )
        successor_decision, successor_run, successor_binding = (
            self._route_and_bind(
                "run-2", RunStatus.CREATED, SECONDARY_IDENTITY
            )
        )
        with self.assertRaises(RoutingReplacementError):
            register_replacement_run(
                predecessor=predecessor_run,
                successor=successor_run,
                predecessor_binding=predecessor_binding,
                successor_binding=successor_binding,
                reason_code=REPLACEMENT_REASON_PROVIDER_FAILURE,
            )

    def test_replacement_requires_a_new_decision(self) -> None:
        """Replacement 不是重试：复用旧 Decision 即失败。"""
        decision, predecessor_run, binding = self._route_and_bind(
            "run-1", RunStatus.FAILED
        )
        successor_run = _run(
            decision.selected_variant, "run-2", RunStatus.CREATED
        )
        with self.assertRaises(RoutingReplacementError):
            register_replacement_run(
                predecessor=predecessor_run,
                successor=successor_run,
                predecessor_binding=binding,
                successor_binding=binding.model_copy(
                    update={"run_id": "run-2"}
                ),
                reason_code=REPLACEMENT_REASON_PROVIDER_FAILURE,
            )

    def test_replacement_rejects_identity_mismatches(self) -> None:
        decision, predecessor_run, predecessor_binding = (
            self._route_and_bind("run-1", RunStatus.FAILED)
        )
        successor_decision, successor_run, successor_binding = (
            self._route_and_bind(
                "run-2", RunStatus.CREATED, SECONDARY_IDENTITY
            )
        )
        # 绑定与 Run 身份不一致。
        with self.assertRaises(RoutingReplacementError):
            register_replacement_run(
                predecessor=predecessor_run,
                successor=successor_run,
                predecessor_binding=predecessor_binding.model_copy(
                    update={"run_id": "run-other"}
                ),
                successor_binding=successor_binding,
                reason_code=REPLACEMENT_REASON_PROVIDER_FAILURE,
            )
        # successor 必须是不同的 Run。
        with self.assertRaises(RoutingReplacementError):
            register_replacement_run(
                predecessor=predecessor_run,
                successor=predecessor_run,
                predecessor_binding=predecessor_binding,
                successor_binding=successor_binding,
                reason_code=REPLACEMENT_REASON_PROVIDER_FAILURE,
            )


if __name__ == "__main__":
    unittest.main()
