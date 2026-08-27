"""版本化 Routing Policy 与 Deployment Constraints（ADR 0041）。

Routing Policy 是 Runtime Companion 中版本化的 Run 前选择规则：组合
Model Requirements（hard 能力/容量条件）、候选 allowlist、Deployment
Constraints 与 hard 价格/质量/稳定性门槛，以及声明了方向、缺失值策略
的字典序 soft 排序目标。它不使用隐式加权总分，不改变 Agent
Definition 的运行语义，也不包含凭据或敏感 endpoint 配置。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import enum
import json
import hashlib

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from ..._model import ModelRequirements


class _FrozenRoutingValue(BaseModel):
    """Routing 层的冻结值：拒绝未知字段，不静默放宽规则。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


def canonical_digest(payload: object) -> str:
    """对规范化 JSON 表示计算稳定 sha256 摘要（确定性复跑的基础）。"""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class RoutingPolicyIdentity(_FrozenRoutingValue):
    """一个 Routing Policy 的精确 `policy_id + version` 身份。"""

    policy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class DeploymentConstraints(_FrozenRoutingValue):
    """Deployment 硬选择条件（ADR 0041）。

    provider、region、endpoint class 与数据保留声明版本都是非敏感
    deployment 属性上的硬匹配；``None`` 表示该维度不施加约束。约束
    声明的维度上，Catalog 条目属性未知（未声明）时 fail closed，被
    过滤为不兼容候选；合规结论只能引用外部版本化证据
    （:class:`~m_agent.companion.routing.RetentionEvidenceSnapshot`），
    凭据与敏感 endpoint 配置不属于本类型。
    """

    allowed_providers: frozenset[str] | None = None
    allowed_regions: frozenset[str] | None = None
    allowed_endpoint_classes: frozenset[str] | None = None
    allowed_retention_evidence_versions: frozenset[str] | None = None

    @field_serializer(
        "allowed_providers",
        "allowed_regions",
        "allowed_endpoint_classes",
        "allowed_retention_evidence_versions",
    )
    def _serialize_sorted(
        self, value: frozenset[str] | None
    ) -> list[str] | None:
        """排序序列化，使 ``content_digest`` 跨进程确定。

        ``frozenset`` 的迭代顺序受 ``PYTHONHASHSEED`` 影响；与
        ``_model`` 的 deterministic fingerprint 规范一致，集合值
        序列化为排序后的列表，保证相同内容的 Policy 在任何进程
        中产生相同 digest。
        """
        if value is None:
            return None
        return sorted(value)


class ObjectiveDimension(str, enum.Enum):
    """字典序排序可用的证据维度。"""

    COST = "COST"
    LATENCY = "LATENCY"
    QUALITY = "QUALITY"
    STABILITY = "STABILITY"


class ObjectiveDirection(str, enum.Enum):
    MINIMIZE = "MINIMIZE"
    MAXIMIZE = "MAXIMIZE"


class MissingValuePolicy(str, enum.Enum):
    """排序维度值缺失（快照缺失/过期/字段为空）时的确定性位置。"""

    ORDER_LAST = "ORDER_LAST"
    ORDER_FIRST = "ORDER_FIRST"


class RoutingObjective(_FrozenRoutingValue):
    """一个字典序排序目标：维度 + 方向 + 缺失值策略。"""

    dimension: ObjectiveDimension
    direction: ObjectiveDirection
    missing_value: MissingValuePolicy = MissingValuePolicy.ORDER_LAST


class HardRoutingGates(_FrozenRoutingValue):
    """hard policy 门槛：不满足即过滤，证据缺失/过期整体 fail closed。

    - ``max_input_price_per_mtok``：硬价格上限（Pricing Snapshot，
      每百万 input token）；
    - ``min_quality_score`` / ``min_stability_score``：硬质量/稳定性
      下限（Model Evidence，[0, 1]）；
    - ``require_availability``：硬可用性要求（Availability Snapshot）；
    - ``operational_limits``：硬运行限额门槛（Operational Limits
      Snapshot，RPM/TPM/并发/周期配额余量）；
    - ``worst_case_cost_cap``：硬最坏情况运行成本上限（Pricing
      Snapshot × 冻结 Contract Limits × Execution Budget；硬成本
      规则在价格证据缺失/过期/不完整时 fail closed）。
    """

    max_input_price_per_mtok: Decimal | None = Field(
        default=None, ge=Decimal("0")
    )
    min_quality_score: float | None = Field(default=None, ge=0.0, le=1.0)
    min_stability_score: float | None = Field(default=None, ge=0.0, le=1.0)
    require_availability: bool = False
    operational_limits: "OperationalLimitsGate | None" = None
    worst_case_cost_cap: "WorstCaseCostCap | None" = None


class OperationalLimitsGate(_FrozenRoutingValue):
    """硬运行限额门槛：Operational Limits Snapshot 的最低要求。

    声明了最低值的维度上，快照未申报该维度（未知）时 fail closed，
    绝不把未知当充足；低于最低值按硬门槛过滤。
    """

    min_available_rpm: int | None = Field(default=None, ge=0)
    min_available_tpm: int | None = Field(default=None, ge=0)
    min_available_concurrency: int | None = Field(default=None, ge=0)
    min_remaining_period_quota: Decimal | None = Field(
        default=None, ge=Decimal("0")
    )


class WorstCaseCostCap(_FrozenRoutingValue):
    """硬最坏情况运行成本上限（声明币种，不做隐式换算）。"""

    currency: str = Field(min_length=1)
    max_run_cost: Decimal = Field(ge=Decimal("0"))


class SoftRoutingPreferences(_FrozenRoutingValue):
    """soft evidence 偏好：异常快照产生显式降级 warning，不阻断。

    ADR 0041：硬可用性策略遇过期快照 fail closed，软偏好可带
    ``STALE_AVAILABILITY`` warning 继续。跟踪到的异常快照不进入
    evidence reference——两种路径都不把异常快照当作健康证据。
    """

    track_availability: bool = False
    track_operational_limits: bool = False


class RecommendationPublication(_FrozenRoutingValue):
    """显式发布记录：应用把 Eval Recommendation 的事实桥接为 Routing 输入。

    Runtime Companion 不自动采纳 Recommendation：发布是上层应用的
    显式决策。本类型只冻结被发布 Recommendation 的关键事实（目标
    Variant、gate 结论、置信度、有效期与证据引用），由
    :func:`~m_agent.companion.routing.publish_recommendation_as_policy`
    消费并产生新的 Routing Policy 版本；未发布的 Recommendation 对
    Router 完全不可见。
    """

    recommendation_id: str = Field(min_length=1)
    recommendation_version: str = Field(min_length=1)
    report_id: str = Field(min_length=1)
    report_revision: int = Field(ge=1)
    evidence_digest: str = Field(min_length=1)
    target_variant_id: str = Field(min_length=1)
    target_variant_version: str = Field(min_length=1)
    hard_gate_passed: bool
    quality_gate_passed: bool
    confidence: float = Field(ge=0.0, le=1.0)
    valid_until: datetime


class RoutingPolicy(_FrozenRoutingValue):
    """冻结的版本化 Run 前选择规则。

    相同的 Catalog、Policy 与 evidence snapshot 输入必须产生完全相同
    的 Routing Decision 与 reason trace；候选范围
    （``allowed_variants``，``None`` 表示 Catalog 全体）、排序维度或
    门槛变化时必须创建新的 Policy 版本，绝不覆盖既有版本。

    ``published_from`` 记录该版本由某个显式发布的 Recommendation
    派生（未发布的版本为 ``None``）；``soft_preferences`` 是 soft
    evidence 偏好：异常快照只产生显式降级 warning，不放宽规则。
    """

    identity: RoutingPolicyIdentity
    model_requirements: ModelRequirements = Field(
        default_factory=ModelRequirements
    )
    deployment: DeploymentConstraints = Field(
        default_factory=DeploymentConstraints
    )
    hard_gates: HardRoutingGates = Field(default_factory=HardRoutingGates)
    objectives: tuple[RoutingObjective, ...] = Field(default_factory=tuple)
    soft_preferences: SoftRoutingPreferences = Field(
        default_factory=SoftRoutingPreferences
    )
    #: 候选 allowlist：``(variant_id, version)`` 精确身份；
    #: ``None`` 表示不收窄候选范围；空元组是结构不完整的规则
    #: （Router 返回 ``INVALID_POLICY``）。
    allowed_variants: tuple[tuple[str, str], ...] | None = None
    published_from: RecommendationPublication | None = None

    def content_digest(self) -> str:
        """规则的规范化内容摘要（不含对象标识语义）。"""
        return canonical_digest(self.model_dump(mode="json"))
