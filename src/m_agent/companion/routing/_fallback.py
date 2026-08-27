"""Pre-Run Fallback 与显式 Replacement Run（ADR 0041，Ticket 20）。

Pre-Run Fallback 只在任何 Run / Session Claim 创建之前依照冻结的候选
Routing Policy 序列执行：每次尝试都是纯函数 Router 调用（零模型
dispatch、零状态突变），次数被显式上限结构性约束，且每次尝试的
outcome 与 reason 都进入可检查的尝试记录。

Run 创建后遇到 provider failure 绝不自动切换 Variant：需要跨模型
继续时由应用显式创建 Replacement Run，并通过
register_replacement_run 冻结
predecessor / reason / successor 关系——Replacement 是新的 Run 与
新的 Routing Decision，绝不伪装成原 Run 的重试或恢复。
"""

from __future__ import annotations

from datetime import datetime
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..._run import RunRecord
from ..._status import RunStatus
from ._catalog import ModelCatalog
from ._evidence import RoutingEvidence
from ._policy import RoutingPolicy, RoutingPolicyIdentity
from ._router import (
    ModelRouter,
    RoutingDecisionBinding,
    RoutingError,
    RoutingOutcome,
    RoutingResult,
)

_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: Fallback 结果的稳定 reason code。
FALLBACK_SELECTED = "FALLBACK_SELECTED"
FALLBACK_EXHAUSTED = "FALLBACK_EXHAUSTED"

#: 稳定 Replacement reason code（应用可声明更多大写 code）。
REPLACEMENT_REASON_PROVIDER_FAILURE = "PROVIDER_FAILURE"
REPLACEMENT_REASON_POLICY_RETIRED = "POLICY_RETIRED"
REPLACEMENT_REASON_COST_BREACH = "COST_BREACH"


class RoutingReplacementError(RoutingError):
    """Replacement Run 关系不完整或前后身份不一致。"""


class _FrozenFallbackValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FallbackSequence(_FrozenFallbackValue):
    """冻结的候选 Routing Policy 序列与显式尝试上限。

    序列在任何 Run / Session Claim 创建之前冻结，执行期不可变；
    重复策略身份与超过上限的序列都是结构错误，构造即失败。
    """

    policies: tuple[RoutingPolicy, ...]
    max_attempts: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_sequence(self) -> "FallbackSequence":
        if not self.policies:
            raise ValueError("fallback sequence must declare at least one policy")
        if len(self.policies) > self.max_attempts:
            raise ValueError(
                "fallback sequence length must not exceed max_attempts"
            )
        identities = {
            (policy.identity.policy_id, policy.identity.version)
            for policy in self.policies
        }
        if len(identities) != len(self.policies):
            raise ValueError("fallback policy identities must be unique")
        return self

    @property
    def attempt_count(self) -> int:
        """确定性的尝试次数上限（结构性有限）。"""
        return len(self.policies)


class FallbackAttempt(_FrozenFallbackValue):
    """一次 fallback 尝试的可检查记录：策略身份、结果与原因。"""

    attempt_index: int = Field(ge=1)
    policy_identity: RoutingPolicyIdentity
    outcome: RoutingOutcome
    reason_code: str
    decision_id: str | None = None

    @field_validator("reason_code")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        if not _REASON_CODE.fullmatch(value):
            raise ValueError("reason code must be a stable uppercase code")
        return value


class PreRunFallbackResult(_FrozenFallbackValue):
    """Pre-Run Fallback 的整体结果：全部尝试与最终选择。"""

    attempts: tuple[FallbackAttempt, ...]
    outcome: RoutingOutcome
    reason_code: str
    selected: RoutingResult | None = None
    exhausted: bool

    @field_validator("reason_code")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        if value not in (FALLBACK_SELECTED, FALLBACK_EXHAUSTED):
            raise ValueError("fallback reason must be a stable fallback code")
        return value


def execute_pre_run_fallback(
    *,
    catalog: ModelCatalog,
    sequence: FallbackSequence,
    evidence: RoutingEvidence | None = None,
    as_of: datetime,
    router: ModelRouter | None = None,
) -> PreRunFallbackResult:
    """在任何 Run / Session Claim 之前执行冻结的候选策略序列。

    按声明顺序逐个调用纯函数 Router：首次 SELECTED 即停止并携带完整
    结果；全部尝试失败则标记 exhausted。本函数不创建 Claim、Run 或
    任何模型 dispatch——每次尝试都只是 Router 的确定性重放。
    """
    active_router = router if router is not None else ModelRouter()
    attempts: list[FallbackAttempt] = []
    selected: RoutingResult | None = None
    for index, policy in enumerate(sequence.policies, start=1):
        result = active_router.select(
            catalog=catalog,
            policy=policy,
            evidence=evidence,
            as_of=as_of,
        )
        attempts.append(
            FallbackAttempt(
                attempt_index=index,
                policy_identity=policy.identity,
                outcome=result.outcome,
                reason_code=result.reason_code,
                decision_id=(
                    result.decision.decision_id if result.decision else None
                ),
            )
        )
        if result.outcome is RoutingOutcome.SELECTED:
            selected = result
            break
    exhausted = selected is None
    final_outcome = (
        selected.outcome if selected is not None else attempts[-1].outcome
    )
    return PreRunFallbackResult(
        attempts=tuple(attempts),
        outcome=final_outcome,
        reason_code=(
            FALLBACK_SELECTED if selected is not None else FALLBACK_EXHAUSTED
        ),
        selected=selected,
        exhausted=exhausted,
    )


class ReplacementRunRecord(_FrozenFallbackValue):
    """显式 Replacement Run 的不可变关系记录。

    predecessor 是因 provider failure 等原因终止的旧 Run，successor
    是经新 Routing Decision 显式创建的新 Run；记录保留双方 Run 与
    Variant 身份、两次 Decision 身份与稳定 reason code。
    """

    predecessor_run_id: str = Field(min_length=1)
    successor_run_id: str = Field(min_length=1)
    reason_code: str
    predecessor_variant_id: str = Field(min_length=1)
    predecessor_variant_version: str = Field(min_length=1)
    predecessor_decision_id: str = Field(min_length=1)
    successor_variant_id: str = Field(min_length=1)
    successor_variant_version: str = Field(min_length=1)
    successor_decision_id: str = Field(min_length=1)

    @field_validator("reason_code")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        if not _REASON_CODE.fullmatch(value):
            raise ValueError("replacement reason must be a stable uppercase code")
        return value


def register_replacement_run(
    *,
    predecessor: RunRecord,
    successor: RunRecord,
    predecessor_binding: RoutingDecisionBinding,
    successor_binding: RoutingDecisionBinding,
    reason_code: str,
) -> ReplacementRunRecord:
    """校验并冻结显式 Replacement Run 关系。

    只有终态（例如 provider failure 后 FAILED）的 Run 才能被替换；
    successor 必须是绑定到新 Routing Decision 的不同 Run——
    Replacement 绝不是原 Run 的重试、恢复或 Run 内换模。
    """
    if predecessor_binding.run_id != predecessor.run_id:
        raise RoutingReplacementError(
            "predecessor binding does not reference the predecessor run"
        )
    if successor_binding.run_id != successor.run_id:
        raise RoutingReplacementError(
            "successor binding does not reference the successor run"
        )
    if successor.run_id == predecessor.run_id:
        raise RoutingReplacementError(
            "replacement successor must be a different run"
        )

    if not RunStatus(predecessor.status.value).is_terminal:
        raise RoutingReplacementError(
            "only a terminal run can be replaced; in-run model switching is"
            " forbidden"
        )
    if successor_binding.decision_id == predecessor_binding.decision_id:
        raise RoutingReplacementError(
            "replacement must be routed by a new decision, never a retry of"
            " the old one"
        )
    return ReplacementRunRecord(
        predecessor_run_id=predecessor.run_id,
        successor_run_id=successor.run_id,
        reason_code=reason_code,
        predecessor_variant_id=predecessor_binding.variant_id,
        predecessor_variant_version=predecessor_binding.variant_version,
        predecessor_decision_id=predecessor_binding.decision_id,
        successor_variant_id=successor_binding.variant_id,
        successor_variant_version=successor_binding.variant_version,
        successor_decision_id=successor_binding.decision_id,
    )
