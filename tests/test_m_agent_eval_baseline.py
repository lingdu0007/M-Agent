"""Ticket 18 AC 7：Baseline 冻结 immutable revision 引用并做五态比较。

可复现证据：

- Baseline 显式引用一个 (report_id, revision) 冻结点：后续新
  revision 不改变本引用，baseline 永不自动更新；
- 比较错版本即 fail-closed（ValueError），绝不比较错误 revision；
- case × variant 五态：UNCHANGED / CHANGED / NEW / MISSING /
  INCONCLUSIVE；hard gate 恶化恒为 REGRESSION（不可协商），
  quality gate / pass^k 恶化按 policy 声明；
- 任一侧证据不足（INCONCLUSIVE/ERROR/UNSUPPORTED）-> INCONCLUSIVE，
  缺失证据不是「无回归」的证明；
- 整体：任一 REGRESSION -> REGRESSION；否则任一 INCONCLUSIVE ->
  INCONCLUSIVE；否则 COMPARABLE。
"""

from __future__ import annotations

import unittest

from m_agent.companion.eval import (
    BaselineComparison,
    CaseComparison,
    CaseVariantReport,
    ComparisonOverall,
    ComparisonPolicy,
    ComparisonVerdict,
    EvalBaselineRecord,
    EvaluatorOutcome,
    ReportRevisionRecord,
    build_report_revision,
    compare_report_revisions,
)
from m_agent.companion.eval._report import NumericSummary, RepetitionOutcome

_POLICY = ComparisonPolicy(
    policy_id="policy-1", version="1.0",
    quality_regression=True, pass_at_k_regression=True,
)
_NO_QUALITY_POLICY = ComparisonPolicy(
    policy_id="policy-1", version="1.0",
    quality_regression=False, pass_at_k_regression=False,
)


def _repetition(
    *, outcome: EvaluatorOutcome = EvaluatorOutcome.PASS,
    hard: EvaluatorOutcome = EvaluatorOutcome.PASS,
    quality: EvaluatorOutcome = EvaluatorOutcome.PASS,
    score: float | None = None,
    index: int = 0,
) -> RepetitionOutcome:
    return RepetitionOutcome(
        repetition_index=index, outcome=outcome,
        hard_outcome=hard, quality_outcome=quality, score=score,
    )


def _case_report(
    *, case_id: str = "case-1", variant_id: str = "variant-a",
    repetitions=(), hard: EvaluatorOutcome = EvaluatorOutcome.PASS,
    quality: EvaluatorOutcome = EvaluatorOutcome.PASS,
    overall: EvaluatorOutcome = EvaluatorOutcome.PASS,
    pass_at_k: bool = True, scores=(),
) -> CaseVariantReport:
    return CaseVariantReport(
        case_id=case_id, variant_id=variant_id,
        repetitions=tuple(repetitions) or (_repetition(),),
        hard_outcome=hard, quality_outcome=quality,
        overall_outcome=overall, pass_at_k=pass_at_k,
        scores=tuple(scores),
    )


def _revision(
    *, case_results=(), revision: int = 1,
    report_id: str = "report-1", suite_id: str = "suite-1",
    suite_version: str = "1.0",
    hard: EvaluatorOutcome = EvaluatorOutcome.PASS,
    quality: EvaluatorOutcome = EvaluatorOutcome.PASS,
    overall: EvaluatorOutcome = EvaluatorOutcome.PASS,
) -> ReportRevisionRecord:
    return ReportRevisionRecord.build(
        report_id=report_id, revision=revision, execution_id="exec-1",
        suite_id=suite_id, suite_version=suite_version,
        suite_digest="d-" + suite_id,
        case_results=tuple(case_results),
        hard_outcome=hard, quality_outcome=quality, overall_outcome=overall,
    )


def _baseline(report: ReportRevisionRecord) -> EvalBaselineRecord:
    return EvalBaselineRecord(
        baseline_id="baseline-1",
        report_id=report.report_id,
        report_revision=report.revision,
        suite_id=report.suite_id,
        suite_version=report.suite_version,
        comparison_policy=_POLICY,
    )


class BaselineFreezeAndVersionTests(unittest.TestCase):
    """Baseline 冻结引用与 fail-closed。"""

    def test_baseline_freezes_a_precise_revision_reference(self) -> None:
        report = _revision()
        baseline = _baseline(report)
        self.assertEqual(baseline.report_id, "report-1")
        self.assertEqual(baseline.report_revision, 1)
        self.assertEqual(baseline.suite_id, "suite-1")
        # baseline 是不可变记录。
        self.assertTrue(baseline.model_config.get("frozen"))

    def test_wrong_report_id_is_rejected_fail_closed(self) -> None:
        current = _revision()
        baseline = _baseline(current)
        wrong = _revision(report_id="other-report")
        with self.assertRaises(ValueError):
            compare_report_revisions(
                current=current, baseline=baseline,
                baseline_report=wrong,
            )

    def test_wrong_revision_is_rejected_fail_closed(self) -> None:
        current = _revision()
        baseline = _baseline(current)
        wrong = _revision(revision=99)
        with self.assertRaises(ValueError):
            compare_report_revisions(
                current=current, baseline=baseline,
                baseline_report=wrong,
            )


class BaselineVerdictTests(unittest.TestCase):
    """case × variant 五态分类。"""

    def test_identical_outcomes_are_unchanged(self) -> None:
        case = _case_report()
        baseline = _baseline(_revision(case_results=(case,)))
        current = _revision(case_results=(case,))
        comparison = compare_report_revisions(
            current=current, baseline=baseline,
            baseline_report=baseline.report_id and _revision(
                case_results=(case,), revision=baseline.report_revision,
            ),
        )
        self.assertEqual(comparison.overall, ComparisonOverall.COMPARABLE)
        self.assertEqual(len(comparison.case_comparisons), 1)
        self.assertEqual(
            comparison.case_comparisons[0].verdict,
            ComparisonVerdict.UNCHANGED,
        )

    def test_new_case_variant_appears_in_current(self) -> None:
        base_case = _case_report(case_id="case-1")
        new_case = _case_report(case_id="case-2")
        baseline_report = _revision(case_results=(base_case,))
        baseline = _baseline(baseline_report)
        current = _revision(case_results=(base_case, new_case))
        comparison = compare_report_revisions(
            current=current, baseline=baseline,
            baseline_report=baseline_report,
        )
        verdicts = {c.verdict for c in comparison.case_comparisons}
        self.assertIn(ComparisonVerdict.NEW, verdicts)
        self.assertEqual(comparison.overall, ComparisonOverall.COMPARABLE)

    def test_missing_case_variant_disappears_from_current(self) -> None:
        base_case = _case_report(case_id="case-1")
        gone_case = _case_report(case_id="case-2")
        baseline_report = _revision(case_results=(base_case, gone_case))
        baseline = _baseline(baseline_report)
        current = _revision(case_results=(base_case,))
        comparison = compare_report_revisions(
            current=current, baseline=baseline,
            baseline_report=baseline_report,
        )
        verdicts = {c.verdict for c in comparison.case_comparisons}
        self.assertIn(ComparisonVerdict.MISSING, verdicts)
        # 缺失本身不构成回归，但也不是变更。
        self.assertEqual(comparison.overall, ComparisonOverall.COMPARABLE)

    def test_changed_outcome_without_regression(self) -> None:
        # baseline FAIL -> current PASS：改善，不是回归。
        base_case = _case_report(
            hard=EvaluatorOutcome.FAIL, overall=EvaluatorOutcome.FAIL,
            pass_at_k=False,
        )
        cur_case = _case_report(
            hard=EvaluatorOutcome.PASS, overall=EvaluatorOutcome.PASS,
            pass_at_k=True,
        )
        baseline_report = _revision(
            case_results=(base_case,),
            hard=EvaluatorOutcome.FAIL, overall=EvaluatorOutcome.FAIL,
        )
        baseline = _baseline(baseline_report)
        current = _revision(case_results=(cur_case,))
        comparison = compare_report_revisions(
            current=current, baseline=baseline,
            baseline_report=baseline_report,
        )
        self.assertEqual(
            comparison.case_comparisons[0].verdict,
            ComparisonVerdict.CHANGED,
        )
        self.assertEqual(comparison.overall, ComparisonOverall.COMPARABLE)


class BaselineRegressionTests(unittest.TestCase):
    """REGRESSION：hard 不可协商；quality / pass^k 按 policy。"""

    def test_hard_gate_regression_is_mandatory(self) -> None:
        # 即使 policy 关闭 quality / pass^k，hard 恶化仍恒为 REGRESSION。
        base_case = _case_report(
            hard=EvaluatorOutcome.PASS, overall=EvaluatorOutcome.PASS,
        )
        cur_case = _case_report(
            hard=EvaluatorOutcome.FAIL, overall=EvaluatorOutcome.FAIL,
            pass_at_k=False,
        )
        baseline_report = _revision(case_results=(base_case,))
        baseline = EvalBaselineRecord(
            baseline_id="baseline-1",
            report_id=baseline_report.report_id,
            report_revision=baseline_report.revision,
            suite_id=baseline_report.suite_id,
            suite_version=baseline_report.suite_version,
            comparison_policy=_NO_QUALITY_POLICY,
        )
        current = _revision(
            case_results=(cur_case,),
            hard=EvaluatorOutcome.FAIL, overall=EvaluatorOutcome.FAIL,
        )
        comparison = compare_report_revisions(
            current=current, baseline=baseline,
            baseline_report=baseline_report,
        )
        self.assertEqual(
            comparison.case_comparisons[0].verdict,
            ComparisonVerdict.REGRESSION,
        )
        self.assertEqual(comparison.overall, ComparisonOverall.REGRESSION)
        self.assertIn("hard gate", comparison.case_comparisons[0].detail)

    def test_quality_gate_regression_when_policy_enabled(self) -> None:
        base_case = _case_report(
            hard=EvaluatorOutcome.PASS, quality=EvaluatorOutcome.PASS,
            overall=EvaluatorOutcome.PASS,
        )
        cur_case = _case_report(
            hard=EvaluatorOutcome.PASS, quality=EvaluatorOutcome.FAIL,
            overall=EvaluatorOutcome.FAIL, pass_at_k=False,
        )
        baseline_report = _revision(case_results=(base_case,))
        baseline = _baseline(baseline_report)
        current = _revision(case_results=(cur_case,))
        comparison = compare_report_revisions(
            current=current, baseline=baseline,
            baseline_report=baseline_report,
        )
        self.assertEqual(
            comparison.case_comparisons[0].verdict,
            ComparisonVerdict.REGRESSION,
        )
        self.assertIn("quality gate", comparison.case_comparisons[0].detail)

    def test_pass_at_k_regression_when_policy_enabled(self) -> None:
        base_case = _case_report(
            pass_at_k=True, hard=EvaluatorOutcome.PASS,
            quality=EvaluatorOutcome.PASS, overall=EvaluatorOutcome.PASS,
        )
        cur_case = _case_report(
            pass_at_k=False, hard=EvaluatorOutcome.PASS,
            quality=EvaluatorOutcome.PASS, overall=EvaluatorOutcome.PASS,
        )
        baseline_report = _revision(case_results=(base_case,))
        baseline = _baseline(baseline_report)
        current = _revision(case_results=(cur_case,))
        comparison = compare_report_revisions(
            current=current, baseline=baseline,
            baseline_report=baseline_report,
        )
        self.assertEqual(
            comparison.case_comparisons[0].verdict,
            ComparisonVerdict.REGRESSION,
        )
        self.assertIn("pass^k", comparison.case_comparisons[0].detail)

    def test_quality_regression_ignored_when_policy_disabled(self) -> None:
        base_case = _case_report(
            quality=EvaluatorOutcome.PASS, overall=EvaluatorOutcome.PASS,
        )
        cur_case = _case_report(
            quality=EvaluatorOutcome.FAIL, overall=EvaluatorOutcome.FAIL,
            pass_at_k=False,
        )
        baseline_report = _revision(case_results=(base_case,))
        baseline = EvalBaselineRecord(
            baseline_id="baseline-1",
            report_id=baseline_report.report_id,
            report_revision=baseline_report.revision,
            suite_id=baseline_report.suite_id,
            suite_version=baseline_report.suite_version,
            comparison_policy=_NO_QUALITY_POLICY,
        )
        current = _revision(case_results=(cur_case,))
        comparison = compare_report_revisions(
            current=current, baseline=baseline,
            baseline_report=baseline_report,
        )
        # policy 关闭 quality / pass^k -> 仅 CHANGED，不判 REGRESSION。
        self.assertEqual(
            comparison.case_comparisons[0].verdict,
            ComparisonVerdict.CHANGED,
        )
        self.assertEqual(comparison.overall, ComparisonOverall.COMPARABLE)


class BaselineInconclusiveAndOverallTests(unittest.TestCase):
    """证据不足归 INCONCLUSIVE，整体优先级 REGRESSION > INCONCLUSIVE。"""

    def test_missing_evidence_on_either_side_is_inconclusive(self) -> None:
        base_case = _case_report(overall=EvaluatorOutcome.INCONCLUSIVE)
        cur_case = _case_report()
        baseline_report = _revision(case_results=(base_case,))
        baseline = _baseline(baseline_report)
        current = _revision(case_results=(cur_case,))
        comparison = compare_report_revisions(
            current=current, baseline=baseline,
            baseline_report=baseline_report,
        )
        self.assertEqual(
            comparison.case_comparisons[0].verdict,
            ComparisonVerdict.INCONCLUSIVE,
        )
        # 缺失证据不是「无回归」的证明 -> 整体 INCONCLUSIVE。
        self.assertEqual(comparison.overall, ComparisonOverall.INCONCLUSIVE)

    def test_suite_mismatch_is_inconclusive_overall(self) -> None:
        baseline_report = _revision(suite_id="suite-a")
        baseline = _baseline(baseline_report)
        current = _revision(suite_id="suite-b")
        comparison = compare_report_revisions(
            current=current, baseline=baseline,
            baseline_report=baseline_report,
        )
        self.assertEqual(comparison.overall, ComparisonOverall.INCONCLUSIVE)
        self.assertEqual(comparison.case_comparisons, ())
        self.assertIn("suite ids differ", comparison.detail)

    def test_any_regression_dominates_inconclusive(self) -> None:
        # case-1 回归、case-2 证据不足：整体 REGRESSION（不被 INCONCLUSIVE 吞）。
        reg_case = _case_report(
            case_id="case-1", hard=EvaluatorOutcome.FAIL,
            overall=EvaluatorOutcome.FAIL, pass_at_k=False,
        )
        inconclusive_case = _case_report(
            case_id="case-2", overall=EvaluatorOutcome.INCONCLUSIVE,
        )
        base_pass = _case_report(
            case_id="case-1", hard=EvaluatorOutcome.PASS,
            overall=EvaluatorOutcome.PASS,
        )
        base_inconclusive = _case_report(
            case_id="case-2", overall=EvaluatorOutcome.PASS,
        )
        baseline_report = _revision(case_results=(base_pass, base_inconclusive))
        baseline = _baseline(baseline_report)
        current = _revision(
            case_results=(reg_case, inconclusive_case),
            hard=EvaluatorOutcome.FAIL, overall=EvaluatorOutcome.FAIL,
        )
        comparison = compare_report_revisions(
            current=current, baseline=baseline,
            baseline_report=baseline_report,
        )
        self.assertEqual(comparison.overall, ComparisonOverall.REGRESSION)
        verdicts = {c.verdict for c in comparison.case_comparisons}
        self.assertIn(ComparisonVerdict.REGRESSION, verdicts)
        self.assertIn(ComparisonVerdict.INCONCLUSIVE, verdicts)

    def test_any_inconclusive_without_regression_is_inconclusive(self) -> None:
        new_case = _case_report(case_id="case-2")
        inconclusive_case = _case_report(
            case_id="case-1", overall=EvaluatorOutcome.INCONCLUSIVE,
        )
        base_case = _case_report(
            case_id="case-1", overall=EvaluatorOutcome.PASS,
        )
        baseline_report = _revision(case_results=(base_case,))
        baseline = _baseline(baseline_report)
        current = _revision(case_results=(inconclusive_case, new_case))
        comparison = compare_report_revisions(
            current=current, baseline=baseline,
            baseline_report=baseline_report,
        )
        self.assertEqual(comparison.overall, ComparisonOverall.INCONCLUSIVE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
