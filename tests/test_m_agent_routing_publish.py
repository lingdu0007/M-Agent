"""Ticket 20: Eval Recommendation 显式发布（explicit promotion）契约测试。

Runtime Companion 绝不自动采纳 Recommendation：
- 发布是上层应用的显式决策，前置条件 fail closed（两条 gate 结论、
  有效期、新版本号）；
- 发布只产生新的 Routing Policy 版本并收窄候选范围到目标 Variant；
  base policy、旧 Run、旧 Decision 与 Baseline 一律不变；
- 新 Policy 版本要求为其显式注册 Variant revision，且只有之后的
  路由才能看到新版本。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from m_agent.companion.routing import (
    ModelRouter,
    PublicationError,
    RecommendationPublication,
    RoutingPolicyIdentity,
    publish_recommendation_as_policy,
    register_variant_for_policy,
)

from routing_fixtures import (
    AS_OF,
    make_catalog,
    make_contract,
    make_evidence,
    make_entry,
    make_policy,
    make_variant,
)

BASE_IDENTITY = RoutingPolicyIdentity(
    policy_id="finance-default", version="3"
)
PUBLISHED_IDENTITY = RoutingPolicyIdentity(
    policy_id="finance-default", version="5"
)


def _publication(
    *,
    hard_gate: bool = True,
    quality_gate: bool = True,
    valid_until: datetime | None = None,
    target_version: str = "1",
) -> RecommendationPublication:
    return RecommendationPublication(
        recommendation_id="rec-001",
        recommendation_version="1",
        report_id="report-001",
        report_revision=2,
        evidence_digest="sha256:" + "a" * 64,
        target_variant_id="variant-economy",
        target_variant_version=target_version,
        hard_gate_passed=hard_gate,
        quality_gate_passed=quality_gate,
        confidence=0.87,
        valid_until=valid_until or (AS_OF + timedelta(days=1)),
    )


def _variants():
    economy = make_variant(
        "variant-economy", make_contract("contract-economy")
    )
    premium = make_variant(
        "variant-premium", make_contract("contract-premium")
    )
    return economy, premium


class PublicationPreconditionTests(unittest.TestCase):
    """发布的 fail-closed 前置条件。"""

    def test_publishing_creates_a_new_version_scoped_to_the_target(
        self,
    ) -> None:
        policy = make_policy()
        published = publish_recommendation_as_policy(
            _publication(),
            base_policy=policy,
            new_version="5",
            as_of=AS_OF,
        )
        self.assertEqual(published.identity, PUBLISHED_IDENTITY)
        self.assertEqual(
            published.allowed_variants, (("variant-economy", "1"),)
        )
        self.assertEqual(published.published_from, _publication())
        # base policy 绝不被覆盖：身份与候选范围原样保留。
        self.assertEqual(policy.identity, BASE_IDENTITY)
        self.assertIsNone(policy.allowed_variants)
        self.assertIsNone(policy.published_from)

    def test_hard_gate_failure_cannot_publish(self) -> None:
        with self.assertRaises(PublicationError):
            publish_recommendation_as_policy(
                _publication(hard_gate=False),
                base_policy=make_policy(),
                new_version="5",
                as_of=AS_OF,
            )

    def test_quality_gate_failure_cannot_publish(self) -> None:
        with self.assertRaises(PublicationError):
            publish_recommendation_as_policy(
                _publication(quality_gate=False),
                base_policy=make_policy(),
                new_version="5",
                as_of=AS_OF,
            )

    def test_expired_recommendation_cannot_publish(self) -> None:
        with self.assertRaises(PublicationError):
            publish_recommendation_as_policy(
                _publication(valid_until=AS_OF - timedelta(seconds=1)),
                base_policy=make_policy(),
                new_version="5",
                as_of=AS_OF,
            )

    def test_publishing_requires_a_new_version(self) -> None:
        with self.assertRaises(PublicationError):
            publish_recommendation_as_policy(
                _publication(),
                base_policy=make_policy(),
                new_version="3",
                as_of=AS_OF,
            )


class VariantRevisionRegistrationTests(unittest.TestCase):
    """新 Policy 版本要求显式注册 Variant revision。"""

    def test_registration_extends_policy_identities(self) -> None:
        economy, _ = _variants()
        revision = register_variant_for_policy(
            economy, PUBLISHED_IDENTITY, new_version="2"
        )
        self.assertEqual(revision.version, "2")
        self.assertEqual(
            revision.policy_identities, (BASE_IDENTITY, PUBLISHED_IDENTITY)
        )
        # 原 Variant 声明保持不变。
        self.assertEqual(economy.version, "1")
        self.assertEqual(economy.policy_identities, (BASE_IDENTITY,))

    def test_registration_requires_a_new_variant_version(self) -> None:
        economy, _ = _variants()
        with self.assertRaises(PublicationError):
            register_variant_for_policy(
                economy, PUBLISHED_IDENTITY, new_version="1"
            )

    def test_registration_rejects_duplicate_identities(self) -> None:
        economy, _ = _variants()
        with self.assertRaises(PublicationError):
            register_variant_for_policy(
                economy, BASE_IDENTITY, new_version="2"
            )


class ExplicitPromotionRoutingTests(unittest.TestCase):
    """显式 promotion 只影响之后的路由，历史不变。"""

    def test_published_policy_only_affects_future_routing(self) -> None:
        economy, premium = _variants()
        base_policy = make_policy()
        # 显式 promotion 的两步：先为新 Policy 身份注册 Variant
        # revision（同一冻结内容、新版本、携带新旧两个身份），再发布
        # 以该 revision 为目标的新 Policy 版本。
        economy_revision = register_variant_for_policy(
            economy, PUBLISHED_IDENTITY, new_version="2"
        )
        premium_revision = register_variant_for_policy(
            premium, PUBLISHED_IDENTITY, new_version="2"
        )
        publication = _publication(target_version="2")
        published = publish_recommendation_as_policy(
            publication,
            base_policy=base_policy,
            new_version="5",
            as_of=AS_OF,
        )
        catalog = make_catalog(
            make_entry(economy_revision), make_entry(premium_revision)
        )
        evidence = make_evidence((economy_revision, premium_revision))

        new_result = ModelRouter().select(
            catalog=catalog,
            policy=published,
            evidence=evidence,
            as_of=AS_OF,
        )
        # 新版本路由只看被推荐 Variant；另一个候选因候选范围被收窄出局。
        self.assertIsNotNone(new_result.decision)
        assert new_result.decision is not None
        self.assertEqual(
            new_result.decision.selected_variant.variant_id,
            "variant-economy",
        )
        self.assertEqual(
            new_result.decision.selected_variant.version, "2"
        )
        rejected = [
            evaluation
            for evaluation in new_result.decision.candidate_evaluations
            if evaluation.variant_id == "variant-premium"
        ]
        self.assertEqual(len(rejected), 1)
        assert rejected
        self.assertEqual(
            rejected[0].reason_code, "NOT_IN_CANDIDATE_SCOPE"
        )

        # 旧 Policy 版本继续按原语义路由：两个候选都在范围内，
        # 历史 Decision / Baseline 语义不被新版本改写。
        old_result = ModelRouter().select(
            catalog=catalog,
            policy=base_policy,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIsNotNone(old_result.decision)
        assert old_result.decision is not None
        self.assertNotEqual(
            old_result.decision.decision_id,
            new_result.decision.decision_id,
        )
        considered = {
            evaluation.variant_id
            for evaluation in old_result.decision.candidate_evaluations
            if evaluation.reason_code == "OUTRANKED"
        }
        self.assertIn("variant-premium", considered)

    def test_unpublished_policy_identity_is_invisible_to_the_router(
        self,
    ) -> None:
        """未发布/未注册的 Policy 版本对 Router 不可见。"""
        economy, premium = _variants()
        catalog = make_catalog(make_entry(economy), make_entry(premium))
        evidence = make_evidence((economy, premium))
        unpublished = make_policy(identity=PUBLISHED_IDENTITY)
        result = ModelRouter().select(
            catalog=catalog,
            policy=unpublished,
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIsNone(result.decision)
        reasons = {
            evaluation.reason_code
            for evaluation in result.candidate_evaluations
        }
        self.assertIn("NOT_REGISTERED_FOR_POLICY", reasons)


if __name__ == "__main__":
    unittest.main()
