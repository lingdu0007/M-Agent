"""Ticket 19: deterministic Agent Variant routing public contracts.

Router 是 Runtime Companion 的纯函数：只组合公开契约与只读快照输入，
在任何 Session Claim / Run 创建之前完成选择，失败 fail closed 且不隐式
放宽规则。本文件覆盖 Catalog 冻结与身份冲突、Variant Binding Set 完整
性、typed capability / numeric limits / deployment 过滤、字典序排序与
tie-break 稳定性、六种 Routing Result 负例、stale/missing snapshot
fail closed、确定性复跑与 Router 输入边界。
"""

from __future__ import annotations

import inspect
import unittest
from datetime import timedelta
from decimal import Decimal

from m_agent.runtime import (
    ModelCapabilities,
    ModelCapabilityCombination,
    ModelLimits,
    ModelPurpose,
    ModelRequirements,
    StreamingMode,
    StructuredOutputMode,
    ToolCallingMode,
)

from m_agent.companion.routing import (
    AgentVariant,
    AvailabilitySnapshot,
    DeploymentAttributes,
    DeploymentConstraints,
    HardRoutingGates,
    MissingValuePolicy,
    ModelCatalog,
    ModelCatalogEntry,
    ModelEvidenceSnapshot,
    ModelRouter,
    ObjectiveDimension,
    ObjectiveDirection,
    PricingSnapshot,
    RetentionEvidenceSnapshot,
    RoutingEvidence,
    RoutingObjective,
    RoutingOutcome,
    RoutingPolicy,
    RoutingPolicyIdentity,
    WARNING_SOFT_EVIDENCE_MISSING,
    WARNING_SOFT_EVIDENCE_STALE,
)
from m_agent.companion.routing import (
    REASON_AMBIGUOUS_OBJECTIVES,
    REASON_AVAILABILITY_SNAPSHOT_MISSING,
    REASON_AVAILABILITY_SNAPSHOT_STALE,
    REASON_CONTRACT_FINGERPRINT_CONFLICT,
    REASON_DUPLICATE_VARIANT_IDENTITY,
    REASON_EMPTY_CANDIDATE_SCOPE,
    REASON_ENDPOINT_CLASS_UNKNOWN,
    REASON_HARD_AVAILABILITY_GATE_FAILED,
    REASON_HARD_PRICE_GATE_FAILED,
    REASON_HARD_QUALITY_GATE_FAILED,
    REASON_HARD_STABILITY_GATE_FAILED,
    REASON_MODEL_EVIDENCE_MISSING,
    REASON_MODEL_EVIDENCE_STALE,
    REASON_NOT_IN_CANDIDATE_SCOPE,
    REASON_NOT_REGISTERED_FOR_POLICY,
    REASON_NO_COMPATIBLE_VARIANT,
    REASON_POLICY_UNSATISFIED,
    REASON_PRICING_SNAPSHOT_MISSING,
    REASON_PRICING_SNAPSHOT_NOT_EFFECTIVE,
    REASON_PRICING_SNAPSHOT_STALE,
    REASON_RETENTION_EVIDENCE_MISSING,
    REASON_RETENTION_EVIDENCE_STALE,
    REASON_RETENTION_EVIDENCE_VERSION_MISMATCH,
    REASON_RETENTION_EVIDENCE_VERSION_NOT_ALLOWED,
    REASON_RETENTION_EVIDENCE_UNKNOWN,
    REASON_SELECTED,
)

from routing_fixtures import (
    AS_OF,
    POLICY_IDENTITY,
    cost_objective,
    make_availability,
    make_catalog,
    make_contract,
    make_evidence,
    make_entry,
    make_model_evidence,
    make_policy,
    make_pricing,
    make_retention_evidence,
    make_variant,
    non_concurrent_capabilities,
    quality_objective,
    tool_and_structured_capabilities,
)


def _trace(result):  # noqa: ANN001 - test helper
    return {
        (evaluation.variant_id, evaluation.variant_version): evaluation.reason_code
        for evaluation in result.candidate_evaluations
    }


class CatalogFreezingTests(unittest.TestCase):
    """Catalog 冻结与不可变身份冲突。"""

    def test_catalog_entries_are_frozen_with_digest(self) -> None:
        contract = make_contract("contract-a")
        variant = make_variant("variant-a", contract)
        catalog = make_catalog(make_entry(variant))
        self.assertEqual(catalog.catalog_digest(), catalog.catalog_digest())
        with self.assertRaises(Exception):
            catalog.entries[0].variant.variant_id = "mutated"  # type: ignore[misc]

    def test_duplicate_variant_identity_with_different_content_is_conflict(
        self,
    ) -> None:
        contract = make_contract("contract-a")
        variant = make_variant("variant-a", contract)
        other = make_variant(
            "variant-a",
            make_contract("contract-a2"),
            definition_id="definition-other",
        )
        catalog = make_catalog(make_entry(variant), make_entry(other))
        result = ModelRouter().select(
            catalog=catalog,
            policy=make_policy(),
            evidence=RoutingEvidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.CATALOG_CONFLICT)
        self.assertEqual(result.reason_code, REASON_DUPLICATE_VARIANT_IDENTITY)
        self.assertIsNone(result.decision)

    def test_same_contract_identity_with_different_fingerprint_is_conflict(
        self,
    ) -> None:
        left = make_contract("contract-a")
        right = make_contract("contract-a").model_copy(
            update={
                "limits": ModelLimits(
                    context_window_tokens=64_000, max_output_tokens=8_000
                )
            }
        )
        self.assertEqual(
            (left.contract_id, left.version), (right.contract_id, right.version)
        )
        self.assertNotEqual(left.fingerprint, right.fingerprint)
        catalog = make_catalog(
            make_entry(make_variant("variant-a", left)),
            make_entry(make_variant("variant-b", right)),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=make_policy(),
            evidence=RoutingEvidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.CATALOG_CONFLICT)
        self.assertEqual(result.reason_code, REASON_CONTRACT_FINGERPRINT_CONFLICT)


class VariantBindingSetCompletenessTests(unittest.TestCase):
    """Agent Variant 冻结完整 Binding Set 与所需策略身份。"""

    def test_variant_freezes_definition_identity_and_policy_identity(self) -> None:
        contract = make_contract("contract-a")
        variant = make_variant("variant-a", contract, policy_identity=POLICY_IDENTITY)
        self.assertEqual(variant.definition_id, "definition-variant-a")
        self.assertEqual(variant.definition_version, "1")
        self.assertEqual(variant.policy_identities, (POLICY_IDENTITY,))
        self.assertEqual(
            variant.model_bindings.for_purpose(ModelPurpose.PRIMARY).contract,
            contract,
        )
        self.assertIsNotNone(variant.model_execution_budget)
        self.assertEqual(variant.primary_contract(), contract)

    def test_variant_requires_at_least_one_policy_identity(self) -> None:
        template = make_variant("template", make_contract("contract-a"))
        with self.assertRaises(Exception):
            AgentVariant(
                variant_id="variant-a",
                version="1",
                definition_id="definition-a",
                definition_version="1",
                model_bindings=template.model_bindings,
                model_execution_budget=template.model_execution_budget,
                policy_identities=(),
            )

    def test_variant_internal_binding_mismatch_is_filtered(self) -> None:
        """显式三用途绑定中 requirements 超出自身 Contract 的 Variant 被过滤。"""
        from m_agent.runtime import ModelBinding, ModelBindingSet

        contract = make_contract("contract-a", context_window=128_000)
        impossible = ModelRequirements(min_context_window_tokens=999_999)
        binding_set = ModelBindingSet(
            bindings=(
                ModelBinding(
                    purpose=ModelPurpose.PRIMARY,
                    contract=contract,
                    requirements=impossible,
                ),
                ModelBinding(
                    purpose=ModelPurpose.CONTEXT_COMPRESSION,
                    contract=contract,
                ),
                ModelBinding(
                    purpose=ModelPurpose.OUTPUT_REPAIR,
                    contract=contract,
                ),
            )
        )
        variant = AgentVariant(
            variant_id="variant-a",
            version="1",
            definition_id="definition-a",
            definition_version="1",
            model_bindings=binding_set,
            model_execution_budget=template_budget(),
            policy_identities=(POLICY_IDENTITY,),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant)),
            policy=make_policy(),
            evidence=RoutingEvidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.NO_COMPATIBLE_VARIANT)
        self.assertEqual(
            _trace(result)[("variant-a", "1")], "CONTEXT_WINDOW_TOO_SMALL"
        )


def template_budget():  # noqa: ANN202 - test helper
    from m_agent.runtime import ModelExecutionBudget

    return ModelExecutionBudget()


class CapabilityAndLimitsFilteringTests(unittest.TestCase):
    """typed capability 组合与 numeric limits 的确定性过滤。"""

    def test_tool_calling_requirement_filters_variant_without_capability(self) -> None:
        plain = make_variant("variant-a", make_contract("contract-plain"))
        rich = make_variant(
            "variant-b",
            make_contract(
                "contract-rich", capabilities=tool_and_structured_capabilities()
            ),
        )
        policy = make_policy(
            requirements=ModelRequirements(
                capabilities=ModelCapabilities(
                    tool_calling=ToolCallingMode.NATIVE
                ).as_typed()
            )
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(plain), make_entry(rich)),
            policy=policy,
            evidence=make_evidence((plain, rich)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(result.decision.selected_variant.variant_id, "variant-b")
        self.assertEqual(_trace(result)[("variant-a", "1")], "TOOL_CALLING_UNSUPPORTED")

    def test_concurrent_capability_combination_requirement_filters(self) -> None:
        streaming_tools = make_variant(
            "variant-a",
            make_contract(
                "contract-combo", capabilities=non_concurrent_capabilities()
            ),
        )
        combination = ModelCapabilityCombination(
            streaming=StreamingMode.DELTA,
            tool_calling=ToolCallingMode.NATIVE,
        )
        policy = make_policy(
            requirements=ModelRequirements(
                capabilities=ModelCapabilities(
                    streaming=StreamingMode.DELTA,
                    tool_calling=ToolCallingMode.NATIVE,
                    supported_combinations=(combination,),
                ).as_typed()
            )
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(streaming_tools)),
            policy=policy,
            evidence=make_evidence((streaming_tools,)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.NO_COMPATIBLE_VARIANT)
        self.assertEqual(
            _trace(result)[("variant-a", "1")],
            "CAPABILITY_COMBINATION_UNSUPPORTED",
        )

    def test_numeric_limits_filter_context_window(self) -> None:
        small = make_variant(
            "variant-a",
            make_contract("contract-small", context_window=32_000),
        )
        big = make_variant(
            "variant-b",
            make_contract("contract-big", context_window=200_000),
        )
        policy = make_policy(
            requirements=ModelRequirements(min_context_window_tokens=128_000)
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(small), make_entry(big)),
            policy=policy,
            evidence=make_evidence((small, big)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(result.decision.selected_variant.variant_id, "variant-b")
        self.assertEqual(
            _trace(result)[("variant-a", "1")], "CONTEXT_WINDOW_TOO_SMALL"
        )

    def test_numeric_limits_filter_max_output(self) -> None:
        variant_a = make_variant(
            "variant-a",
            make_contract("contract-a", max_output=1_000),
        )
        policy = make_policy(
            requirements=ModelRequirements(min_output_tokens=4_000)
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=make_evidence((variant_a,)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.NO_COMPATIBLE_VARIANT)
        self.assertEqual(_trace(result)[("variant-a", "1")], "MAX_OUTPUT_TOO_SMALL")

    def test_structured_output_requirement_uses_typed_modes(self) -> None:
        strict_required = make_variant("variant-a", make_contract("contract-plain"))
        policy = make_policy(
            requirements=ModelRequirements(
                capabilities=ModelCapabilities(
                    structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
                ).as_typed()
            )
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(strict_required)),
            policy=policy,
            evidence=make_evidence((strict_required,)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.NO_COMPATIBLE_VARIANT)
        self.assertEqual(
            _trace(result)[("variant-a", "1")], "STRUCTURED_OUTPUT_UNSUPPORTED"
        )


class DeploymentConstraintFilteringTests(unittest.TestCase):
    """Deployment Constraints 硬匹配、未知属性 fail closed 与合规证据。"""

    def _two_variants(self):  # noqa: ANN202 - test helper
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        variant_b = make_variant("variant-b", make_contract("contract-b"))
        return variant_a, variant_b

    def test_provider_allowlist_filters_deployment_hard_match(self) -> None:
        variant_a, variant_b = self._two_variants()
        policy = make_policy(
            deployment=DeploymentConstraints(
                allowed_providers=frozenset({"eu-provider"})
            )
        )
        entry_a = make_entry(variant_a, provider="offline-provider")
        entry_b = make_entry(variant_b, provider="eu-provider")
        result = ModelRouter().select(
            catalog=make_catalog(entry_a, entry_b),
            policy=policy,
            evidence=make_evidence((variant_a, variant_b)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(result.decision.selected_variant.variant_id, "variant-b")
        self.assertEqual(_trace(result)[("variant-a", "1")], "PROVIDER_NOT_ALLOWED")

    def test_unknown_constrained_attribute_fails_closed(self) -> None:
        variant_a, _variant_b = self._two_variants()
        entry_unknown_region = make_entry(variant_a, region=None)
        policy = make_policy(
            deployment=DeploymentConstraints(
                allowed_regions=frozenset({"cn-north"})
            )
        )
        result = ModelRouter().select(
            catalog=make_catalog(entry_unknown_region),
            policy=policy,
            evidence=make_evidence((variant_a,)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.NO_COMPATIBLE_VARIANT)
        self.assertEqual(result.reason_code, REASON_NO_COMPATIBLE_VARIANT)
        self.assertEqual(_trace(result)[("variant-a", "1")], "REGION_UNKNOWN")

    def test_endpoint_class_constraint(self) -> None:
        variant_a, variant_b = self._two_variants()
        policy = make_policy(
            deployment=DeploymentConstraints(
                allowed_endpoint_classes=frozenset({"dedicated"})
            )
        )
        catalog = make_catalog(
            make_entry(variant_a, endpoint_class=None),
            make_entry(variant_b, endpoint_class="dedicated"),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=policy,
            evidence=make_evidence((variant_a, variant_b)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(result.decision.selected_variant.variant_id, "variant-b")
        self.assertEqual(
            _trace(result)[("variant-a", "1")], REASON_ENDPOINT_CLASS_UNKNOWN
        )

    def test_retention_version_not_allowed_is_filtered(self) -> None:
        variant_a, variant_b = self._two_variants()
        policy = make_policy(
            deployment=DeploymentConstraints(
                allowed_retention_evidence_versions=frozenset({"retention-2026-01"})
            )
        )
        catalog = make_catalog(
            make_entry(variant_a, retention="retention-2025-12"),
            make_entry(variant_b, retention="retention-2026-01"),
        )
        result = ModelRouter().select(
            catalog=catalog,
            policy=policy,
            evidence=make_evidence((variant_a, variant_b)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(result.decision.selected_variant.variant_id, "variant-b")
        self.assertEqual(
            _trace(result)[("variant-a", "1")],
            REASON_RETENTION_EVIDENCE_VERSION_NOT_ALLOWED,
        )

    def test_retention_evidence_missing_fails_closed(self) -> None:
        variant_a, _ = self._two_variants()
        policy = make_policy(
            deployment=DeploymentConstraints(
                allowed_retention_evidence_versions=frozenset({"retention-2026-01"})
            )
        )
        evidence = make_evidence((variant_a,), retention=())
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(result.reason_code, REASON_RETENTION_EVIDENCE_MISSING)

    def test_retention_evidence_stale_fails_closed(self) -> None:
        variant_a, _ = self._two_variants()
        policy = make_policy(
            deployment=DeploymentConstraints(
                allowed_retention_evidence_versions=frozenset({"retention-2026-01"})
            )
        )
        evidence = make_evidence(
            (variant_a,),
            retention=(
                make_retention_evidence(
                    variant_a, valid_until=AS_OF - timedelta(days=1)
                ),
            ),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(result.reason_code, REASON_RETENTION_EVIDENCE_STALE)

    def test_unknown_retention_attribute_fails_closed_without_evidence_demand(
        self,
    ) -> None:
        variant_a, _ = self._two_variants()
        policy = make_policy(
            deployment=DeploymentConstraints(
                allowed_retention_evidence_versions=frozenset({"retention-2026-01"})
            )
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a, retention=None)),
            policy=policy,
            evidence=RoutingEvidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.NO_COMPATIBLE_VARIANT)
        self.assertEqual(
            _trace(result)[("variant-a", "1")], REASON_RETENTION_EVIDENCE_UNKNOWN
        )

    def test_retention_evidence_not_covering_declaration_fails_closed(self) -> None:
        variant_a, _ = self._two_variants()
        policy = make_policy(
            deployment=DeploymentConstraints(
                allowed_retention_evidence_versions=frozenset({"retention-2026-01"})
            )
        )
        evidence = make_evidence(
            (variant_a,),
            retention=(
                make_retention_evidence(
                    variant_a, verified_version="retention-2025-12"
                ),
            ),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a, retention="retention-2026-01")),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.NO_COMPATIBLE_VARIANT)
        self.assertEqual(
            _trace(result)[("variant-a", "1")],
            REASON_RETENTION_EVIDENCE_VERSION_MISMATCH,
        )


class PolicyScopeFilteringTests(unittest.TestCase):
    """候选范围：策略注册身份与 allowlist。"""

    def test_variant_not_registered_for_policy_is_out_of_scope(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        other_policy = make_policy(
            identity=RoutingPolicyIdentity(policy_id="other-policy", version="1")
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=other_policy,
            evidence=make_evidence((variant_a,)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.NO_COMPATIBLE_VARIANT)
        self.assertEqual(
            _trace(result)[("variant-a", "1")], REASON_NOT_REGISTERED_FOR_POLICY
        )

    def test_candidate_allowlist_excludes_variant(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        variant_b = make_variant("variant-b", make_contract("contract-b"))
        policy = make_policy(allowed_variants=(("variant-b", "1"),))
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a), make_entry(variant_b)),
            policy=policy,
            evidence=make_evidence((variant_a, variant_b)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(result.decision.selected_variant.variant_id, "variant-b")
        self.assertEqual(
            _trace(result)[("variant-a", "1")], REASON_NOT_IN_CANDIDATE_SCOPE
        )

    def test_empty_candidate_scope_is_invalid_policy(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        policy = make_policy(allowed_variants=())
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=make_evidence((variant_a,)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.INVALID_POLICY)
        self.assertEqual(result.reason_code, REASON_EMPTY_CANDIDATE_SCOPE)


class HardGateTests(unittest.TestCase):
    """hard policy：价格/质量/稳定性门槛与证据有效性 fail closed。"""

    def test_hard_price_gate_filters_expensive_compatible_candidate(self) -> None:
        cheap = make_variant("variant-a", make_contract("contract-cheap"))
        expensive = make_variant("variant-b", make_contract("contract-expensive"))
        policy = make_policy(
            hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("5.00"))
        )
        evidence = make_evidence(
            (cheap, expensive),
            pricing=(
                make_pricing(cheap, input_price="1.00"),
                make_pricing(expensive, input_price="12.00"),
            ),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(cheap), make_entry(expensive)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(result.decision.selected_variant.variant_id, "variant-a")
        self.assertEqual(
            _trace(result)[("variant-b", "1")], REASON_HARD_PRICE_GATE_FAILED
        )

    def test_all_compatible_candidates_failing_hard_gates_is_policy_unsatisfied(
        self,
    ) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        policy = make_policy(
            hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("1.00"))
        )
        evidence = make_evidence(
            (variant_a,), pricing=(make_pricing(variant_a, input_price="9.00"),)
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.POLICY_UNSATISFIED)
        self.assertEqual(result.reason_code, REASON_POLICY_UNSATISFIED)
        self.assertIsNone(result.decision)

    def test_missing_hard_pricing_snapshot_fails_closed(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        policy = make_policy(
            hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("5.00"))
        )
        evidence = make_evidence((variant_a,), pricing=())
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(result.reason_code, REASON_PRICING_SNAPSHOT_MISSING)

    def test_stale_hard_pricing_snapshot_fails_closed(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        policy = make_policy(
            hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("5.00"))
        )
        evidence = make_evidence(
            (variant_a,),
            pricing=(make_pricing(variant_a, valid_until=AS_OF - timedelta(hours=1)),),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(result.reason_code, REASON_PRICING_SNAPSHOT_STALE)

    def test_not_yet_effective_pricing_snapshot_is_unavailable(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        policy = make_policy(
            hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("5.00"))
        )
        evidence = make_evidence(
            (variant_a,),
            pricing=(make_pricing(variant_a, effective_at=AS_OF + timedelta(days=1)),),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(result.reason_code, REASON_PRICING_SNAPSHOT_NOT_EFFECTIVE)

    def test_hard_quality_and_stability_gates(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        variant_b = make_variant("variant-b", make_contract("contract-b"))
        policy = make_policy(
            hard_gates=HardRoutingGates(
                min_quality_score=0.9, min_stability_score=0.99
            )
        )
        evidence = make_evidence(
            (variant_a, variant_b),
            model_evidence=(
                make_model_evidence(variant_a, quality=0.85, stability=0.995),
                make_model_evidence(variant_b, quality=0.95, stability=0.98),
            ),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a), make_entry(variant_b)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.POLICY_UNSATISFIED)
        self.assertEqual(
            _trace(result)[("variant-a", "1")], REASON_HARD_QUALITY_GATE_FAILED
        )
        self.assertEqual(
            _trace(result)[("variant-b", "1")], REASON_HARD_STABILITY_GATE_FAILED
        )

    def test_missing_and_stale_model_evidence_fail_closed(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        policy = make_policy(hard_gates=HardRoutingGates(min_quality_score=0.9))
        missing = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=make_evidence((variant_a,), model_evidence=()),
            as_of=AS_OF,
        )
        self.assertIs(missing.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(missing.reason_code, REASON_MODEL_EVIDENCE_MISSING)

        stale = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=make_evidence(
                (variant_a,),
                model_evidence=(
                    make_model_evidence(
                        variant_a, valid_until=AS_OF - timedelta(days=1)
                    ),
                ),
            ),
            as_of=AS_OF,
        )
        self.assertIs(stale.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(stale.reason_code, REASON_MODEL_EVIDENCE_STALE)

    def test_hard_availability_gate(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        variant_b = make_variant("variant-b", make_contract("contract-b"))
        policy = make_policy(hard_gates=HardRoutingGates(require_availability=True))
        evidence = make_evidence(
            (variant_a, variant_b),
            availability=(
                make_availability(variant_a, available=False),
                make_availability(variant_b, available=True),
            ),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a), make_entry(variant_b)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(result.decision.selected_variant.variant_id, "variant-b")
        self.assertEqual(
            _trace(result)[("variant-a", "1")], REASON_HARD_AVAILABILITY_GATE_FAILED
        )

    def test_missing_and_stale_availability_snapshot_fail_closed(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        policy = make_policy(hard_gates=HardRoutingGates(require_availability=True))
        missing = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=make_evidence((variant_a,), availability=()),
            as_of=AS_OF,
        )
        self.assertIs(missing.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(missing.reason_code, REASON_AVAILABILITY_SNAPSHOT_MISSING)

        stale = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=make_evidence(
                (variant_a,),
                availability=(
                    make_availability(
                        variant_a, valid_until=AS_OF - timedelta(minutes=1)
                    ),
                ),
            ),
            as_of=AS_OF,
        )
        self.assertIs(stale.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(stale.reason_code, REASON_AVAILABILITY_SNAPSHOT_STALE)

    def test_hard_gate_evidence_gap_aborts_even_when_other_candidate_passes(
        self,
    ) -> None:
        """硬策略证据缺失不隐式收窄候选集：整体 fail closed。"""
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        variant_b = make_variant("variant-b", make_contract("contract-b"))
        policy = make_policy(
            hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("5.00"))
        )
        evidence = make_evidence(
            (variant_a, variant_b),
            pricing=(make_pricing(variant_b, input_price="1.00"),),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a), make_entry(variant_b)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        self.assertEqual(result.reason_code, REASON_PRICING_SNAPSHOT_MISSING)


class LexicographicOrderingTests(unittest.TestCase):
    """字典序目标排序：方向、缺失值策略与稳定 tie-break。"""

    def _variants(self):  # noqa: ANN202 - test helper
        return (
            make_variant("variant-a", make_contract("contract-a")),
            make_variant("variant-b", make_contract("contract-b")),
        )

    def test_cost_objective_selects_cheaper_variant(self) -> None:
        variant_a, variant_b = self._variants()
        evidence = make_evidence(
            (variant_a, variant_b),
            pricing=(
                make_pricing(variant_a, input_price="2.00"),
                make_pricing(variant_b, input_price="0.50"),
            ),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a), make_entry(variant_b)),
            policy=make_policy(objectives=(cost_objective(),)),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(result.decision.selected_variant.variant_id, "variant-b")
        ranked = sorted(
            (
                evaluation
                for evaluation in result.decision.candidate_evaluations
                if evaluation.rank is not None
            ),
            key=lambda evaluation: evaluation.rank,
        )
        self.assertEqual(
            [evaluation.variant_id for evaluation in ranked],
            ["variant-b", "variant-a"],
        )

    def test_objective_direction_is_respected(self) -> None:
        variant_a, variant_b = self._variants()
        evidence = make_evidence(
            (variant_a, variant_b),
            model_evidence=(
                make_model_evidence(variant_a, quality=0.93),
                make_model_evidence(variant_b, quality=0.88),
            ),
        )
        maximize = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a), make_entry(variant_b)),
            policy=make_policy(objectives=(quality_objective(),)),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertEqual(maximize.decision.selected_variant.variant_id, "variant-a")

    def test_lexicographic_priority_follows_declared_order(self) -> None:
        variant_a, variant_b = self._variants()
        evidence = make_evidence(
            (variant_a, variant_b),
            pricing=(
                make_pricing(variant_a, input_price="2.00"),
                make_pricing(variant_b, input_price="0.50"),
            ),
            model_evidence=(
                make_model_evidence(variant_a, quality=0.95),
                make_model_evidence(variant_b, quality=0.80),
            ),
        )
        catalog = make_catalog(make_entry(variant_a), make_entry(variant_b))
        cost_first = ModelRouter().select(
            catalog=catalog,
            policy=make_policy(objectives=(cost_objective(), quality_objective())),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertEqual(cost_first.decision.selected_variant.variant_id, "variant-b")

        quality_first = ModelRouter().select(
            catalog=catalog,
            policy=make_policy(objectives=(quality_objective(), cost_objective())),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertEqual(quality_first.decision.selected_variant.variant_id, "variant-a")

    def test_stable_variant_identity_tie_break_breaks_ties(self) -> None:
        variant_b = make_variant("variant-b", make_contract("contract-b"))
        variant_a = make_variant("variant-a", make_contract("contract-a2"))
        evidence = make_evidence(
            (variant_a, variant_b),
            pricing=(
                make_pricing(variant_a, input_price="1.00"),
                make_pricing(variant_b, input_price="1.00"),
            ),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_b), make_entry(variant_a)),
            policy=make_policy(objectives=(cost_objective(),)),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(result.decision.selected_variant.variant_id, "variant-a")
        self.assertTrue(result.decision.tie_break_applied)

    def test_tie_break_uses_version_when_ids_match(self) -> None:
        contract = make_contract("contract-a")
        newer = make_variant("shared", contract, version="2")
        older = make_variant("shared", contract, version="10")
        evidence = make_evidence(
            (newer, older),
            pricing=(
                make_pricing(newer, input_price="1.00"),
                make_pricing(older, input_price="1.00"),
            ),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(newer), make_entry(older)),
            policy=make_policy(objectives=(cost_objective(),)),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertEqual(result.decision.selected_variant.version, "10")

    def test_missing_value_policy_orders_missing_candidates(self) -> None:
        variant_a, variant_b = self._variants()
        evidence = make_evidence(
            (variant_a, variant_b),
            pricing=(make_pricing(variant_b, input_price="3.00"),),
        )
        order_last = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a), make_entry(variant_b)),
            policy=make_policy(
                objectives=(
                    RoutingObjective(
                        dimension=ObjectiveDimension.COST,
                        direction=ObjectiveDirection.MINIMIZE,
                        missing_value=MissingValuePolicy.ORDER_LAST,
                    ),
                )
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertEqual(order_last.decision.selected_variant.variant_id, "variant-b")

        order_first = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a), make_entry(variant_b)),
            policy=make_policy(
                objectives=(
                    RoutingObjective(
                        dimension=ObjectiveDimension.COST,
                        direction=ObjectiveDirection.MINIMIZE,
                        missing_value=MissingValuePolicy.ORDER_FIRST,
                    ),
                )
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertEqual(order_first.decision.selected_variant.variant_id, "variant-a")

    def test_soft_evidence_gap_produces_warning_not_failure(self) -> None:
        variant_a, variant_b = self._variants()
        evidence = make_evidence(
            (variant_a, variant_b),
            pricing=(make_pricing(variant_b, input_price="3.00"),),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a), make_entry(variant_b)),
            policy=make_policy(objectives=(cost_objective(),)),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        warnings = {
            (warning.variant_id, warning.code, warning.dimension)
            for warning in result.warnings
        }
        self.assertIn(
            ("variant-a", WARNING_SOFT_EVIDENCE_MISSING, ObjectiveDimension.COST),
            warnings,
        )

    def test_stale_soft_evidence_produces_stale_warning(self) -> None:
        variant_a, variant_b = self._variants()
        evidence = make_evidence(
            (variant_a, variant_b),
            pricing=(
                make_pricing(variant_a, valid_until=AS_OF - timedelta(hours=2)),
                make_pricing(variant_b, input_price="3.00"),
            ),
        )
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a), make_entry(variant_b)),
            policy=make_policy(objectives=(cost_objective(),)),
            evidence=evidence,
            as_of=AS_OF,
        )
        warnings = {
            (warning.variant_id, warning.code) for warning in result.warnings
        }
        self.assertIn(("variant-a", WARNING_SOFT_EVIDENCE_STALE), warnings)


class DeterminismTests(unittest.TestCase):
    """相同输入 snapshot 产生完全相同的 Decision 与 reason trace。"""

    def test_replay_produces_identical_decision_and_trace(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        variant_b = make_variant("variant-b", make_contract("contract-b"))
        catalog = make_catalog(make_entry(variant_a), make_entry(variant_b))
        policy = make_policy(
            objectives=(cost_objective(), quality_objective()),
            hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("4.00")),
        )
        evidence = make_evidence(
            (variant_a, variant_b),
            pricing=(
                make_pricing(variant_a, input_price="1.50"),
                make_pricing(variant_b, input_price="2.50"),
            ),
            model_evidence=(
                make_model_evidence(variant_a, quality=0.92, latency_ms=900.0),
                make_model_evidence(variant_b, quality=0.97, latency_ms=400.0),
            ),
        )
        router = ModelRouter()
        first = router.select(
            catalog=catalog, policy=policy, evidence=evidence, as_of=AS_OF
        )
        second = router.select(
            catalog=catalog, policy=policy, evidence=evidence, as_of=AS_OF
        )
        self.assertEqual(first, second)
        self.assertEqual(first.decision, second.decision)
        self.assertEqual(first.decision.decision_id, second.decision.decision_id)
        self.assertEqual(first.candidate_evaluations, second.candidate_evaluations)

    def test_policy_digest_is_independent_of_frozenset_iteration_order(self) -> None:
        """frozenset 序列化必须排序：digest 跨 ``PYTHONHASHSEED`` 进程稳定。"""
        left = make_policy(
            deployment=DeploymentConstraints(
                allowed_providers=frozenset(
                    {"provider-c", "provider-a", "provider-b"}
                ),
                allowed_regions=frozenset({"region-2", "region-1"}),
                allowed_endpoint_classes=frozenset(
                    {"class-b", "class-a", "class-c"}
                ),
                allowed_retention_evidence_versions=frozenset(
                    {"retention-2026-02", "retention-2026-01"}
                ),
            ),
        )
        right = make_policy(
            deployment=DeploymentConstraints(
                allowed_providers=frozenset(
                    {"provider-b", "provider-c", "provider-a"}
                ),
                allowed_regions=frozenset({"region-1", "region-2"}),
                allowed_endpoint_classes=frozenset(
                    {"class-c", "class-a", "class-b"}
                ),
                allowed_retention_evidence_versions=frozenset(
                    {"retention-2026-01", "retention-2026-02"}
                ),
            ),
        )
        dumped = left.model_dump(mode="json")["deployment"]
        self.assertEqual(
            dumped["allowed_providers"],
            ["provider-a", "provider-b", "provider-c"],
        )
        self.assertEqual(dumped["allowed_regions"], ["region-1", "region-2"])
        self.assertEqual(
            dumped["allowed_endpoint_classes"],
            ["class-a", "class-b", "class-c"],
        )
        self.assertEqual(
            dumped["allowed_retention_evidence_versions"],
            ["retention-2026-01", "retention-2026-02"],
        )
        self.assertEqual(left.content_digest(), right.content_digest())

    def test_catalog_entry_order_does_not_change_selection(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        variant_b = make_variant("variant-b", make_contract("contract-b"))
        evidence = make_evidence(
            (variant_a, variant_b),
            pricing=(
                make_pricing(variant_a, input_price="1.00"),
                make_pricing(variant_b, input_price="2.00"),
            ),
        )
        policy = make_policy(objectives=(cost_objective(),))
        forward = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a), make_entry(variant_b)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        backward = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_b), make_entry(variant_a)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertEqual(
            forward.decision.selected_variant, backward.decision.selected_variant
        )
        self.assertEqual(_trace(forward), _trace(backward))


class DecisionEvidenceBindingTests(unittest.TestCase):
    """Decision 记录快照版本、有效期与 tie-break 证据。"""

    def test_decision_records_snapshot_versions_and_validity(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        policy = make_policy(
            objectives=(cost_objective(),),
            hard_gates=HardRoutingGates(
                max_input_price_per_mtok=Decimal("5.00"),
                require_availability=True,
            ),
        )
        evidence = make_evidence((variant_a,))
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        decision = result.decision
        self.assertEqual(decision.policy_identity, POLICY_IDENTITY)
        self.assertEqual(decision.catalog_id, "finance-catalog")
        self.assertEqual(decision.catalog_version, "7")
        kinds = {reference.kind for reference in decision.evidence_references}
        self.assertIn("PRICING", kinds)
        self.assertIn("AVAILABILITY", kinds)
        for reference in decision.evidence_references:
            self.assertTrue(reference.valid_until >= AS_OF)
            self.assertEqual(reference.variant_id, "variant-a")
        self.assertEqual(
            decision.selected_contract_fingerprint,
            variant_a.primary_contract().fingerprint,
        )

    def test_decision_is_immutable(self) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=make_policy(),
            evidence=make_evidence((variant_a,)),
            as_of=AS_OF,
        )
        with self.assertRaises(Exception):
            result.decision.selected_variant = variant_a  # type: ignore[misc]


class InvalidPolicyTests(unittest.TestCase):
    """规则不完整或不能形成确定顺序为 INVALID_POLICY。"""

    def test_duplicate_objective_dimensions_cannot_form_deterministic_order(
        self,
    ) -> None:
        variant_a = make_variant("variant-a", make_contract("contract-a"))
        policy = make_policy(objectives=(cost_objective(), cost_objective()))
        result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant_a)),
            policy=policy,
            evidence=make_evidence((variant_a,)),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.INVALID_POLICY)
        self.assertEqual(result.reason_code, REASON_AMBIGUOUS_OBJECTIVES)
        self.assertIsNone(result.decision)


class RouterBoundaryTests(unittest.TestCase):
    """Router 输入边界：不读取 prompt、Conversation History 或敏感内容。"""

    ROUTING_INPUT_MODELS = (
        AgentVariant,
        ModelCatalog,
        ModelCatalogEntry,
        DeploymentAttributes,
        DeploymentConstraints,
        RoutingPolicy,
        RoutingEvidence,
        ModelEvidenceSnapshot,
        PricingSnapshot,
        AvailabilitySnapshot,
        RetentionEvidenceSnapshot,
    )
    FORBIDDEN_FIELD_NAMES = frozenset(
        {"input", "prompt", "history", "messages", "message", "context"}
    )
    FORBIDDEN_FIELD_SUBSTRINGS = (
        "prompt",
        "history",
        "conversation",
        "credential",
        "api_key",
        "secret",
        "tenant",
        "endpoint_url",
        "context_item",
    )

    def test_routing_inputs_have_no_prompt_or_history_fields(self) -> None:
        for model in self.ROUTING_INPUT_MODELS:
            with self.subTest(model=model.__name__):
                for field in model.model_fields:
                    self.assertNotIn(field.lower(), self.FORBIDDEN_FIELD_NAMES)
                    for substring in self.FORBIDDEN_FIELD_SUBSTRINGS:
                        self.assertNotIn(
                            substring,
                            field.lower(),
                            f"{model.__name__}.{field} 疑似敏感输入字段",
                        )

    def test_select_signature_only_takes_public_snapshots(self) -> None:
        signature = inspect.signature(ModelRouter.select)
        self.assertEqual(
            set(signature.parameters),
            {"self", "catalog", "policy", "evidence", "as_of"},
        )

    def test_router_lives_in_runtime_companion_layer(self) -> None:
        from m_agent.testing import find_runtime_dependency_violations

        self.assertEqual(find_runtime_dependency_violations(), ())
        import m_agent.companion.routing as routing_module
        import m_agent.runtime as runtime_module

        self.assertEqual(routing_module.__name__, "m_agent.companion.routing")
        self.assertNotIn("ModelRouter", getattr(runtime_module, "__all__", ()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
