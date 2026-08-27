"""Ticket 17 AC 5 / AC 7：最小授权 Projection 与确定性 Evaluator。

Evaluator 显式声明 Evidence Requirements；Projection 只包含所需且
获授权的字段，Evaluator 无法绕过 Projection 读取未授权 payload；
证据缺失或未授权返回 INCONCLUSIVE（而非「问题不存在」）；
结构化 result/reason code 区分 subject / evaluator / evidence failure，
任何单一质量分数不得覆盖 hard outcome。
"""

from __future__ import annotations

import unittest
from typing import Any

from m_agent.companion.eval import (
    REASON_EVALUATOR_RAISED,
    REASON_EVIDENCE_MISSING,
    REASON_EVIDENCE_UNAUTHORIZED,
    REASON_SUBJECT_OUTPUT_MISMATCH,
    EvidenceArtifact,
    EvidenceField,
    EvidenceRequirements,
    EvidenceCompleteness,
    EvalFailureKind,
    EvalMode,
    EvalObservation,
    EvaluatorOutcome,
    EvaluatorRef,
    ObservationProjectionPolicy,
    OutputMatchesEvaluator,
    aggregate_evaluator_results,
    evidence_digest,
    project_observation,
    run_evaluator,
)
from m_agent.runtime import RunStatus


def _observation(**overrides) -> EvalObservation:
    values: dict[str, Any] = dict(
        observation_id="obs-1",
        mode=EvalMode.OBSERVE,
        subject_run_id="run-a",
        definition_id="assistant",
        definition_version="1.0",
        completeness=EvidenceCompleteness.COMPLETE,
        reason_code="SUBJECT_TERMINAL",
        run_status=RunStatus.SUCCEEDED,
        run_input="check the order",
        run_output='{"status": "shipped"}',
    )
    values.update(overrides)
    return EvalObservation(**values)


def _requirements(*fields, evaluator_id="output-match") -> EvidenceRequirements:
    return EvidenceRequirements(
        evaluator=EvaluatorRef(evaluator_id=evaluator_id, version="1.0"),
        fields=frozenset(fields),
    )


def _policy(*fields, artifacts=()) -> ObservationProjectionPolicy:
    return ObservationProjectionPolicy(
        policy_id="policy-1",
        version="1.0",
        allowed_fields=frozenset(fields),
        authorized_evidence_ids=frozenset(artifacts),
    )


class _SpyEvaluator:
    """记录是否被实际调用的Evaluator（负例：缺失/未授权不得调用）。"""

    def __init__(self) -> None:
        self.invoked = False
        self.requirements = _requirements(EvidenceField.RUN_OUTPUT)

    @property
    def identity(self) -> EvaluatorRef:
        return EvaluatorRef(evaluator_id="spy", version="1.0")

    @property
    def hard(self) -> bool:
        return False

    @property
    def evidence_requirements(self) -> EvidenceRequirements:
        return self.requirements

    def evaluate(self, projection):  # noqa: ANN001
        self.invoked = True
        from m_agent.companion.eval import EvaluatorResult

        return EvaluatorResult(
            evaluator=self.identity,
            outcome=EvaluatorOutcome.PASS,
            failure_kind=EvalFailureKind.NONE,
            reason_code="SPY_PASS",
        )


class ProjectionAuthorizationTests(unittest.TestCase):
    """AC 5：Projection 只交付所需且获授权的字段。"""

    def test_projection_delivers_required_and_authorized_fields(self) -> None:
        observation = _observation()
        projection = project_observation(
            observation,
            _requirements(EvidenceField.RUN_STATUS, EvidenceField.RUN_OUTPUT),
            _policy(EvidenceField.RUN_STATUS, EvidenceField.RUN_OUTPUT),
        )
        self.assertEqual(
            projection.delivered,
            frozenset({EvidenceField.RUN_STATUS, EvidenceField.RUN_OUTPUT}),
        )
        self.assertEqual(projection.denied, frozenset())
        self.assertEqual(projection.unavailable, frozenset())
        self.assertEqual(
            projection.get(EvidenceField.RUN_OUTPUT), '{"status": "shipped"}'
        )
        self.assertEqual(
            projection.get(EvidenceField.RUN_STATUS), RunStatus.SUCCEEDED
        )

    def test_unauthorized_required_field_is_denied_not_leaked(self) -> None:
        observation = _observation()
        projection = project_observation(
            observation,
            _requirements(EvidenceField.RUN_STATUS, EvidenceField.RUN_OUTPUT),
            _policy(EvidenceField.RUN_STATUS),
        )
        self.assertEqual(
            projection.delivered, frozenset({EvidenceField.RUN_STATUS})
        )
        self.assertEqual(
            projection.denied, frozenset({EvidenceField.RUN_OUTPUT})
        )
        # Evaluator 无法绕过 Projection：未授权字段的值根本不在投影里。
        self.assertNotIn(EvidenceField.RUN_OUTPUT, projection.values)
        self.assertIsNone(projection.get(EvidenceField.RUN_OUTPUT))
        dumped = projection.model_dump_json()
        self.assertNotIn('{"status": "shipped"}', dumped)

    def test_authorized_but_absent_field_is_unavailable(self) -> None:
        observation = _observation(run_output=None)
        projection = project_observation(
            observation,
            _requirements(EvidenceField.RUN_OUTPUT),
            _policy(EvidenceField.RUN_OUTPUT),
        )
        self.assertEqual(projection.delivered, frozenset())
        self.assertEqual(projection.unavailable, frozenset({EvidenceField.RUN_OUTPUT}))
        self.assertEqual(projection.denied, frozenset())

    def test_projection_never_exposes_raw_observation(self) -> None:
        projection = project_observation(
            _observation(),
            _requirements(EvidenceField.RUN_STATUS),
            _policy(EvidenceField.RUN_STATUS),
        )
        self.assertFalse(hasattr(projection, "observation"))
        self.assertFalse(hasattr(projection, "run_store"))


class EvaluatorEvidenceFailureTests(unittest.TestCase):
    """AC 5：证据缺失/未授权 => INCONCLUSIVE，而非问题不存在。"""

    def test_unauthorized_evidence_is_inconclusive_not_fail(self) -> None:
        spy = _SpyEvaluator()
        projection = project_observation(
            _observation(),
            spy.evidence_requirements,
            _policy(EvidenceField.RUN_STATUS),
        )
        result = run_evaluator(spy, projection)
        self.assertEqual(result.outcome, EvaluatorOutcome.INCONCLUSIVE)
        self.assertEqual(result.failure_kind, EvalFailureKind.EVIDENCE)
        self.assertEqual(result.reason_code, REASON_EVIDENCE_UNAUTHORIZED)
        # 未授权时不得调用 Evaluator 本体。
        self.assertFalse(spy.invoked)
        # 明确不是 subject failure：INCONCLUSIVE 不等于「问题不存在」。
        self.assertNotEqual(result.outcome, EvaluatorOutcome.FAIL)

    def test_missing_evidence_is_inconclusive_not_fail(self) -> None:
        spy = _SpyEvaluator()
        projection = project_observation(
            _observation(run_output=None),
            spy.evidence_requirements,
            _policy(EvidenceField.RUN_OUTPUT),
        )
        result = run_evaluator(spy, projection)
        self.assertEqual(result.outcome, EvaluatorOutcome.INCONCLUSIVE)
        self.assertEqual(result.failure_kind, EvalFailureKind.EVIDENCE)
        self.assertEqual(result.reason_code, REASON_EVIDENCE_MISSING)
        self.assertFalse(spy.invoked)


class _RaisingEvaluator(_SpyEvaluator):
    """evaluate() 抛异常：必须归一为 evaluator failure，而非传播。"""

    def evaluate(self, projection):  # noqa: ANN001
        self.invoked = True
        raise RuntimeError("evaluator bug")


class EvaluatorFailureKindTests(unittest.TestCase):
    """AC 7：subject / evaluator / evidence failure 的结构化区分。"""

    def test_subject_failure_uses_subject_reason_code(self) -> None:
        evaluator = OutputMatchesEvaluator(
            evaluator_id="output-match",
            version="1.0",
            expected='{"status": "refunded"}',
        )
        projection = project_observation(
            _observation(),
            evaluator.evidence_requirements,
            _policy(EvidenceField.RUN_OUTPUT),
        )
        result = run_evaluator(evaluator, projection)
        self.assertEqual(result.outcome, EvaluatorOutcome.FAIL)
        self.assertEqual(result.failure_kind, EvalFailureKind.SUBJECT)
        self.assertEqual(result.reason_code, REASON_SUBJECT_OUTPUT_MISMATCH)

    def test_subject_pass_keeps_subject_kind_clean(self) -> None:
        evaluator = OutputMatchesEvaluator(
            evaluator_id="output-match",
            version="1.0",
            expected='{"status": "shipped"}',
        )
        projection = project_observation(
            _observation(),
            evaluator.evidence_requirements,
            _policy(EvidenceField.RUN_OUTPUT),
        )
        result = run_evaluator(evaluator, projection)
        self.assertEqual(result.outcome, EvaluatorOutcome.PASS)
        self.assertEqual(result.failure_kind, EvalFailureKind.NONE)

    def test_evaluator_exception_becomes_evaluator_failure(self) -> None:
        evaluator = _RaisingEvaluator()
        projection = project_observation(
            _observation(),
            evaluator.evidence_requirements,
            _policy(EvidenceField.RUN_OUTPUT),
        )
        result = run_evaluator(evaluator, projection)
        self.assertEqual(result.outcome, EvaluatorOutcome.ERROR)
        self.assertEqual(result.failure_kind, EvalFailureKind.EVALUATOR)
        self.assertEqual(result.reason_code, REASON_EVALUATOR_RAISED)
        self.assertTrue(evaluator.invoked)

    def test_unsupported_observation_short_circuits_without_invocation(self) -> None:
        spy = _SpyEvaluator()
        projection = project_observation(
            _observation(
                completeness=EvidenceCompleteness.UNSUPPORTED,
                reason_code="MODEL_CAPABILITIES_MISSING",
                run_output=None,
            ),
            spy.evidence_requirements,
            _policy(EvidenceField.RUN_OUTPUT),
        )
        result = run_evaluator(spy, projection)
        self.assertEqual(result.outcome, EvaluatorOutcome.UNSUPPORTED)
        self.assertEqual(result.reason_code, "MODEL_CAPABILITIES_MISSING")
        self.assertFalse(spy.invoked)

    def test_inconclusive_observation_maps_to_evidence_failure(self) -> None:
        spy = _SpyEvaluator()
        projection = project_observation(
            _observation(
                completeness=EvidenceCompleteness.INCONCLUSIVE,
                reason_code="SUBJECT_NOT_TERMINAL",
                run_output=None,
            ),
            spy.evidence_requirements,
            _policy(EvidenceField.RUN_OUTPUT),
        )
        result = run_evaluator(spy, projection)
        self.assertEqual(result.outcome, EvaluatorOutcome.INCONCLUSIVE)
        self.assertEqual(result.failure_kind, EvalFailureKind.EVIDENCE)
        self.assertFalse(spy.invoked)


class HardOutcomeAggregationTests(unittest.TestCase):
    """AC 7：单一质量分数不得覆盖 hard outcome。"""

    @staticmethod
    def _result(outcome, *, hard=False, score=None, evaluator_id="e"):  # noqa: ANN001
        from m_agent.companion.eval import EvaluatorResult

        return EvaluatorResult(
            evaluator=EvaluatorRef(evaluator_id=evaluator_id, version="1.0"),
            outcome=outcome,
            failure_kind=EvalFailureKind.SUBJECT
            if outcome is EvaluatorOutcome.FAIL
            else EvalFailureKind.NONE,
            reason_code="AGG_TEST",
            score=score,
            hard=hard,
        )

    def test_quality_score_cannot_override_hard_failure(self) -> None:
        hard_fail = self._result(
            EvaluatorOutcome.FAIL, hard=True, evaluator_id="hard"
        )
        quality_pass = self._result(
            EvaluatorOutcome.PASS, score=0.99, evaluator_id="quality"
        )
        aggregate = aggregate_evaluator_results([hard_fail, quality_pass])
        self.assertEqual(aggregate.outcome, EvaluatorOutcome.FAIL)
        self.assertEqual(aggregate.hard_outcome, EvaluatorOutcome.FAIL)
        # 分数保留为独立维度，绝不参与 verdict。
        self.assertEqual(aggregate.scores, (0.99,))

    def test_evaluator_error_blocks_pass_verdict(self) -> None:
        from m_agent.companion.eval import EvaluatorResult

        error = EvaluatorResult(
            evaluator=EvaluatorRef(evaluator_id="broken", version="1.0"),
            outcome=EvaluatorOutcome.ERROR,
            failure_kind=EvalFailureKind.EVALUATOR,
            reason_code=REASON_EVALUATOR_RAISED,
        )
        passes = self._result(EvaluatorOutcome.PASS, evaluator_id="ok")
        aggregate = aggregate_evaluator_results([passes, error])
        self.assertEqual(aggregate.outcome, EvaluatorOutcome.ERROR)

    def test_inconclusive_blocks_pass_verdict(self) -> None:
        inconclusive = self._result(EvaluatorOutcome.INCONCLUSIVE)
        aggregate = aggregate_evaluator_results([inconclusive])
        self.assertEqual(aggregate.outcome, EvaluatorOutcome.INCONCLUSIVE)

    def test_all_pass_aggregates_to_pass(self) -> None:
        aggregate = aggregate_evaluator_results(
            [
                self._result(EvaluatorOutcome.PASS, hard=True),
                self._result(EvaluatorOutcome.PASS, score=0.8),
            ]
        )
        self.assertEqual(aggregate.outcome, EvaluatorOutcome.PASS)
        self.assertEqual(aggregate.hard_outcome, EvaluatorOutcome.PASS)

class ExternalEvidenceProjectionTests(unittest.TestCase):
    """AC 5 负例补充：外部 Evidence id 的授权与缺失路径。"""

    @staticmethod
    def _artifact(artifact_id: str = "ledger") -> EvidenceArtifact:
        payload = "entry:written"
        return EvidenceArtifact(
            artifact_id=artifact_id,
            subject_ref="run-a",
            schema_name="ledger-entries",
            schema_version="1.0",
            adapter_kind="APPEND_ONLY_JOURNAL",
            source="/tmp/ledger.jsonl",
            payload=payload,
            digest=evidence_digest(artifact_id, "run-a", payload),
        )

    @staticmethod
    def _external_requirements(*artifact_ids: str) -> EvidenceRequirements:
        return EvidenceRequirements(
            evaluator=EvaluatorRef(evaluator_id="ext", version="1.0"),
            fields=frozenset({EvidenceField.EXTERNAL_EVIDENCE}),
            external_evidence_ids=frozenset(artifact_ids),
        )

    def _spy(self, requirements):  # noqa: ANN001, ANN202
        from m_agent.companion.eval import EvaluatorResult

        class _ExternalEvidenceSpy:
            def __init__(self) -> None:
                self.invoked = False

            @property
            def identity(self) -> EvaluatorRef:
                return EvaluatorRef(evaluator_id="ext", version="1.0")

            @property
            def hard(self) -> bool:
                return False

            @property
            def evidence_requirements(self) -> EvidenceRequirements:
                return requirements

            def evaluate(self, projection):  # noqa: ANN001
                self.invoked = True
                return EvaluatorResult(
                    evaluator=self.identity,
                    outcome=EvaluatorOutcome.PASS,
                    failure_kind=EvalFailureKind.NONE,
                    reason_code="SPY_PASS",
                )

        return _ExternalEvidenceSpy()

    def test_authorized_external_evidence_is_delivered(self) -> None:
        artifact = self._artifact()
        observation = _observation(external_evidence=(artifact,))
        projection = project_observation(
            observation,
            self._external_requirements("ledger"),
            _policy(EvidenceField.EXTERNAL_EVIDENCE, artifacts=("ledger",)),
        )
        self.assertEqual(
            projection.delivered,
            frozenset({EvidenceField.EXTERNAL_EVIDENCE}),
        )
        self.assertEqual(projection.denied_evidence_ids, frozenset())
        self.assertEqual(projection.missing_evidence_ids, frozenset())
        delivered = projection.get(EvidenceField.EXTERNAL_EVIDENCE)
        assert delivered is not None  # type narrowing for mypy
        self.assertIn(artifact, delivered)
        spy = self._spy(self._external_requirements("ledger"))
        result = run_evaluator(spy, projection)
        self.assertEqual(result.outcome, EvaluatorOutcome.PASS)
        self.assertTrue(spy.invoked)

    def test_unauthorized_external_evidence_id_denies_field(self) -> None:
        artifact = self._artifact()
        observation = _observation(external_evidence=(artifact,))
        projection = project_observation(
            observation,
            self._external_requirements("ledger"),
            # policy 放行字段但未授权任何 artifact id。
            _policy(EvidenceField.EXTERNAL_EVIDENCE),
        )
        self.assertEqual(
            projection.denied_evidence_ids, frozenset({"ledger"})
        )
        # 部分外部证据未授权：整体字段不可用，绝不交付半份证据。
        self.assertNotIn(
            EvidenceField.EXTERNAL_EVIDENCE, projection.delivered
        )
        self.assertIn(
            EvidenceField.EXTERNAL_EVIDENCE, projection.unavailable
        )
        spy = self._spy(self._external_requirements("ledger"))
        result = run_evaluator(spy, projection)
        self.assertEqual(result.outcome, EvaluatorOutcome.INCONCLUSIVE)
        self.assertEqual(result.failure_kind, EvalFailureKind.EVIDENCE)
        self.assertEqual(result.reason_code, REASON_EVIDENCE_UNAUTHORIZED)
        self.assertFalse(spy.invoked)

    def test_missing_external_evidence_id_marks_missing(self) -> None:
        # Observation 未携带任何外部证据：授权但缺失 => INCONCLUSIVE。
        observation = _observation()
        projection = project_observation(
            observation,
            self._external_requirements("ledger"),
            _policy(EvidenceField.EXTERNAL_EVIDENCE, artifacts=("ledger",)),
        )
        self.assertEqual(
            projection.missing_evidence_ids, frozenset({"ledger"})
        )
        self.assertIn(
            EvidenceField.EXTERNAL_EVIDENCE, projection.unavailable
        )
        spy = self._spy(self._external_requirements("ledger"))
        result = run_evaluator(spy, projection)
        self.assertEqual(result.outcome, EvaluatorOutcome.INCONCLUSIVE)
        self.assertEqual(result.reason_code, REASON_EVIDENCE_MISSING)
        self.assertFalse(spy.invoked)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
