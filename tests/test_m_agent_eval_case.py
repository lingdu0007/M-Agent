"""Ticket 17 AC 1：Eval Case/Suite 冻结与确定性展开。

Eval Case 必须冻结 input、Conversation History、Agent Variant、
content-addressed Fixture Bundle、Execution Protocol、Evaluator versions
与 tags——任何冻结字段缺失或非法都在构造时确定性失败。Eval Suite 把
Case × Variant × repetition 展开为有序、身份稳定的独立单元；Case 声明
的冻结 Variant 不在 Suite 变体集合内时展开 fail-closed。
"""

from __future__ import annotations

import unittest
from typing import Any

from pydantic import ValidationError

from m_agent.companion.eval import (
    AgentVariant,
    EvalCase,
    EvalSuite,
    EvalSuiteError,
    EvaluatorRef,
    ExecutionProtocol,
    FixtureBundle,
    FixtureFact,
)
from m_agent.runtime import ConversationMessage, ConversationRole

_VARIANT_A = AgentVariant(
    variant_id="variant-a",
    definition_id="assistant",
    definition_version="1.0",
)
_VARIANT_B = AgentVariant(
    variant_id="variant-b",
    definition_id="assistant",
    definition_version="2.0",
)


def _bundle(bundle_id: str = "bundle-1") -> FixtureBundle:
    return FixtureBundle.build(
        bundle_id=bundle_id,
        facts=(FixtureFact(fact_id="order", payload='{"status":"shipped"}'),),
        declared_external_effects=("ledger_write",),
        expected_evidence_ids=("ledger",),
    )


def _case(case_id: str = "case-1", **overrides) -> EvalCase:
    values: dict[str, Any] = dict(
        case_id=case_id,
        input="please check the order",
        history=(
            ConversationMessage(role=ConversationRole.USER, content="earlier"),
        ),
        variant=_VARIANT_A,
        fixture_bundle=_bundle(),
        execution_protocol=ExecutionProtocol(deterministic=True),
        evaluators=(EvaluatorRef(evaluator_id="output-match", version="1.0"),),
        tags=("regression", "orders"),
    )
    values.update(overrides)
    return EvalCase(**values)


class FixtureBundleTests(unittest.TestCase):
    """Fixture Bundle 的 content addressing 契约。"""

    def test_bundle_is_content_addressed(self) -> None:
        first = _bundle()
        second = _bundle()
        self.assertEqual(first.digest, second.digest)
        self.assertRegex(first.digest, r"^[0-9a-f]{64}$")

    def test_bundle_digest_changes_with_content(self) -> None:
        altered = FixtureBundle.build(
            bundle_id="bundle-1",
            facts=(
                FixtureFact(fact_id="order", payload='{"status":"refunded"}'),
            ),
            declared_external_effects=("ledger_write",),
            expected_evidence_ids=("ledger",),
        )
        self.assertNotEqual(_bundle().digest, altered.digest)

    def test_bundle_rejects_tampered_digest(self) -> None:
        with self.assertRaises(ValidationError):
            FixtureBundle(
                bundle_id="bundle-1",
                facts=(FixtureFact(fact_id="order", payload="{}"),),
                declared_external_effects=(),
                expected_evidence_ids=(),
                digest="0" * 64,
            )


class EvalCaseFreezeTests(unittest.TestCase):
    """AC 1：Case 冻结字段齐全；缺失/非法即构造失败。"""

    def test_case_freezes_every_declared_field(self) -> None:
        case = _case()
        self.assertEqual(case.input, "please check the order")
        self.assertEqual(len(case.history), 1)
        self.assertEqual(case.history[0].role, ConversationRole.USER)
        self.assertEqual(case.variant, _VARIANT_A)
        self.assertEqual(case.fixture_bundle.digest, _bundle().digest)
        self.assertTrue(case.execution_protocol.deterministic)
        self.assertEqual(
            case.evaluators,
            (EvaluatorRef(evaluator_id="output-match", version="1.0"),),
        )
        self.assertEqual(case.tags, ("regression", "orders"))

    def test_case_rejects_blank_identity(self) -> None:
        with self.assertRaises(ValidationError):
            _case(case_id="   ")

    def test_case_rejects_missing_evaluator_versions(self) -> None:
        with self.assertRaises(ValidationError):
            _case(evaluators=())

    def test_case_rejects_blank_variant_fields(self) -> None:
        with self.assertRaises(ValidationError):
            _case(
                variant=AgentVariant(
                    variant_id="",
                    definition_id="assistant",
                    definition_version="1.0",
                )
            )

    def test_case_rejects_missing_fixture_bundle(self) -> None:
        with self.assertRaises(ValidationError):
            _case(fixture_bundle=None)  # type: ignore[arg-type]

    def test_deterministic_protocol_forces_single_repetition(self) -> None:
        with self.assertRaises(ValidationError):
            _case(
                execution_protocol=ExecutionProtocol(
                    deterministic=True, repetitions=3
                )
            )

    def test_nondeterministic_protocol_requires_seed_and_repetitions(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            ExecutionProtocol(deterministic=False, repetitions=3, seed=None)
        with self.assertRaises(ValidationError):
            ExecutionProtocol(deterministic=False, repetitions=0, seed="s-1")
        protocol = ExecutionProtocol(
            deterministic=False, repetitions=3, seed="s-1"
        )
        self.assertEqual(protocol.repetitions, 3)


class EvalSuiteExpansionTests(unittest.TestCase):
    """AC 1：Suite 确定性展开 Case × Variant × repetition。"""

    def _suite(self, **overrides) -> EvalSuite:
        values: dict[str, Any] = dict(
            suite_id="suite-1",
            version="1.0",
            cases=(
                _case(case_id="case-1"),
                _case(
                    case_id="case-2",
                    execution_protocol=ExecutionProtocol(
                        deterministic=False, repetitions=3, seed="seed-2"
                    ),
                ),
            ),
            variants=(_VARIANT_A, _VARIANT_B),
        )
        values.update(overrides)
        return EvalSuite(**values)

    def test_expansion_is_deterministic_and_ordered(self) -> None:
        suite = self._suite()
        first = suite.expand()
        second = suite.expand()
        self.assertEqual(first, second)
        # case-1（确定性 1 次）+ case-2（3 次）× 2 variants = 8 items。
        self.assertEqual(len(first), 8)
        # 顺序固定：case -> variant -> repetition。
        self.assertEqual(
            [(item.case_id, item.variant.variant_id) for item in first],
            [
                ("case-1", "variant-a"),
                ("case-1", "variant-b"),
                ("case-2", "variant-a"),
                ("case-2", "variant-a"),
                ("case-2", "variant-a"),
                ("case-2", "variant-b"),
                ("case-2", "variant-b"),
                ("case-2", "variant-b"),
            ],
        )
        self.assertEqual(
            [item.repetition_index for item in first],
            [0, 0, 0, 1, 2, 0, 1, 2],
        )

    def test_item_identity_is_stable_and_unique(self) -> None:
        items = self._suite().expand()
        ids = [item.item_id for item in items]
        self.assertEqual(len(ids), len(set(ids)))
        # 同一 Suite 身份重新展开得到同一批 item identity。
        self.assertEqual(ids, [i.item_id for i in self._suite().expand()])
        # Suite 版本变化 => item identity 变化。
        other = self._suite(version="1.1").expand()
        self.assertNotEqual(set(ids), {i.item_id for i in other})

    def test_suite_rejects_duplicate_cases_and_variants(self) -> None:
        with self.assertRaises(ValidationError):
            EvalSuite(
                suite_id="suite-1",
                version="1.0",
                cases=(_case(), _case()),
                variants=(_VARIANT_A,),
            )
        with self.assertRaises(ValidationError):
            EvalSuite(
                suite_id="suite-1",
                version="1.0",
                cases=(_case(),),
                variants=(_VARIANT_A, _VARIANT_A),
            )

    def test_expansion_fails_closed_when_case_variant_missing(self) -> None:
        suite = EvalSuite(
            suite_id="suite-1",
            version="1.0",
            cases=(_case(),),
            variants=(_VARIANT_B,),
        )
        with self.assertRaises(EvalSuiteError):
            suite.expand()

    def test_suite_content_digest_is_stable(self) -> None:
        self.assertEqual(
            self._suite().content_digest(), self._suite().content_digest()
        )
        self.assertNotEqual(
            self._suite().content_digest(),
            self._suite(version="1.1").content_digest(),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
