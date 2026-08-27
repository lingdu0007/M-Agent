"""Eval Recommendation 的显式发布（ADR 0041，Ticket 20）。

Runtime Companion 绝不自动采纳 Recommendation：发布是上层应用的
显式决策。发布产生新的 Routing Policy 版本（候选范围收窄到被
推荐 Variant）并要求为其显式重新注册 Variant revision——旧 Policy
版本、旧 Run、旧 Decision 与 Baseline 全部保持不变，只有之后的
路由才会看到新版本。未发布的 Recommendation 对 Router 完全不可见。
"""

from __future__ import annotations

from datetime import datetime

from ._catalog import AgentVariant
from ._policy import (
    RecommendationPublication,
    RoutingPolicy,
    RoutingPolicyIdentity,
)
from ._router import RoutingError

__all__ = [
    "PublicationError",
    "publish_recommendation_as_policy",
    "register_variant_for_policy",
]


class PublicationError(RoutingError):
    """显式发布的前置条件不满足。"""


def publish_recommendation_as_policy(
    publication: RecommendationPublication,
    *,
    base_policy: RoutingPolicy,
    new_version: str,
    as_of: datetime,
) -> RoutingPolicy:
    """把显式发布的 Recommendation 桥接为新的 Routing Policy 版本。

    前置条件（fail closed）：两条 gate 结论都必须通过、发布时刻
    Recommendation 仍在有效期内、新版本号必须不同于基础版本。产生
    的新 Policy 携带 published_from 记录、候选范围收窄到目标
    Variant；base_policy 本身绝不变化（版本化，不覆盖）。
    """
    if not publication.hard_gate_passed:
        raise PublicationError(
            "cannot publish a recommendation whose hard gate did not pass"
        )
    if not publication.quality_gate_passed:
        raise PublicationError(
            "cannot publish a recommendation whose quality gate did not pass"
        )
    if publication.valid_until < as_of:
        raise PublicationError(
            "cannot publish an expired recommendation"
        )
    if new_version == base_policy.identity.version:
        raise PublicationError(
            "published policy must be a new version, never an overwrite"
        )


    return RoutingPolicy(
        identity=RoutingPolicyIdentity(
            policy_id=base_policy.identity.policy_id,
            version=new_version,
        ),
        model_requirements=base_policy.model_requirements,
        deployment=base_policy.deployment,
        hard_gates=base_policy.hard_gates,
        objectives=base_policy.objectives,
        soft_preferences=base_policy.soft_preferences,
        allowed_variants=(
            (
                publication.target_variant_id,
                publication.target_variant_version,
            ),
        ),
        published_from=publication,
    )


def register_variant_for_policy(
    variant: AgentVariant,
    policy_identity: RoutingPolicyIdentity,
    *,
    new_version: str,
) -> AgentVariant:
    """为新的 Policy 身份显式注册 Variant revision。

    发布新 Policy 版本后，应用必须显式注册参与该版本的 Variant
    revision：新 Variant 版本携带原 policy_identities 加上新身份，
    原 Variant 声明保持不变。
    """
    if new_version == variant.version:
        raise PublicationError(
            "registered revision must be a new variant version"
        )
    if policy_identity in variant.policy_identities:
        raise PublicationError(
            "variant is already registered for this policy identity"
        )
    return AgentVariant(
        variant_id=variant.variant_id,
        version=new_version,
        definition_id=variant.definition_id,
        definition_version=variant.definition_version,
        model_bindings=variant.model_bindings,
        model_execution_budget=variant.model_execution_budget,
        policy_identities=variant.policy_identities + (policy_identity,),
    )
