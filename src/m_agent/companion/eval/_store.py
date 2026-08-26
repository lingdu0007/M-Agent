"""EvalStore：append-only 持久化事实与最小公开 view（Ticket 17 AC 8 / Ticket 18）。

EvalStore 只做 append-only：同 id 同内容重放幂等，同 id 异内容
确定性冲突（已保存事实绝不改写）。Ticket 18 起持久化六类事实：
Eval Execution、Observation、Evaluator Result、不可变 Report
revision、Baseline 与 Recommendation。公开 view（observation_view /
execution_view / report_view）足以支撑验收，同时不暴露未授权
payload——run input/output/history 不进入 view。

InMemoryEvalStore 与 SQLiteEvalStore 共享同一行为契约，并由
``tests/eval_store_contract.py`` 的共享契约套件同时验证。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..._steps import utc_now
from ._baseline import EvalBaselineRecord
from ._errors import EvalRecordConflictError
from ._evaluator import EvaluatorOutcome, EvaluatorResultRecord
from ._observation import EvalObservation
from ._recommendation import ModelRecommendationRecord
from ._report import ReportRevisionRecord

__all__ = [
    "EvalCaseView",
    "EvalExecutionRecord",
    "EvalExecutionView",
    "EvalObservationView",
    "EvalReportView",
    "EvalStore",
    "InMemoryEvalStore",
]


class EvalExecutionRecord(BaseModel):
    """一次 Suite 展开/执行编排的持久化事实（mode + item 集合）。

    execution_id 由内容派生（Ticket 18 引擎）：manifest/fixture/
    evaluator 变化产生新 execution，旧证据绝不覆盖。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str = Field(min_length=1)
    suite_id: str = Field(min_length=1)
    suite_version: str = Field(min_length=1)
    suite_digest: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    item_ids: tuple[str, ...] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class EvalObservationView(BaseModel):
    """Observation 的最小公开 view：无任何 payload 字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str
    mode: str
    subject_run_id: str
    definition_id: str
    definition_version: str
    variant_id: str | None
    completeness: str
    reason_code: str
    execution_id: str | None
    collected_at: datetime


class EvalExecutionView(BaseModel):
    """Execution 的最小公开 view（关联的 observation 身份有序列出）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    suite_id: str
    suite_version: str
    suite_digest: str
    mode: str
    item_ids: tuple[str, ...]
    created_at: datetime
    observation_ids: tuple[str, ...] = ()


class EvalCaseView(BaseModel):
    """Report 中一个 case × variant 的最小公开 view。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    variant_id: str
    overall_outcome: EvaluatorOutcome
    pass_at_k: bool
    sample_count: int


class EvalReportView(BaseModel):
    """Report revision 的最小公开 view：统计与结论，无任何证据正文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_id: str
    revision: int
    suite_id: str
    suite_version: str
    hard_outcome: EvaluatorOutcome
    quality_outcome: EvaluatorOutcome
    overall_outcome: EvaluatorOutcome
    cases: tuple[EvalCaseView, ...]
    created_at: datetime


@runtime_checkable
class EvalStore(Protocol):
    """EvalStore 行为契约：append-only 不可变身份 + 最小公开 view。

    六类记录（execution / observation / evaluator result / report
    revision / baseline / recommendation）都遵循同一不可变语义：同
    id 同内容重放幂等、异内容确定性冲突、view 不泄漏 payload。
    """

    async def record_execution(
        self, execution: EvalExecutionRecord
    ) -> EvalExecutionRecord: ...

    async def get_execution(
        self, execution_id: str
    ) -> EvalExecutionRecord | None: ...

    async def record_observation(
        self, observation: EvalObservation
    ) -> EvalObservation: ...

    async def get_observation(
        self, observation_id: str
    ) -> EvalObservation | None: ...

    async def observation_view(
        self, observation_id: str
    ) -> EvalObservationView | None: ...

    async def execution_view(
        self, execution_id: str
    ) -> EvalExecutionView | None: ...

    async def record_evaluator_result(
        self, result: EvaluatorResultRecord
    ) -> EvaluatorResultRecord: ...

    async def get_evaluator_result(
        self, result_id: str
    ) -> EvaluatorResultRecord | None: ...

    async def record_report(
        self, report: ReportRevisionRecord
    ) -> ReportRevisionRecord: ...

    async def get_report(
        self, report_id: str, revision: int
    ) -> ReportRevisionRecord | None: ...

    async def latest_report_revision(
        self, report_id: str
    ) -> int | None: ...

    async def report_view(
        self, report_id: str, revision: int
    ) -> EvalReportView | None: ...

    async def record_baseline(
        self, baseline: EvalBaselineRecord
    ) -> EvalBaselineRecord: ...

    async def get_baseline(
        self, baseline_id: str
    ) -> EvalBaselineRecord | None: ...

    async def record_recommendation(
        self, recommendation: ModelRecommendationRecord
    ) -> ModelRecommendationRecord: ...

    async def get_recommendation(
        self, recommendation_id: str
    ) -> ModelRecommendationRecord | None: ...


def _conflict(kind: str, record_id: str) -> EvalRecordConflictError:
    """同 id 异内容的确定性冲突（已保存事实绝不改写）。"""
    return EvalRecordConflictError(
        f"{kind} {record_id!r} already stored with different content;"
        " eval records are immutable"
    )


class InMemoryEvalStore:
    """进程内 append-only EvalStore（离线评估与测试默认实现）。

    与 SQLiteEvalStore 共享同一行为契约（Ticket 18）。
    """

    def __init__(self) -> None:
        self._executions: dict[str, EvalExecutionRecord] = {}
        self._observations: dict[str, EvalObservation] = {}
        self._execution_observations: dict[str, list[str]] = {}
        self._evaluator_results: dict[str, EvaluatorResultRecord] = {}
        self._reports: dict[tuple[str, int], ReportRevisionRecord] = {}
        self._baselines: dict[str, EvalBaselineRecord] = {}
        self._recommendations: dict[str, ModelRecommendationRecord] = {}

    async def record_execution(
        self, execution: EvalExecutionRecord
    ) -> EvalExecutionRecord:
        existing = self._executions.get(execution.execution_id)
        if existing is None:
            self._executions[execution.execution_id] = execution
            self._execution_observations.setdefault(
                execution.execution_id, []
            )
            return execution
        if existing != execution:
            raise _conflict("execution", execution.execution_id)
        return existing

    async def get_execution(
        self, execution_id: str
    ) -> EvalExecutionRecord | None:
        return self._executions.get(execution_id)

    async def record_observation(
        self, observation: EvalObservation
    ) -> EvalObservation:
        existing = self._observations.get(observation.observation_id)
        if existing is None:
            self._observations[observation.observation_id] = observation
            if observation.execution_id is not None:
                self._execution_observations.setdefault(
                    observation.execution_id, []
                ).append(observation.observation_id)
            return observation
        if existing != observation:
            raise _conflict("observation", observation.observation_id)
        return existing

    async def get_observation(
        self, observation_id: str
    ) -> EvalObservation | None:
        return self._observations.get(observation_id)

    async def observation_view(
        self, observation_id: str
    ) -> EvalObservationView | None:
        observation = self._observations.get(observation_id)
        if observation is None:
            return None
        return EvalObservationView(
            observation_id=observation.observation_id,
            mode=observation.mode.value,
            subject_run_id=observation.subject_run_id,
            definition_id=observation.definition_id,
            definition_version=observation.definition_version,
            variant_id=observation.variant_id,
            completeness=observation.completeness.value,
            reason_code=observation.reason_code,
            execution_id=observation.execution_id,
            collected_at=observation.collected_at,
        )

    async def execution_view(
        self, execution_id: str
    ) -> EvalExecutionView | None:
        execution = self._executions.get(execution_id)
        if execution is None:
            return None
        return EvalExecutionView(
            execution_id=execution.execution_id,
            suite_id=execution.suite_id,
            suite_version=execution.suite_version,
            suite_digest=execution.suite_digest,
            mode=execution.mode,
            item_ids=execution.item_ids,
            created_at=execution.created_at,
            observation_ids=tuple(
                self._execution_observations.get(execution_id, ())
            ),
        )

    async def record_evaluator_result(
        self, result: EvaluatorResultRecord
    ) -> EvaluatorResultRecord:
        existing = self._evaluator_results.get(result.result_id)
        if existing is None:
            self._evaluator_results[result.result_id] = result
            return result
        if existing != result:
            raise _conflict("evaluator result", result.result_id)
        return existing

    async def get_evaluator_result(
        self, result_id: str
    ) -> EvaluatorResultRecord | None:
        return self._evaluator_results.get(result_id)

    async def record_report(
        self, report: ReportRevisionRecord
    ) -> ReportRevisionRecord:
        key = (report.report_id, report.revision)
        existing = self._reports.get(key)
        if existing is None:
            self._reports[key] = report
            return report
        if existing != report:
            raise _conflict(
                f"report revision {report.report_id}#{report.revision}",
                report.report_id,
            )
        return existing

    async def get_report(
        self, report_id: str, revision: int
    ) -> ReportRevisionRecord | None:
        return self._reports.get((report_id, revision))

    async def latest_report_revision(
        self, report_id: str
    ) -> int | None:
        revisions = [
            revision
            for (existing_id, revision) in self._reports
            if existing_id == report_id
        ]
        return max(revisions) if revisions else None

    async def report_view(
        self, report_id: str, revision: int
    ) -> EvalReportView | None:
        report = self._reports.get((report_id, revision))
        if report is None:
            return None
        return _report_view(report)

    async def record_baseline(
        self, baseline: EvalBaselineRecord
    ) -> EvalBaselineRecord:
        existing = self._baselines.get(baseline.baseline_id)
        if existing is None:
            self._baselines[baseline.baseline_id] = baseline
            return baseline
        if existing != baseline:
            raise _conflict("baseline", baseline.baseline_id)
        return existing

    async def get_baseline(
        self, baseline_id: str
    ) -> EvalBaselineRecord | None:
        return self._baselines.get(baseline_id)

    async def record_recommendation(
        self, recommendation: ModelRecommendationRecord
    ) -> ModelRecommendationRecord:
        existing = self._recommendations.get(
            recommendation.recommendation_id
        )
        if existing is None:
            self._recommendations[
                recommendation.recommendation_id
            ] = recommendation
            return recommendation
        if existing != recommendation:
            raise _conflict(
                "recommendation", recommendation.recommendation_id
            )
        return existing

    async def get_recommendation(
        self, recommendation_id: str
    ) -> ModelRecommendationRecord | None:
        return self._recommendations.get(recommendation_id)


def _report_view(report: ReportRevisionRecord) -> EvalReportView:
    """构造最小公开 Report view（两种实现共用）。"""
    cases = tuple(
        EvalCaseView(
            case_id=case.case_id,
            variant_id=case.variant_id,
            overall_outcome=case.overall_outcome,
            pass_at_k=case.pass_at_k,
            sample_count=(
                case.score_summary.sample_count
                if case.score_summary is not None
                else len(case.scores)
            ),
        )
        for case in report.case_results
    )
    return EvalReportView(
        report_id=report.report_id,
        revision=report.revision,
        suite_id=report.suite_id,
        suite_version=report.suite_version,
        hard_outcome=report.hard_outcome,
        quality_outcome=report.quality_outcome,
        overall_outcome=report.overall_outcome,
        cases=cases,
        created_at=report.created_at,
    )
