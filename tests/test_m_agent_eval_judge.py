"""Ticket 18 AC 6：LLM-as-judge 是独立、隔离且绝不覆盖 hard gate 的 Run。

可复现证据：

- Judge Run 只存在于专用 Judge RunStore（与 subject Eval RunStore
  分开），run identity 由 execution + item + judge 身份确定性派生；
- Judge Definition 声明任何业务 Tool 都在创建 Run 之前 fail-closed
  （结构上无外部写入、无递归 Judge）；非确定性 Adapter 同样被拒；
- Verdict 解析失败归一为 evaluator failure（ERROR），绝不崩溃、
  绝不把非法输出当 PASS；Judge Run 失败同样归一为 ERROR；
- Judge 结果 hard 恒为 False：deterministic hard/safety failure
  永不被 Judge 或任何质量分覆盖；
- 已完成的 Judge Run 不重复 dispatch（崩溃恢复语义）。
"""

from __future__ import annotations

import unittest

from m_agent.adapters import (
    DeterministicModelAdapter,
    DeterministicTool,
    InMemoryRunStore,
    PlaintextPayloadCodec,
)
from m_agent.companion.eval import (
    REASON_JUDGE_RUN_FAILED,
    REASON_JUDGE_VERDICT_INVALID,
    REASON_JUDGE_VERDICT_RENDERED,
    EvalError,
    EvalExecutionEngine,
    EvalFixtureBoundaryError,
    EvidenceCompleteness,
    EvidenceField,
    EvidenceRequirements,
    EvaluatorOutcome,
    EvaluatorRef,
    InMemoryEvalStore,
    JudgeBinding,
    JudgeRunExecutor,
    ObservationProjection,
    ObservationProjectionPolicy,
    parse_judge_verdict,
)
from m_agent.companion.eval._identity import canonical_json
from m_agent.runtime import (
    AgentDefinition,
    DefinitionRegistry,
    ModelCapabilities,
    RunNotFoundError,
    RunStatus,
    ToolCallingMode,
    ToolEffect,
)

_JUDGE = EvaluatorRef(evaluator_id="llm-judge", version="1.0")


class _LiveLikeAdapter(DeterministicModelAdapter):
    """模拟 live 语义的 Adapter：deterministic=False。"""

    deterministic = False

    def __init__(self) -> None:
        super().__init__(responses=("judge says fine",))


class _FailingAdapter(DeterministicModelAdapter):
    """恒失败的确定性 Adapter：让 Judge Run 进入 FAILED 终态。"""

    async def generate(self, request):  # noqa: ANN001
        raise RuntimeError("judge provider exploded")


def _projection() -> ObservationProjection:
    return ObservationProjection(
        observation_id="obs-1",
        completeness=EvidenceCompleteness.COMPLETE,
        reason_code="SUBJECT_TERMINAL",
        delivered=frozenset({EvidenceField.RUN_OUTPUT}),
        denied=frozenset(),
        unavailable=frozenset(),
        values={EvidenceField.RUN_OUTPUT: "helpful answer"},
    )


def _binding() -> JudgeBinding:
    return JudgeBinding(
        judge=_JUDGE,
        definition_id="judge",
        definition_version="1.0",
        requirements=EvidenceRequirements(
            evaluator=_JUDGE,
            fields=frozenset({EvidenceField.RUN_OUTPUT}),
        ),
    )


def _judge_definition(adapter, tools=()):  # noqa: ANN001
    return AgentDefinition.for_adapter(
        definition_id="judge",
        version="1.0",
        instructions="judge instructions",
        model_adapter=adapter,
        tools=list(tools),
    )


class ParseJudgeVerdictTests(unittest.TestCase):
    """Verdict 解析：结构化输出或 evaluator failure，绝不崩溃。"""

    def test_valid_pass_verdict_with_score(self) -> None:
        result = parse_judge_verdict(
            '{"verdict": "PASS", "score": 0.9, "rationale": "good"}', _JUDGE
        )
        self.assertEqual(result.outcome, EvaluatorOutcome.PASS)
        self.assertEqual(result.score, 0.9)
        self.assertFalse(result.hard)
        self.assertEqual(result.reason_code, REASON_JUDGE_VERDICT_RENDERED)
        self.assertEqual(result.detail, "good")

    def test_valid_fail_verdict_is_subject_failure(self) -> None:
        result = parse_judge_verdict(
            '{"verdict": "FAIL", "score": null, "rationale": "off topic"}',
            _JUDGE,
        )
        self.assertEqual(result.outcome, EvaluatorOutcome.FAIL)
        self.assertIsNone(result.score)
        self.assertFalse(result.hard)
        self.assertEqual(result.reason_code, REASON_JUDGE_VERDICT_RENDERED)

    def test_invalid_json_normalizes_to_evaluator_error(self) -> None:
        result = parse_judge_verdict("not json at all", _JUDGE)
        self.assertEqual(result.outcome, EvaluatorOutcome.ERROR)
        self.assertEqual(result.reason_code, REASON_JUDGE_VERDICT_INVALID)
        self.assertFalse(result.hard)

    def test_unknown_verdict_value_is_rejected(self) -> None:
        result = parse_judge_verdict('{"verdict": "MAYBE"}', _JUDGE)
        self.assertEqual(result.outcome, EvaluatorOutcome.ERROR)
        self.assertEqual(result.reason_code, REASON_JUDGE_VERDICT_INVALID)

    def test_non_object_payload_is_rejected(self) -> None:
        result = parse_judge_verdict('["PASS"]', _JUDGE)
        self.assertEqual(result.outcome, EvaluatorOutcome.ERROR)
        self.assertEqual(result.reason_code, REASON_JUDGE_VERDICT_INVALID)

    def test_non_numeric_score_is_rejected(self) -> None:
        result = parse_judge_verdict(
            '{"verdict": "PASS", "score": "high"}', _JUDGE
        )
        self.assertEqual(result.outcome, EvaluatorOutcome.ERROR)
        self.assertEqual(result.reason_code, REASON_JUDGE_VERDICT_INVALID)


class JudgeRunIsolationTests(unittest.IsolatedAsyncioTestCase):
    """AC 6：独立 Judge Run 落在专用 RunStore 且输入最小化。"""

    def _executor(self, adapter, tools=()):  # noqa: ANN001
        registry = DefinitionRegistry()
        registry.register(_judge_definition(adapter, tools=tools))
        run_store = InMemoryRunStore(PlaintextPayloadCodec())
        return JudgeRunExecutor(registry=registry, run_store=run_store)

    async def test_judge_run_lives_in_dedicated_store(self) -> None:
        adapter = DeterministicModelAdapter(
            responses=('{"verdict": "PASS", "score": 0.9, "rationale": "ok"}',)
        )
        executor = self._executor(adapter)
        record = await executor.run_judge(
            binding=_binding(), projection=_projection(),
            execution_id="exec-1", item_id="item-1",
            case_id="case-1", variant_id="variant-a", repetition_index=0,
        )
        run_id = JudgeRunExecutor.judge_run_id("exec-1", "item-1", _JUDGE)
        self.assertEqual(record.judge_run_id, run_id)
        run = await executor.run_store.get_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run.status, RunStatus.SUCCEEDED)
        # 结果身份确定性派生；hard 恒为 False（永不覆盖 hard gate）。
        self.assertEqual(
            record.result_id,
            JudgeRunExecutor.result_id("exec-1", "item-1", _JUDGE),
        )
        self.assertFalse(record.hard)
        self.assertEqual(record.outcome, EvaluatorOutcome.PASS)
        self.assertEqual(record.score, 0.9)
        self.assertEqual(record.reason_code, REASON_JUDGE_VERDICT_RENDERED)
        self.assertEqual(record.observation_id, "obs-1")

    async def test_judge_input_is_the_minimal_projection_payload(self) -> None:
        adapter = DeterministicModelAdapter(
            responses=('{"verdict": "PASS", "rationale": "ok"}',)
        )
        executor = self._executor(adapter)
        projection = _projection()
        await executor.run_judge(
            binding=_binding(), projection=projection,
            execution_id="exec-1", item_id="item-1",
        )
        run_id = JudgeRunExecutor.judge_run_id("exec-1", "item-1", _JUDGE)
        run = await executor.run_store.get_run(run_id)
        assert run is not None  # type narrowing for mypy
        # Judge 输入是 Projection 的 canonical JSON：结构上无法访问
        # Observation、RunStore 或任何未授权字段。
        self.assertEqual(
            run.input, canonical_json(projection.model_dump(mode="json"))
        )

    async def test_completed_judge_run_is_not_re_dispatched(self) -> None:
        adapter = DeterministicModelAdapter(
            responses=('{"verdict": "PASS", "rationale": "ok"}',)
        )
        executor = self._executor(adapter)
        kwargs = dict(
            binding=_binding(), projection=_projection(),
            execution_id="exec-1", item_id="item-1",
        )
        first = await executor.run_judge(**kwargs)
        second = await executor.run_judge(**kwargs)
        self.assertEqual(first.result_id, second.result_id)
        self.assertEqual(first.outcome, second.outcome)
        # 确定性 Adapter 只被调用一次：第二次是恢复复用权威终态。
        self.assertEqual(adapter.call_count, 1)


class JudgeBoundaryAndFailureTests(unittest.IsolatedAsyncioTestCase):
    """AC 6：fixture boundary fail-closed 与失败归一为 ERROR。"""

    def _executor(self, adapter, tools=()):  # noqa: ANN001
        registry = DefinitionRegistry()
        registry.register(_judge_definition(adapter, tools=tools))
        run_store = InMemoryRunStore(PlaintextPayloadCodec())
        return JudgeRunExecutor(registry=registry, run_store=run_store)

    async def test_judge_definition_with_tools_fails_closed_before_any_run(self) -> None:
        adapter = DeterministicModelAdapter(
            responses=('{"verdict": "PASS"}',),
            capabilities=ModelCapabilities(
                tool_calling=ToolCallingMode.NATIVE
            ),
        )
        ledger = DeterministicTool(
            name="ledger_write",
            effect=ToolEffect.NON_IDEMPOTENT,
            handler=lambda request: "written",
        )
        executor = self._executor(adapter, tools=[ledger])
        with self.assertRaises(EvalFixtureBoundaryError):
            await executor.run_judge(
                binding=_binding(), projection=_projection(),
                execution_id="exec-1", item_id="item-1",
            )
        # fail closed 发生在任何 Judge Run 创建与模型 dispatch 之前。
        self.assertEqual(adapter.call_count, 0)
        run_id = JudgeRunExecutor.judge_run_id("exec-1", "item-1", _JUDGE)
        self.assertIsNone(await executor.run_store.get_run(run_id))

    async def test_nondeterministic_judge_adapter_is_outside_boundary(self) -> None:
        adapter = _LiveLikeAdapter()
        executor = self._executor(adapter)
        with self.assertRaises(EvalFixtureBoundaryError):
            await executor.run_judge(
                binding=_binding(), projection=_projection(),
                execution_id="exec-1", item_id="item-1",
            )
        self.assertEqual(adapter.call_count, 0)

    async def test_failed_judge_run_normalizes_to_evaluator_error(self) -> None:
        adapter = _FailingAdapter()
        executor = self._executor(adapter)
        record = await executor.run_judge(
            binding=_binding(), projection=_projection(),
            execution_id="exec-1", item_id="item-1",
        )
        self.assertEqual(record.outcome, EvaluatorOutcome.ERROR)
        self.assertEqual(record.reason_code, REASON_JUDGE_RUN_FAILED)
        self.assertFalse(record.hard)
        self.assertIsNone(record.score)
        self.assertIn("FAILED", record.detail)

    async def test_invalid_verdict_output_normalizes_to_evaluator_error(self) -> None:
        adapter = DeterministicModelAdapter(responses=("the answer looks fine",))
        executor = self._executor(adapter)
        record = await executor.run_judge(
            binding=_binding(), projection=_projection(),
            execution_id="exec-1", item_id="item-1",
        )
        # Judge Run 成功但输出非法：归一为 ERROR，绝不伪造 PASS。
        self.assertEqual(record.outcome, EvaluatorOutcome.ERROR)
        self.assertEqual(record.reason_code, REASON_JUDGE_VERDICT_INVALID)
        self.assertFalse(record.hard)


class JudgeStoreSeparationTests(unittest.IsolatedAsyncioTestCase):
    """引擎层 fail-closed：Judge RunStore 必须与 subject RunStore 分开。"""

    def _policy(self) -> ObservationProjectionPolicy:
        return ObservationProjectionPolicy(
            policy_id="policy-1", version="1.0",
            allowed_fields=frozenset({EvidenceField.RUN_OUTPUT}),
        )

    async def test_engine_rejects_shared_judge_and_subject_run_store(self) -> None:
        registry = DefinitionRegistry()
        run_store = InMemoryRunStore(PlaintextPayloadCodec())
        shared_judge = JudgeRunExecutor(
            registry=registry, run_store=run_store
        )
        with self.assertRaises(EvalError):
            EvalExecutionEngine(
                registry=registry, run_store=run_store,
                eval_store=InMemoryEvalStore(),
                evaluators={}, projection_policy=self._policy(),
                judge=shared_judge,
            )

    async def test_engine_accepts_dedicated_judge_run_store(self) -> None:
        registry = DefinitionRegistry()
        judge_store = InMemoryRunStore(PlaintextPayloadCodec())
        dedicated_judge = JudgeRunExecutor(
            registry=registry, run_store=judge_store
        )
        engine = EvalExecutionEngine(
            registry=registry,
            run_store=InMemoryRunStore(PlaintextPayloadCodec()),
            eval_store=InMemoryEvalStore(),
            evaluators={}, projection_policy=self._policy(),
            judge=dedicated_judge,
        )
        self.assertIsNotNone(engine)
        self.assertIsNot(dedicated_judge.run_store, None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
