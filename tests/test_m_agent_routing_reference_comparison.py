"""Ticket 19: offline reference comparison of two Agent Variants.

参考比较（PRD `model-routing` 决策的前置契约证据）：至少两个离线
Agent Variant 在能力组合过滤、数值 Limits、deployment 硬约束过滤、
稳定排序与 tie-break，以及 stale/missing snapshot 的 fail closed
负例上给出可复现、可审计的路由结论。每个 case 只依赖公共
``m_agent.companion.routing`` 契约，输出冻结的期望结果表。
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from m_agent.runtime import ModelRequirements

from m_agent.companion.routing import (
    DeploymentConstraints,
    HardRoutingGates,
    ModelRouter,
    RoutingEvidence,
    RoutingOutcome,
)

from routing_fixtures import (
    AS_OF,
    cost_objective,
    make_availability,
    make_catalog,
    make_contract,
    make_evidence,
    make_entry,
    make_model_evidence,
    make_policy,
    make_pricing,
    make_variant,
    quality_objective,
    tool_and_structured_capabilities,
)


class TwoVariantReferenceComparison(unittest.TestCase):
    """variant-economy（便宜、能力少） vs variant-premium（贵、能力全）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.economy = make_variant(
            "variant-economy",
            make_contract("contract-economy", context_window=64_000),
        )
        cls.premium = make_variant(
            "variant-premium",
            make_contract(
                "contract-premium",
                context_window=200_000,
                capabilities=tool_and_structured_capabilities(),
            ),
        )
        cls.entries = (make_entry(cls.economy), make_entry(cls.premium))
        cls.catalog = make_catalog(*cls.entries)
        cls.router = ModelRouter()

    def evidence(
        self,
        *,
        economy_price: str = "0.80",
        premium_price: str = "6.00",
        economy_quality: float = 0.86,
        premium_quality: float = 0.96,
    ) -> RoutingEvidence:
        return make_evidence(
            (self.economy, self.premium),
            pricing=(
                make_pricing(self.economy, input_price=economy_price),
                make_pricing(self.premium, input_price=premium_price),
            ),
            model_evidence=(
                make_model_evidence(
                    self.economy, quality=economy_quality, latency_ms=1_200.0
                ),
                make_model_evidence(
                    self.premium, quality=premium_quality, latency_ms=500.0
                ),
            ),
        )

    def test_case_capability_combination_requires_premium(self) -> None:
        policy = make_policy(
            requirements=ModelRequirements(
                min_context_window_tokens=100_000,
            )
        )
        result = self.router.select(
            catalog=self.catalog,
            policy=policy,
            evidence=self.evidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(
            result.decision.selected_variant.variant_id, "variant-premium"
        )
        reasons = {
            evaluation.variant_id: evaluation.reason_code
            for evaluation in result.candidate_evaluations
        }
        self.assertEqual(reasons["variant-economy"], "CONTEXT_WINDOW_TOO_SMALL")

    def test_case_typed_tool_calling_filters_economy(self) -> None:
        from m_agent.runtime import ModelCapabilities, ToolCallingMode

        policy = make_policy(
            requirements=ModelRequirements(
                capabilities=ModelCapabilities(
                    tool_calling=ToolCallingMode.NATIVE
                ).as_typed()
            )
        )
        result = self.router.select(
            catalog=self.catalog,
            policy=policy,
            evidence=self.evidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(
            result.decision.selected_variant.variant_id, "variant-premium"
        )
        reasons = {
            evaluation.variant_id: evaluation.reason_code
            for evaluation in result.candidate_evaluations
        }
        self.assertEqual(reasons["variant-economy"], "TOOL_CALLING_UNSUPPORTED")

    def test_case_deployment_provider_hard_constraint(self) -> None:
        policy = make_policy(
            deployment=DeploymentConstraints(
                allowed_providers=frozenset({"eu-provider"})
            )
        )
        catalog = make_catalog(
            make_entry(self.economy, provider="offline-provider"),
            make_entry(self.premium, provider="eu-provider"),
        )
        result = self.router.select(
            catalog=catalog,
            policy=policy,
            evidence=self.evidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(
            result.decision.selected_variant.variant_id, "variant-premium"
        )
        reasons = {
            evaluation.variant_id: evaluation.reason_code
            for evaluation in result.candidate_evaluations
        }
        self.assertEqual(reasons["variant-economy"], "PROVIDER_NOT_ALLOWED")

    def test_case_cost_objective_prefers_economy(self) -> None:
        result = self.router.select(
            catalog=self.catalog,
            policy=make_policy(objectives=(cost_objective(),)),
            evidence=self.evidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(
            result.decision.selected_variant.variant_id, "variant-economy"
        )

    def test_case_quality_objective_prefers_premium(self) -> None:
        result = self.router.select(
            catalog=self.catalog,
            policy=make_policy(objectives=(quality_objective(),)),
            evidence=self.evidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(
            result.decision.selected_variant.variant_id, "variant-premium"
        )

    def test_case_hard_price_gate_keeps_economy_only(self) -> None:
        policy = make_policy(
            hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("1.00"))
        )
        result = self.router.select(
            catalog=self.catalog,
            policy=policy,
            evidence=self.evidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(
            result.decision.selected_variant.variant_id, "variant-economy"
        )
        reasons = {
            evaluation.variant_id: evaluation.reason_code
            for evaluation in result.candidate_evaluations
        }
        self.assertEqual(reasons["variant-premium"], "HARD_PRICE_GATE_FAILED")

    def test_case_identical_objectives_fall_back_to_identity_tie_break(self) -> None:
        evidence = self.evidence(economy_price="2.00", premium_price="2.00")
        result = self.router.select(
            catalog=self.catalog,
            policy=make_policy(objectives=(cost_objective(),)),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(
            result.decision.selected_variant.variant_id, "variant-economy"
        )
        self.assertTrue(result.decision.tie_break_applied)

    def test_case_stale_pricing_snapshot_fails_closed_on_hard_gate(self) -> None:
        policy = make_policy(
            hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("5.00"))
        )
        stale = self.evidence()
        stale = RoutingEvidence(
            model_evidence=stale.model_evidence,
            availability=stale.availability,
            retention=stale.retention,
            pricing=(
                make_pricing(
                    self.economy, valid_until=AS_OF - timedelta(minutes=30)
                ),
                make_pricing(self.premium),
            ),
        )
        result = self.router.select(
            catalog=self.catalog,
            policy=policy,
            evidence=stale,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(result.reason_code, "PRICING_SNAPSHOT_STALE")

    def test_case_missing_availability_snapshot_fails_closed(self) -> None:
        policy = make_policy(hard_gates=HardRoutingGates(require_availability=True))
        evidence = self.evidence()
        missing_availability = RoutingEvidence(
            model_evidence=evidence.model_evidence,
            pricing=evidence.pricing,
            retention=evidence.retention,
            availability=(),
        )
        result = self.router.select(
            catalog=self.catalog,
            policy=policy,
            evidence=missing_availability,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(result.reason_code, "AVAILABILITY_SNAPSHOT_MISSING")

    def test_case_stale_model_evidence_fails_closed_on_quality_gate(self) -> None:
        policy = make_policy(hard_gates=HardRoutingGates(min_quality_score=0.8))
        evidence = self.evidence()
        stale = RoutingEvidence(
            pricing=evidence.pricing,
            availability=evidence.availability,
            retention=evidence.retention,
            model_evidence=(
                make_model_evidence(
                    self.economy, valid_until=AS_OF - timedelta(days=2)
                ),
                make_model_evidence(self.premium),
            ),
        )
        result = self.router.select(
            catalog=self.catalog,
            policy=policy,
            evidence=stale,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(result.reason_code, "MODEL_EVIDENCE_STALE")

    def test_case_no_compatible_variant_when_requirements_exclude_both(self) -> None:
        policy = make_policy(
            requirements=ModelRequirements(min_context_window_tokens=500_000)
        )
        result = self.router.select(
            catalog=self.catalog,
            policy=policy,
            evidence=self.evidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.NO_COMPATIBLE_VARIANT)
        reasons = {
            evaluation.variant_id: evaluation.reason_code
            for evaluation in result.candidate_evaluations
        }
        self.assertEqual(
            reasons["variant-economy"], "CONTEXT_WINDOW_TOO_SMALL"
        )
        self.assertEqual(
            reasons["variant-premium"], "CONTEXT_WINDOW_TOO_SMALL"
        )

    def test_case_deterministic_replay_of_whole_matrix(self) -> None:
        """同一 snapshot 输入重复路由，全部 case 结论保持逐字节一致。"""
        policies = {
            "cost": make_policy(objectives=(cost_objective(),)),
            "quality": make_policy(objectives=(quality_objective(),)),
            "price-gate": make_policy(
                hard_gates=HardRoutingGates(
                    max_input_price_per_mtok=Decimal("1.00")
                )
            ),
            "limits": make_policy(
                requirements=ModelRequirements(min_context_window_tokens=100_000)
            ),
        }
        evidence = self.evidence()
        for name, policy in policies.items():
            with self.subTest(policy=name):
                first = self.router.select(
                    catalog=self.catalog,
                    policy=policy,
                    evidence=evidence,
                    as_of=AS_OF,
                )
                second = self.router.select(
                    catalog=self.catalog,
                    policy=policy,
                    evidence=evidence,
                    as_of=AS_OF,
                )
                self.assertEqual(first, second)
                self.assertEqual(
                    first.candidate_evaluations, second.candidate_evaluations
                )
                if first.decision is not None:
                    self.assertEqual(
                        first.decision.decision_id, second.decision.decision_id
                    )

    def test_case_unavailable_premium_routes_to_economy_under_availability_gate(
        self,
    ) -> None:
        policy = make_policy(hard_gates=HardRoutingGates(require_availability=True))
        evidence = self.evidence()
        degraded = RoutingEvidence(
            pricing=evidence.pricing,
            model_evidence=evidence.model_evidence,
            retention=evidence.retention,
            availability=(
                make_availability(self.economy, available=True),
                make_availability(self.premium, available=False),
            ),
        )
        result = self.router.select(
            catalog=self.catalog,
            policy=policy,
            evidence=degraded,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(
            result.decision.selected_variant.variant_id, "variant-economy"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
