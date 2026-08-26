"""确定性 Evaluator：结构化 result/reason code 与最小证据消费。

Evaluator 只读 Observation Projection；harness 先依据 Projection 的
completeness / denied / unavailable 归一证据失败（INCONCLUSIVE，而非
「问题不存在」），再调用 Evaluator 本体；本体异常归一为 evaluator
failure（ERROR）。subject failure、evaluator failure 与 evidence
failure 由 outcome + failure_kind + reason code 三元组区分；任何单一
质量分数不得覆盖 hard outcome（aggregate 只按 outcome 计算 verdict）。
"""

from __future__ import annotations

import enum
from datetime import datetime
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..._steps import utc_now
from ._case import EvaluatorRef
from ._observation import EvidenceCompleteness
from ._projection import EvidenceField, EvidenceRequirements, ObservationProjection

__all__ = [
    "DeterministicEvaluator",
    "EvalFailureKind",
    "EvaluatorAggregate",
    "EvaluatorOutcome",
    "EvaluatorResult",
    "EvaluatorResultRecord",
    "OutputMatchesEvaluator",
    "REASON_EVALUATOR_RAISED",
    "REASON_EVIDENCE_MISSING",
    "REASON_EVIDENCE_UNAUTHORIZED",
    "REASON_SUBJECT_EVIDENCE_INCONCLUSIVE",
    "REASON_SUBJECT_EVIDENCE_UNAVAILABLE",
    "REASON_SUBJECT_OUTPUT_MATCHED",
    "REASON_SUBJECT_OUTPUT_MISMATCH",
    "RunStatusEvaluator",
    "aggregate_evaluator_results",
    "run_evaluator",
]


class EvaluatorOutcome(str, enum.Enum):
    """Evaluator 结果的稳定分类。"""

    PASS = "PASS"
    FAIL = "FAIL"
    UNSUPPORTED = "UNSUPPORTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    ERROR = "ERROR"

    def __str__(self) -> str:  # pragma: no cover - 便捷展示
        return self.value


class EvalFailureKind(str, enum.Enum):
    """失败归属：subject / evaluator / evidence 三类互斥。"""

    NONE = "NONE"
    SUBJECT = "SUBJECT"
    EVALUATOR = "EVALUATOR"
    EVIDENCE = "EVIDENCE"

    def __str__(self) -> str:  # pragma: no cover - 便捷展示
        return self.value


#: Evaluator 层稳定 reason code。
REASON_EVIDENCE_MISSING = "EVIDENCE_MISSING"
REASON_EVIDENCE_UNAUTHORIZED = "EVIDENCE_UNAUTHORIZED"
REASON_EVALUATOR_RAISED = "EVALUATOR_RAISED"
REASON_SUBJECT_EVIDENCE_UNAVAILABLE = "SUBJECT_EVIDENCE_UNAVAILABLE"
REASON_SUBJECT_EVIDENCE_INCONCLUSIVE = "SUBJECT_EVIDENCE_INCONCLUSIVE"
REASON_SUBJECT_OUTPUT_MATCHED = "SUBJECT_OUTPUT_MATCHED"
REASON_SUBJECT_OUTPUT_MISMATCH = "SUBJECT_OUTPUT_MISMATCH"
REASON_SUBJECT_STATUS_MISMATCH = "SUBJECT_STATUS_MISMATCH"


class EvaluatorResult(BaseModel):
    """一次评估的结构化结论。

    ``score`` 是可选质量维度（永远不参与 verdict 计算）；``hard``
    标记 hard/safety gate——其 FAIL 不可被任何分数或 Judge 覆盖。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluator: EvaluatorRef
    outcome: EvaluatorOutcome
    failure_kind: EvalFailureKind
    reason_code: str = Field(min_length=1)
    detail: str = ""
    score: float | None = None
    hard: bool = False
    evidence_refs: tuple[str, ...] = ()


@runtime_checkable
class DeterministicEvaluator(Protocol):
    """确定性 Evaluator 契约：只消费 Projection，产出结构化结果。"""

    @property
    def identity(self) -> EvaluatorRef: ...

    @property
    def hard(self) -> bool: ...

    @property
    def evidence_requirements(self) -> EvidenceRequirements: ...

    def evaluate(self, projection: ObservationProjection) -> EvaluatorResult: ...


def run_evaluator(
    evaluator: DeterministicEvaluator,
    projection: ObservationProjection,
) -> EvaluatorResult:
    """按证据前置检查归一后调用 Evaluator 本体。

    归一规则（在调用本体之前）：

    1. completeness 为 UNSUPPORTED -> UNSUPPORTED（沿用 Observation 的
       reason code，不调用本体）；
    2. completeness 为 UNAVAILABLE / INCONCLUSIVE -> INCONCLUSIVE /
       EVIDENCE（subject 证据不可用/不足，不调用本体）；
    3. denied 非空 -> INCONCLUSIVE / EVIDENCE / EVIDENCE_UNAUTHORIZED
       （不调用本体——评估不是数据访问旁路）；
    4. unavailable 非空 -> INCONCLUSIVE / EVIDENCE / EVIDENCE_MISSING；
    5. 本体异常 -> ERROR / EVALUATOR / EVALUATOR_RAISED。
    """
    if projection.completeness is EvidenceCompleteness.UNSUPPORTED:
        return EvaluatorResult(
            evaluator=evaluator.identity,
            outcome=EvaluatorOutcome.UNSUPPORTED,
            failure_kind=EvalFailureKind.NONE,
            reason_code=projection.reason_code,
            hard=evaluator.hard,
        )
    if projection.completeness in (
        EvidenceCompleteness.UNAVAILABLE,
        EvidenceCompleteness.INCONCLUSIVE,
    ):
        reason = (
            REASON_SUBJECT_EVIDENCE_UNAVAILABLE
            if projection.completeness is EvidenceCompleteness.UNAVAILABLE
            else REASON_SUBJECT_EVIDENCE_INCONCLUSIVE
        )
        return EvaluatorResult(
            evaluator=evaluator.identity,
            outcome=EvaluatorOutcome.INCONCLUSIVE,
            failure_kind=EvalFailureKind.EVIDENCE,
            reason_code=reason,
            hard=evaluator.hard,
        )
    if projection.denied or projection.denied_evidence_ids:
        return EvaluatorResult(
            evaluator=evaluator.identity,
            outcome=EvaluatorOutcome.INCONCLUSIVE,
            failure_kind=EvalFailureKind.EVIDENCE,
            reason_code=REASON_EVIDENCE_UNAUTHORIZED,
            detail="required evidence is not authorized by the projection "
            "policy; evaluation is not a data-access bypass",
            hard=evaluator.hard,
        )
    if projection.unavailable or projection.missing_evidence_ids:
        return EvaluatorResult(
            evaluator=evaluator.identity,
            outcome=EvaluatorOutcome.INCONCLUSIVE,
            failure_kind=EvalFailureKind.EVIDENCE,
            reason_code=REASON_EVIDENCE_MISSING,
            detail="required evidence is unavailable; missing data is not "
            "proof that no problem exists",
            hard=evaluator.hard,
        )
    try:
        result = evaluator.evaluate(projection)
    except Exception as exc:
        return EvaluatorResult(
            evaluator=evaluator.identity,
            outcome=EvaluatorOutcome.ERROR,
            failure_kind=EvalFailureKind.EVALUATOR,
            reason_code=REASON_EVALUATOR_RAISED,
            detail=f"{type(exc).__name__}: {exc}",
            hard=evaluator.hard,
        )
    return result.model_copy(update={"hard": result.hard or evaluator.hard})


class EvaluatorAggregate(BaseModel):
    """一次评估的多维度聚合结论。

    ``outcome`` 是整体 verdict，``hard_outcome`` 只聚合 hard/safety
    gate；``scores`` 保留为独立质量维度——聚合规则永不读取 scores，
    因此任何单一质量分数都不能覆盖 hard outcome。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EvaluatorOutcome
    hard_outcome: EvaluatorOutcome
    scores: tuple[float, ...] = ()
    results: tuple[EvaluatorResult, ...] = ()


def aggregate_evaluator_results(
    results: Sequence[EvaluatorResult],
) -> EvaluatorAggregate:
    """按确定性优先级聚合：hard FAIL > ERROR > INCONCLUSIVE > 其它。

    优先级（文档化且确定性）：

    1. 任一 hard 结果 FAIL -> FAIL；
    2. 否则任一结果 ERROR -> ERROR；
    3. 否则任一结果 INCONCLUSIVE -> INCONCLUSIVE；
    4. 否则任一 hard 结果 UNSUPPORTED -> UNSUPPORTED；
    5. 否则 PASS。

    scores 只被收集、从不参与上述判定。
    """
    frozen = tuple(results)
    hard_results = [result for result in frozen if result.hard]

    def worst(candidates: Sequence[EvaluatorResult]) -> EvaluatorOutcome:
        outcomes = {result.outcome for result in candidates}
        if EvaluatorOutcome.FAIL in outcomes:
            return EvaluatorOutcome.FAIL
        if EvaluatorOutcome.ERROR in outcomes:
            return EvaluatorOutcome.ERROR
        if EvaluatorOutcome.INCONCLUSIVE in outcomes:
            return EvaluatorOutcome.INCONCLUSIVE
        if EvaluatorOutcome.UNSUPPORTED in outcomes:
            return EvaluatorOutcome.UNSUPPORTED
        return EvaluatorOutcome.PASS

    hard_outcome = worst(hard_results)
    overall = worst(frozen)
    return EvaluatorAggregate(
        outcome=overall,
        hard_outcome=hard_outcome,
        scores=tuple(
            result.score for result in frozen if result.score is not None
        ),
        results=frozen,
    )


class OutputMatchesEvaluator:
    """参考确定性 Evaluator：断言 subject 最终输出等于期望文本。

    只声明 RUN_OUTPUT 一个证据字段；结果归属 subject（PASS=匹配，
    FAIL=不匹配），reason code 稳定可比较。
    """

    def __init__(
        self,
        *,
        evaluator_id: str,
        version: str,
        expected: str,
        hard: bool = False,
    ) -> None:
        self._identity = EvaluatorRef(
            evaluator_id=evaluator_id, version=version
        )
        self._expected = expected
        self._hard = hard

    @property
    def identity(self) -> EvaluatorRef:
        return self._identity

    @property
    def hard(self) -> bool:
        return self._hard

    @property
    def evidence_requirements(self) -> EvidenceRequirements:
        return EvidenceRequirements(
            evaluator=self._identity,
            fields=frozenset({EvidenceField.RUN_OUTPUT}),
        )

    def evaluate(self, projection: ObservationProjection) -> EvaluatorResult:
        output = projection.get(EvidenceField.RUN_OUTPUT)
        matched = output == self._expected
        return EvaluatorResult(
            evaluator=self._identity,
            outcome=EvaluatorOutcome.PASS if matched else EvaluatorOutcome.FAIL,
            failure_kind=EvalFailureKind.NONE if matched else EvalFailureKind.SUBJECT,
            reason_code=REASON_SUBJECT_OUTPUT_MATCHED
            if matched
            else REASON_SUBJECT_OUTPUT_MISMATCH,
            detail="" if matched else f"expected {self._expected!r}",
            hard=self._hard,
            evidence_refs=(projection.observation_id,),
        )


class RunStatusEvaluator:
    """参考确定性 hard-gate Evaluator：断言 subject 到达期望终态。"""

    def __init__(
        self,
        *,
        evaluator_id: str,
        version: str,
        expected_status: str,
    ) -> None:
        self._identity = EvaluatorRef(
            evaluator_id=evaluator_id, version=version
        )
        self._expected_status = expected_status

    @property
    def identity(self) -> EvaluatorRef:
        return self._identity

    @property
    def hard(self) -> bool:
        return True

    @property
    def evidence_requirements(self) -> EvidenceRequirements:
        return EvidenceRequirements(
            evaluator=self._identity,
            fields=frozenset({EvidenceField.RUN_STATUS}),
        )

    def evaluate(self, projection: ObservationProjection) -> EvaluatorResult:
        status = projection.get(EvidenceField.RUN_STATUS)
        matched = getattr(status, "value", status) == self._expected_status
        return EvaluatorResult(
            evaluator=self._identity,
            outcome=EvaluatorOutcome.PASS if matched else EvaluatorOutcome.FAIL,
            failure_kind=EvalFailureKind.NONE if matched else EvalFailureKind.SUBJECT,
            reason_code="SUBJECT_STATUS_MATCHED"
            if matched
            else REASON_SUBJECT_STATUS_MISMATCH,
            detail=f"expected {self._expected_status!r}",
            hard=True,
            evidence_refs=(projection.observation_id,),
        )


class EvaluatorResultRecord(BaseModel):
    """一次 Evaluator 执行的持久化不可变结果（EvalStore 记录）。

    与 :class:`EvaluatorResult` 同构，附加确定性身份（``result_id``）、
    执行关联（execution / observation / item / case / variant /
    repetition）与 Judge 溯源（``judge_run_id``）。记录一经保存即
    不可变：同 id 同内容重放幂等，异内容确定性冲突。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: str = Field(min_length=1)
    execution_id: str | None = None
    observation_id: str = Field(min_length=1)
    item_id: str = ""
    case_id: str = ""
    variant_id: str = ""
    repetition_index: int = Field(default=0, ge=0)
    evaluator: EvaluatorRef
    outcome: EvaluatorOutcome
    failure_kind: EvalFailureKind
    reason_code: str = Field(min_length=1)
    detail: str = ""
    score: float | None = None
    hard: bool = False
    evidence_refs: tuple[str, ...] = ()
    judge_run_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
