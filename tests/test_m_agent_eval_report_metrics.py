"""Ticket 18 AC 4/5：pass^k、统计披露与分离 gate 的报告语义。

可复现证据：

- 报告保留每次 repetition（best-of-many 绝不吞掉失败样本），
  ``pass^k`` 只在全部 repetition 通过时为 True；
- 统计披露 sample count、min/max/median；样本不足（< 20）时
  ``p95=None`` 且 ``p95_justified=False``，样本充足时才报告 P95；
- hard gate、quality gate 与 overall outcome 分开聚合：hard FAIL
  不被 quality PASS 或任何 Judge 质量分覆盖。
"""

from __future__ import annotations

import unittest

from m_agent.companion.eval import (
    DEFAULT_MIN_SAMPLES_FOR_PERCENTILE,
    AgentVariant,
    EvalCase,
    EvalSuite,
    EvaluatorOutcome,
    EvaluatorRef,
    EvaluatorResultRecord,
    ExecutionProtocol,
    FixtureBundle,
    build_report_revision,
    summarize_samples,
)

_VARIANT = AgentVariant(
    variant_id="variant-a", definition_id="assistant",
    definition_version="1.0",
)
_MATCH = EvaluatorRef(evaluator_id="output-match", version="1.0")
_JUDGE = EvaluatorRef(evaluator_id="llm-judge", version="1.0")


def _bundle() -> FixtureBundle:
    return FixtureBundle.build(bundle_id="bundle-1")


def _case(repetitions: int = 1, case_id: str = "case-1") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        input="check the order",
        variant=_VARIANT,
        fixture_bundle=_bundle(),
        execution_protocol=ExecutionProtocol(
            deterministic=False, repetitions=repetitions, seed="seed-1"
        ),
        evaluators=(_MATCH,),
    )


def _suite(repetitions: int = 1, case_id: str = "case-1") -> EvalSuite:
    case = _case(repetitions, case_id)
    return EvalSuite(
        suite_id="suite-1", version="1.0", cases=(case,), variants=(_VARIANT,)
    )


def _result(
    *, repetition_index: int = 0, outcome: EvaluatorOutcome = EvaluatorOutcome.PASS,
    score: float | None = None, hard: bool = False,
    evaluator: EvaluatorRef = _MATCH, case_id: str = "case-1",
) -> EvaluatorResultRecord:
    return EvaluatorResultRecord(
        result_id=f"result-{case_id}-{repetition_index}-{evaluator.evaluator_id}",
        execution_id="exec-1",
        observation_id=f"obs-{case_id}-{repetition_index}",
        item_id=f"item-{case_id}-{repetition_index}",
        case_id=case_id,
        variant_id=_VARIANT.variant_id,
        repetition_index=repetition_index,
        evaluator=evaluator,
        outcome=outcome,
        failure_kind="NONE" if outcome is EvaluatorOutcome.PASS else "SUBJECT",
        reason_code="SUBJECT_OUTPUT_MATCHED",
        score=score,
        hard=hard,
    )


class SummarizeSamplesTests(unittest.TestCase):
    """统计披露：无依据 P95 绝不报告。"""

    def test_empty_samples_disclose_zero_count(self) -> None:
        summary = summarize_samples(())
        self.assertEqual(summary.sample_count, 0)
        self.assertIsNone(summary.minimum)
        self.assertIsNone(summary.p95)
        self.assertFalse(summary.p95_justified)

    def test_small_samples_report_basic_statistics_without_p95(self) -> None:
        summary = summarize_samples((0.5, 0.75, 1.0))
        self.assertEqual(summary.sample_count, 3)
        self.assertEqual(summary.minimum, 0.5)
        self.assertEqual(summary.maximum, 1.0)
        self.assertEqual(summary.median, 0.75)
        self.assertIsNone(summary.p95)
        self.assertFalse(summary.p95_justified)

    def test_sufficient_samples_report_justified_p95(self) -> None:
        samples = tuple(i / 100.0 for i in range(1, 26))
        summary = summarize_samples(samples)
        self.assertEqual(summary.sample_count, 25)
        self.assertTrue(summary.p95_justified)
        self.assertIsNotNone(summary.p95)
        assert summary.p95 is not None  # type narrowing for mypy
        # nearest-rank P95：ceil(0.95 * 25) = 24 -> 第 24 个次序统计量。
        self.assertEqual(summary.p95, 24 / 100.0)


    def test_boundary_sample_count_is_exactly_the_minimum(self) -> None:
        samples = tuple(float(i) for i in range(1, 21))
        summary = summarize_samples(samples)
        self.assertTrue(summary.p95_justified)
        assert summary.p95 is not None  # type narrowing for mypy
        # ceil(0.95 * 20) = 19 -> 第 19 个次序统计量。
        self.assertEqual(summary.p95, 19.0)


class BuildReportRevisionTests(unittest.TestCase):
    """build_report_revision：pass^k、分离 gate 与 suite 冻结展开。"""

    def test_single_repetition_pass_reports_pass_at_k(self) -> None:
        revision = build_report_revision(
            report_id="report-1", revision=1, execution_id="exec-1",
            suite=_suite(), results=(_result(),),
        )
        self.assertEqual(len(revision.case_results), 1)
        case_report = revision.case_results[0]
        self.assertTrue(case_report.pass_at_k)
        self.assertEqual(case_report.overall_outcome, EvaluatorOutcome.PASS)
        self.assertEqual(revision.overall_outcome, EvaluatorOutcome.PASS)

    def test_pass_at_k_requires_every_repetition_to_pass(self) -> None:
        # repetition 0 通过、repetition 1 失败：best-of-many 不吞掉失败样本。
        results = (
            _result(repetition_index=0),
            _result(
                repetition_index=1, outcome=EvaluatorOutcome.FAIL,
            ),
        )
        revision = build_report_revision(
            report_id="report-1", revision=1, execution_id="exec-1",
            suite=_suite(repetitions=2), results=results,
        )
        case_report = revision.case_results[0]
        self.assertFalse(case_report.pass_at_k)
        self.assertEqual(
            [outcome.outcome for outcome in case_report.repetitions],
            [EvaluatorOutcome.PASS, EvaluatorOutcome.FAIL],
        )
        self.assertEqual(case_report.overall_outcome, EvaluatorOutcome.FAIL)
        self.assertEqual(revision.overall_outcome, EvaluatorOutcome.FAIL)

    def test_missing_repetition_is_inconclusive_not_silent_pass(self) -> None:
        # 只有 repetition 0 的证据；repetition 1 缺失 -> INCONCLUSIVE。
        revision = build_report_revision(
            report_id="report-1", revision=1, execution_id="exec-1",
            suite=_suite(repetitions=2), results=(_result(repetition_index=0),),
        )
        case_report = revision.case_results[0]
        self.assertEqual(len(case_report.repetitions), 2)
        missing = case_report.repetitions[1]
        self.assertEqual(missing.outcome, EvaluatorOutcome.INCONCLUSIVE)
        self.assertIsNone(missing.score)
        self.assertFalse(case_report.pass_at_k)
        self.assertEqual(
            case_report.overall_outcome, EvaluatorOutcome.INCONCLUSIVE
        )

    def test_hard_failure_is_not_covered_by_quality_pass_or_scores(self) -> None:
        # hard FAIL + Judge quality PASS（带高分）：overall 仍是 FAIL，
        # 任何质量分数永不参与 gate 判定。
        results = (
            _result(outcome=EvaluatorOutcome.FAIL, hard=True),
            _result(
                evaluator=_JUDGE, outcome=EvaluatorOutcome.PASS, score=0.98,
            ),
        )
        revision = build_report_revision(
            report_id="report-1", revision=1, execution_id="exec-1",
            suite=_suite(), results=results,
        )
        case_report = revision.case_results[0]
        self.assertEqual(case_report.hard_outcome, EvaluatorOutcome.FAIL)
        self.assertEqual(case_report.quality_outcome, EvaluatorOutcome.PASS)
        self.assertEqual(case_report.overall_outcome, EvaluatorOutcome.FAIL)
        self.assertEqual(revision.hard_outcome, EvaluatorOutcome.FAIL)
        self.assertEqual(revision.overall_outcome, EvaluatorOutcome.FAIL)
        # 质量分数仍被披露（0.98 保留在样本中），但改变不了 gate。
        self.assertEqual(case_report.scores, (0.98,))

    def test_repetition_score_is_mean_of_scoring_evaluators(self) -> None:
        results = (
            _result(evaluator=_JUDGE, outcome=EvaluatorOutcome.PASS, score=0.5),
            _result(evaluator=_MATCH, outcome=EvaluatorOutcome.PASS),
        )
        revision = build_report_revision(
            report_id="report-1", revision=1, execution_id="exec-1",
            suite=_suite(), results=results,
        )
        repetition = revision.case_results[0].repetitions[0]
        self.assertEqual(repetition.score, 0.5)

    def test_report_level_gate_is_worst_of_all_cases(self) -> None:
        # case-1 PASS、case-2 FAIL：report 级 hard/overall 取 worst-of。
        case_one = _case(case_id="case-1")
        case_two = _case(case_id="case-2")
        suite = EvalSuite(
            suite_id="suite-1", version="1.0",
            cases=(case_one, case_two), variants=(_VARIANT,),
        )
        results = (
            _result(case_id="case-1"),
            _result(
                case_id="case-2", outcome=EvaluatorOutcome.FAIL, hard=True,
            ),
        )
        revision = build_report_revision(
            report_id="report-1", revision=1, execution_id="exec-1",
            suite=suite, results=results,
        )
        self.assertEqual(revision.hard_outcome, EvaluatorOutcome.FAIL)
        self.assertEqual(revision.quality_outcome, EvaluatorOutcome.PASS)
        self.assertEqual(revision.overall_outcome, EvaluatorOutcome.FAIL)
        # case_results 按确定性顺序排序（case_id, variant_id）。
        self.assertEqual(
            [report.case_id for report in revision.case_results],
            ["case-1", "case-2"],
        )

    def test_revision_freezes_suite_digest_and_content_digest(self) -> None:
        suite = _suite()
        revision = build_report_revision(
            report_id="report-1", revision=1, execution_id="exec-1",
            suite=suite, results=(_result(),),
        )
        self.assertEqual(revision.suite_id, "suite-1")
        self.assertEqual(revision.suite_version, "1.0")
        self.assertEqual(revision.suite_digest, suite.content_digest())
        # content_digest 覆盖除自身外的全部字段（含 created_at）。
        self.assertEqual(
            revision.content_digest, revision.content_digest_payload()
        )
        rebuilt = build_report_revision(
            report_id="report-1", revision=2, execution_id="exec-1",
            suite=suite, results=(_result(),),
        )
        self.assertEqual(rebuilt.revision, 2)
        # revision 不同 -> 内容不同 -> digest 不同（新证据新 revision）。
        self.assertNotEqual(revision.content_digest, rebuilt.content_digest)
