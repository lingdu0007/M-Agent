"""InMemoryEvalStore 与 SQLiteEvalStore 共享的行为契约测试（Ticket 18 AC 1）。

EvalStore 的两种实现必须通过同一份实现无关的公共契约套件；本模块以
mixin 形式提供，具体实现各自继承（须同时继承
``unittest.IsolatedAsyncioTestCase``）并提供 ``make_store()`` 工厂。

契约覆盖 Ticket 18 新增的持久化事实：Evaluator Result、不可变 Report
revision、Baseline 与 Recommendation——全部 append-only：同 id 同内容
重放幂等，同 id 异内容确定性冲突，已保存事实绝不改写；公开 view 足以
验收且不暴露未授权 payload。execution / observation 的 Ticket 17 契约
（不可变身份 + 最小 view）也纳入本套件，保证两种后端行为一致。
"""

from __future__ import annotations

import unittest
from typing import TYPE_CHECKING

from m_agent.companion.eval import (
  ComparisonPolicy,
  EvalBaselineRecord,
  EvalExecutionRecord,
  EvalMode,
  EvalObservation,
  EvalRecordConflictError,
  EvaluatorOutcome,
  EvaluatorRef,
  EvaluatorResultRecord,
  EvidenceArtifact,
  EvidenceCompleteness,
  ModelRecommendationRecord,
  NumericSummary,
  RecommendationTarget,
  ReportRevisionRecord,
  CaseVariantReport,
  RepetitionOutcome,
  evidence_digest,
)
from m_agent.runtime import RunStatus

if TYPE_CHECKING:
  # 静态检查视角：Mixin 的 assert* 断言来自 TestCase 绑定基类；
  # 运行时保持纯 Mixin（object 基类），pytest 不单独收集本类。
  _ContractBase = unittest.TestCase
else:
  _ContractBase = object


_EVALUATOR = EvaluatorRef(evaluator_id="output-match", version="1.0")
_POLICY = ComparisonPolicy(policy_id="compare", version="1.0")


def _observation(**overrides) -> EvalObservation:
  values: dict = dict(
      observation_id="obs-1",
      mode=EvalMode.EXECUTE,
      subject_run_id="run-1",
      definition_id="assistant",
      definition_version="1.0",
      completeness=EvidenceCompleteness.COMPLETE,
      reason_code="SUBJECT_TERMINAL",
      execution_id="exec-1",
      run_status=RunStatus.SUCCEEDED,
      run_input="secret-grade input",
      run_output="secret-grade output",
  )
  values.update(overrides)
  return EvalObservation(**values)


def _execution(**overrides) -> EvalExecutionRecord:
  values: dict = dict(
      execution_id="exec-1",
      suite_id="suite-1",
      suite_version="1.0",
      suite_digest="d" * 64,
      mode=EvalMode.EXECUTE,
      item_ids=("item-1", "item-2"),
  )
  values.update(overrides)
  return EvalExecutionRecord(**values)


def _result(**overrides) -> EvaluatorResultRecord:
  values: dict = dict(
      result_id="result-1",
      execution_id="exec-1",
      observation_id="obs-1",
      item_id="item-1",
      evaluator=_EVALUATOR,
      outcome=EvaluatorOutcome.PASS,
      failure_kind="NONE",
      reason_code="SUBJECT_OUTPUT_MATCHED",
  )
  values.update(overrides)
  return EvaluatorResultRecord(**values)


def _report(revision: int = 1, **overrides) -> ReportRevisionRecord:
  values: dict = dict(
      report_id="report-1",
      revision=revision,
      execution_id="exec-1",
      suite_id="suite-1",
      suite_version="1.0",
      suite_digest="d" * 64,
      case_results=(
          CaseVariantReport(
              case_id="case-1",
              variant_id="variant-a",
              repetitions=(
                  RepetitionOutcome(
                      repetition_index=0,
                      outcome=EvaluatorOutcome.PASS,
                  ),
                  RepetitionOutcome(
                      repetition_index=1,
                      outcome=EvaluatorOutcome.FAIL,
                  ),
              ),
              hard_outcome=EvaluatorOutcome.PASS,
              quality_outcome=EvaluatorOutcome.FAIL,
              overall_outcome=EvaluatorOutcome.FAIL,
              pass_at_k=False,
          ),
      ),
      hard_outcome=EvaluatorOutcome.PASS,
      quality_outcome=EvaluatorOutcome.FAIL,
      overall_outcome=EvaluatorOutcome.FAIL,
  )
  values.update(overrides)
  return ReportRevisionRecord(**values)


def _baseline(**overrides) -> EvalBaselineRecord:
  values: dict = dict(
      baseline_id="baseline-1",
      report_id="report-1",
      report_revision=1,
      suite_id="suite-1",
      suite_version="1.0",
      comparison_policy=_POLICY,
  )
  values.update(overrides)
  return EvalBaselineRecord(**values)


def _recommendation(**overrides) -> ModelRecommendationRecord:
  values: dict = dict(
      recommendation_id="rec-1",
      version="1.0",
      report_id="report-1",
      report_revision=1,
      target=RecommendationTarget(
          kind="AGENT_VARIANT", target_id="variant-a", target_version="1.0"
      ),
      hard_gate=EvaluatorOutcome.PASS,
      quality_gate=EvaluatorOutcome.PASS,
      overall_outcome=EvaluatorOutcome.PASS,
      confidence=0.9,
      evidence_digest="e" * 64,
  )
  values.update(overrides)
  return ModelRecommendationRecord(**values)


class EvalStoreContractMixin(_ContractBase):
  """EvalStore 行为契约。子类必须同时继承
  ``unittest.IsolatedAsyncioTestCase`` 并实现 ``make_store()``。
  """

  def make_store(self):  # pragma: no cover - 由绑定类提供
      raise NotImplementedError

  # -- Ticket 17 契约回归：execution / observation 不可变身份 --------

  async def test_execution_identity_is_immutable_and_idempotent(self) -> None:
      store = self.make_store()
      execution = _execution()
      await store.record_execution(execution)
      await store.record_execution(execution)
      self.assertEqual(await store.get_execution("exec-1"), execution)
      with self.assertRaises(EvalRecordConflictError):
          await store.record_execution(
              _execution(item_ids=("item-1",))
          )
      self.assertEqual(await store.get_execution("exec-1"), execution)

  async def test_observation_identity_is_immutable_and_idempotent(self) -> None:
      store = self.make_store()
      observation = _observation(
          external_evidence=(
              EvidenceArtifact(
                  artifact_id="ledger",
                  subject_ref="run-1",
                  schema_name="ledger",
                  schema_version="1",
                  adapter_kind="SENTINEL",
                  source="sentinel.json",
                  payload='{"ok":true}',
                  digest=evidence_digest(
                      "ledger", "run-1", '{"ok":true}'
                  ),
              ),
          )
      )
      await store.record_observation(observation)
      # 同内容重放幂等（崩溃恢复安全）。
      await store.record_observation(observation)
      self.assertEqual(
          await store.get_observation("obs-1"), observation
      )
      with self.assertRaises(EvalRecordConflictError):
          await store.record_observation(
              _observation(run_output="different output")
          )
      self.assertEqual(
          await store.get_observation("obs-1"), observation
      )

  async def test_views_hide_unauthorized_payloads(self) -> None:
      store = self.make_store()
      await store.record_execution(_execution())
      await store.record_observation(_observation())
      observation_view = await store.observation_view("obs-1")
      self.assertIsNotNone(observation_view)
      assert observation_view is not None  # type narrowing for mypy
      self.assertNotIn("secret-grade", observation_view.model_dump_json())
      execution_view = await store.execution_view("exec-1")
      self.assertIsNotNone(execution_view)
      assert execution_view is not None  # type narrowing for mypy
      self.assertEqual(execution_view.observation_ids, ("obs-1",))

  async def test_unknown_ids_return_none(self) -> None:
      store = self.make_store()
      self.assertIsNone(await store.get_execution("missing"))
      self.assertIsNone(await store.get_observation("missing"))
      self.assertIsNone(await store.observation_view("missing"))
      self.assertIsNone(await store.execution_view("missing"))
      self.assertIsNone(await store.get_evaluator_result("missing"))
      self.assertIsNone(await store.get_report("missing", 1))
      self.assertIsNone(await store.latest_report_revision("missing"))
      self.assertIsNone(await store.report_view("missing", 1))
      self.assertIsNone(await store.get_baseline("missing"))
      self.assertIsNone(await store.get_recommendation("missing"))

  # -- Ticket 18：Evaluator Result 不可变身份 ------------------------

  async def test_evaluator_result_identity_is_immutable(self) -> None:
      store = self.make_store()
      result = _result()
      await store.record_evaluator_result(result)
      await store.record_evaluator_result(result)
      self.assertEqual(
          await store.get_evaluator_result("result-1"), result
      )
      conflicting = _result(outcome=EvaluatorOutcome.FAIL)
      with self.assertRaises(EvalRecordConflictError):
          await store.record_evaluator_result(conflicting)
      # 冲突不改动已保存事实。
      self.assertEqual(
          await store.get_evaluator_result("result-1"), result
      )

  async def test_evaluator_result_judge_provenance_is_preserved(self) -> None:
      store = self.make_store()
      judge_result = _result(
          result_id="result-judge",
          judge_run_id="judge-run-1",
          score=0.8,
      )
      await store.record_evaluator_result(judge_result)
      stored = await store.get_evaluator_result("result-judge")
      self.assertEqual(stored, judge_result)
      self.assertEqual(stored.judge_run_id, "judge-run-1")

  # -- Ticket 18：不可变 Report revision -----------------------------

  async def test_report_revision_is_immutable_and_chains(self) -> None:
      store = self.make_store()
      first = _report(revision=1)
      second = _report(
          revision=2,
          quality_outcome=EvaluatorOutcome.PASS,
          overall_outcome=EvaluatorOutcome.PASS,
      )
      await store.record_report(first)
      # 同 revision 同内容：幂等重放。
      await store.record_report(first)
      # 同 revision 异内容：确定性冲突，历史不被改写。
      forged = _report(
          revision=1, overall_outcome=EvaluatorOutcome.PASS
      )
      with self.assertRaises(EvalRecordConflictError):
          await store.record_report(forged)
      self.assertEqual(await store.get_report("report-1", 1), first)
      # 新 revision 是新记录：两个 revision 并存可查。
      await store.record_report(second)
      self.assertEqual(await store.get_report("report-1", 1), first)
      self.assertEqual(await store.get_report("report-1", 2), second)
      self.assertEqual(await store.latest_report_revision("report-1"), 2)

  async def test_report_view_discloses_statistics_without_payload(self) -> None:
      store = self.make_store()
      report = _report(
          case_results=(
              CaseVariantReport(
                  case_id="case-1",
                  variant_id="variant-a",
                  repetitions=(
                      RepetitionOutcome(
                          repetition_index=index,
                          outcome=EvaluatorOutcome.PASS,
                          score=0.5 + index * 0.25,
                      )
                      for index in range(2)
                  ),
                  hard_outcome=EvaluatorOutcome.PASS,
                  quality_outcome=EvaluatorOutcome.PASS,
                  overall_outcome=EvaluatorOutcome.PASS,
                  pass_at_k=True,
                  scores=(0.5, 0.75),
                  score_summary=NumericSummary(
                      sample_count=2, minimum=0.5, maximum=0.75,
                      median=0.625,
                  ),
              ),
          ),
      )
      await store.record_report(report)
      view = await store.report_view("report-1", 1)
      self.assertIsNotNone(view)
      assert view is not None  # type narrowing for mypy
      self.assertEqual(view.report_id, "report-1")
      self.assertEqual(view.revision, 1)
      self.assertEqual(view.overall_outcome, EvaluatorOutcome.FAIL)
      self.assertEqual(len(view.cases), 1)
      case_view = view.cases[0]
      self.assertEqual(case_view.case_id, "case-1")
      self.assertIs(case_view.pass_at_k, True)
      self.assertEqual(case_view.sample_count, 2)
      # view 不携带任何 repetition 明细 payload 或评语文本。
      self.assertNotIn("secret-grade", view.model_dump_json())

  # -- Ticket 18：Baseline 显式引用 exact revision -------------------

  async def test_baseline_references_exact_revision_and_is_immutable(self) -> None:
      store = self.make_store()
      await store.record_report(_report(revision=1))
      await store.record_report(_report(revision=2))
      baseline = _baseline()
      await store.record_baseline(baseline)
      await store.record_baseline(baseline)
      self.assertEqual(await store.get_baseline("baseline-1"), baseline)
      # 同 id 异内容：确定性冲突。
      with self.assertRaises(EvalRecordConflictError):
          await store.record_baseline(
              _baseline(report_revision=2)
          )
      stored = await store.get_baseline("baseline-1")
      assert stored is not None  # type narrowing for mypy
      # Baseline 冻结 exact revision：新增 revision 不改变其引用。
      self.assertEqual(stored.report_revision, 1)
      self.assertEqual(stored.comparison_policy, _POLICY)

  # -- Ticket 18：Recommendation 只读引用 ----------------------------

  async def test_recommendation_is_immutable_reference_only(self) -> None:
      store = self.make_store()
      report = _report()
      await store.record_report(report)
      recommendation = _recommendation()
      await store.record_recommendation(recommendation)
      await store.record_recommendation(recommendation)
      self.assertEqual(
          await store.get_recommendation("rec-1"), recommendation
      )
      with self.assertRaises(EvalRecordConflictError):
          await store.record_recommendation(
              _recommendation(overall_outcome=EvaluatorOutcome.FAIL)
          )
      # Recommendation 记录不改变它引用的报告与基线事实。
      self.assertEqual(await store.get_report("report-1", 1), report)

  # -- 观测与 execution 的关联顺序 -----------------------------------

  async def test_execution_view_lists_observation_append_order(self) -> None:
      store = self.make_store()
      await store.record_execution(_execution())
      await store.record_observation(_observation(observation_id="obs-1"))
      await store.record_observation(_observation(observation_id="obs-2"))
      view = await store.execution_view("exec-1")
      self.assertIsNotNone(view)
      assert view is not None  # type narrowing for mypy
      self.assertEqual(view.observation_ids, ("obs-1", "obs-2"))
