"""Immutable Eval Observation：两种模式归一化的稳定证据契约（ADR 0029）。

EXECUTE 与 OBSERVE 都归一化为不可变 Observation，以稳定 completeness
状态区分 complete、sampled、unsupported、unavailable 与 inconclusive
证据；sampled 证据必须携带抽样披露且不得表述为全量证明。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..._history import ConversationMessage
from ..._model import ModelUsage
from ..._run import RunInspection
from ..._status import RunStatus, is_terminal
from ..._steps import utc_now


class EvalMode(str, enum.Enum):
    """Eval 的两种受控模式。"""

    EXECUTE = "EXECUTE"
    OBSERVE = "OBSERVE"

    def __str__(self) -> str:  # pragma: no cover - 便捷展示
        return self.value


class EvidenceCompleteness(str, enum.Enum):
    """Observation 证据完备性的稳定分类。

    - ``COMPLETE``：终态 subject 的完整、未抽样证据；
    - ``SAMPLED``：来自抽样选择的证据，必须携带 SamplingDisclosure，
      不得表述为全量证明；
    - ``UNSUPPORTED``：subject 所需能力静态缺失，执行被短路（零
      provider 请求、零副作用）；
    - ``UNAVAILABLE``：subject 或证据来源不存在/不可读；
    - ``INCONCLUSIVE``：subject 存在但证据不足以得出结论（如非终态）。
    """

    COMPLETE = "COMPLETE"
    SAMPLED = "SAMPLED"
    UNSUPPORTED = "UNSUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"
    INCONCLUSIVE = "INCONCLUSIVE"

    def __str__(self) -> str:  # pragma: no cover - 便捷展示
        return self.value


#: Observation 归一化使用的稳定 reason code。
REASON_SUBJECT_TERMINAL = "SUBJECT_TERMINAL"
REASON_SUBJECT_NOT_TERMINAL = "SUBJECT_NOT_TERMINAL"
REASON_SUBJECT_RUN_MISSING = "SUBJECT_RUN_MISSING"
REASON_SAMPLING_APPLIED = "SAMPLING_APPLIED"
#: EXECUTE 静态能力短路（零 Run、零 dispatch）。
REASON_MODEL_CAPABILITIES_MISSING = "MODEL_CAPABILITIES_MISSING"
#: EXECUTE 无法解析 Variant 指向的 Definition。
REASON_VARIANT_UNRESOLVED = "VARIANT_UNRESOLVED"


class SamplingDisclosure(BaseModel):
    """抽样选择的显式披露（sampled 证据不得冒充全量证明）。

    ``candidates`` 是候选总体规模，``included`` 是本次实际纳入数；
    ``selection_method`` 与 ``seed`` 使抽样可复现。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    selection_method: str = Field(min_length=1)
    seed: str = Field(min_length=1)
    included: int = Field(ge=0)
    candidates: int = Field(ge=0)

    @model_validator(mode="after")
    def _reject_inconsistent_counts(self) -> "SamplingDisclosure":
        if self.included > self.candidates:
            raise ValueError(
                "sampling disclosure cannot include more runs than "
                "the candidate population"
            )
        return self

    def is_full_population(self) -> bool:
        """该披露是否覆盖全部候选（included == candidates 且 > 0）。"""
        return self.candidates > 0 and self.included == self.candidates


class EvalObservation(BaseModel):
    """一次归一化的不可变评估观测。

    携带 Projection 所需的最小证据字段；字段为 None 表示「该证据
    不可用」，绝不伪造。SAMPLED 状态必须携带 sampling 披露。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str = Field(min_length=1)
    mode: EvalMode
    subject_run_id: str
    definition_id: str = ""
    definition_version: str = ""
    variant_id: str | None = None
    completeness: EvidenceCompleteness
    reason_code: str = Field(min_length=1)
    collected_at: datetime = Field(default_factory=utc_now)
    #: 关联的 Eval Execution（EXECUTE 模式）；OBSERVE 为 None。
    execution_id: str | None = None
    #: -- Projection 来源字段（None = 不可用，绝不伪造） ----------------
    run_status: RunStatus | None = None
    run_input: str | None = None
    run_output: str | None = None
    conversation_history: tuple[ConversationMessage, ...] | None = None
    error_code: str | None = None
    step_types: tuple[str, ...] | None = None
    usage: ModelUsage | None = None
    sampling: SamplingDisclosure | None = None
    #: 关联的 Evidence Artifact（外部只读证据）。
    external_evidence: tuple[Any, ...] = ()

    @model_validator(mode="after")
    def _sampled_requires_disclosure(self) -> "EvalObservation":
        if self.completeness is EvidenceCompleteness.SAMPLED and (
            self.sampling is None
        ):
            raise ValueError(
                "SAMPLED observations must carry a sampling disclosure"
            )
        return self

    def claims_full_population(self) -> bool:
        """该 Observation 是否可作为全量总体证明。

        只有非抽样且完备的证据才允许全量表述；SAMPLED 恒为 False。
        """
        return self.completeness is EvidenceCompleteness.COMPLETE


def observation_from_inspection(
    *,
    observation_id: str,
    mode: EvalMode,
    inspection: RunInspection | None,
    subject_run_id: str,
    execution_id: str | None = None,
    variant_id: str | None = None,
    sampling: SamplingDisclosure | None = None,
) -> EvalObservation:
    """把公开 RunInspection（或其缺失）归一化为不可变 Observation。

    - inspection 为 None：UNAVAILABLE（subject 不存在/不可读）；
    - 非终态：INCONCLUSIVE（不伪造结论）；
    - 终态：COMPLETE，或在携带 sampling 披露时升级为 SAMPLED。
    """
    if inspection is None:
        return EvalObservation(
            observation_id=observation_id,
            mode=mode,
            subject_run_id=subject_run_id,
            completeness=EvidenceCompleteness.UNAVAILABLE,
            reason_code=REASON_SUBJECT_RUN_MISSING,
            execution_id=execution_id,
            variant_id=variant_id,
            sampling=sampling,
        )
    run = inspection.run
    usage = _total_usage(inspection)
    common: dict[str, Any] = dict(
        observation_id=observation_id,
        mode=mode,
        subject_run_id=run.run_id,
        definition_id=run.definition_id,
        definition_version=run.definition_version,
        execution_id=execution_id,
        variant_id=variant_id,
        run_status=run.status,
        conversation_history=run.history or None,
        step_types=tuple(step.step_type.value for step in inspection.steps),
        usage=usage,
        sampling=sampling,
    )
    if not is_terminal(run.status):
        return EvalObservation(
            completeness=EvidenceCompleteness.INCONCLUSIVE,
            reason_code=REASON_SUBJECT_NOT_TERMINAL,
            **common,
        )
    completeness = EvidenceCompleteness.COMPLETE
    reason = REASON_SUBJECT_TERMINAL
    if sampling is not None:
        completeness = EvidenceCompleteness.SAMPLED
        reason = REASON_SAMPLING_APPLIED
    return EvalObservation(
        completeness=completeness,
        reason_code=reason,
        run_input=run.input,
        run_output=run.output,
        error_code=run.error_code,
        **common,
    )


def _total_usage(inspection: RunInspection) -> ModelUsage | None:
    """汇总全部 Attempt 的 usage；缺失字段保持缺失，绝不发明数值。"""
    totals: dict[str, int] = {}
    for attempt in inspection.attempts:
        usage = attempt.usage
        if usage is None:
            continue
        for field in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
        ):
            value = getattr(usage, field)
            if value is None:
                continue
            totals[field] = totals.get(field, 0) + value
    if not totals:
        return None
    return ModelUsage(**totals)  # type: ignore[arg-type]
