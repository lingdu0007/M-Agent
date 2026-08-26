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
    DeploymentAttributes,
    HardRoutingGates,
    ModelCatalog,
    ModelCatalogEntry,
    ModelEvidenceSnapshot,
    PricingSnapshot,
    AvailabilitySnapshot,
    RetentionEvidenceSnapshot,
    RoutingEvidence,
    RoutingObjective,
    RoutingPolicy,
    RoutingPolicyIdentity,
    DeploymentConstraints,
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
        policy_identities=(policy_identity or POLICY_IDENTITY,),
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
) -> RoutingPolicy:
    return RoutingPolicy(
        identity=identity or POLICY_IDENTITY,
        model_requirements=requirements or ModelRequirements(),
        deployment=deployment or DeploymentConstraints(),
        hard_gates=hard_gates or HardRoutingGates(),
        objectives=objectives,
        allowed_variants=allowed_variants,
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
    version: str = "price-1",
    effective_at: datetime | None = None,
    valid_until: datetime | None = None,
) -> PricingSnapshot:
    return PricingSnapshot(
        variant_id=variant.variant_id,
        variant_version=variant.version,
        currency="USD",
        input_price_per_mtok=Decimal(input_price),
        version=version,
        source="offline-price-sheet",
        effective_at=effective_at or (AS_OF - timedelta(days=1)),
        valid_until=valid_until or (AS_OF + timedelta(days=7)),
    )


def make_availability(
    variant: AgentVariant,
    *,
    available: bool = True,
    version: str = "avail-1",
    valid_until: datetime | None = None,
) -> AvailabilitySnapshot:
    return AvailabilitySnapshot(
        variant_id=variant.variant_id,
        variant_version=variant.version,
        available=available,
        version=version,
        source="offline-probe",
        sampled_at=AS_OF - timedelta(hours=1),
        valid_until=valid_until or (AS_OF + timedelta(hours=6)),
    )


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


def make_evidence(
    variants: tuple[AgentVariant, ...],
    *,
    model_evidence: tuple[ModelEvidenceSnapshot, ...] | None = None,
    pricing: tuple[PricingSnapshot, ...] | None = None,
    availability: tuple[AvailabilitySnapshot, ...] | None = None,
    retention: tuple[RetentionEvidenceSnapshot, ...] | None = None,
) -> RoutingEvidence:
    """为一个或多个 variant 生成默认有效的完整 evidence bundle。"""

    def defaulted(
        provided, builder
    ) -> tuple:  # noqa: ANN001 - fixture helper for tuples
        if provided is not None:
            return tuple(provided)
        return tuple(builder(variant) for variant in variants)

    return RoutingEvidence(
        model_evidence=defaulted(model_evidence, make_model_evidence),
        pricing=defaulted(pricing, make_pricing),
        availability=defaulted(availability, make_availability),
        retention=defaulted(retention, make_retention_evidence),
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
