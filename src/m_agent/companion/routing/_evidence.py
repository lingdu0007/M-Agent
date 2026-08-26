"""Router 消费的版本化只读快照输入（ADR 0041）。

Model Evidence、Pricing、Availability 与数据保留合规证据都是带来源、
版本与有效期的只读快照：Router 只消费它们，不发起隐藏模型探测，也不
实现其获取与更新机制（Ticket 20）。快照 subject 是精确的
``variant_id + variant_version`` 身份；同一 subject 的多条记录由
Router 确定性地选取最新一条。缺字段值保留为缺失，绝不伪造。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ._policy import canonical_digest


class _FrozenSnapshotValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelEvidenceSnapshot(_FrozenSnapshotValue):
    """一个 Variant 绑定 Contract 的经验证据（Eval/运行观测）。

    quality/stability 属于经验性质（Model Evidence），不是 Adapter
    自我声明的硬能力；``latency_ms_p50`` 缺失保持缺失。
    """

    variant_id: str = Field(min_length=1)
    variant_version: str = Field(min_length=1)
    quality_score: float = Field(ge=0.0, le=1.0)
    stability_score: float = Field(ge=0.0, le=1.0)
    latency_ms_p50: float | None = Field(default=None, ge=0.0)
    version: str = Field(min_length=1)
    source: str = Field(min_length=1)
    collected_at: datetime
    valid_until: datetime


class PricingSnapshot(_FrozenSnapshotValue):
    """版本化模型价格证据：来源、币种、生效时间与有效期。"""

    variant_id: str = Field(min_length=1)
    variant_version: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    input_price_per_mtok: Decimal = Field(ge=Decimal("0"))
    version: str = Field(min_length=1)
    source: str = Field(min_length=1)
    effective_at: datetime
    valid_until: datetime


class AvailabilitySnapshot(_FrozenSnapshotValue):
    """版本化 endpoint 状态证据：来源、采样时间与有效期。

    Router 只消费应用或只读探针提供的快照，不为一次路由隐藏发起
    模型请求；快照也不保证 dispatch 时仍可用。
    """

    variant_id: str = Field(min_length=1)
    variant_version: str = Field(min_length=1)
    available: bool
    version: str = Field(min_length=1)
    source: str = Field(min_length=1)
    sampled_at: datetime
    valid_until: datetime


class RetentionEvidenceSnapshot(_FrozenSnapshotValue):
    """数据保留声明合规性的外部版本化证据。

    只记录为 Variant 验证过的声明版本与证据有效期；合规结论只能
    引用该版本化证据，不进入 Catalog、Decision 或 Definition
    Snapshot 本身。
    """

    variant_id: str = Field(min_length=1)
    variant_version: str = Field(min_length=1)
    verified_retention_evidence_version: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source: str = Field(min_length=1)
    collected_at: datetime
    valid_until: datetime


class RoutingEvidence(_FrozenSnapshotValue):
    """一次路由请求的完整只读快照输入集合。"""

    model_evidence: tuple[ModelEvidenceSnapshot, ...] = Field(
        default_factory=tuple
    )
    pricing: tuple[PricingSnapshot, ...] = Field(default_factory=tuple)
    availability: tuple[AvailabilitySnapshot, ...] = Field(
        default_factory=tuple
    )
    retention: tuple[RetentionEvidenceSnapshot, ...] = Field(
        default_factory=tuple
    )

    def evidence_digest(self) -> str:
        """全部快照的规范化摘要（Decision 复现审计输入）。"""
        return canonical_digest(self.model_dump(mode="json"))

    def latest_model_evidence(
        self, variant_id: str, variant_version: str
    ) -> ModelEvidenceSnapshot | None:
        return _latest(
            self.model_evidence,
            variant_id,
            variant_version,
            key=lambda snapshot: (snapshot.collected_at, snapshot.version),
        )

    def latest_pricing(
        self, variant_id: str, variant_version: str
    ) -> PricingSnapshot | None:
        return _latest(
            self.pricing,
            variant_id,
            variant_version,
            key=lambda snapshot: (snapshot.effective_at, snapshot.version),
        )

    def latest_availability(
        self, variant_id: str, variant_version: str
    ) -> AvailabilitySnapshot | None:
        return _latest(
            self.availability,
            variant_id,
            variant_version,
            key=lambda snapshot: (snapshot.sampled_at, snapshot.version),
        )

    def latest_retention(
        self, variant_id: str, variant_version: str
    ) -> RetentionEvidenceSnapshot | None:
        return _latest(
            self.retention,
            variant_id,
            variant_version,
            key=lambda snapshot: (snapshot.collected_at, snapshot.version),
        )


def _latest(
    snapshots: tuple,  # noqa: ANN001 - internal generic helper
    variant_id: str,
    variant_version: str,
    *,
    key,  # noqa: ANN001 - deterministic ordering key
):
    """同一 subject 的多条快照按确定性顺序取最新一条。"""
    matching = [
        snapshot
        for snapshot in snapshots
        if snapshot.variant_id == variant_id
        and snapshot.variant_version == variant_version
    ]
    if not matching:
        return None
    return max(matching, key=key)
