"""版本化 Routing Policy 与 Deployment Constraints（ADR 0041）。

Routing Policy 是 Runtime Companion 中版本化的 Run 前选择规则：组合
Model Requirements（hard 能力/容量条件）、候选 allowlist、Deployment
Constraints 与 hard 价格/质量/稳定性门槛，以及声明了方向、缺失值策略
的字典序 soft 排序目标。它不使用隐式加权总分，不改变 Agent
Definition 的运行语义，也不包含凭据或敏感 endpoint 配置。
"""

from __future__ import annotations

from decimal import Decimal
import enum
import json
import hashlib

from pydantic import BaseModel, ConfigDict, Field, field_serializer

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
    - ``require_availability``：硬可用性要求（Availability Snapshot）。
    """

    max_input_price_per_mtok: Decimal | None = Field(
        default=None, ge=Decimal("0")
    )
    min_quality_score: float | None = Field(default=None, ge=0.0, le=1.0)
    min_stability_score: float | None = Field(default=None, ge=0.0, le=1.0)
    require_availability: bool = False


class RoutingPolicy(_FrozenRoutingValue):
    """冻结的版本化 Run 前选择规则。

    相同的 Catalog、Policy 与 evidence snapshot 输入必须产生完全相同
    的 Routing Decision 与 reason trace；候选范围
    （``allowed_variants``，``None`` 表示 Catalog 全体）、排序维度或
    门槛变化时必须创建新的 Policy 版本，绝不覆盖既有版本。
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
    #: 候选 allowlist：``(variant_id, version)`` 精确身份；
    #: ``None`` 表示不收窄候选范围；空元组是结构不完整的规则
    #: （Router 返回 ``INVALID_POLICY``）。
    allowed_variants: tuple[tuple[str, str], ...] | None = None

    def content_digest(self) -> str:
        """规则的规范化内容摘要（不含对象标识语义）。"""
        return canonical_digest(self.model_dump(mode="json"))
