"""不可变 Report Revision：pass^k、统计披露与分离 gate（Ticket 18）。

Report 把一次 Eval Execution 的 Evaluator 结果聚合为不可变 revision：
每次 repetition 的结果原样保留（best-of-many 绝不吞掉失败样本），
``pass^k`` 要求全部 repetition 通过；数值统计只披露有依据的量——
sample count、min/max/median 恒可披露，P95 仅在样本充足
（>= ``DEFAULT_MIN_SAMPLES_FOR_PERCENTILE``）时报告。

hard gate、quality gate 与 overall outcome 分开聚合：任何质量分数
（包括 Judge 分）永不参与 gate 判定，deterministic hard/safety failure
不可被聚合覆盖。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..._steps import utc_now
from ._case import EvalSuite
from ._evaluator import EvaluatorOutcome, EvaluatorResultRecord
from ._identity import canonical_json, sha256_hex

__all__ = [
    "DEFAULT_MIN_SAMPLES_FOR_PERCENTILE",
    "CaseVariantReport",
    "NumericSummary",
    "RepetitionOutcome",
    "ReportRevisionRecord",
    "build_report_revision",
    "summarize_samples",
]

#: P95 等高阶分位数要求的最小样本量（低于该值不报告，绝不无依据披露）。
DEFAULT_MIN_SAMPLES_FOR_PERCENTILE = 20


class RepetitionOutcome(BaseModel):
    """一次 repetition 的保留结果（聚合不吞掉单次结果）。

    ``outcome`` 是该 repetition 的整体结论；hard/quality 分开保留
    （build_report_revision 恒写入分离值；直接构造缺省为 None 表示
    未单独报告该 gate）。
    ``score`` 是该 repetition 的质量测量（多个打分 Evaluator 的均值），
    无任何打分时为 None——缺失绝不伪造成 0。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repetition_index: int = Field(ge=0)
    outcome: EvaluatorOutcome
    hard_outcome: EvaluatorOutcome | None = None
    quality_outcome: EvaluatorOutcome | None = None
    score: float | None = None


class NumericSummary(BaseModel):
    """数值统计披露：sample count 与 min/max/median；P95 需样本充足。

    ``p95`` 为 None 且 ``p95_justified`` 为 False 表示样本不足、
    不报告无依据的 P95；``p95_justified`` 为 True 时 ``p95`` 必须存在。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sample_count: int = Field(ge=0)
    minimum: float | None = None
    maximum: float | None = None
    median: float | None = None
    p95: float | None = None
    p95_justified: bool = False

    @classmethod
    def empty(cls) -> "NumericSummary":
        """无任何样本时的披露（sample_count=0，无统计量）。"""
        return cls(sample_count=0)


class CaseVariantReport(BaseModel):
    """Report 中一个 case × variant 的聚合结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1)
    variant_id: str = Field(min_length=1)
    repetitions: tuple[RepetitionOutcome, ...] = ()
    hard_outcome: EvaluatorOutcome
    quality_outcome: EvaluatorOutcome
    overall_outcome: EvaluatorOutcome
    #: pass^k：全部 repetition 整体 PASS 才为 True（非 best-of-many）。
    pass_at_k: bool
    scores: tuple[float, ...] = ()
    score_summary: NumericSummary | None = None


class ReportRevisionRecord(BaseModel):
    """不可变 Report revision（新证据创建新 revision，绝不覆盖历史）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    execution_id: str | None = None
    suite_id: str = Field(min_length=1)
    suite_version: str = Field(min_length=1)
    suite_digest: str = Field(min_length=1)
    case_results: tuple[CaseVariantReport, ...] = ()
    hard_outcome: EvaluatorOutcome
    quality_outcome: EvaluatorOutcome
    overall_outcome: EvaluatorOutcome
    created_at: datetime = Field(default_factory=utc_now)
    content_digest: str = ""

    @classmethod
    def build(
        cls,
        *,
        report_id: str,
        revision: int,
        suite_id: str,
        suite_version: str,
        suite_digest: str,
        case_results: tuple[CaseVariantReport, ...],
        hard_outcome: EvaluatorOutcome,
        quality_outcome: EvaluatorOutcome,
        overall_outcome: EvaluatorOutcome,
        execution_id: str | None = None,
        created_at: datetime | None = None,
    ) -> "ReportRevisionRecord":
        """构造 revision 并按内容计算 ``content_digest``。

        digest 覆盖除 ``content_digest`` 外的全部字段（含 created_at），
        是该条 revision 记录的精确指纹。参数显式声明：字段名拼错在
        静态检查期即报错，而不是等到运行期 pydantic 校验。
        """
        record = cls(
            report_id=report_id,
            revision=revision,
            suite_id=suite_id,
            suite_version=suite_version,
            suite_digest=suite_digest,
            case_results=case_results,
            hard_outcome=hard_outcome,
            quality_outcome=quality_outcome,
            overall_outcome=overall_outcome,
            execution_id=execution_id,
            created_at=created_at if created_at is not None else utc_now(),
        )
        return record.model_copy(
            update={"content_digest": record.content_digest_payload()}
        )

    def content_digest_payload(self) -> str:
        """按排除 ``content_digest`` 的规范 JSON 计算内容摘要。"""
        payload = self.model_dump(mode="json", exclude={"content_digest"})
        return sha256_hex(canonical_json(payload))


def summarize_samples(
    samples: Sequence[float],
    *,
    min_samples_for_percentile: int = DEFAULT_MIN_SAMPLES_FOR_PERCENTILE,
) -> NumericSummary:
    """把数值样本汇总为有依据的统计披露。

    sample count、min/max/median 恒披露（有样本时）；P95 仅在
    ``len(samples) >= min_samples_for_percentile`` 时按 nearest-rank
    报告——样本不足时 ``p95=None`` 且 ``p95_justified=False``。
    """
    if not samples:
        return NumericSummary.empty()
    ordered = sorted(samples)
    count = len(ordered)
    if count % 2:
        median: float = ordered[count // 2]
    else:
        median = (ordered[count // 2 - 1] + ordered[count // 2]) / 2
    p95: float | None = None
    justified = False
    if count >= min_samples_for_percentile:
        rank = math.ceil(0.95 * count)
        p95 = ordered[rank - 1]
        justified = True
    return NumericSummary(
        sample_count=count,
        minimum=ordered[0],
        maximum=ordered[-1],
        median=median,
        p95=p95,
        p95_justified=justified,
    )


def _worst(outcomes: Sequence[EvaluatorOutcome]) -> EvaluatorOutcome:
    """确定性优先级：FAIL > ERROR > INCONCLUSIVE > UNSUPPORTED > PASS。"""
    present = set(outcomes)
    for candidate in (
        EvaluatorOutcome.FAIL,
        EvaluatorOutcome.ERROR,
        EvaluatorOutcome.INCONCLUSIVE,
        EvaluatorOutcome.UNSUPPORTED,
    ):
        if candidate in present:
            return candidate
    return EvaluatorOutcome.PASS


def _repetition_outcome(
    records: Sequence[EvaluatorResultRecord], repetition_index: int
) -> RepetitionOutcome:
    """把一个 repetition 的 evaluator 结果聚合为保留结果。"""
    if not records:
        # 缺失证据不是「无问题」：归一为 INCONCLUSIVE 而非 PASS。
        return RepetitionOutcome(
            repetition_index=repetition_index,
            outcome=EvaluatorOutcome.INCONCLUSIVE,
            hard_outcome=EvaluatorOutcome.INCONCLUSIVE,
            quality_outcome=EvaluatorOutcome.INCONCLUSIVE,
        )
    hard_outcomes = [record.outcome for record in records if record.hard]
    quality_outcomes = [
        record.outcome for record in records if not record.hard
    ]
    scores = [record.score for record in records if record.score is not None]
    return RepetitionOutcome(
        repetition_index=repetition_index,
        outcome=_worst([record.outcome for record in records]),
        hard_outcome=_worst(hard_outcomes),
        quality_outcome=_worst(quality_outcomes),
        score=(sum(scores) / len(scores)) if scores else None,
    )


def build_report_revision(
    *,
    report_id: str,
    revision: int,
    execution_id: str | None,
    suite: EvalSuite,
    results: Sequence[EvaluatorResultRecord],
    min_samples_for_percentile: int = DEFAULT_MIN_SAMPLES_FOR_PERCENTILE,
) -> ReportRevisionRecord:
    """把 execution 的 evaluator 结果聚合为不可变 revision。

    聚合规则（确定性，永不读取分数参与判定）：

    - case × variant 集合以 Suite 冻结展开为权威：任何没有结果的
      repetition 归一为 INCONCLUSIVE（缺失证据不是「无问题」）；
    - 每个 repetition 保留一条 :class:`RepetitionOutcome`
      （hard/quality/overall 分开 worst-of）；
    - ``pass_at_k`` 为 True 当且仅当全部 repetition overall PASS；
    - score 统计只汇总真实存在的分数样本（每个 repetition 的测量
      是其打分 Evaluator 的均值）；
    - Report 级 gate 是全部 case gate 的 worst-of。
    """
    grouped: dict[tuple[str, str], dict[int, list[EvaluatorResultRecord]]] = {}
    for result in results:
        key = (result.case_id, result.variant_id)
        grouped.setdefault(key, {}).setdefault(
            result.repetition_index, []
        ).append(result)

    case_reports: list[CaseVariantReport] = []
    seen_keys: set[tuple[str, str]] = set()
    for item in suite.expand():
        key = (item.case_id, item.variant.variant_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        case = suite.case_by_id(item.case_id)
        repetitions = grouped.get(key, {})
        ordered = tuple(
            _repetition_outcome(
                repetitions.get(index, ()), index
            )
            for index in range(case.execution_protocol.repetitions)
        )
        samples = tuple(
            outcome.score for outcome in ordered if outcome.score is not None
        )
        case_reports.append(
            CaseVariantReport(
                case_id=key[0],
                variant_id=key[1],
                repetitions=ordered,
                hard_outcome=_worst([
                    outcome.hard_outcome
                    for outcome in ordered
                    if outcome.hard_outcome is not None
                ]),
                quality_outcome=_worst([
                    outcome.quality_outcome
                    for outcome in ordered
                    if outcome.quality_outcome is not None
                ]),
                overall_outcome=_worst([
                    outcome.outcome for outcome in ordered
                ]),
                pass_at_k=bool(ordered)
                and all(
                    outcome.outcome is EvaluatorOutcome.PASS
                    for outcome in ordered
                ),
                scores=samples,
                score_summary=summarize_samples(
                    samples,
                    min_samples_for_percentile=min_samples_for_percentile,
                ),
            )
        )
    case_reports.sort(key=lambda report: (report.case_id, report.variant_id))

    return ReportRevisionRecord.build(
        report_id=report_id,
        revision=revision,
        execution_id=execution_id,
        suite_id=suite.suite_id,
        suite_version=suite.version,
        suite_digest=suite.content_digest(),
        case_results=tuple(case_reports),
        hard_outcome=_worst([r.hard_outcome for r in case_reports]),
        quality_outcome=_worst([r.quality_outcome for r in case_reports]),
        overall_outcome=_worst([r.overall_outcome for r in case_reports]),
        created_at=utc_now(),
    )
