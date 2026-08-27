"""Offline model-routing scenario (ADR 0041 / ADR 0042, Ticket 20).

`model-routing` Scenario 用纯函数 Router 与公开 routing 契约完整证明
versioned evidence routing：typed capability 与 Contract Limits 过滤、
Operational Limits 门槛（floor、unknown fail-closed、stale 软降级、
integrity fail-closed）、声明式 usage/cost 估算（上界语义 + 证据缺口，
绝不伪造精确费用）、Deployment Constraints 硬匹配、六种已决议
Routing Outcome、pre-Run Fallback（有限次数 + 可检查原因）、失败与
成功的零副作用（输入 snapshot 摘要不变）、Routing Store 的 immutable
Decision（幂等重放 + 内容冲突 fail + 重开不重算）、显式 Replacement
Run（Run 未终止绝不换模）与 Eval Recommendation 的显式发布（未发布
身份对 Router 不可见）。

受控变异由 :func:`reconcile_model_routing` 检出：任一观察布尔被改写
都必须产生非空问题列表，否则 Harness 为 ERROR。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from ..companion.routing import (
    FALLBACK_EXHAUSTED,
    FALLBACK_SELECTED,
    CostFormula,
    DeploymentAttributes,
    DeploymentConstraints,
    FallbackSequence,
    HardRoutingGates,
    MissingValuePolicy,
    ModelCatalog,
    ModelCatalogEntry,
    ModelRouter,
    ObjectiveDimension,
    ObjectiveDirection,
    OperationalLimitsGate,
    PublicationError,
    REPLACEMENT_REASON_PROVIDER_FAILURE,
    RecommendationPublication,
    RoutingDecisionConflictError,
    RoutingEvidence,
    RoutingObjective,
    RoutingOutcome,
    RoutingPolicy,
    RoutingPolicyIdentity,
    RoutingReplacementError,
    RunCostPolicy,
    SoftRoutingPreferences,
    SQLiteRoutingStore,
    UsageObservation,
    WARNING_STALE_OPERATIONAL_LIMITS,
    bind_decision_to_run,
    estimate_run_cost,
    execute_pre_run_fallback,
    publish_recommendation_as_policy,
    register_replacement_run,
    register_variant_for_policy,
    with_integrity,
)
from ..companion.routing import (
    REASON_AMBIGUOUS_OBJECTIVES,
    REASON_DUPLICATE_VARIANT_IDENTITY,
    REASON_ENDPOINT_CLASS_NOT_ALLOWED,
    REASON_PROVIDER_NOT_ALLOWED,
    REASON_PROVIDER_UNKNOWN,
    REASON_REGION_NOT_ALLOWED,
    REASON_RETENTION_EVIDENCE_VERSION_NOT_ALLOWED,
    REASON_HARD_OPERATIONAL_LIMIT_GATE_FAILED,
    REASON_NOT_REGISTERED_FOR_POLICY,
    REASON_OPERATIONAL_LIMITS_SNAPSHOT_INTEGRITY_FAILURE,
    REASON_OPERATIONAL_LIMITS_SNAPSHOT_MISSING,
    REASON_OPERATIONAL_LIMITS_UNKNOWN,
)
from ..runtime import (
    DefinitionSnapshot,
    ModelBinding,
    ModelBindingSet,
    ModelCapabilities,
    ModelCapabilityCombination,
    ModelContract,
    ModelExecutionBudget,
    ModelLimits,
    ModelPurpose,
    ModelRequirementReason,
    ModelRequirements,
    RevisionStability,
    RunRecord,
    RunStatus,
    StructuredOutputMode,
    ToolCallingMode,
    UsageProvenance,
)
from ._pack import AcceptanceCheckResult, AcceptanceCheckStatus, EvidenceLevel

_AS_OF = datetime(2026, 8, 26, 12, 0, 0)
_PRIMARY_IDENTITY = RoutingPolicyIdentity(
    policy_id="scenario-finance", version="1"
)
_SECONDARY_IDENTITY = RoutingPolicyIdentity(
    policy_id="scenario-finance", version="2"
)
_PUBLISHED_IDENTITY = RoutingPolicyIdentity(
    policy_id="scenario-finance", version="9"
)
_UNPUBLISHED_IDENTITY = RoutingPolicyIdentity(
    policy_id="scenario-finance", version="8"
)


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


# -- 自包含构建块：只用公开契约，无网络、无随机、无墙钟 -------------------


def _contract(
    contract_id: str,
    *,
    context_window: int = 128_000,
    max_output: int = 8_000,
    capabilities: ModelCapabilities | None = None,
) -> ModelContract:
    return ModelContract(
        contract_id=contract_id,
        version="1",
        revision_stability=RevisionStability.PINNED,
        model_identity=f"offline:{contract_id}",
        capabilities=(capabilities or ModelCapabilities()).as_typed(),
        limits=ModelLimits(
            context_window_tokens=context_window,
            max_output_tokens=max_output,
        ),
        input_sizer_id=f"sizer-{contract_id}-v1",
        serialization_id=f"serial-{contract_id}-v1",
    )


def _variant(
    variant_id: str,
    contract: ModelContract,
    *,
    policy_identities: tuple[RoutingPolicyIdentity, ...] | None = None,
    requirements: ModelRequirements | None = None,
    budget: ModelExecutionBudget | None = None,
):
    primary = ModelBinding(
        purpose=ModelPurpose.PRIMARY,
        contract=contract,
        requirements=requirements or ModelRequirements(),
    )
    return _catalog_variant(
        variant_id,
        ModelBindingSet(
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
        ),
        policy_identities=policy_identities,
        budget=budget,
    )


def _catalog_variant(
    variant_id: str,
    bindings: ModelBindingSet,
    *,
    policy_identities: tuple[RoutingPolicyIdentity, ...] | None = None,
    budget: ModelExecutionBudget | None = None,
):
    from ..companion.routing import AgentVariant

    return AgentVariant(
        variant_id=variant_id,
        version="1",
        definition_id=f"definition-{variant_id}",
        definition_version="1",
        model_bindings=bindings,
        model_execution_budget=budget
        or ModelExecutionBudget(
            run_max_attempts=4,
            primary_max_attempts=2,
            context_compression_max_attempts=1,
            output_repair_max_attempts=1,
        ),
        policy_identities=policy_identities or (_PRIMARY_IDENTITY,),
    )


def _entry(
    variant,
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


def _catalog(*entries: ModelCatalogEntry) -> ModelCatalog:
    return ModelCatalog(
        catalog_id="scenario-catalog",
        version="3",
        entries=tuple(entries),
    )


def _policy(
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
        identity=identity or _PRIMARY_IDENTITY,
        model_requirements=requirements or ModelRequirements(),
        deployment=deployment or DeploymentConstraints(),
        hard_gates=hard_gates or HardRoutingGates(),
        objectives=objectives,
        allowed_variants=allowed_variants,
        soft_preferences=soft_preferences or SoftRoutingPreferences(),
    )


def _pricing(
    variant,
    *,
    input_price: str = "3.50",
    output_price: str = "12.00",
    currency: str = "USD",
):
    from ..companion.routing import PricingSnapshot

    return with_integrity(
        PricingSnapshot(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            currency=currency,
            input_price_per_mtok=Decimal(input_price),
            output_price_per_mtok=Decimal(output_price),
            version="price-1",
            source="offline-price-sheet",
            effective_at=_AS_OF - timedelta(days=1),
            valid_until=_AS_OF + timedelta(days=7),
            contract_fingerprint=variant.primary_contract().fingerprint,
        )
    )


def _availability(variant, *, available: bool = True):
    from ..companion.routing import AvailabilitySnapshot

    return with_integrity(
        AvailabilitySnapshot(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            available=available,
            version="avail-1",
            source="offline-probe",
            sampled_at=_AS_OF - timedelta(hours=1),
            valid_until=_AS_OF + timedelta(hours=6),
            contract_fingerprint=variant.primary_contract().fingerprint,
        )
    )


def _model_evidence(variant, *, quality: float = 0.9, stability: float = 0.97):
    from ..companion.routing import ModelEvidenceSnapshot

    return ModelEvidenceSnapshot(
        variant_id=variant.variant_id,
        variant_version=variant.version,
        quality_score=quality,
        stability_score=stability,
        latency_ms_p50=800.0,
        version="ev-1",
        source="offline-eval-report",
        collected_at=_AS_OF - timedelta(days=1),
        valid_until=_AS_OF + timedelta(days=7),
    )


def _retention(variant, *, verified: str = "retention-2026-01"):
    from ..companion.routing import RetentionEvidenceSnapshot

    return RetentionEvidenceSnapshot(
        variant_id=variant.variant_id,
        variant_version=variant.version,
        verified_retention_evidence_version=verified,
        version="ret-ev-1",
        source="offline-compliance-office",
        collected_at=_AS_OF - timedelta(days=2),
        valid_until=_AS_OF + timedelta(days=30),
    )


def _ops(
    variant,
    *,
    rpm: int | None = 600,
    tpm: int | None = 120_000,
    concurrency: int | None = 8,
    quota: str | None = "0.75",
    valid_until: datetime | None = None,
):
    from ..companion.routing import OperationalLimitsSnapshot

    return with_integrity(
        OperationalLimitsSnapshot(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            available_rpm=rpm,
            available_tpm=tpm,
            available_concurrency=concurrency,
            remaining_period_quota=(
                None if quota is None else Decimal(quota)
            ),
            version="ops-1",
            source="offline-quota-probe",
            collected_at=_AS_OF - timedelta(minutes=30),
            valid_until=valid_until or (_AS_OF + timedelta(hours=2)),
            contract_fingerprint=variant.primary_contract().fingerprint,
        )
    )


def _evidence(
    variants,
    *,
    pricing: tuple | None = None,
    availability: tuple | None = None,
    operational_limits: tuple | None = None,
) -> RoutingEvidence:
    """默认构造完整有效的 evidence bundle。

    ``model_construct`` 跳过元素级重校验，使探针可携带消费侧才检出的
    篡改快照（integrity failure）。
    """

    def defaulted(provided, builder):  # noqa: ANN001, ANN202
        if provided is not None:
            return tuple(provided)
        return tuple(builder(variant) for variant in variants)

    return RoutingEvidence.model_construct(
        model_evidence=defaulted(None, _model_evidence) if variants else (),
        pricing=defaulted(pricing, _pricing),
        availability=defaulted(availability, _availability),
        retention=defaulted(None, _retention) if variants else (),
        operational_limits=defaulted(operational_limits, _ops),
    )


def _select(catalog, policy, evidence):
    return ModelRouter().select(
        catalog=catalog,
        policy=policy,
        evidence=evidence,
        as_of=_AS_OF,
    )


def _run(variant, run_id: str, status: RunStatus) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        definition_id=variant.definition_id,
        definition_version=variant.definition_version,
        input="route me",
        status=status,
        snapshot=DefinitionSnapshot(
            definition_id=variant.definition_id,
            version=variant.definition_version,
            instructions="offline routing scenario agent",
            model_bindings=variant.model_bindings,
        ),
    )


_TOOLING = ModelCapabilities(
    tool_calling=ToolCallingMode.NATIVE,
    structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
    supported_combinations=(
        ModelCapabilityCombination(
            tool_calling=ToolCallingMode.NATIVE,
            structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
        ),
    ),
)

_QUALITY_OBJECTIVE = RoutingObjective(
    dimension=ObjectiveDimension.QUALITY,
    direction=ObjectiveDirection.MAXIMIZE,
    missing_value=MissingValuePolicy.ORDER_LAST,
)


# -- 探针：每个探针返回带 problems 列表的观察 ----------------------------


def _typed_capability_probe() -> dict:
    """typed capability 与 Contract Limits 的确定性过滤。"""
    problems: list[str] = []
    capable = _variant(
        "variant-capable", _contract("contract-capable", capabilities=_TOOLING)
    )
    basic = _variant("variant-basic", _contract("contract-basic"))
    catalog = _catalog(_entry(capable), _entry(basic))
    evidence = _evidence((capable, basic))

    result = _select(
        catalog, _policy(requirements=ModelRequirements(capabilities=_TOOLING)), evidence
    )
    if (
        result.outcome is not RoutingOutcome.SELECTED
        or result.decision is None
        or result.decision.selected_variant.variant_id != "variant-capable"
    ):
        problems.append("typed_capability_selection_failed")
    by_variant = {
        evaluation.variant_id: evaluation
        for evaluation in result.candidate_evaluations
    }
    basic_eval = by_variant.get("variant-basic")
    if (
        basic_eval is None
        or basic_eval.passed
        or basic_eval.reason_code
        != ModelRequirementReason.TOOL_CALLING_UNSUPPORTED.value
    ):
        problems.append("typed_capability_filter_reason_missing")

    # Contract Limits（上下文窗口）同属 typed 契约表面：窗口不足即过滤。
    limits_result = _select(
        catalog,
        _policy(
            requirements=ModelRequirements(min_context_window_tokens=256_000)
        ),
        evidence,
    )
    if limits_result.outcome is not RoutingOutcome.NO_COMPATIBLE_VARIANT:
        problems.append("typed_contract_limits_not_enforced")
    limits_reasons = {
        evaluation.reason_code
        for evaluation in limits_result.candidate_evaluations
    }
    if ModelRequirementReason.CONTEXT_WINDOW_TOO_SMALL.value not in limits_reasons:
        problems.append("typed_contract_limits_reason_missing")
    return {"problems": problems}


def _operational_limits_probe() -> dict:
    """Operational Limits 门槛：floor、unknown/missing fail-closed、
    stale 软降级、integrity failure fail-closed。"""
    problems: list[str] = []
    strong = _variant("variant-strong", _contract("contract-strong"))
    weak = _variant("variant-weak", _contract("contract-weak"))
    catalog = _catalog(_entry(strong), _entry(weak))
    gate = HardRoutingGates(
        operational_limits=OperationalLimitsGate(min_available_rpm=300)
    )
    hard = _policy(hard_gates=gate)

    low = _select(
        catalog,
        hard,
        _evidence(
            (strong, weak),
            operational_limits=(
                _ops(strong, rpm=100),
                _ops(weak, rpm=120),
            ),
        ),
    )
    if low.outcome is not RoutingOutcome.POLICY_UNSATISFIED:
        problems.append("limits_floor_not_enforced")
    if not low.candidate_evaluations or any(
        evaluation.reason_code != REASON_HARD_OPERATIONAL_LIMIT_GATE_FAILED
        for evaluation in low.candidate_evaluations
    ):
        problems.append("limits_floor_reason_not_inspectable")

    unknown = _select(
        catalog,
        hard,
        _evidence(
            (strong, weak),
            operational_limits=(
                _ops(strong, rpm=None),
                _ops(weak, rpm=None),
            ),
        ),
    )
    if (
        unknown.outcome is not RoutingOutcome.EVIDENCE_UNAVAILABLE
        or unknown.reason_code != REASON_OPERATIONAL_LIMITS_UNKNOWN
    ):
        problems.append("limits_unknown_not_fail_closed")

    missing = _select(
        catalog, hard, _evidence((strong, weak), operational_limits=())
    )
    if (
        missing.outcome is not RoutingOutcome.EVIDENCE_UNAVAILABLE
        or missing.reason_code != REASON_OPERATIONAL_LIMITS_SNAPSHOT_MISSING
    ):
        problems.append("limits_missing_not_fail_closed")

    healthy = _select(catalog, hard, _evidence((strong, weak)))
    if healthy.outcome is not RoutingOutcome.SELECTED:
        problems.append("limits_healthy_rejected")

    # soft 偏好：无硬限额门槛时 stale 快照只产生降级 warning，不阻断。
    stale = _select(
        catalog,
        _policy(
            soft_preferences=SoftRoutingPreferences(
                track_operational_limits=True
            )
        ),
        _evidence(
            (strong, weak),
            operational_limits=(
                _ops(strong, valid_until=_AS_OF - timedelta(hours=1)),
                _ops(weak, valid_until=_AS_OF - timedelta(hours=1)),
            ),
        ),
    )
    if stale.outcome is not RoutingOutcome.SELECTED:
        problems.append("stale_limits_soft_degradation_blocked")
    if WARNING_STALE_OPERATIONAL_LIMITS not in {
        warning.code for warning in stale.warnings
    }:
        problems.append("stale_limits_soft_warning_missing")

    tampered = _ops(strong, rpm=600).model_copy(update={"available_rpm": 5})
    integrity = _select(
        catalog,
        hard,
        _evidence(
            (strong, weak),
            operational_limits=(tampered, _ops(weak, rpm=1_200)),
        ),
    )
    if (
        integrity.outcome is not RoutingOutcome.EVIDENCE_UNAVAILABLE
        or integrity.reason_code
        != REASON_OPERATIONAL_LIMITS_SNAPSHOT_INTEGRITY_FAILURE
    ):
        problems.append("limits_integrity_not_fail_closed")
    return {"problems": problems}


def _usage_cost_probe() -> dict:
    """声明式 usage/cost：上界语义、证据缺口、绝不伪造精确费用。"""
    problems: list[str] = []
    variant = _variant("variant-cost", _contract("contract-cost"))
    evidence = _evidence((variant,))

    worst = RunCostPolicy(
        policy_id="scenario-cost",
        version="1",
        currency="USD",
        formula=CostFormula.WORST_CASE_TOKEN_BUDGET,
        usage_provenance=UsageProvenance.RUNTIME_SIZED,
    )
    sized = estimate_run_cost(
        policy=worst,
        variant=variant,
        evidence=evidence,
        as_of=_AS_OF,
        sized_input_tokens=50_000,
    )
    expected_worst = (
        Decimal(58_000) / Decimal(1_000_000) * Decimal("3.50")
        + Decimal(8_000) / Decimal(1_000_000) * Decimal("12.00")
    ) * Decimal(4)
    if (
        sized.upper_bound != expected_worst
        or sized.lower_bound is not None
        or not sized.worst_case_upper_bound
    ):
        problems.append("worst_case_budget_math_wrong")

    unsized = estimate_run_cost(
        policy=worst, variant=variant, evidence=evidence, as_of=_AS_OF
    )
    if "INPUT_SIZE_UNAVAILABLE" not in unsized.evidence_gaps or (
        unsized.upper_bound is None
    ):
        problems.append("input_size_fallback_gap_missing")

    reported = RunCostPolicy(
        policy_id="scenario-cost-usage",
        version="1",
        currency="USD",
        formula=CostFormula.REPORTED_USAGE,
        usage_provenance=UsageProvenance.PROVIDER_REPORTED,
    )
    observation = UsageObservation(
        input_tokens=50_000,
        output_tokens=8_000,
        provenance=UsageProvenance.PROVIDER_REPORTED,
    )
    measured = estimate_run_cost(
        policy=reported,
        variant=variant,
        evidence=evidence,
        as_of=_AS_OF,
        usage=observation,
    )
    expected_measured = (
        Decimal(50_000) / Decimal(1_000_000) * Decimal("3.50")
        + Decimal(8_000) / Decimal(1_000_000) * Decimal("12.00")
    )
    if (
        measured.upper_bound != expected_measured
        or measured.lower_bound != expected_measured
        or measured.worst_case_upper_bound
    ):
        problems.append("reported_usage_estimate_wrong")

    mismatched = UsageObservation(
        input_tokens=50_000,
        output_tokens=8_000,
        provenance=UsageProvenance.RUNTIME_SIZED,
    )
    gap = estimate_run_cost(
        policy=reported,
        variant=variant,
        evidence=evidence,
        as_of=_AS_OF,
        usage=mismatched,
    )
    if "USAGE_PROVENANCE_MISMATCH" not in gap.evidence_gaps or (
        gap.upper_bound is not None
    ):
        problems.append("usage_provenance_mismatch_not_gapped")

    no_usage = estimate_run_cost(
        policy=reported, variant=variant, evidence=evidence, as_of=_AS_OF
    )
    if "USAGE_UNAVAILABLE" not in no_usage.evidence_gaps or (
        no_usage.upper_bound is not None
    ):
        problems.append("missing_usage_not_gapped")

    no_pricing = estimate_run_cost(
        policy=worst,
        variant=variant,
        evidence=RoutingEvidence(),
        as_of=_AS_OF,
        sized_input_tokens=1_000,
    )
    if "PRICING_MISSING" not in no_pricing.evidence_gaps or (
        no_pricing.upper_bound is not None
    ):
        problems.append("missing_pricing_fabricated_estimate")

    if (
        sized.settlement_guaranteed
        or measured.settlement_guaranteed
        or no_pricing.settlement_guaranteed
    ):
        problems.append("settlement_guarantee_claimed")
    return {"problems": problems}


def _deployment_constraints_probe() -> dict:
    """Deployment Constraints 硬匹配与 unknown fail-closed。"""
    problems: list[str] = []
    home = _variant("variant-home", _contract("contract-home"))
    edge = _variant("variant-edge", _contract("contract-edge"))
    far = _variant("variant-far", _contract("contract-far"))
    batch = _variant("variant-batch", _contract("contract-batch"))
    legacy = _variant("variant-legacy", _contract("contract-legacy"))
    unannounced = _variant("variant-unannounced", _contract("contract-unannounced"))
    catalog = _catalog(
        _entry(home),
        _entry(edge, provider="edge-provider"),
        _entry(far, region="us-west"),
        _entry(batch, endpoint_class="batch"),
        _entry(legacy, retention="retention-2025-06"),
        _entry(unannounced, provider=None),
    )
    policy = _policy(
        deployment=DeploymentConstraints(
            allowed_providers=frozenset({"offline-provider"}),
            allowed_regions=frozenset({"cn-north"}),
            allowed_endpoint_classes=frozenset({"standard"}),
            allowed_retention_evidence_versions=frozenset(
                {"retention-2026-01"}
            ),
        )
    )
    variants = (home, edge, far, batch, legacy, unannounced)
    result = _select(catalog, policy, _evidence(variants))
    if (
        result.outcome is not RoutingOutcome.SELECTED
        or result.decision is None
        or result.decision.selected_variant.variant_id != "variant-home"
    ):
        problems.append("deployment_constraints_selection_failed")
    reasons = {
        evaluation.variant_id: evaluation.reason_code
        for evaluation in result.candidate_evaluations
    }
    expected = {
        "variant-edge": REASON_PROVIDER_NOT_ALLOWED,
        "variant-far": REASON_REGION_NOT_ALLOWED,
        "variant-batch": REASON_ENDPOINT_CLASS_NOT_ALLOWED,
        "variant-legacy": REASON_RETENTION_EVIDENCE_VERSION_NOT_ALLOWED,
        "variant-unannounced": REASON_PROVIDER_UNKNOWN,
    }
    for variant_id, code in expected.items():
        if reasons.get(variant_id) != code:
            problems.append(f"deployment_constraint_reason_missing:{variant_id}")
    return {"problems": problems}


def _six_outcomes_probe() -> dict:
    """六种已决议 Routing Outcome 全部可观察且原因可检查。"""
    problems: list[str] = []
    good = _variant("variant-good", _contract("contract-good"))
    other = _variant("variant-other", _contract("contract-other"))
    catalog = _catalog(_entry(good), _entry(other))
    evidence = _evidence((good, other))

    selected = _select(catalog, _policy(), evidence)
    if selected.outcome is not RoutingOutcome.SELECTED or (
        selected.decision is None
    ):
        problems.append("outcome_selected_not_observed")

    strict = _select(
        catalog,
        _policy(
            hard_gates=HardRoutingGates(
                max_input_price_per_mtok=Decimal("1.00")
            )
        ),
        evidence,
    )
    if (
        strict.outcome is not RoutingOutcome.POLICY_UNSATISFIED
        or strict.reason_code != "POLICY_UNSATISFIED"
    ):
        problems.append("outcome_policy_unsatisfied_not_observed")

    unregistered = _select(
        catalog,
        _policy(
            identity=RoutingPolicyIdentity(
                policy_id="scenario-unregistered", version="1"
            )
        ),
        evidence,
    )
    if (
        unregistered.outcome is not RoutingOutcome.NO_COMPATIBLE_VARIANT
        or unregistered.reason_code != "NO_COMPATIBLE_VARIANT"
    ):
        problems.append("outcome_no_compatible_not_observed")
    if REASON_NOT_REGISTERED_FOR_POLICY not in {
        evaluation.reason_code
        for evaluation in unregistered.candidate_evaluations
    }:
        problems.append("outcome_no_compatible_reason_not_inspectable")

    unavailable = _select(
        catalog,
        _policy(hard_gates=HardRoutingGates(require_availability=True)),
        _evidence((good, other), availability=()),
    )
    if (
        unavailable.outcome is not RoutingOutcome.EVIDENCE_UNAVAILABLE
        or unavailable.reason_code != "AVAILABILITY_SNAPSHOT_MISSING"
    ):
        problems.append("outcome_evidence_unavailable_not_observed")

    twin = _variant(
        "variant-good",
        _contract("contract-good"),
        budget=ModelExecutionBudget(
            run_max_attempts=8,
            primary_max_attempts=2,
            context_compression_max_attempts=1,
            output_repair_max_attempts=1,
        ),
    )
    conflict = _select(
        _catalog(_entry(good), _entry(twin)), _policy(), evidence
    )
    if (
        conflict.outcome is not RoutingOutcome.CATALOG_CONFLICT
        or conflict.reason_code != REASON_DUPLICATE_VARIANT_IDENTITY
    ):
        problems.append("outcome_catalog_conflict_not_observed")

    ambiguous = _select(
        catalog,
        _policy(objectives=(_QUALITY_OBJECTIVE, _QUALITY_OBJECTIVE)),
        evidence,
    )
    if (
        ambiguous.outcome is not RoutingOutcome.INVALID_POLICY
        or ambiguous.reason_code != REASON_AMBIGUOUS_OBJECTIVES
    ):
        problems.append("outcome_invalid_policy_not_observed")
    return {"problems": problems}


def _fallback_probe() -> dict:
    """pre-Run Fallback：有限次数、逐次可检查、结构约束 fail closed。"""
    problems: list[str] = []
    identities = (_PRIMARY_IDENTITY, _SECONDARY_IDENTITY)
    economy = _variant(
        "variant-economy",
        _contract("contract-economy"),
        policy_identities=identities,
    )
    premium = _variant(
        "variant-premium",
        _contract("contract-premium"),
        policy_identities=identities,
    )
    catalog = _catalog(_entry(economy), _entry(premium))
    evidence = _evidence((economy, premium))

    strict = _policy(
        hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("1.00"))
    )
    relaxed = _policy(identity=_SECONDARY_IDENTITY)
    result = execute_pre_run_fallback(
        catalog=catalog,
        sequence=FallbackSequence(policies=(strict, relaxed), max_attempts=2),
        evidence=evidence,
        as_of=_AS_OF,
    )
    if (
        result.outcome is not RoutingOutcome.SELECTED
        or result.reason_code != FALLBACK_SELECTED
        or result.selected is None
        or result.selected.decision is None
        or result.selected.decision.selected_variant.variant_id
        != "variant-economy"
    ):
        problems.append("fallback_did_not_select")
    if len(result.attempts) != 2:
        problems.append("fallback_attempt_count_wrong")
    first = result.attempts[0]
    if (
        first.outcome is not RoutingOutcome.POLICY_UNSATISFIED
        or first.decision_id is not None
        or first.policy_identity != _PRIMARY_IDENTITY
    ):
        problems.append("fallback_first_attempt_not_inspectable")

    strict_b = _policy(
        identity=_SECONDARY_IDENTITY,
        hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("1.00")),
    )
    exhausted = execute_pre_run_fallback(
        catalog=catalog,
        sequence=FallbackSequence(
            policies=(strict, strict_b), max_attempts=2
        ),
        evidence=evidence,
        as_of=_AS_OF,
    )
    if (
        not exhausted.exhausted
        or exhausted.reason_code != FALLBACK_EXHAUSTED
        or exhausted.selected is not None
    ):
        problems.append("fallback_exhaustion_not_recorded")
    if [attempt.reason_code for attempt in exhausted.attempts] != [
        "POLICY_UNSATISFIED",
        "POLICY_UNSATISFIED",
    ]:
        problems.append("fallback_exhausted_reasons_not_inspectable")

    try:
        FallbackSequence(
            policies=(strict, _policy(identity=_PRIMARY_IDENTITY)),
            max_attempts=2,
        )
        problems.append("fallback_duplicate_identity_accepted")
    except ValueError:
        pass
    try:
        FallbackSequence(policies=(strict,), max_attempts=0)
        problems.append("fallback_zero_attempts_accepted")
    except ValueError:
        pass
    return {"problems": problems}


def _zero_side_effect_probe() -> dict:
    """成功与失败的 routing 全程零副作用：输入 snapshot 摘要不变。"""
    problems: list[str] = []
    good = _variant("variant-good", _contract("contract-good"))
    other = _variant("variant-other", _contract("contract-other"))
    catalog = _catalog(_entry(good), _entry(other))
    evidence = _evidence((good, other))
    policies = (
        _policy(),
        _policy(
            hard_gates=HardRoutingGates(
                max_input_price_per_mtok=Decimal("1.00")
            )
        ),
        _policy(hard_gates=HardRoutingGates(require_availability=True)),
        _policy(
            identity=RoutingPolicyIdentity(
                policy_id="scenario-unregistered", version="1"
            )
        ),
    )
    evidence_before = evidence.evidence_digest()
    catalog_before = catalog.catalog_digest()
    policy_before = tuple(policy.content_digest() for policy in policies)
    for policy in policies:
        _select(catalog, policy, evidence)
    execute_pre_run_fallback(
        catalog=catalog,
        sequence=FallbackSequence(policies=(policies[1],), max_attempts=1),
        evidence=evidence,
        as_of=_AS_OF,
    )
    if evidence.evidence_digest() != evidence_before:
        problems.append("evidence_mutated_by_routing")
    if catalog.catalog_digest() != catalog_before:
        problems.append("catalog_mutated_by_routing")
    if tuple(policy.content_digest() for policy in policies) != policy_before:
        problems.append("policy_mutated_by_routing")
    return {"problems": problems}


def _immutable_decision_probe() -> dict:
    """Routing Store：确定性 decision_id、幂等重放、冲突 fail、
    重开数据库原样可读（绝不重算历史选择）。"""
    problems: list[str] = []
    good = _variant("variant-good", _contract("contract-good"))
    other = _variant("variant-other", _contract("contract-other"))
    catalog = _catalog(_entry(good), _entry(other))
    evidence = _evidence((good, other))
    policy = _policy()
    decision = _select(catalog, policy, evidence).decision
    replay = _select(catalog, policy, evidence).decision
    if decision is None or replay is None:
        problems.append("selection_did_not_produce_decision")
        return {"problems": problems}
    if decision.decision_id != replay.decision_id:
        problems.append("decision_id_not_deterministic")

    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "routing-store.sqlite3"
        store = SQLiteRoutingStore(path)
        try:
            store.save_decision(decision, evidence=evidence)
            store.save_decision(decision, evidence=evidence)
            if store.decision_ids() != (decision.decision_id,):
                problems.append("replay_not_idempotent")
            tampered = decision.model_copy(
                update={"catalog_version": "999"}
            )
            try:
                store.save_decision(tampered, evidence=evidence)
                problems.append("conflicting_content_accepted")
            except RoutingDecisionConflictError:
                pass
            binding = bind_decision_to_run(
                decision, _run(decision.selected_variant, "run-1", RunStatus.CREATED)
            )
            store.attach_run_binding(decision.decision_id, binding)
        finally:
            store.close()
        reopened = SQLiteRoutingStore(path)
        try:
            stored = reopened.load_decision(decision.decision_id)
        finally:
            reopened.close()
        if stored is None:
            problems.append("decision_lost_after_reopen")
        else:
            if stored.decision != decision or stored.evidence != evidence:
                problems.append("reopened_decision_recomputed_or_changed")
            if len(stored.run_bindings) != 1:
                problems.append("run_binding_lost_after_reopen")
    return {"problems": problems}


def _no_in_run_switch_probe() -> dict:
    """显式 Replacement Run：Run 未终止绝不换模；替换必须新 Decision。"""
    problems: list[str] = []
    identities = (_PRIMARY_IDENTITY, _SECONDARY_IDENTITY)
    economy = _variant(
        "variant-economy",
        _contract("contract-economy"),
        policy_identities=identities,
    )
    premium = _variant(
        "variant-premium",
        _contract("contract-premium"),
        policy_identities=identities,
    )
    catalog = _catalog(_entry(economy), _entry(premium))
    evidence = _evidence((economy, premium))
    predecessor_result = _select(catalog, _policy(), evidence)
    successor_result = _select(catalog, _policy(identity=_SECONDARY_IDENTITY), evidence)
    if predecessor_result.decision is None or successor_result.decision is None:
        problems.append("replacement_setup_selection_failed")
        return {"problems": problems}
    predecessor_decision = predecessor_result.decision
    successor_decision = successor_result.decision
    if predecessor_decision.decision_id == successor_decision.decision_id:
        problems.append("replacement_setup_decisions_identical")

    predecessor_binding = bind_decision_to_run(
        predecessor_decision,
        _run(
            predecessor_decision.selected_variant,
            "run-1",
            RunStatus.RUNNING,
        ),
    )
    successor_binding = bind_decision_to_run(
        successor_decision,
        _run(
            successor_decision.selected_variant,
            "run-2",
            RunStatus.CREATED,
        ),
    )
    try:
        register_replacement_run(
            predecessor=_run(
                predecessor_decision.selected_variant,
                "run-1",
                RunStatus.RUNNING,
            ),
            successor=_run(
                successor_decision.selected_variant,
                "run-2",
                RunStatus.CREATED,
            ),
            predecessor_binding=predecessor_binding,
            successor_binding=successor_binding,
            reason_code=REPLACEMENT_REASON_PROVIDER_FAILURE,
        )
        problems.append("running_predecessor_replaced")
    except RoutingReplacementError:
        pass

    predecessor_run = _run(
        predecessor_decision.selected_variant, "run-1", RunStatus.FAILED
    )
    predecessor_binding = bind_decision_to_run(
        predecessor_decision, predecessor_run
    )
    record = register_replacement_run(
        predecessor=predecessor_run,
        successor=_run(
            successor_decision.selected_variant, "run-2", RunStatus.CREATED
        ),
        predecessor_binding=predecessor_binding,
        successor_binding=successor_binding,
        reason_code=REPLACEMENT_REASON_PROVIDER_FAILURE,
    )
    if (
        record.predecessor_run_id != "run-1"
        or record.successor_run_id != "run-2"
        or record.reason_code != REPLACEMENT_REASON_PROVIDER_FAILURE
        or record.predecessor_decision_id
        == record.successor_decision_id
    ):
        problems.append("replacement_relation_not_frozen")

    try:
        register_replacement_run(
            predecessor=predecessor_run,
            successor=_run(
                successor_decision.selected_variant, "run-3", RunStatus.CREATED
            ),
            predecessor_binding=predecessor_binding,
            successor_binding=predecessor_binding.model_copy(
                update={"run_id": "run-3"}
            ),
            reason_code=REPLACEMENT_REASON_PROVIDER_FAILURE,
        )
        problems.append("replacement_reused_old_decision")
    except RoutingReplacementError:
        pass
    return {"problems": problems}


def _explicit_promotion_probe() -> dict:
    """Eval Recommendation 显式发布：fail-closed 前置条件、只影响
    之后路由、未发布身份对 Router 不可见。"""
    problems: list[str] = []
    economy = _variant("variant-economy", _contract("contract-economy"))
    premium = _variant("variant-premium", _contract("contract-premium"))
    base_policy = _policy()
    economy_revision = register_variant_for_policy(
        economy, _PUBLISHED_IDENTITY, new_version="2"
    )
    premium_revision = register_variant_for_policy(
        premium, _PUBLISHED_IDENTITY, new_version="2"
    )
    publication = RecommendationPublication(
        recommendation_id="rec-scenario-1",
        recommendation_version="1",
        report_id="report-scenario-1",
        report_revision=2,
        evidence_digest="sha256:" + "b" * 64,
        target_variant_id="variant-economy",
        target_variant_version="2",
        hard_gate_passed=True,
        quality_gate_passed=True,
        confidence=0.91,
        valid_until=_AS_OF + timedelta(days=1),
    )
    for bad in (
        publication.model_copy(update={"hard_gate_passed": False}),
        publication.model_copy(update={"quality_gate_passed": False}),
        publication.model_copy(
            update={"valid_until": _AS_OF - timedelta(seconds=1)}
        ),
    ):
        try:
            publish_recommendation_as_policy(
                bad,
                base_policy=base_policy,
                new_version="9",
                as_of=_AS_OF,
            )
            problems.append("publication_precondition_not_enforced")
            break
        except PublicationError:
            pass
    try:
        publish_recommendation_as_policy(
            publication,
            base_policy=base_policy,
            new_version=base_policy.identity.version,
            as_of=_AS_OF,
        )
        problems.append("publication_version_overwrite_accepted")
    except PublicationError:
        pass

    published = publish_recommendation_as_policy(
        publication,
        base_policy=base_policy,
        new_version="9",
        as_of=_AS_OF,
    )
    if published.identity != _PUBLISHED_IDENTITY or (
        published.allowed_variants != (("variant-economy", "2"),)
    ):
        problems.append("published_policy_scope_wrong")
    if base_policy.allowed_variants is not None or (
        base_policy.identity.version != "1"
    ):
        problems.append("base_policy_rewritten_by_publication")

    catalog = _catalog(_entry(economy_revision), _entry(premium_revision))
    evidence = _evidence((economy_revision, premium_revision))
    new_result = _select(catalog, published, evidence)
    if (
        new_result.outcome is not RoutingOutcome.SELECTED
        or new_result.decision is None
        or new_result.decision.selected_variant.variant_id != "variant-economy"
    ):
        problems.append("published_policy_did_not_select_target")
    old_result = _select(catalog, base_policy, evidence)
    if (
        old_result.outcome is not RoutingOutcome.SELECTED
        or old_result.decision is None
        or old_result.decision.decision_id
        == (new_result.decision.decision_id if new_result.decision else None)
    ):
        problems.append("old_policy_semantics_changed")

    unpublished = _select(
        catalog, _policy(identity=_UNPUBLISHED_IDENTITY), evidence
    )
    if (
        unpublished.outcome is not RoutingOutcome.NO_COMPATIBLE_VARIANT
        or REASON_NOT_REGISTERED_FOR_POLICY
        not in {
            evaluation.reason_code
            for evaluation in unpublished.candidate_evaluations
        }
    ):
        problems.append("unpublished_policy_visible_to_router")
    return {"problems": problems}


# -- 对账与受控变异 -------------------------------------------------------


def reconcile_model_routing(
    observation: Mapping[str, object],
) -> list[str]:
    """验证 model-routing Scenario 观察：十类语义结论全部为真。

    ``observation`` 需要包含十个布尔键（``typed_capability_observed`` …
    ``explicit_promotion_observed``）。任一受控变异（对应键被改写为
    False）都必须产生非空问题列表，否则 Harness 为 ERROR。
    """
    problems: list[str] = []
    for key, problem in (
        ("typed_capability_observed", "typed_capability_not_observed"),
        ("operational_limits_observed", "operational_limits_not_observed"),
        ("usage_cost_observed", "usage_cost_not_observed"),
        ("deployment_constraints_observed", "deployment_constraints_not_observed"),
        ("six_outcomes_observed", "six_outcomes_not_observed"),
        ("fallback_observed", "fallback_not_observed"),
        ("zero_side_effect_observed", "zero_side_effect_not_observed"),
        ("immutable_decision_observed", "immutable_decision_not_observed"),
        ("no_in_run_switch_observed", "no_in_run_switch_not_observed"),
        ("explicit_promotion_observed", "explicit_promotion_not_observed"),
    ):
        if observation.get(key) is not True:
            problems.append(problem)
    return problems


def _mutation_probe(observation: Mapping[str, object]) -> dict:
    """每个观察布尔被改写都必须被 reconcile 检出。"""
    detected: dict[str, bool] = {}
    for key in (
        "typed_capability_observed",
        "operational_limits_observed",
        "usage_cost_observed",
        "deployment_constraints_observed",
        "six_outcomes_observed",
        "fallback_observed",
        "zero_side_effect_observed",
        "immutable_decision_observed",
        "no_in_run_switch_observed",
        "explicit_promotion_observed",
    ):
        mutated = dict(observation)
        mutated[key] = False
        detected[key] = bool(reconcile_model_routing(mutated))
    problems = (
        []
        if all(detected.values())
        else ["undetected_mutation"]
    )
    return {
        "mutation_probes": len(detected),
        "mutations_detected": detected,
        "problems": problems,
    }


_PROBE_KEYS = (
    ("typed_capability", _typed_capability_probe),
    ("operational_limits", _operational_limits_probe),
    ("usage_cost", _usage_cost_probe),
    ("deployment_constraints", _deployment_constraints_probe),
    ("six_outcomes", _six_outcomes_probe),
    ("fallback", _fallback_probe),
    ("zero_side_effect", _zero_side_effect_probe),
    ("immutable_decision", _immutable_decision_probe),
    ("no_in_run_switch", _no_in_run_switch_probe),
    ("explicit_promotion", _explicit_promotion_probe),
)


def run_model_routing() -> tuple[
    tuple[AcceptanceCheckResult, ...],
    dict[str, str | int | bool],
    dict[str, str],
]:
    """运行 model-routing Scenario 的完整离线证明。

    返回 ``(checks, evidence_view, independent_evidence)``，与
    :func:`m_agent.testing.run_eval_regression` 相同的形态。
    """
    probes = {name: probe() for name, probe in _PROBE_KEYS}
    observation = {
        f"{name}_observed": not probe["problems"]
        for name, probe in probes.items()
    }
    mutation = _mutation_probe(observation)

    evidence_view: dict[str, str | int | bool] = {
        **observation,
        "mutation_probes": mutation["mutation_probes"],
        "mutation_detected": not mutation["problems"],
        "six_outcomes_count": 6,
    }
    independent_evidence: dict[str, str] = {}
    for name, probe in probes.items():
        evidence_view[f"{name}_authoritative_digest"] = _digest(probe)
        independent_evidence[f"{name}_independent_digest"] = _digest(
            {"problems": probe["problems"]}
        )
    evidence_view["mutation_authoritative_digest"] = _digest(mutation)
    independent_evidence["mutation_independent_digest"] = _digest(
        {
            "mutations_detected": mutation["mutations_detected"],
            "problems": mutation["problems"],
        }
    )

    def result(check_id: str, passed: bool, digest: str) -> AcceptanceCheckResult:
        return AcceptanceCheckResult(
            check_id=check_id,
            status=(
                AcceptanceCheckStatus.PASS if passed else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
            reason_code="model_routing_observed",
            evidence_digest=digest,
        )

    checks = tuple(
        result(
            f"model.routing.{name.replace('_', '-')}",
            bool(observation[f"{name}_observed"]),
            str(evidence_view[f"{name}_authoritative_digest"]),
        )
        for name, _ in _PROBE_KEYS
    ) + (
        result(
            "model.routing.mutation",
            not mutation["problems"],
            str(evidence_view["mutation_authoritative_digest"]),
        ),
    )
    return checks, evidence_view, independent_evidence
