"""Durable Eval Execution 恢复引擎（Ticket 18）。

引擎把「展开 -> subject 执行 -> evaluator/Judge 编排」变成可中断、
可恢复的 durable 流程：

- execution 身份由 Suite 冻结内容派生（suite_id + version +
  content digest + mode）：同一 manifest 重跑命中同一 execution（幂等
  恢复）；manifest/fixture/evaluator 任何变化产生**新** execution，
  旧证据绝不覆盖；
- 恢复只续跑未完成单元：observation / evaluator result 都以确定性
  身份先查后跑，已完成的 subject Run 与 Evaluator 结果不重复执行；
- Judge 与 subject 使用不同的 RunStore（构造时 fail-closed 校验），
  Judge 结果与 deterministic evaluator 结果同样 append-only 落盘。
"""

from __future__ import annotations

from typing import Mapping

from pydantic import BaseModel, ConfigDict

from ..._definition import DefinitionRegistry
from ..._store import RunStore
from .._session import SessionStore
from ._case import EvalSuite, EvalSuiteItem, EvaluatorRef
from ._errors import EvalError
from ._evaluator import DeterministicEvaluator, EvaluatorResultRecord, run_evaluator
from ._execute import EvalExecutor
from ._judge import JudgeBinding, JudgeRunExecutor
from ._observation import (
    REASON_SUBJECT_NOT_TERMINAL,
    EvalMode,
    EvalObservation,
)
from ._projection import ObservationProjectionPolicy, project_observation
from ._identity import digest_of
from ._store import EvalExecutionRecord, EvalStore

__all__ = [
    "EvalExecutionEngine",
    "EvalSuiteRunResult",
]


class EvalSuiteRunResult(BaseModel):
    """一次（或恢复续跑的）Suite 执行的可观测结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution: EvalExecutionRecord
    observations: tuple[EvalObservation, ...]
    results: tuple[EvaluatorResultRecord, ...]


class EvalExecutionEngine:
    """可中断、可恢复的 durable Eval Execution 编排引擎。

    :param registry: subject Variant 指向的 Definition 注册表。
    :param run_store: 隔离的 subject Eval RunStore。
    :param eval_store: append-only EvalStore（execution / observation /
        evaluator result 的 durable 事实库）。
    :param evaluators: evaluator_id -> 确定性 Evaluator（身份版本必须
        与 Case 冻结引用精确一致，否则 fail-closed）。
    :param projection_policy: Evaluator/Judge 的最小授权投影策略。
    :param judge: 可选的 Judge 执行器；其 RunStore 必须与 subject
        RunStore 是不同实例（专用 Judge RunStore）。
    :param judge_bindings: evaluator_id -> Judge 绑定（judge 身份 +
        独立 Judge Definition + 证据需求）。
    :param session_store: 可选的隔离 Eval SessionStore。
    """

    def __init__(
        self,
        *,
        registry: DefinitionRegistry,
        run_store: RunStore,
        eval_store: EvalStore,
        evaluators: Mapping[str, DeterministicEvaluator],
        projection_policy: ObservationProjectionPolicy,
        judge: JudgeRunExecutor | None = None,
        judge_bindings: Mapping[str, JudgeBinding] | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        if judge is not None and judge.run_store is run_store:
            raise EvalError(
                "judge must use a dedicated RunStore separate from the"
                " subject eval RunStore; judge isolation is mandatory"
            )
        self._eval_store = eval_store
        self._evaluators = dict(evaluators)
        self._projection_policy = projection_policy
        self._judge = judge
        self._judge_bindings = dict(judge_bindings) if judge_bindings else {}
        self._executor = EvalExecutor(
            registry=registry, run_store=run_store, session_store=session_store
        )

    def _binding_for(self, ref: EvaluatorRef) -> JudgeBinding | None:
        """按 Case 冻结引用解析 Judge 绑定；身份错配 fail-closed。

        deterministic evaluator 有严格的版本 fail-closed；Judge 绑定
        同样必须与 Case 冻结的 ``EvaluatorRef``（id + version）精确
        一致，否则绑定身份会静默替换被评估的 evaluator 版本。
        """
        binding = self._judge_bindings.get(ref.evaluator_id)
        if binding is not None and binding.judge != ref:
            raise EvalError(
                f"judge binding for evaluator {ref.evaluator_id!r}"
                f" freezes version {binding.judge.version!r} but the"
                f" case freezes {ref.version!r}; judge identity must"
                " match the frozen evaluator reference"
            )
        return binding

    @staticmethod
    def execution_identity(
        suite: EvalSuite, mode: EvalMode = EvalMode.EXECUTE
    ) -> str:
        """由 Suite 冻结内容派生的确定性 execution 身份。

        同一 manifest 恒得到同一 execution（幂等恢复）；manifest /
        fixture / evaluator 版本变化 -> 新 digest -> 新 execution。
        """
        return "eval-exec-" + digest_of(
            "eval-execution", suite.suite_id, suite.version,
            suite.content_digest(), mode.value,
        )[:32]

    @staticmethod
    def result_identity(
        execution_id: str, item: EvalSuiteItem, ref: EvaluatorRef
    ) -> str:
        """Evaluator（含 Judge）结果记录的确定性身份。"""
        return digest_of(
            "eval-evaluator-result", execution_id, item.item_id,
            ref.evaluator_id, ref.version,
        )

    async def run_suite(
        self,
        suite: EvalSuite,
        *,
        execution_id: str | None = None,
    ) -> EvalSuiteRunResult:
        """执行（或恢复）一个 Suite：只续跑未完成单元。

        每个 item 的顺序：observation 先查后跑（已存在即复用，subject
        Run 不重复执行）；每个 Case 冻结的 Evaluator 依次先查后跑
        （deterministic 或 Judge），结果 append-only 落盘。
        """
        items = suite.expand()
        resolved_id = execution_id or self.execution_identity(suite)
        execution = EvalExecutionRecord(
            execution_id=resolved_id,
            suite_id=suite.suite_id,
            suite_version=suite.version,
            suite_digest=suite.content_digest(),
            mode=EvalMode.EXECUTE.value,
            item_ids=tuple(item.item_id for item in items),
        )
        # 幂等恢复：如果 execution 已存在，校验 Suite 内容与冻结事实
        # 精确一致（同一内容），直接复用已有记录，避免 created_at 差异
        # 触发 append-only 冲突；内容错配 fail-closed——同一 execution
        # 身份绝不混入异质 Suite 的证据。
        existing = await self._eval_store.get_execution(resolved_id)
        if existing is not None:
            self._verify_execution_identity(existing, suite)
            execution = existing
        else:
            await self._eval_store.record_execution(execution)
        observations: list[EvalObservation] = []
        results: list[EvaluatorResultRecord] = []
        for item in items:
            observation = await self._ensure_observation(
                suite=suite, item=item, execution_id=resolved_id
            )
            observations.append(observation)
            case = suite.case_by_id(item.case_id)
            for ref in case.evaluators:
                results.append(
                    await self._ensure_result(
                        item=item,
                        execution_id=resolved_id,
                        observation=observation,
                        ref=ref,
                    )
                )
        return EvalSuiteRunResult(
            execution=execution,
            observations=tuple(observations),
            results=tuple(results),
        )

    async def resume_execution(
        self, suite: EvalSuite, execution_id: str
    ) -> EvalSuiteRunResult:
        """恢复既有 execution：语义与 run_suite 相同（显式身份）。

        显式传入的 ``execution_id`` 不存在时 fail-closed（拼错的身份
        绝不静默退化为新建 execution）；恢复前同样校验 Suite 内容与
        冻结事实精确一致。
        """
        existing = await self._eval_store.get_execution(execution_id)
        if existing is None:
            raise EvalError(
                f"cannot resume execution {execution_id!r}: no such"
                " execution is stored in the eval store"
            )
        return await self.run_suite(suite, execution_id=execution_id)

    # -- 内部：durable 单元 ----------------------------------------------

    @staticmethod
    def _verify_execution_identity(
        existing: EvalExecutionRecord, suite: EvalSuite
    ) -> None:
        """复用既有 execution 前校验冻结身份与当前 Suite 精确一致。

        manifest / fixture / evaluator 的任何变化都会改变
        ``suite.content_digest()``；digest（或 suite 身份、mode）错配
        时 fail-closed，而不是把异质 Suite 的证据混进同一 execution。
        """
        current_digest = suite.content_digest()
        if (
            existing.suite_id != suite.suite_id
            or existing.suite_digest != current_digest
            or existing.mode != EvalMode.EXECUTE.value
        ):
            raise EvalError(
                f"execution {existing.execution_id!r} was created for"
                f" suite {existing.suite_id!r} digest"
                f" {existing.suite_digest!r} (mode {existing.mode!r})"
                " but is being resumed with suite"
                f" {suite.suite_id!r} digest {current_digest!r};"
                " suite content changed under a frozen execution"
                " identity - use a new execution instead"
            )

    async def _ensure_observation(
        self, *, suite: EvalSuite, item: EvalSuiteItem, execution_id: str
    ) -> EvalObservation:
        """observation 先查后跑：已完成单元直接复用存储事实。

        subject Run 未到达终态（如 WAITING 等待应用 resolution）时
        fail-closed：非终态 INCONCLUSIVE 观察一旦 append-only 落盘，
        后续任何 resolution 都无法再被观察到（事实不可改写）。此
        时抛 :class:`EvalError`，待 Run 终态化后再 resume 即可正常
        观察并落盘。
        """
        observation_id = EvalExecutor.observation_id(execution_id, item)
        existing = await self._eval_store.get_observation(observation_id)
        if existing is not None:
            return existing
        observation = await self._executor.execute_item(
            suite=suite, item=item, execution_id=execution_id
        )
        if observation.reason_code == REASON_SUBJECT_NOT_TERMINAL:
            raise EvalError(
                f"subject run {observation.subject_run_id!r} for item"
                f" {item.item_id!r} has not reached a terminal state;"
                " an INCONCLUSIVE observation would be permanently"
                " frozen by the append-only store - resolve the run"
                " and resume the execution"
            )
        return await self._eval_store.record_observation(observation)

    async def _ensure_result(
        self,
        *,
        item: EvalSuiteItem,
        execution_id: str,
        observation: EvalObservation,
        ref: EvaluatorRef,
    ) -> EvaluatorResultRecord:
        """evaluator / judge 结果先查后跑：已保存结果不重复执行。

        Judge 结果的确定性身份由 JudgeRunExecutor.result_id 派生
        （域 eval-judge-result），与 deterministic evaluator
        的 result_identity（域 eval-evaluator-result）不同。
        此处统一用与 Judge 一致的 identity 查询，保证先查后跑命中。
        """
        binding = self._binding_for(ref)
        if binding is not None:
            result_id = JudgeRunExecutor.result_id(
                execution_id, item.item_id, binding.judge
            )
        else:
            result_id = self.result_identity(execution_id, item, ref)
        existing = await self._eval_store.get_evaluator_result(result_id)
        if existing is not None:
            return existing
        record = await self._execute_evaluator(
            item=item,
            execution_id=execution_id,
            observation=observation,
            ref=ref,
        )
        return await self._eval_store.record_evaluator_result(record)

    async def _execute_evaluator(
        self,
        *,
        item: EvalSuiteItem,
        execution_id: str,
        observation: EvalObservation,
        ref: EvaluatorRef,
    ) -> EvaluatorResultRecord:
        """按冻结引用执行 deterministic evaluator 或独立 Judge Run。"""
        binding = self._binding_for(ref)
        if binding is not None:
            if self._judge is None:
                raise EvalError(
                    "case freezes judge evaluator"
                    f" {ref.evaluator_id!r} but no JudgeRunExecutor was"
                    " configured"
                )
            projection = project_observation(
                observation, binding.requirements, self._projection_policy
            )
            return await self._judge.run_judge(
                binding=binding,
                projection=projection,
                execution_id=execution_id,
                item_id=item.item_id,
                case_id=item.case_id,
                variant_id=item.variant.variant_id,
                repetition_index=item.repetition_index,
            )
        evaluator = self._evaluators.get(ref.evaluator_id)
        if evaluator is None:
            raise EvalError(
                f"case freezes evaluator {ref.evaluator_id!r} which is not"
                " registered with the execution engine"
            )
        if evaluator.identity.version != ref.version:
            raise EvalError(
                f"evaluator {ref.evaluator_id!r} version mismatch: case"
                f" freezes {ref.version!r} but the registered evaluator"
                f" is {evaluator.identity.version!r}"
            )
        projection = project_observation(
            observation, evaluator.evidence_requirements,
            self._projection_policy,
        )
        result = run_evaluator(evaluator, projection)
        return EvaluatorResultRecord(
            result_id=self.result_identity(execution_id, item, ref),
            execution_id=execution_id,
            observation_id=observation.observation_id,
            item_id=item.item_id,
            case_id=item.case_id,
            variant_id=item.variant.variant_id,
            repetition_index=item.repetition_index,
            evaluator=result.evaluator,
            outcome=result.outcome,
            failure_kind=result.failure_kind,
            reason_code=result.reason_code,
            detail=result.detail,
            score=result.score,
            hard=result.hard,
            evidence_refs=result.evidence_refs,
        )
