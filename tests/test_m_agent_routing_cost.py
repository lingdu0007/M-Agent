"""Ticket 20: declarative Run Cost Policy with evidence-gap semantics (AC 3).

cost policy 显式声明 currency、估算 formula、usage provenance 与最坏
情况上界语义。估算依据版本化 Pricing Snapshot 与冻结 Contract Limits：

- ``WORST_CASE_TOKEN_BUDGET``：以（计量输入或上下文窗口回退）+ 预留
  输出 × 执行预算次数为最坏情况上界，声明为上界而非精确费用；
- ``REPORTED_USAGE``：基于实际 usage 计量的证据性估算，usage 缺失或
  provenance 与声明不符时记录证据缺口；
- 价格缺失/过期/漂移/integrity failure、币种不一致、输出价格缺失时
  只记录证据缺口，绝不伪造精确费用；
- 估算结构上不可能宣称保证结算预算（``settlement_guaranteed`` 恒
  False）。
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from m_agent.runtime import UsageProvenance
from m_agent.companion.routing import (
    CostFormula,
    RunCostPolicy,
    UsageObservation,
    estimate_run_cost,
    GAP_CURRENCY_MISMATCH,
    GAP_INPUT_SIZE_UNAVAILABLE,
    GAP_OUTPUT_PRICE_MISSING,
    GAP_PRICING_FINGERPRINT_DRIFT,
    GAP_PRICING_INTEGRITY_FAILURE,
    GAP_PRICING_MISSING,
    GAP_PRICING_STALE,
    GAP_USAGE_PROVENANCE_MISMATCH,
    GAP_USAGE_UNAVAILABLE,
)

from routing_fixtures import (
    AS_OF,
    make_contract,
    make_evidence,
    make_pricing,
    make_variant,
)


def _policy(**overrides):  # noqa: ANN003, ANN202
    values = dict(
        policy_id="cost-policy",
        version="1",
        currency="USD",
        formula=CostFormula.WORST_CASE_TOKEN_BUDGET,
        usage_provenance=UsageProvenance.RUNTIME_SIZED,
    )
    values.update(overrides)
    return RunCostPolicy(**values)


def _variant():  # noqa: ANN202 - test helper
    return make_variant("variant-cost", make_contract("contract-cost"))


def _usage(**overrides):  # noqa: ANN003, ANN202
    values = dict(
        input_tokens=50_000,
        output_tokens=2_000,
        provenance=UsageProvenance.PROVIDER_REPORTED,
    )
    values.update(overrides)
    return UsageObservation(**values)


class RunCostPolicyDeclarationTests(unittest.TestCase):
    """cost policy 的四项显式声明。"""

    def test_policy_declares_currency_formula_usage_provenance(self) -> None:
        policy = _policy()
        self.assertEqual(policy.currency, "USD")
        self.assertIs(policy.formula, CostFormula.WORST_CASE_TOKEN_BUDGET)
        self.assertIs(policy.usage_provenance, UsageProvenance.RUNTIME_SIZED)
        self.assertTrue(policy.content_digest())

    def test_reported_usage_formula_cannot_rely_on_unavailable_usage(self) -> None:
        with self.assertRaises(ValueError):
            _policy(
                formula=CostFormula.REPORTED_USAGE,
                usage_provenance=UsageProvenance.UNAVAILABLE,
            )


class WorstCaseEstimateTests(unittest.TestCase):
    """最坏情况上界语义。"""

    def test_worst_case_upper_bound_with_sized_input(self) -> None:
        variant = _variant()
        # 单次最坏 = (60_000 + 8_000)/1e6×3.50 + 8_000/1e6×12.00
        #          = 0.238 + 0.096 = 0.334；预算 4 次 → 1.336
        estimate = estimate_run_cost(
            policy=_policy(),
            variant=variant,
            evidence=make_evidence((variant,)),
            as_of=AS_OF,
            sized_input_tokens=60_000,
        )
        self.assertTrue(estimate.worst_case_upper_bound)
        self.assertIsNotNone(estimate.upper_bound)
        self.assertEqual(
            estimate.upper_bound, Decimal("1.3360")
        )
        self.assertIsNone(estimate.lower_bound)
        self.assertEqual(estimate.evidence_gaps, ())
        self.assertEqual(estimate.currency, "USD")
        self.assertEqual(estimate.pricing_snapshot_version, "price-1")
        self.assertEqual(
            estimate.contract_fingerprint,
            variant.primary_contract().fingerprint,
        )

    def test_unsized_input_falls_back_to_context_window_with_gap(self) -> None:
        variant = _variant()
        estimate = estimate_run_cost(
            policy=_policy(),
            variant=variant,
            evidence=make_evidence((variant,)),
            as_of=AS_OF,
        )
        # 回退：128K 上下文窗口 + 8K 预留输出，预算 4 次。
        expected = (
            (Decimal(128_000 + 8_000) / Decimal(1_000_000))
            * Decimal("3.50")
            + (Decimal(8_000) / Decimal(1_000_000)) * Decimal("12.00")
        ) * Decimal(4)
        self.assertEqual(estimate.upper_bound, expected)
        self.assertIn(GAP_INPUT_SIZE_UNAVAILABLE, estimate.evidence_gaps)
        self.assertTrue(estimate.worst_case_upper_bound)

    def test_estimate_never_claims_settlement_guarantee(self) -> None:
        variant = _variant()
        estimate = estimate_run_cost(
            policy=_policy(),
            variant=variant,
            evidence=make_evidence((variant,)),
            as_of=AS_OF,
            sized_input_tokens=1_000,
        )
        self.assertIs(estimate.settlement_guaranteed, False)

    def test_settlement_guarantee_is_structurally_forbidden(self) -> None:
        """估算类型结构上禁止宣称结算预算：True 即构造失败。"""
        variant = _variant()
        estimate = estimate_run_cost(
            policy=_policy(),
            variant=variant,
            evidence=make_evidence((variant,)),
            as_of=AS_OF,
            sized_input_tokens=1_000,
        )
        with self.assertRaises(ValueError):
            estimate.model_copy(update={"settlement_guaranteed": True})
        from m_agent.companion.routing import RunCostEstimate

        with self.assertRaises(ValueError):
            RunCostEstimate.model_validate(
                {
                    **estimate.model_dump(mode="json"),
                    "settlement_guaranteed": True,
                }
            )


class ReportedUsageEstimateTests(unittest.TestCase):
    """基于 usage 的证据性估算与 provenance 语义。"""

    def _usage_policy(self):  # noqa: ANN202
        return _policy(
            formula=CostFormula.REPORTED_USAGE,
            usage_provenance=UsageProvenance.PROVIDER_REPORTED,
        )

    def test_reported_usage_yields_evidence_based_estimate(self) -> None:
        variant = _variant()
        estimate = estimate_run_cost(
            policy=self._usage_policy(),
            variant=variant,
            evidence=make_evidence((variant,)),
            as_of=AS_OF,
            usage=_usage(),
        )
        self.assertFalse(estimate.worst_case_upper_bound)
        expected = (Decimal(50_000) / Decimal(1_000_000)) * Decimal("3.50") + (
            Decimal(2_000) / Decimal(1_000_000)
        ) * Decimal("12.00")
        self.assertEqual(estimate.lower_bound, expected)
        self.assertEqual(estimate.upper_bound, expected)
        self.assertEqual(estimate.evidence_gaps, ())

    def test_missing_usage_records_gap_without_fabrication(self) -> None:
        variant = _variant()
        estimate = estimate_run_cost(
            policy=self._usage_policy(),
            variant=variant,
            evidence=make_evidence((variant,)),
            as_of=AS_OF,
        )
        self.assertIsNone(estimate.upper_bound)
        self.assertIsNone(estimate.lower_bound)
        self.assertIn(GAP_USAGE_UNAVAILABLE, estimate.evidence_gaps)

    def test_provenance_mismatch_records_gap(self) -> None:
        variant = _variant()
        estimate = estimate_run_cost(
            policy=self._usage_policy(),
            variant=variant,
            evidence=make_evidence((variant,)),
            as_of=AS_OF,
            usage=_usage(provenance=UsageProvenance.RUNTIME_SIZED),
        )
        self.assertIn(
            GAP_USAGE_PROVENANCE_MISMATCH, estimate.evidence_gaps
        )

    def test_missing_output_price_records_gap(self) -> None:
        variant = _variant()
        evidence = make_evidence(
            (variant,), pricing=(make_pricing(variant, output_price=None),)
        )
        estimate = estimate_run_cost(
            policy=_policy(),
            variant=variant,
            evidence=evidence,
            as_of=AS_OF,
            sized_input_tokens=1_000,
        )
        self.assertIsNone(estimate.upper_bound)
        self.assertIn(GAP_OUTPUT_PRICE_MISSING, estimate.evidence_gaps)


class CostEvidenceGapTests(unittest.TestCase):
    """价格证据不可靠时的缺口语义（不伪造精确费用）。"""

    def _estimate(self, evidence, **kwargs):  # noqa: ANN001, ANN003, ANN202
        variant = _variant()
        values = dict(
            policy=_policy(),
            variant=variant,
            evidence=evidence,
            as_of=AS_OF,
            sized_input_tokens=1_000,
        )
        values.update(kwargs)
        return estimate_run_cost(**values)

    def test_missing_pricing_records_gap(self) -> None:
        variant = _variant()
        estimate = self._estimate(make_evidence((variant,), pricing=()))
        self.assertIn(GAP_PRICING_MISSING, estimate.evidence_gaps)
        self.assertIsNone(estimate.upper_bound)

    def test_stale_pricing_records_gap(self) -> None:
        variant = _variant()
        estimate = self._estimate(
            make_evidence(
                (variant,),
                pricing=(
                    make_pricing(
                        variant, valid_until=AS_OF - timedelta(hours=1)
                    ),
                ),
            )
        )
        self.assertIn(GAP_PRICING_STALE, estimate.evidence_gaps)
        self.assertIsNone(estimate.upper_bound)

    def test_drifted_pricing_records_gap(self) -> None:
        variant = _variant()
        estimate = self._estimate(
            make_evidence(
                (variant,),
                pricing=(
                    make_pricing(variant, contract_fingerprint="d" * 64),
                ),
            )
        )
        self.assertIn(GAP_PRICING_FINGERPRINT_DRIFT, estimate.evidence_gaps)
        self.assertIsNone(estimate.upper_bound)

    def test_tampered_pricing_records_gap(self) -> None:
        variant = _variant()
        sealed = make_pricing(variant)
        tampered = sealed.model_copy(
            update={"input_price_per_mtok": Decimal("0.01")}
        )
        estimate = self._estimate(
            make_evidence((variant,), pricing=(tampered,))
        )
        self.assertIn(GAP_PRICING_INTEGRITY_FAILURE, estimate.evidence_gaps)
        self.assertIsNone(estimate.upper_bound)

    def test_currency_mismatch_records_gap_without_conversion(self) -> None:
        variant = _variant()
        estimate = self._estimate(
            make_evidence(
                (variant,), pricing=(make_pricing(variant, currency="CNY"),)
            )
        )
        self.assertIn(GAP_CURRENCY_MISMATCH, estimate.evidence_gaps)
        self.assertIsNone(estimate.upper_bound)


if __name__ == "__main__":
    unittest.main()
