"""Baseline 显式引用 immutable Report revision 的确定性比较（Ticket 18）。

Baseline 冻结一个完整的不可变 Report revision 与版本化 comparison
policy；比较区分 changed、new、missing、inconclusive 与 regression，
证据不足（INCONCLUSIVE/ERROR/UNSUPPORTED）绝不冒充「无回归」。
Baseline 永不自动更新：最新执行、价格或 Judge 变化不改变其引用。
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..._steps import utc_now
from ._evaluator import EvaluatorOutcome
from ._report import CaseVariantReport, ReportRevisionRecord

__all__ = [
    "CaseComparison",
    "ComparisonOverall",
    "ComparisonPolicy",
    "ComparisonVerdict",
    "BaselineComparison",
    "EvalBaselineRecord",
    "compare_report_revisions",
]


class ComparisonPolicy(BaseModel):
    """版本化回归比较策略：哪些 gate 恶化构成 regression。

    hard gate 恶化恒为 regression（deterministic hard/safety 门槛
    不可协商）；quality gate 与 pass^k 恶化是否构成 regression 由
    策略显式声明。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    quality_regression: bool = True
    pass_at_k_regression: bool = True


class EvalBaselineRecord(BaseModel):
    """显式引用完整 immutable Report revision 的回归参照。

    ``report_revision`` 是冻结的精确 revision：后续新 revision 不
    改变本引用。记录一经保存即不可变（同 id 异内容确定性冲突）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    baseline_id: str = Field(min_length=1)
    report_id: str = Field(min_length=1)
    report_revision: int = Field(ge=1)
    suite_id: str = Field(min_length=1)
    suite_version: str = Field(min_length=1)
    comparison_policy: ComparisonPolicy
    created_at: datetime = Field(default_factory=utc_now)


class ComparisonVerdict(str, enum.Enum):
    """case × variant 级比较分类（含未变化的显式稳定态）。"""

    UNCHANGED = "UNCHANGED"
    CHANGED = "CHANGED"
    NEW = "NEW"
    MISSING = "MISSING"
    INCONCLUSIVE = "INCONCLUSIVE"
    REGRESSION = "REGRESSION"

    def __str__(self) -> str:  # pragma: no cover - 便捷展示
        return self.value


class ComparisonOverall(str, enum.Enum):
    """整份报告的比较结论。"""

    COMPARABLE = "COMPARABLE"
    INCONCLUSIVE = "INCONCLUSIVE"
    REGRESSION = "REGRESSION"

    def __str__(self) -> str:  # pragma: no cover - 便捷展示
        return self.value


class CaseComparison(BaseModel):
    """一个 case × variant 的比较结论。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1)
    variant_id: str = Field(min_length=1)
    verdict: ComparisonVerdict
    baseline_overall: EvaluatorOutcome | None = None
    current_overall: EvaluatorOutcome | None = None
    detail: str = ""


class BaselineComparison(BaseModel):
    """current revision 相对冻结 Baseline 的整体比较结论。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    baseline_id: str = Field(min_length=1)
    baseline_report_id: str = Field(min_length=1)
    baseline_revision: int = Field(ge=1)
    current_report_id: str = Field(min_length=1)
    current_revision: int = Field(ge=1)
    overall: ComparisonOverall
    case_comparisons: tuple[CaseComparison, ...] = ()
    detail: str = ""


#: 任一侧处于这些 outcome 时证据不足，比较不可下结论。
_NON_COMPARABLE = frozenset({
    EvaluatorOutcome.INCONCLUSIVE,
    EvaluatorOutcome.ERROR,
    EvaluatorOutcome.UNSUPPORTED,
})


def compare_report_revisions(
    *,
    current: ReportRevisionRecord,
    baseline: EvalBaselineRecord,
    baseline_report: ReportRevisionRecord,
) -> BaselineComparison:
    """按冻结 policy 比较当前 revision 与 Baseline 引用的精确 revision。

    规则（确定性）：

    - baseline_report 必须与 baseline 记录的 (report_id, revision)
      精确一致，否则 ValueError（fail-closed，绝不比较错误 revision）；
    - suite 不兼容（suite_id 不同）时整体 INCONCLUSIVE；
    - case × variant 仅在当前报告出现 -> NEW；仅在 baseline 出现 ->
      MISSING；任一侧证据不足 -> INCONCLUSIVE；
    - hard gate 恶化（baseline PASS -> current FAIL）恒为 REGRESSION；
      quality gate / pass^k 恶化按 policy 声明构成 REGRESSION；
    - gate 全等 -> UNCHANGED；其余差异 -> CHANGED；
    - 整体：任一 REGRESSION -> REGRESSION；否则任一 INCONCLUSIVE ->
      INCONCLUSIVE；否则 COMPARABLE。
    """
    if (
        baseline_report.report_id != baseline.report_id
        or baseline_report.revision != baseline.report_revision
    ):
        raise ValueError(
            "baseline_report does not match the frozen baseline"
            f" reference ({baseline.report_id!r} revision"
            f" {baseline.report_revision})"
        )
    if current.suite_id != baseline_report.suite_id:
        return BaselineComparison(
            baseline_id=baseline.baseline_id,
            baseline_report_id=baseline.report_id,
            baseline_revision=baseline.report_revision,
            current_report_id=current.report_id,
            current_revision=current.revision,
            overall=ComparisonOverall.INCONCLUSIVE,
            case_comparisons=(),
            detail=(
                "suite ids differ; reports are only comparable"
                " within the same suite"
            ),
        )
    policy = baseline.comparison_policy

    baseline_cases = {
        (case.case_id, case.variant_id): case
        for case in baseline_report.case_results
    }
    current_cases = {
        (case.case_id, case.variant_id): case
        for case in current.case_results
    }
    comparisons: list[CaseComparison] = []
    for key in sorted(set(baseline_cases) | set(current_cases)):
        case_id, variant_id = key
        baseline_case = baseline_cases.get(key)
        current_case = current_cases.get(key)
        if baseline_case is None:
            comparisons.append(
                CaseComparison(
                    case_id=case_id,
                    variant_id=variant_id,
                    verdict=ComparisonVerdict.NEW,
                    current_overall=(
                        current_case.overall_outcome if current_case else None
                    ),
                    detail="case variant absent from the baseline report",
                )
            )
            continue
        if current_case is None:
            comparisons.append(
                CaseComparison(
                    case_id=case_id,
                    variant_id=variant_id,
                    verdict=ComparisonVerdict.MISSING,
                    baseline_overall=baseline_case.overall_outcome,
                    detail="case variant absent from the current report",
                )
            )
            continue
        non_comparable = (
            baseline_case.overall_outcome in _NON_COMPARABLE
            or current_case.overall_outcome in _NON_COMPARABLE
        )
        if non_comparable:
            comparisons.append(
                CaseComparison(
                    case_id=case_id,
                    variant_id=variant_id,
                    verdict=ComparisonVerdict.INCONCLUSIVE,
                    baseline_overall=baseline_case.overall_outcome,
                    current_overall=current_case.overall_outcome,
                    detail=(
                        "insufficient evidence on at least one side;"
                        " missing data is not proof of no regression"
                    ),
                )
            )
            continue
        verdict, detail = _classify(baseline_case, current_case, policy)
        comparisons.append(
            CaseComparison(
                case_id=case_id,
                variant_id=variant_id,
                verdict=verdict,
                baseline_overall=baseline_case.overall_outcome,
                current_overall=current_case.overall_outcome,
                detail=detail,
            )
        )

    verdicts = {comparison.verdict for comparison in comparisons}
    if ComparisonVerdict.REGRESSION in verdicts:
        overall = ComparisonOverall.REGRESSION
    elif ComparisonVerdict.INCONCLUSIVE in verdicts:
        overall = ComparisonOverall.INCONCLUSIVE
    else:
        overall = ComparisonOverall.COMPARABLE
    return BaselineComparison(
        baseline_id=baseline.baseline_id,
        baseline_report_id=baseline.report_id,
        baseline_revision=baseline.report_revision,
        current_report_id=current.report_id,
        current_revision=current.revision,
        overall=overall,
        case_comparisons=tuple(comparisons),
    )


def _classify(
    baseline_case: CaseVariantReport,
    current_case: CaseVariantReport,
    policy: ComparisonPolicy,
) -> tuple[ComparisonVerdict, str]:
    """对两侧证据齐全的 case variant 做确定性分类。"""
    hard_regression = (
        baseline_case.hard_outcome is EvaluatorOutcome.PASS
        and current_case.hard_outcome is EvaluatorOutcome.FAIL
    )
    quality_regression = policy.quality_regression and (
        baseline_case.quality_outcome is EvaluatorOutcome.PASS
        and current_case.quality_outcome is EvaluatorOutcome.FAIL
    )
    pass_at_k_regression = policy.pass_at_k_regression and (
        baseline_case.pass_at_k and not current_case.pass_at_k
    )
    regressions = [
        name
        for name, hit in (
            ("hard gate", hard_regression),
            ("quality gate", quality_regression),
            ("pass^k", pass_at_k_regression),
        )
        if hit
    ]
    if regressions:
        return ComparisonVerdict.REGRESSION, "regression: " + ", ".join(regressions)
    identical = (
        baseline_case.hard_outcome == current_case.hard_outcome
        and baseline_case.quality_outcome == current_case.quality_outcome
        and baseline_case.overall_outcome == current_case.overall_outcome
        and baseline_case.pass_at_k == current_case.pass_at_k
    )
    if identical:
        return ComparisonVerdict.UNCHANGED, ""
    return ComparisonVerdict.CHANGED, "outcome changed without regression"
