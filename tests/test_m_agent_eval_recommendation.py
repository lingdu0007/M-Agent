"""Ticket 18 AC 8：Model Recommendation 是只读引用证据与目标的版本化记录。

Recommendation 的持久化幂等/冲突由共享 EvalStore 契约覆盖；本套件
聚焦模型层只读引用语义：

- Recommendation 冻结 gate 结论、置信度与目标精确身份（Policy / Variant），
  引用 Report revision digest 与可选 Baseline，不携带任何写接口；
- 它不自动修改 Baseline、Definition、Catalog 或 routing——本类型不
  暴露任何会触发生效动作的方法；
- 目标 kind 是稳定常量（ROUTING_POLICY / AGENT_VARIANT），不做隐式
  解析或行为触发；
- 记录一经构造即不可变（frozen），version 与 evidence_digest 必填。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from m_agent.companion.eval import (
    ModelRecommendationRecord,
    RECOMMENDATION_TARGET_AGENT_VARIANT,
    RECOMMENDATION_TARGET_ROUTING_POLICY,
    RecommendationTarget,
)
from m_agent.companion.eval import EvaluatorOutcome


def _target(kind: str = RECOMMENDATION_TARGET_AGENT_VARIANT) -> RecommendationTarget:
    return RecommendationTarget(
        kind=kind, target_id="variant-a", target_version="1.0",
    )


def _recommendation(**overrides) -> ModelRecommendationRecord:
    values: dict = dict(
        recommendation_id="rec-1",
        version="1.0",
        report_id="report-1",
        report_revision=1,
        baseline_id="baseline-1",
        target=_target(),
        hard_gate=EvaluatorOutcome.PASS,
        quality_gate=EvaluatorOutcome.PASS,
        overall_outcome=EvaluatorOutcome.PASS,
        confidence=0.9,
        evidence_digest="e" * 64,
    )
    values.update(overrides)
    return ModelRecommendationRecord(**values)


class RecommendationReadonlyReferenceTests(unittest.TestCase):
    """Recommendation 是只读引用证据与目标的版本化记录。"""

    def test_recommendation_is_frozen_immutable(self) -> None:
        rec = _recommendation()
        self.assertTrue(rec.model_config.get("frozen"))
        # pydantic model_copy 绕过验证：但 frozen 记录本身永不就地变更，
        # 修改只出现在返回的副本上，原记录保持冻结。
        clone = rec.model_copy(update={"confidence": 0.5})
        self.assertEqual(clone.confidence, 0.5)
        self.assertEqual(rec.confidence, 0.9)
        # 通过重新构造触发验证：非法 confidence 必须被拒。
        with self.assertRaises(ValidationError):
            _recommendation(confidence=1.5)

    def test_recommendation_freezes_gate_conclusions_and_target(self) -> None:
        rec = _recommendation(
            hard_gate=EvaluatorOutcome.FAIL,
            quality_gate=EvaluatorOutcome.PASS,
            overall_outcome=EvaluatorOutcome.FAIL,
            confidence=0.42,
        )
        self.assertEqual(rec.hard_gate, EvaluatorOutcome.FAIL)
        self.assertEqual(rec.quality_gate, EvaluatorOutcome.PASS)
        self.assertEqual(rec.overall_outcome, EvaluatorOutcome.FAIL)
        self.assertEqual(rec.confidence, 0.42)
        self.assertEqual(rec.target.target_id, "variant-a")
        self.assertEqual(rec.target.target_version, "1.0")

    def test_recommendation_references_report_revision_and_evidence_digest(self) -> None:
        rec = _recommendation(report_id="report-7", report_revision=3)
        self.assertEqual(rec.report_id, "report-7")
        self.assertEqual(rec.report_revision, 3)
        self.assertEqual(rec.evidence_digest, "e" * 64)
        self.assertEqual(rec.baseline_id, "baseline-1")

    def test_target_kinds_are_stable_constants(self) -> None:
        self.assertEqual(
            RECOMMENDATION_TARGET_ROUTING_POLICY, "ROUTING_POLICY"
        )
        self.assertEqual(
            RECOMMENDATION_TARGET_AGENT_VARIANT, "AGENT_VARIANT"
        )
        routing = _target(RECOMMENDATION_TARGET_ROUTING_POLICY)
        variant = _target(RECOMMENDATION_TARGET_AGENT_VARIANT)
        self.assertEqual(routing.kind, "ROUTING_POLICY")
        self.assertEqual(variant.kind, "AGENT_VARIANT")

    def test_baseline_reference_is_optional(self) -> None:
        without_baseline = _recommendation(baseline_id=None)
        self.assertIsNone(without_baseline.baseline_id)
        with_baseline = _recommendation(baseline_id="baseline-2")
        self.assertEqual(with_baseline.baseline_id, "baseline-2")

    def test_exposes_no_write_or_activation_interface(self) -> None:
        rec = _recommendation()
        # 公开方法只来自 BaseModel（model_dump 等）；不存在任何会改动
        # Baseline / Definition / Catalog / routing 的业务方法。
        # update_forward_refs / update_json_schema 是 Pydantic 框架
        # 自身的类方法，不属于业务写接口，需要排除。
        pydantic_internal = {
            "update_forward_refs", "update_json_schema",
        }
        write_methods = {
            name for name in dir(rec)
            if name.startswith(("apply", "activate", "deploy", "promote",
                               "update", "set_", "mutate", "persist"))
            and not name.startswith("_")
            and name not in pydantic_internal
        }
        self.assertEqual(write_methods, set())

    def test_validation_rejects_invalid_confidence_and_blank_ids(self) -> None:
        with self.assertRaises(ValidationError):
            _recommendation(confidence=1.5)
        with self.assertRaises(ValidationError):
            _recommendation(confidence=-0.1)
        with self.assertRaises(ValidationError):
            _recommendation(recommendation_id="")
        with self.assertRaises(ValidationError):
            _recommendation(evidence_digest="")
        with self.assertRaises(ValidationError):
            RecommendationTarget(
                kind="", target_id="", target_version="",
            )

    def test_valid_until_is_optional(self) -> None:
        rec = _recommendation(valid_until=None)
        self.assertIsNone(rec.valid_until)
        future = datetime(2030, 1, 1, tzinfo=timezone.utc)
        expiring = _recommendation(valid_until=future)
        self.assertEqual(expiring.valid_until, future)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
