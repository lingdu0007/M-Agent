"""Ticket 20: hard/soft evidence consumption in the deterministic Router.

snapshot 缺失、过期、subject 不匹配、fingerprint 漂移或 integrity
failure 的稳定消费语义（AC 2）：

- hard Policy：任何异常 → 整体 ``EVIDENCE_UNAVAILABLE`` fail closed，
  绝不隐式收窄候选或把异常快照当健康证据；
- soft Policy：显式降级 warning（如 ``STALE_AVAILABILITY``）后继续，
  异常快照不进入 evidence reference、不产生排序读数；
- Operational Limits 快照：hard gate 消费（含维度未知 fail closed）；
- worst-case cost cap：硬成本策略在价格证据缺失/过期/不完整时
  fail closed，超出上限按策略过滤。
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from m_agent.companion.routing import (
    HardRoutingGates,
    ModelRouter,
    OperationalLimitsGate,
    RoutingOutcome,
    SoftRoutingPreferences,
    WorstCaseCostCap,
    WARNING_AVAILABILITY_FINGERPRINT_DRIFT,
    WARNING_AVAILABILITY_INTEGRITY_FAILURE,
    WARNING_AVAILABILITY_SNAPSHOT_MISSING,
    WARNING_ENDPOINT_UNAVAILABLE,
    WARNING_OPERATIONAL_LIMITS_SNAPSHOT_MISSING,
    WARNING_PRICING_FINGERPRINT_DRIFT,
    WARNING_PRICING_INTEGRITY_FAILURE,
    WARNING_SOFT_EVIDENCE_STALE,
    WARNING_STALE_AVAILABILITY,
    WARNING_STALE_OPERATIONAL_LIMITS,
    REASON_AVAILABILITY_SNAPSHOT_FINGERPRINT_DRIFT,
    REASON_AVAILABILITY_SNAPSHOT_INTEGRITY_FAILURE,
    REASON_COST_CURRENCY_MISMATCH,
    REASON_HARD_COST_EVIDENCE_INCOMPLETE,
    REASON_HARD_COST_GATE_FAILED,
    REASON_HARD_OPERATIONAL_LIMIT_GATE_FAILED,
    REASON_OPERATIONAL_LIMITS_SNAPSHOT_MISSING,
    REASON_OPERATIONAL_LIMITS_SNAPSHOT_STALE,
    REASON_OPERATIONAL_LIMITS_UNKNOWN,
    REASON_POLICY_UNSATISFIED,
    REASON_PRICING_SNAPSHOT_FINGERPRINT_DRIFT,
    REASON_PRICING_SNAPSHOT_INTEGRITY_FAILURE,
)

from routing_fixtures import (
    AS_OF,
    make_availability,
    make_catalog,
    make_contract,
    make_evidence,
    make_entry,
    make_model_evidence,
    make_operational_limits,
    make_policy,
    make_pricing,
    make_variant,
)


def _single():  # noqa: ANN202 - test helper
    variant = make_variant("variant-a", make_contract("contract-a"))
    return variant, make_catalog(make_entry(variant))


def _warnings(result):  # noqa: ANN001, ANN202
    return {(warning.variant_id, warning.code) for warning in result.warnings}


class HardEvidenceFailClosedTests(unittest.TestCase):
    """hard Policy：指纹漂移与 integrity failure 都稳定 fail closed。"""

    def test_hard_price_gate_with_fingerprint_drift_fails_closed(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence(
            (variant,),
            pricing=(
                make_pricing(variant, contract_fingerprint="d" * 64),
            ),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=make_policy(
                hard_gates=HardRoutingGates(
                    max_input_price_per_mtok=Decimal("5.00")
                )
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(
            result.reason_code, REASON_PRICING_SNAPSHOT_FINGERPRINT_DRIFT
        )

    def test_hard_price_gate_with_tampered_pricing_fails_closed(self) -> None:
        variant, catalog = _single()
        sealed = make_pricing(variant, input_price="1.00")
        tampered = sealed.model_copy(
            update={"input_price_per_mtok": Decimal("0.01")}
        )
        evidence = make_evidence((variant,), pricing=(tampered,))
        result = ModelRouter().select(
            catalog=catalog,
            policy=make_policy(
                hard_gates=HardRoutingGates(
                    max_input_price_per_mtok=Decimal("5.00")
                )
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(
            result.reason_code, REASON_PRICING_SNAPSHOT_INTEGRITY_FAILURE
        )

    def test_hard_price_gate_with_unsealed_pricing_fails_closed(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence(
            (variant,),
            pricing=(make_pricing(variant, sealed=False),),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=make_policy(
                hard_gates=HardRoutingGates(
                    max_input_price_per_mtok=Decimal("5.00")
                )
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(
            result.reason_code, REASON_PRICING_SNAPSHOT_INTEGRITY_FAILURE
        )

    def test_hard_availability_gate_with_drift_fails_closed(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence(
            (variant,),
            availability=(
                make_availability(variant, contract_fingerprint="d" * 64),
            ),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=make_policy(
                hard_gates=HardRoutingGates(require_availability=True)
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(
            result.reason_code, REASON_AVAILABILITY_SNAPSHOT_FINGERPRINT_DRIFT
        )

    def test_hard_availability_gate_with_integrity_failure_fails_closed(
        self,
    ) -> None:
        variant, catalog = _single()
        sealed = make_availability(variant, available=False)
        tampered = sealed.model_copy(update={"available": True})
        evidence = make_evidence((variant,), availability=(tampered,))
        result = ModelRouter().select(
            catalog=catalog,
            policy=make_policy(
                hard_gates=HardRoutingGates(require_availability=True)
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(
            result.reason_code,
            REASON_AVAILABILITY_SNAPSHOT_INTEGRITY_FAILURE,
        )


class SoftDegradationTests(unittest.TestCase):
    """soft Policy：显式降级 warning 后继续，异常快照不作健康证据。"""

    def _track_availability_policy(self):  # noqa: ANN202
        return make_policy(
            soft_preferences=SoftRoutingPreferences(track_availability=True)
        )

    def test_stale_availability_soft_preference_warns_and_continues(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence(
            (variant,),
            availability=(
                make_availability(variant, valid_until=AS_OF - timedelta(hours=1)),
            ),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._track_availability_policy(),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertIn(
            ("variant-a", WARNING_STALE_AVAILABILITY), _warnings(result)
        )
        # 异常快照不进入 evidence reference：不是健康证据。
        references = result.decision.evidence_references
        self.assertFalse(
            any(ref.kind == "AVAILABILITY" for ref in references)
        )

    def test_healthy_availability_soft_preference_records_reference(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence((variant,))
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._track_availability_policy(),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertNotIn(
            ("variant-a", WARNING_STALE_AVAILABILITY), _warnings(result)
        )
        references = result.decision.evidence_references
        self.assertTrue(
            any(ref.kind == "AVAILABILITY" for ref in references)
        )

    def test_endpoint_unavailable_soft_preference_warns_and_continues(
        self,
    ) -> None:
        variant, catalog = _single()
        evidence = make_evidence(
            (variant,),
            availability=(make_availability(variant, available=False),),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._track_availability_policy(),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertIn(
            ("variant-a", WARNING_ENDPOINT_UNAVAILABLE), _warnings(result)
        )
        references = result.decision.evidence_references
        self.assertFalse(
            any(ref.kind == "AVAILABILITY" for ref in references)
        )

    def test_missing_availability_soft_preference_warns(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence((variant,), availability=())
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._track_availability_policy(),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertIn(
            ("variant-a", WARNING_AVAILABILITY_SNAPSHOT_MISSING),
            _warnings(result),
        )

    def test_availability_drift_and_integrity_soft_warnings(self) -> None:
        variant, catalog = _single()
        drifted = make_evidence(
            (variant,),
            availability=(
                make_availability(variant, contract_fingerprint="d" * 64),
            ),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._track_availability_policy(),
            evidence=drifted,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertIn(
            ("variant-a", WARNING_AVAILABILITY_FINGERPRINT_DRIFT),
            _warnings(result),
        )

        sealed = make_availability(variant, available=False)
        tampered = sealed.model_copy(update={"available": True})
        tampered_evidence = make_evidence(
            (variant,), availability=(tampered,)
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._track_availability_policy(),
            evidence=tampered_evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertIn(
            ("variant-a", WARNING_AVAILABILITY_INTEGRITY_FAILURE),
            _warnings(result),
        )

    def test_soft_operational_tracking_warns_on_stale_and_missing(self) -> None:
        variant, catalog = _single()
        stale = make_evidence(
            (variant,),
            operational_limits=(
                make_operational_limits(
                    variant, valid_until=AS_OF - timedelta(hours=1)
                ),
            ),
        )
        policy = make_policy(
            soft_preferences=SoftRoutingPreferences(
                track_operational_limits=True
            )
        )
        result = ModelRouter().select(
            catalog=catalog, policy=policy, evidence=stale, as_of=AS_OF
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertIn(
            ("variant-a", WARNING_STALE_OPERATIONAL_LIMITS),
            _warnings(result),
        )

        missing = make_evidence((variant,), operational_limits=())
        result = ModelRouter().select(
            catalog=catalog, policy=policy, evidence=missing, as_of=AS_OF
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertIn(
            ("variant-a", WARNING_OPERATIONAL_LIMITS_SNAPSHOT_MISSING),
            _warnings(result),
        )

    def test_cost_objective_degrades_on_drifted_pricing(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        variant_b = make_variant("variant-b", make_contract("contract-b"))
        drifted = make_pricing(
            variant_a, input_price="1.00", contract_fingerprint="d" * 64
        )
        evidence = make_evidence(
            (variant_a, variant_b),
            pricing=(drifted, make_pricing(variant_b, input_price="3.00")),
            model_evidence=(
                make_model_evidence(variant_a),
                make_model_evidence(variant_b),
            ),
        )
        from routing_fixtures import cost_objective

        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a), make_entry(variant_b)),
            policy=make_policy(objectives=(cost_objective(),)),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertIn(
            ("variant-a", WARNING_PRICING_FINGERPRINT_DRIFT),
            _warnings(result),
        )
        # 漂移候选的 COST 读数缺失 → 排在最后（ORDER_LAST）。
        self.assertEqual(
            result.decision.selected_variant.variant_id, "variant-b"
        )

    def test_cost_objective_degrades_on_tampered_pricing(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        sealed = make_pricing(variant_a, input_price="1.00")
        tampered = sealed.model_copy(
            update={"input_price_per_mtok": Decimal("0.01")}
        )
        evidence = make_evidence((variant_a,), pricing=(tampered,))
        from routing_fixtures import cost_objective

        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=make_policy(objectives=(cost_objective(),)),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertIn(
            ("variant-a", WARNING_PRICING_INTEGRITY_FAILURE),
            _warnings(result),
        )

    def test_cost_objective_stale_warning_is_preserved(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        evidence = make_evidence(
            (variant_a,),
            pricing=(
                make_pricing(
                    variant_a, valid_until=AS_OF - timedelta(hours=1)
                ),
            ),
        )
        from routing_fixtures import cost_objective

        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=make_policy(objectives=(cost_objective(),)),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertIn(
            ("variant-a", WARNING_SOFT_EVIDENCE_STALE), _warnings(result)
        )


class OperationalLimitsGateTests(unittest.TestCase):
    """hard Operational Limits 门槛：维度阈值、未知与证据异常。"""

    def _policy(self, **gate):  # noqa: ANN003, ANN202
        return make_policy(
            hard_gates=HardRoutingGates(
                operational_limits=OperationalLimitsGate(**gate)
            )
        )

    def test_sufficient_operational_limits_pass(self) -> None:
        variant, catalog = _single()
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(min_available_rpm=100, min_available_concurrency=2),
            evidence=make_evidence((variant,)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        references = result.decision.evidence_references
        self.assertTrue(
            any(ref.kind == "OPERATIONAL_LIMITS" for ref in references)
        )

    def test_below_operational_limit_is_policy_unsatisfied(self) -> None:
        variant, catalog = _single()
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(min_available_rpm=10_000),
            evidence=make_evidence((variant,)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.POLICY_UNSATISFIED)
        self.assertEqual(result.reason_code, REASON_POLICY_UNSATISFIED)
        codes = {
            evaluation.reason_code
            for evaluation in result.candidate_evaluations
            if not evaluation.passed
        }
        self.assertIn(REASON_HARD_OPERATIONAL_LIMIT_GATE_FAILED, codes)

    def test_required_dimension_unknown_fails_closed(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence(
            (variant,),
            operational_limits=(
                make_operational_limits(variant, rpm=None),
            ),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(min_available_rpm=100),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(result.reason_code, REASON_OPERATIONAL_LIMITS_UNKNOWN)

    def test_missing_operational_snapshot_fails_closed(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence((variant,), operational_limits=())
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(min_available_rpm=100),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(
            result.reason_code, REASON_OPERATIONAL_LIMITS_SNAPSHOT_MISSING
        )

    def test_stale_operational_snapshot_fails_closed(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence(
            (variant,),
            operational_limits=(
                make_operational_limits(
                    variant, valid_until=AS_OF - timedelta(minutes=1)
                ),
            ),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(min_available_rpm=100),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(
            result.reason_code, REASON_OPERATIONAL_LIMITS_SNAPSHOT_STALE
        )

    def test_tampered_operational_snapshot_fails_closed(self) -> None:
        variant, catalog = _single()
        sealed = make_operational_limits(variant)
        tampered = sealed.model_copy(update={"available_rpm": 1_000_000})
        evidence = make_evidence(
            (variant,), operational_limits=(tampered,)
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(min_available_rpm=100),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)

    def test_drifted_operational_snapshot_fails_closed(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence(
            (variant,),
            operational_limits=(
                make_operational_limits(variant, contract_fingerprint="d" * 64),
            ),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(min_available_rpm=100),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)


class WorstCaseCostCapTests(unittest.TestCase):
    """硬成本策略：最坏情况运行成本上限（ADR 0041 成本边界）。"""

    def _policy(self, cap: WorstCaseCostCap):  # noqa: ANN202
        return make_policy(
            hard_gates=HardRoutingGates(worst_case_cost_cap=cap)
        )

    def test_candidate_within_cost_cap_is_selected(self) -> None:
        variant, catalog = _single()
        # economy：128K 窗口 × 4 次尝试预算 × (输入 3.50 + 输出 12.00)/Mtok
        # 最坏情况 = 4 × ((128_000+8_000)/1e6×3.50 + 8_000/1e6×12.00)
        #          = 4 × (0.476 + 0.096) = 2.288 < 3.00
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(
                WorstCaseCostCap(currency="USD", max_run_cost=Decimal("3.00"))
            ),
            evidence=make_evidence((variant,)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)

    def test_candidate_over_cost_cap_is_policy_unsatisfied(self) -> None:
        variant, catalog = _single()
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(
                WorstCaseCostCap(currency="USD", max_run_cost=Decimal("1.00"))
            ),
            evidence=make_evidence((variant,)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.POLICY_UNSATISFIED)
        codes = {
            evaluation.reason_code
            for evaluation in result.candidate_evaluations
            if not evaluation.passed
        }
        self.assertIn(REASON_HARD_COST_GATE_FAILED, codes)

    def test_missing_pricing_under_cost_cap_fails_closed(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence((variant,), pricing=())
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(
                WorstCaseCostCap(currency="USD", max_run_cost=Decimal("5.00"))
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)

    def test_stale_pricing_under_cost_cap_fails_closed(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence(
            (variant,),
            pricing=(
                make_pricing(variant, valid_until=AS_OF - timedelta(hours=1)),
            ),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(
                WorstCaseCostCap(currency="USD", max_run_cost=Decimal("5.00"))
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)

    def test_missing_output_price_under_cost_cap_fails_closed(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence(
            (variant,),
            pricing=(make_pricing(variant, output_price=None),),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(
                WorstCaseCostCap(currency="USD", max_run_cost=Decimal("5.00"))
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(
            result.reason_code, REASON_HARD_COST_EVIDENCE_INCOMPLETE
        )

    def test_currency_mismatch_under_cost_cap_fails_closed(self) -> None:
        variant, catalog = _single()
        evidence = make_evidence(
            (variant,),
            pricing=(make_pricing(variant, currency="CNY"),),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=self._policy(
                WorstCaseCostCap(currency="USD", max_run_cost=Decimal("5.00"))
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(result.reason_code, REASON_COST_CURRENCY_MISMATCH)


if __name__ == "__main__":
    unittest.main()
