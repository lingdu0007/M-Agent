"""Router 消费的版本化只读快照输入（ADR 0041，Ticket 20）。

Model Evidence、Pricing、Availability、Operational Limits 与数据保留
合规证据都是带来源、版本与有效期的只读快照：Router 只消费它们，不发
起隐藏模型探测，也不实现其获取与更新机制。快照 subject 是精确的
``variant_id + variant_version`` 身份；同一 subject 的多条记录由
Router 确定性地选取最新一条。缺字段值保留为缺失，绝不伪造。

Ticket 20 起，Pricing、Availability 与 Operational Limits 三类快照
升级为完整证据契约：携带 provenance（``source``/``version``）、
collected/effective time、subject（variant 身份 + 观测时的
``contract_fingerprint``）、``schema_version`` 与内容寻址
``integrity_digest``。快照只描述实例的外部观测，不改写 Model
Contract 或 Agent Variant 语义；异常快照（过期、未生效、subject
不匹配、指纹漂移、integrity failure）由 :class:`SnapshotStatus`
统一分类，供 hard fail-closed 与 soft 降级路径共用，绝不被当作
健康证据。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._catalog import AgentVariant
from ._policy import canonical_digest


class _FrozenSnapshotValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


#: 三类快照的稳定 schema 身份（schema 演进 = 新 schema 版本，不覆盖）。
PRICING_SNAPSHOT_SCHEMA = "pricing-snapshot-v1"
AVAILABILITY_SNAPSHOT_SCHEMA = "availability-snapshot-v1"
OPERATIONAL_LIMITS_SNAPSHOT_SCHEMA = "operational-limits-snapshot-v1"


class SnapshotStatus(str, enum.Enum):
    """快照相对期望 subject 与评估时刻的稳定有效性分类。"""

    VALID = "VALID"
    STALE = "STALE"
    NOT_EFFECTIVE = "NOT_EFFECTIVE"
    SUBJECT_MISMATCH = "SUBJECT_MISMATCH"
    FINGERPRINT_DRIFT = "FINGERPRINT_DRIFT"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


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
    """版本化模型价格证据：来源、币种、生效时间与有效期。

    ``output_price_per_mtok`` 缺失保持缺失（不伪造输出价格）；
    ``contract_fingerprint`` 记录采集时观测到的 PRIMARY Contract
    指纹，用于消费侧漂移判定；``integrity_digest`` 是内容寻址
    摘要，加载时校验、消费时复算。
    """

    variant_id: str = Field(min_length=1)
    variant_version: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    input_price_per_mtok: Decimal = Field(ge=Decimal("0"))
    output_price_per_mtok: Decimal | None = Field(
        default=None, ge=Decimal("0")
    )
    version: str = Field(min_length=1)
    source: str = Field(min_length=1)
    effective_at: datetime
    valid_until: datetime
    schema_version: str = Field(
        default=PRICING_SNAPSHOT_SCHEMA, min_length=1
    )
    contract_fingerprint: str | None = None
    integrity_digest: str | None = None

    @model_validator(mode="after")
    def _verify_declared_integrity(self) -> "PricingSnapshot":
        return _verify_declared_integrity(self)


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
    schema_version: str = Field(
        default=AVAILABILITY_SNAPSHOT_SCHEMA, min_length=1
    )
    contract_fingerprint: str | None = None
    integrity_digest: str | None = None

    @model_validator(mode="after")
    def _verify_declared_integrity(self) -> "AvailabilitySnapshot":
        return _verify_declared_integrity(self)


class OperationalLimitsSnapshot(_FrozenSnapshotValue):
    """版本化运行限额证据：RPM、TPM、并发与周期配额余量。

    这些是动态外部观测，不属于 Model Limits，也不冻结为模型语义；
    Core 不实现其全局计数器。各维度缺失保持缺失（未申报即未知，
    绝不当作充足）。
    """

    variant_id: str = Field(min_length=1)
    variant_version: str = Field(min_length=1)
    available_rpm: int | None = Field(default=None, ge=0)
    available_tpm: int | None = Field(default=None, ge=0)
    available_concurrency: int | None = Field(default=None, ge=0)
    remaining_period_quota: Decimal | None = Field(
        default=None, ge=Decimal("0")
    )
    version: str = Field(min_length=1)
    source: str = Field(min_length=1)
    collected_at: datetime
    valid_until: datetime
    schema_version: str = Field(
        default=OPERATIONAL_LIMITS_SNAPSHOT_SCHEMA, min_length=1
    )
    contract_fingerprint: str | None = None
    integrity_digest: str | None = None

    @model_validator(mode="after")
    def _verify_declared_integrity(self) -> "OperationalLimitsSnapshot":
        return _verify_declared_integrity(self)


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


# -- integrity 摘要与判定 ----------------------------------------------------


def snapshot_integrity_digest(
    snapshot: (
        PricingSnapshot | AvailabilitySnapshot | OperationalLimitsSnapshot
    ),
) -> str:
    """对快照内容（不含摘要自身）计算稳定 sha256 摘要。"""
    payload = snapshot.model_dump(mode="json", exclude={"integrity_digest"})
    return canonical_digest(payload)


def with_integrity(
    snapshot: (
        PricingSnapshot | AvailabilitySnapshot | OperationalLimitsSnapshot
    ),
) -> (
    PricingSnapshot | AvailabilitySnapshot | OperationalLimitsSnapshot
):
    """返回携带计算摘要的快照副本（生产侧 seal 入口）。

    ``model_copy`` 不触发校验，因此 seal 后的快照与直接构造的
    带摘要快照语义一致；篡改（改内容不改摘要）会在加载校验或
    消费时复算中确定性检出。
    """
    return snapshot.model_copy(
        update={"integrity_digest": snapshot_integrity_digest(snapshot)}
    )


def _verify_declared_integrity(snapshot):  # noqa: ANN001, ANN202
    """加载时校验已声明的摘要：声明了但不匹配即确定性失败。"""
    if snapshot.integrity_digest is None:
        return snapshot
    expected = snapshot_integrity_digest(snapshot)
    if snapshot.integrity_digest != expected:
        raise ValueError(
            "snapshot integrity digest does not match its content"
        )
    return snapshot


def _evaluate_snapshot(
    snapshot,  # noqa: ANN001 - shared helper for the three sealed kinds
    *,
    expected_variant: AgentVariant,
    as_of: datetime,
    effective_at: datetime | None,
) -> SnapshotStatus:
    """三类 sealed 快照共享的有效性判定（确定性、无 IO）。"""
    if (
        snapshot.variant_id != expected_variant.variant_id
        or snapshot.variant_version != expected_variant.version
    ):
        return SnapshotStatus.SUBJECT_MISMATCH
    expected_fingerprint = expected_variant.primary_contract().fingerprint
    if (
        snapshot.contract_fingerprint is not None
        and expected_fingerprint is not None
        and snapshot.contract_fingerprint != expected_fingerprint
    ):
        return SnapshotStatus.FINGERPRINT_DRIFT
    if (
        snapshot.integrity_digest is None
        or snapshot.integrity_digest != snapshot_integrity_digest(snapshot)
    ):
        return SnapshotStatus.INTEGRITY_FAILURE
    if effective_at is not None and effective_at > as_of:
        return SnapshotStatus.NOT_EFFECTIVE
    if snapshot.valid_until < as_of:
        return SnapshotStatus.STALE
    return SnapshotStatus.VALID


def evaluate_pricing_snapshot(
    snapshot: PricingSnapshot,
    *,
    expected_variant: AgentVariant,
    as_of: datetime,
) -> SnapshotStatus:
    """判定一个 Pricing 快照对期望 Variant 在 ``as_of`` 时刻的有效性。"""
    return _evaluate_snapshot(
        snapshot,
        expected_variant=expected_variant,
        as_of=as_of,
        effective_at=snapshot.effective_at,
    )


def evaluate_availability_snapshot(
    snapshot: AvailabilitySnapshot,
    *,
    expected_variant: AgentVariant,
    as_of: datetime,
) -> SnapshotStatus:
    """判定一个 Availability 快照对期望 Variant 的有效性。"""
    return _evaluate_snapshot(
        snapshot,
        expected_variant=expected_variant,
        as_of=as_of,
        effective_at=None,
    )


def evaluate_operational_limits_snapshot(
    snapshot: OperationalLimitsSnapshot,
    *,
    expected_variant: AgentVariant,
    as_of: datetime,
) -> SnapshotStatus:
    """判定一个 Operational Limits 快照对期望 Variant 的有效性。"""
    return _evaluate_snapshot(
        snapshot,
        expected_variant=expected_variant,
        as_of=as_of,
        effective_at=None,
    )


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
    operational_limits: tuple[OperationalLimitsSnapshot, ...] = Field(
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

    def latest_operational_limits(
        self, variant_id: str, variant_version: str
    ) -> OperationalLimitsSnapshot | None:
        return _latest(
            self.operational_limits,
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
