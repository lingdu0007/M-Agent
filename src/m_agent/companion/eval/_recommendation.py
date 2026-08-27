"""Model Recommendation：只读引用证据与目标，绝不自动改写未来（Ticket 18）。

Recommendation 是冻结的版本化记录：它只引用证据（Report revision
digest、Baseline）与目标 Policy/Variant 身份，不自动修改 Baseline、
Definition、Catalog 或未来 routing 行为——任何生效动作都是上层
应用的显式决策。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ..._steps import utc_now
from ._evaluator import EvaluatorOutcome

__all__ = [
    "ModelRecommendationRecord",
    "RecommendationTarget",
    "RECOMMENDATION_TARGET_AGENT_VARIANT",
    "RECOMMENDATION_TARGET_ROUTING_POLICY",
]

#: 目标身份的稳定 kind（只读引用，不触发任何 routing 行为）。
RECOMMENDATION_TARGET_ROUTING_POLICY = "ROUTING_POLICY"
RECOMMENDATION_TARGET_AGENT_VARIANT = "AGENT_VARIANT"


class RecommendationTarget(BaseModel):
    """Recommendation 引用的目标 Policy/Variant 精确身份。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    target_version: str = Field(min_length=1)


class ModelRecommendationRecord(BaseModel):
    """版本化只读推荐：引用证据与目标，冻结 gate 结论与置信度。

    记录一经保存即不可变（同 id 异内容确定性冲突）；本类型不提供
    任何会改动 Baseline、Definition、Catalog 或 routing 的接口。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recommendation_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    report_id: str = Field(min_length=1)
    report_revision: int = Field(ge=1)
    baseline_id: str | None = None
    target: RecommendationTarget
    hard_gate: EvaluatorOutcome
    quality_gate: EvaluatorOutcome
    overall_outcome: EvaluatorOutcome
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_digest: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    valid_until: datetime | None = None
