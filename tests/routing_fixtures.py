"""Shared offline fixtures for Ticket 19 deterministic routing tests.

这些 helper 只使用公共契约（``m_agent.runtime`` 与
``m_agent.companion.routing``）构造两个以上离线 Agent Variant 及其
Catalog、Routing Policy 与只读 evidence snapshot，供契约测试、参考比较
与零副作用集成测试共享。所有构造都是确定性的：无网络、无随机、
无墙钟。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from m_agent.runtime import (
    ModelBinding,
    ModelBindingSet,
    ModelCapabilities,
    ModelCapabilityCombination,
    ModelContract,
    ModelExecutionBudget,
    ModelLimits,
    ModelPurpose,
    ModelRequirements,
    RevisionStability,
    StreamingMode,
    StructuredOutputMode,
    ToolCallingMode,
)
from m_agent.companion.routing import (
    AgentVariant,
    AvailabilitySnapshot,
    DeploymentAttributes,
    HardRoutingGates,
    SoftRoutingPreferences,
    ModelCatalog,
    ModelCatalogEntry,
    ModelEvidenceSnapshot,
    OperationalLimitsSnapshot,
    PricingSnapshot,
    RetentionEvidenceSnapshot,
    RoutingEvidence,
    RoutingObjective,
    RoutingPolicy,
    RoutingPolicyIdentity,
    DeploymentConstraints,
    with_integrity,
)
from m_agent.companion.routing import (
    ObjectiveDimension,
    ObjectiveDirection,
    MissingValuePolicy,
)

#: 固定的评估时刻：所有 snapshot 的 collected/sampled/effective 时间都在
#: 它之前、valid_until 都在它之后（除非测试故意构造 stale 负例）。
AS_OF = datetime(2026, 8, 26, 12, 0, 0)

POLICY_IDENTITY = RoutingPolicyIdentity(policy_id="finance-default", version="3")


# 哨兵：缺省时自动绑定 variant 的 PRIMARY Contract 指纹；
# 显式传 None 表示不绑定指纹，传字符串表示覆盖。
_UNBOUND_FINGERPRINT: Any = object()


def _fingerprint(
    variant: AgentVariant, declared: Any
) -> str | None:
    if declared is _UNBOUND_FINGERPRINT:
        return variant.primary_contract().fingerprint
    return declared


def make_contract(
    contract_id: str,
    *,
    version: str = "1",
    context_window: int = 128_000,
    max_output: int = 8_000,
    capabilities: ModelCapabilities | None = None,
    model_identity: str | None = None,
) -> ModelContract:
    """构造一个非敏感的离线 Model Contract。"""
    return ModelContract(
        contract_id=contract_id,
        version=version,
        revision_stability=RevisionStability.PINNED,
        model_identity=model_identity or f"offline:{contract_id}",
        capabilities=(capabilities or ModelCapabilities()).as_typed(),
        limits=ModelLimits(
            context_window_tokens=context_window,
            max_output_tokens=max_output,
        ),
        input_sizer_id=f"sizer-{contract_id}-v1",
        serialization_id=f"serial-{contract_id}-v1",
    )


def basic_capabilities() -> ModelCapabilities:
    return ModelCapabilities().as_typed()


def tool_and_structured_capabilities() -> ModelCapabilities:
    """tool calling + strict structured output 的并发组合。"""
    combination = ModelCapabilityCombination(
        tool_calling=ToolCallingMode.NATIVE,
        structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
    )
    return ModelCapabilities(
        tool_calling=ToolCallingMode.NATIVE,
        structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
        supported_combinations=(combination,),
    )


def non_concurrent_capabilities() -> ModelCapabilities:
    """声明了 streaming 与 tool calling，但并发组合不可用。"""
    return ModelCapabilities(
        streaming=StreamingMode.DELTA,
        tool_calling=ToolCallingMode.NATIVE,
        supported_combinations=(
            ModelCapabilityCombination(streaming=StreamingMode.DELTA),
            ModelCapabilityCombination(tool_calling=ToolCallingMode.NATIVE),
        ),
    )


def make_binding_set(
    contract: ModelContract,
    requirements: ModelRequirements | None = None,
) -> ModelBindingSet:
    """PRIMARY 显式复用到一个完整、显式的 Binding Set。"""
    primary = ModelBinding(
        purpose=ModelPurpose.PRIMARY,
        contract=contract,
        requirements=requirements or ModelRequirements(),
    )
    return ModelBindingSet(
        bindings=(
            primary,
            primary.model_copy(
                update={
                    "purpose": ModelPurpose.CONTEXT_COMPRESSION,
                    "source_purpose": ModelPurpose.PRIMARY,
                }
            ),
            primary.model_copy(
                update={
                    "purpose": ModelPurpose.OUTPUT_REPAIR,
                    "source_purpose": ModelPurpose.PRIMARY,
                }
            ),
        )
    )


def make_variant(
    variant_id: str,
    contract: ModelContract,
    *,
    version: str = "1",
    definition_id: str | None = None,
    policy_identity: RoutingPolicyIdentity | None = None,
    policy_identities: tuple[RoutingPolicyIdentity, ...] | None = None,
    requirements: ModelRequirements | None = None,
    budget: ModelExecutionBudget | None = None,
) -> AgentVariant:
    """冻结一个完整 Agent Variant：Definition 身份 + Binding Set + 策略身份。"""
    return AgentVariant(
        variant_id=variant_id,
        version=version,
        definition_id=definition_id or f"definition-{variant_id}",
        definition_version="1",
        model_bindings=make_binding_set(contract, requirements),
        model_execution_budget=budget or ModelExecutionBudget(
            run_max_attempts=4,
            primary_max_attempts=2,
            context_compression_max_attempts=1,
            output_repair_max_attempts=1,
        ),
        policy_identities=(
            policy_identities
            if policy_identities is not None
            else (policy_identity or POLICY_IDENTITY,)
        ),
    )


def make_entry(
    variant: AgentVariant,
    *,
    provider: str | None = "offline-provider",
    region: str | None = "cn-north",
    endpoint_class: str | None = "standard",
    retention: str | None = "retention-2026-01",
) -> ModelCatalogEntry:
    return ModelCatalogEntry(
        variant=variant,
        deployment=DeploymentAttributes(
            provider=provider,
            region=region,
            endpoint_class=endpoint_class,
            retention_evidence_version=retention,
        ),
    )


def make_catalog(*entries: ModelCatalogEntry) -> ModelCatalog:
    return ModelCatalog(
        catalog_id="finance-catalog",
        version="7",
        entries=tuple(entries),
    )


def make_policy(
    *,
    identity: RoutingPolicyIdentity | None = None,
    requirements: ModelRequirements | None = None,
    deployment: DeploymentConstraints | None = None,
    hard_gates: HardRoutingGates | None = None,
    objectives: tuple[RoutingObjective, ...] = (),
    allowed_variants: tuple[tuple[str, str], ...] | None = None,
    soft_preferences: SoftRoutingPreferences | None = None,
) -> RoutingPolicy:
    return RoutingPolicy(
        identity=identity or POLICY_IDENTITY,
        model_requirements=requirements or ModelRequirements(),
        deployment=deployment or DeploymentConstraints(),
        hard_gates=hard_gates or HardRoutingGates(),
        objectives=objectives,
        allowed_variants=allowed_variants,
        soft_preferences=soft_preferences or SoftRoutingPreferences(),
    )


def make_model_evidence(
    variant: AgentVariant,
    *,
    quality: float = 0.9,
    stability: float = 0.97,
    latency_ms: float | None = 800.0,
    version: str = "ev-1",
    valid_until: datetime | None = None,
) -> ModelEvidenceSnapshot:
    return ModelEvidenceSnapshot(
        variant_id=variant.variant_id,
        variant_version=variant.version,
        quality_score=quality,
        stability_score=stability,
        latency_ms_p50=latency_ms,
        version=version,
        source="offline-eval-report",
        collected_at=AS_OF - timedelta(days=1),
        valid_until=valid_until or (AS_OF + timedelta(days=7)),
    )


def make_pricing(
    variant: AgentVariant,
    *,
    input_price: str = "3.50",
    output_price: str | None = "12.00",
    currency: str = "USD",
    version: str = "price-1",
    effective_at: datetime | None = None,
    valid_until: datetime | None = None,
    contract_fingerprint: Any = _UNBOUND_FINGERPRINT,
    sealed: bool = True,
) -> PricingSnapshot:
    """默认携带输出价格、绑定观测指纹并 seal 的价格快照。"""
    snapshot = PricingSnapshot(
        variant_id=variant.variant_id,
        variant_version=variant.version,
        currency=currency,
        input_price_per_mtok=Decimal(input_price),
        output_price_per_mtok=(
            None if output_price is None else Decimal(output_price)
        ),
        version=version,
        source="offline-price-sheet",
        effective_at=effective_at or (AS_OF - timedelta(days=1)),
        valid_until=valid_until or (AS_OF + timedelta(days=7)),
        contract_fingerprint=_fingerprint(variant, contract_fingerprint),
    )
    if not sealed:
        return snapshot
    return with_integrity(snapshot)  # type: ignore[return-value]


def make_availability(
    variant: AgentVariant,
    *,
    available: bool = True,
    version: str = "avail-1",
    valid_until: datetime | None = None,
    contract_fingerprint: Any = _UNBOUND_FINGERPRINT,
    sealed: bool = True,
) -> AvailabilitySnapshot:
    """默认绑定观测指纹并 seal 的可用性快照。"""
    snapshot = AvailabilitySnapshot(
        variant_id=variant.variant_id,
        variant_version=variant.version,
        available=available,
        version=version,
        source="offline-probe",
        sampled_at=AS_OF - timedelta(hours=1),
        valid_until=valid_until or (AS_OF + timedelta(hours=6)),
        contract_fingerprint=_fingerprint(variant, contract_fingerprint),
    )
    if not sealed:
        return snapshot
    return with_integrity(snapshot)  # type: ignore[return-value]


def make_retention_evidence(
    variant: AgentVariant,
    *,
    verified_version: str = "retention-2026-01",
    version: str = "ret-ev-1",
    valid_until: datetime | None = None,
) -> RetentionEvidenceSnapshot:
    return RetentionEvidenceSnapshot(
        variant_id=variant.variant_id,
        variant_version=variant.version,
        verified_retention_evidence_version=verified_version,
        version=version,
        source="offline-compliance-office",
        collected_at=AS_OF - timedelta(days=2),
        valid_until=valid_until or (AS_OF + timedelta(days=30)),
    )


def make_operational_limits(
    variant: AgentVariant,
    *,
    rpm: int | None = 600,
    tpm: int | None = 120_000,
    concurrency: int | None = 8,
    remaining_quota: str | None = "0.75",
    version: str = "ops-1",
    valid_until: datetime | None = None,
    contract_fingerprint: Any = _UNBOUND_FINGERPRINT,
    sealed: bool = True,
) -> OperationalLimitsSnapshot:
    """默认绑定观测指纹并 seal 的运行限额快照。"""
    snapshot = OperationalLimitsSnapshot(
        variant_id=variant.variant_id,
        variant_version=variant.version,
        available_rpm=rpm,
        available_tpm=tpm,
        available_concurrency=concurrency,
        remaining_period_quota=(
            None if remaining_quota is None else Decimal(remaining_quota)
        ),
        version=version,
        source="offline-quota-probe",
        collected_at=AS_OF - timedelta(minutes=30),
        valid_until=valid_until or (AS_OF + timedelta(hours=2)),
        contract_fingerprint=_fingerprint(variant, contract_fingerprint),
    )
    if not sealed:
        return snapshot
    return with_integrity(snapshot)  # type: ignore[return-value]


def make_evidence(
    variants: tuple[AgentVariant, ...],
    *,
    model_evidence: tuple[ModelEvidenceSnapshot, ...] | None = None,
    pricing: tuple[PricingSnapshot, ...] | None = None,
    availability: tuple[AvailabilitySnapshot, ...] | None = None,
    retention: tuple[RetentionEvidenceSnapshot, ...] | None = None,
    operational_limits: tuple[OperationalLimitsSnapshot, ...] | None = None,
) -> RoutingEvidence:
    """为一个或多个 variant 生成默认有效的完整 evidence bundle。"""

    def defaulted(
        provided, builder
    ) -> tuple:  # noqa: ANN001 - fixture helper for tuples
        if provided is not None:
            return tuple(provided)
        return tuple(builder(variant) for variant in variants)

    # model_construct：跳过元素级重新校验，使 fixture 能携带消费侧
    # 才会检出的篡改快照（模拟从外部存储收到已损坏的 evidence bundle）。
    return RoutingEvidence.model_construct(
        model_evidence=defaulted(model_evidence, make_model_evidence),
        pricing=defaulted(pricing, make_pricing),
        availability=defaulted(availability, make_availability),
        retention=defaulted(retention, make_retention_evidence),
        operational_limits=defaulted(
            operational_limits, make_operational_limits
        ),
    )


def cost_objective(
    direction: ObjectiveDirection = ObjectiveDirection.MINIMIZE,
    missing: MissingValuePolicy = MissingValuePolicy.ORDER_LAST,
) -> RoutingObjective:
    return RoutingObjective(
        dimension=ObjectiveDimension.COST, direction=direction, missing_value=missing
    )


def quality_objective(
    direction: ObjectiveDirection = ObjectiveDirection.MAXIMIZE,
    missing: MissingValuePolicy = MissingValuePolicy.ORDER_LAST,
) -> RoutingObjective:
    return RoutingObjective(
        dimension=ObjectiveDimension.QUALITY, direction=direction, missing_value=missing
    )
