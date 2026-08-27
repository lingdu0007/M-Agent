"""Ticket 18 AC 6: Durable Eval Execution recovery semantics.

Core guarantees:

- Idempotent recovery: same Suite rerun hits same execution_id (content-derived);
  completed observation/evaluator result are looked up before execution, subject
  Run and Evaluator are not re-executed (zero new model dispatch).
- Version change creates new execution: Suite manifest/fixture/evaluator changes
  produce new content_digest -> new execution_id, old evidence is never overwritten.
- Cross-process recovery: SQLiteEvalStore persists facts across processes.
- Crash recovery: multi-item Suite, first half done then crash, resume completes
  the remaining half (first half observations/results already persisted).
- Judge isolation: Judge uses a dedicated RunStore (enforced at construction),
  Judge results are append-only just like deterministic evaluator results.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta

from m_agent.adapters import (
    DeterministicModelAdapter,
    DeterministicTool,
    FakeClock,
    InMemoryRunStore,
    PlaintextPayloadCodec,
)
from m_agent.companion import InMemorySessionStore
from m_agent.companion.eval import (
    AgentVariant,
    EvalCase,
    EvalError,
    EvalExecutionEngine,
    EvalExecutionRecord,
    EvalExecutor,
    EvalMode,
    EvalObservation,
    EvalSuite,
    EvaluatorOutcome,
    EvaluatorRef,
    EvidenceCompleteness,
    EvidenceField,
    EvidenceRequirements,
    ExecutionProtocol,
    FixtureBundle,
    InMemoryEvalStore,
    JudgeBinding,
    JudgeRunExecutor,
    ObservationProjectionPolicy,
    OutputMatchesEvaluator,
    REASON_SUBJECT_TERMINAL,
    SQLiteEvalStore,
)
from m_agent.runtime import (
    DEFAULT_LEASE_TTL,
    AgentDefinition,
    CrashPoint,
    DefinitionRegistry,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Runner,
    RunStatus,
    ToolCall,
    ToolCallingMode,
    ToolEffect,
    ToolOutcome,
    ToolRequest,
)



def _definition(adapter, *, version="1.0"):
    return AgentDefinition.for_adapter(definition_id="assistant", version=version, instructions="assistant instructions", model_adapter=adapter, tools=[]);

def _bundle():
    return FixtureBundle.build(bundle_id="bundle-1", facts=(), declared_external_effects=(), expected_evidence_ids=())

def _case(case_id="case-1"):
    return EvalCase(case_id=case_id, input="hello", variant=AgentVariant(variant_id="variant-a", definition_id="assistant", definition_version="1.0"), fixture_bundle=_bundle(), execution_protocol=ExecutionProtocol(deterministic=True), evaluators=(EvaluatorRef(evaluator_id="output-match", version="1.0"),), tags=())

def _multi_case_suite(n=3):
    cases = tuple(EvalCase(case_id=f"case-{i}", input=f"input-{i}", variant=AgentVariant(variant_id="variant-a", definition_id="assistant", definition_version="1.0"), fixture_bundle=_bundle(), execution_protocol=ExecutionProtocol(deterministic=True), evaluators=(EvaluatorRef(evaluator_id=f"eval-{i}", version="1.0"),)) for i in range(n));
    return EvalSuite(suite_id="suite-multi", version="1.0", cases=cases, variants=(AgentVariant(variant_id="variant-a", definition_id="assistant", definition_version="1.0"),))

def _evaluators_for(suite):
    result = {};
    for case in suite.cases:
        for ref in case.evaluators:
            result[ref.evaluator_id] = OutputMatchesEvaluator(evaluator_id=ref.evaluator_id, version=ref.version, expected="ok");
    return result

def _policy():
    return ObservationProjectionPolicy(policy_id="policy-1", version="1.0", allowed_fields=frozenset({EvidenceField.RUN_OUTPUT}))

def _engine(*, registry, run_store, eval_store, suite):
    return EvalExecutionEngine(registry=registry, run_store=run_store, eval_store=eval_store, evaluators=_evaluators_for(suite), projection_policy=_policy())



class ExecutionIdentityTests(unittest.TestCase):
    def test_same_suite_yields_same_execution_id(self):
        suite = _multi_case_suite(2);
        eid1 = EvalExecutionEngine.execution_identity(suite);
        eid2 = EvalExecutionEngine.execution_identity(suite);
        self.assertEqual(eid1, eid2)

    def test_different_suite_version_yields_different_execution_id(self):
        suite_v1 = _multi_case_suite(2);
        suite_v2 = EvalSuite(suite_id=suite_v1.suite_id, version="2.0", cases=suite_v1.cases, variants=suite_v1.variants);
        eid1 = EvalExecutionEngine.execution_identity(suite_v1);
        eid2 = EvalExecutionEngine.execution_identity(suite_v2);
        self.assertNotEqual(eid1, eid2)

    def test_different_case_input_yields_different_execution_id(self):
        suite_a = _multi_case_suite(1);
        modified_case = EvalCase(**{**suite_a.cases[0].model_dump(), "input": "different-input"});
        suite_b = EvalSuite(suite_id=suite_a.suite_id, version=suite_a.version, cases=(modified_case,), variants=suite_a.variants);
        eid_a = EvalExecutionEngine.execution_identity(suite_a);
        eid_b = EvalExecutionEngine.execution_identity(suite_b);
        self.assertNotEqual(eid_a, eid_b)

    def test_result_identity_is_deterministic(self):
        suite = _multi_case_suite(1);
        item = suite.expand()[0];
        ref = suite.cases[0].evaluators[0];
        rid1 = EvalExecutionEngine.result_identity("exec-1", item, ref);
        rid2 = EvalExecutionEngine.result_identity("exec-1", item, ref);
        self.assertEqual(rid1, rid2);
        rid3 = EvalExecutionEngine.result_identity("exec-2", item, ref);
        self.assertNotEqual(rid1, rid3)



class IdempotentRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rerun_same_suite_reuses_completed_units(self):
        adapter = DeterministicModelAdapter(responses=("ok",));
        registry = DefinitionRegistry();
        registry.register(_definition(adapter));
        run_store = InMemoryRunStore(PlaintextPayloadCodec());
        eval_store = InMemoryEvalStore();
        suite = _multi_case_suite(1);
        engine = _engine(registry=registry, run_store=run_store, eval_store=eval_store, suite=suite);
        first = await engine.run_suite(suite);
        self.assertEqual(len(first.observations), 1);
        self.assertEqual(len(first.results), 1);
        self.assertEqual(first.results[0].outcome, EvaluatorOutcome.PASS);
        dispatch_after_first = adapter.call_count;
        second = await engine.run_suite(suite);
        self.assertEqual(second.execution.execution_id, first.execution.execution_id);
        self.assertEqual(len(second.observations), 1);
        self.assertEqual(len(second.results), 1);
        self.assertEqual(adapter.call_count, dispatch_after_first);
        self.assertEqual(second.observations[0].observation_id, first.observations[0].observation_id);
        self.assertEqual(second.results[0].result_id, first.results[0].result_id)

    async def test_explicit_resume_uses_same_execution_id(self):
        adapter = DeterministicModelAdapter(responses=("ok",));
        registry = DefinitionRegistry();
        registry.register(_definition(adapter));
        run_store = InMemoryRunStore(PlaintextPayloadCodec());
        eval_store = InMemoryEvalStore();
        suite = _multi_case_suite(1);
        engine = _engine(registry=registry, run_store=run_store, eval_store=eval_store, suite=suite);
        first = await engine.run_suite(suite);
        resumed = await engine.resume_execution(suite, first.execution.execution_id);
        self.assertEqual(resumed.execution.execution_id, first.execution.execution_id);
        self.assertEqual(adapter.call_count, 1)



class VersionChangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_suite_version_creates_new_execution(self):
        adapter = DeterministicModelAdapter(responses=("ok", "ok"));
        registry = DefinitionRegistry();
        registry.register(_definition(adapter));
        run_store = InMemoryRunStore(PlaintextPayloadCodec());
        eval_store = InMemoryEvalStore();
        suite_v1 = _multi_case_suite(1);
        engine = _engine(registry=registry, run_store=run_store, eval_store=eval_store, suite=suite_v1);
        first = await engine.run_suite(suite_v1);
        eid_v1 = first.execution.execution_id;
        suite_v2 = EvalSuite(suite_id=suite_v1.suite_id, version="2.0", cases=suite_v1.cases, variants=suite_v1.variants);
        engine_v2 = _engine(registry=registry, run_store=run_store, eval_store=eval_store, suite=suite_v2);
        second = await engine_v2.run_suite(suite_v2);
        self.assertNotEqual(second.execution.execution_id, eid_v1);
        self.assertIsNotNone(await eval_store.get_execution(eid_v1));
        self.assertIsNotNone(await eval_store.get_execution(second.execution.execution_id))

    async def test_changed_evaluator_version_creates_new_execution(self):
        adapter = DeterministicModelAdapter(responses=("ok",));
        registry = DefinitionRegistry();
        registry.register(_definition(adapter));
        run_store = InMemoryRunStore(PlaintextPayloadCodec());
        eval_store = InMemoryEvalStore();
        suite_v1 = _multi_case_suite(1);
        engine = _engine(registry=registry, run_store=run_store, eval_store=eval_store, suite=suite_v1);
        first = await engine.run_suite(suite_v1);
        modified_case = EvalCase(**{**suite_v1.cases[0].model_dump(), "evaluators": (EvaluatorRef(evaluator_id="eval-0", version="2.0"),)});
        suite_v2 = EvalSuite(suite_id=suite_v1.suite_id, version=suite_v1.version, cases=(modified_case,), variants=suite_v1.variants);
        new_evaluators = {"eval-0": OutputMatchesEvaluator(evaluator_id="eval-0", version="2.0", expected="ok")};
        engine_v2 = EvalExecutionEngine(registry=registry, run_store=run_store, eval_store=eval_store, evaluators=new_evaluators, projection_policy=_policy());
        second = await engine_v2.run_suite(suite_v2);
        self.assertNotEqual(second.execution.execution_id, first.execution.execution_id)



class CrashRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_completion_then_resume_completes_remaining(self):
        adapter = DeterministicModelAdapter(responses=("ok-0", "ok-1", "ok-2"));
        registry = DefinitionRegistry();
        registry.register(_definition(adapter));
        run_store = InMemoryRunStore(PlaintextPayloadCodec());
        eval_store = InMemoryEvalStore();
        suite = _multi_case_suite(3);
        engine = _engine(registry=registry, run_store=run_store, eval_store=eval_store, suite=suite);
        items = suite.expand();
        exec_id = EvalExecutionEngine.execution_identity(suite);
        obs0 = await engine._ensure_observation(suite=suite, item=items[0], execution_id=exec_id);
        res0 = await engine._ensure_result(item=items[0], execution_id=exec_id, observation=obs0, ref=suite.cases[0].evaluators[0]);
        dispatch_after_crash = adapter.call_count;
        self.assertEqual(dispatch_after_crash, 1);
        result = await engine.run_suite(suite);
        self.assertEqual(len(result.observations), 3);
        self.assertEqual(len(result.results), 3);
        self.assertEqual(adapter.call_count, 3);
        self.assertEqual(result.results[0].result_id, res0.result_id)



class CrossProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_store_persists_across_engines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "eval.db");
            adapter1 = DeterministicModelAdapter(responses=("ok",));
            registry1 = DefinitionRegistry();
            registry1.register(_definition(adapter1));
            run_store1 = InMemoryRunStore(PlaintextPayloadCodec());
            eval_store1 = SQLiteEvalStore(db_path);
            
            suite = _multi_case_suite(2);
            engine1 = EvalExecutionEngine(registry=registry1, run_store=run_store1, eval_store=eval_store1, evaluators=_evaluators_for(suite), projection_policy=_policy());
            items = suite.expand();
            exec_id = EvalExecutionEngine.execution_identity(suite);
            obs0 = await engine1._ensure_observation(suite=suite, item=items[0], execution_id=exec_id);
            res0 = await engine1._ensure_result(item=items[0], execution_id=exec_id, observation=obs0, ref=suite.cases[0].evaluators[0]);
            eval_store1.close();
            adapter2 = DeterministicModelAdapter(responses=("ok",));
            registry2 = DefinitionRegistry();
            registry2.register(_definition(adapter2));
            run_store2 = InMemoryRunStore(PlaintextPayloadCodec());
            eval_store2 = SQLiteEvalStore(db_path);
            
            engine2 = EvalExecutionEngine(registry=registry2, run_store=run_store2, eval_store=eval_store2, evaluators=_evaluators_for(suite), projection_policy=_policy());
            result = await engine2.run_suite(suite);
            self.assertEqual(result.execution.execution_id, exec_id);
            self.assertEqual(len(result.observations), 2);
            self.assertEqual(len(result.results), 2);
            self.assertEqual(result.observations[0].observation_id, obs0.observation_id);
            self.assertEqual(result.results[0].result_id, res0.result_id);
            self.assertNotEqual(result.observations[1].observation_id, result.observations[0].observation_id);
            eval_store2.close()



class JudgeIsolationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_judge_must_use_dedicated_run_store(self):
        from m_agent.companion.eval import EvalError, JudgeRunExecutor;
        adapter = DeterministicModelAdapter(responses=("ok",));
        registry = DefinitionRegistry();
        registry.register(_definition(adapter));
        run_store = InMemoryRunStore(PlaintextPayloadCodec());
        eval_store = InMemoryEvalStore();
        judge = JudgeRunExecutor(registry=registry, run_store=run_store);
        with self.assertRaises(EvalError):
            EvalExecutionEngine(registry=registry, run_store=run_store, eval_store=eval_store, evaluators={}, projection_policy=_policy(), judge=judge)

    async def test_judge_results_are_append_only_and_reused(self):
        from m_agent.companion.eval import JudgeBinding, JudgeRunExecutor;
        subject_adapter = DeterministicModelAdapter(responses=("ok",));
        registry = DefinitionRegistry();
        registry.register(AgentDefinition.for_adapter(definition_id="assistant", version="1.0", instructions="subject", model_adapter=subject_adapter, tools=[]));
        judge_verdict = json.dumps({"verdict": "PASS", "score": 0.9, "rationale": "ok"});
        judge_adapter = DeterministicModelAdapter(responses=(judge_verdict,));
        registry.register(AgentDefinition.for_adapter(definition_id="judge-agent", version="1.0", instructions="judge", model_adapter=judge_adapter, tools=[]));
        subject_store = InMemoryRunStore(PlaintextPayloadCodec());
        judge_store = InMemoryRunStore(PlaintextPayloadCodec());
        eval_store = InMemoryEvalStore();
        suite = EvalSuite(suite_id="suite-judge", version="1.0", cases=(EvalCase(case_id="case-judge", input="test", variant=AgentVariant(variant_id="variant-a", definition_id="assistant", definition_version="1.0"), fixture_bundle=_bundle(), execution_protocol=ExecutionProtocol(deterministic=True), evaluators=(EvaluatorRef(evaluator_id="judge-eval", version="1.0"),)),), variants=(AgentVariant(variant_id="variant-a", definition_id="assistant", definition_version="1.0"),));
        judge = JudgeRunExecutor(registry=registry, run_store=judge_store);
        binding = JudgeBinding(judge=EvaluatorRef(evaluator_id="judge-eval", version="1.0"), definition_id="judge-agent", definition_version="1.0", requirements=EvidenceRequirements(evaluator=EvaluatorRef(evaluator_id="judge-eval", version="1.0"), fields=frozenset({EvidenceField.RUN_OUTPUT})));
        engine = EvalExecutionEngine(registry=registry, run_store=subject_store, eval_store=eval_store, evaluators={}, projection_policy=_policy(), judge=judge, judge_bindings={"judge-eval": binding});
        first = await engine.run_suite(suite);
        self.assertEqual(len(first.results), 1);
        self.assertEqual(first.results[0].outcome, EvaluatorOutcome.PASS);
        self.assertIsNotNone(first.results[0].judge_run_id);
        subject_dispatch_after = subject_adapter.call_count;
        judge_dispatch_after = judge_adapter.call_count;
        second = await engine.run_suite(suite);
        self.assertEqual(second.results[0].result_id, first.results[0].result_id);
        self.assertEqual(subject_adapter.call_count, subject_dispatch_after);
        self.assertEqual(judge_adapter.call_count, judge_dispatch_after)



if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Review 回归：新增的 fail-closed 语义（frozen identity / judge binding /
# 非终态观察 / link 原子性 / session claim 幂等恢复）
# ---------------------------------------------------------------------------


class _ToolCallModel(DeterministicModelAdapter):
    """确定性模型：先请求外部效果工具，收到结果后给出最终答案。"""

    def __init__(self) -> None:
        super().__init__(
            capabilities=ModelCapabilities(
                tool_calling=ToolCallingMode.NATIVE
            )
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        if not request.tool_outcomes:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="call-1",
                        tool_name="side_effect_tool",
                        arguments="{}",
                    ),
                )
            )
        return ModelResponse(content="ok")


class FrozenExecutionIdentityTests(unittest.IsolatedAsyncioTestCase):
    """同一 execution 身份下的 Suite 内容错配 fail-closed。"""

    async def test_resume_with_mutated_suite_content_fails_closed(self) -> None:
        adapter = DeterministicModelAdapter(responses=("ok",))
        registry = DefinitionRegistry()
        registry.register(_definition(adapter))
        run_store = InMemoryRunStore(PlaintextPayloadCodec())
        eval_store = InMemoryEvalStore()
        suite = _multi_case_suite(1)
        engine = _engine(
            registry=registry, run_store=run_store,
            eval_store=eval_store, suite=suite,
        )
        first = await engine.run_suite(suite)
        mutated_case = EvalCase(
            **{**suite.cases[0].model_dump(), "input": "different-input"}
        )
        mutated = EvalSuite(
            suite_id=suite.suite_id, version=suite.version,
            cases=(mutated_case,), variants=suite.variants,
        )
        with self.assertRaises(EvalError):
            await engine.resume_execution(mutated, first.execution.execution_id)
        # 冻结 execution 的 durable 事实原样保留，未被异质 Suite 混入。
        stored = await eval_store.get_execution(first.execution.execution_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.suite_digest, suite.content_digest())

    async def test_resume_unknown_execution_id_fails_closed(self) -> None:
        adapter = DeterministicModelAdapter(responses=("ok",))
        registry = DefinitionRegistry()
        registry.register(_definition(adapter))
        eval_store = InMemoryEvalStore()
        suite = _multi_case_suite(1)
        engine = _engine(
            registry=registry, run_store=InMemoryRunStore(PlaintextPayloadCodec()),
            eval_store=eval_store, suite=suite,
        )
        with self.assertRaises(EvalError):
            await engine.resume_execution(suite, "eval-exec-does-not-exist")
        # 拼错的身份绝不静默退化为新建 execution。
        self.assertIsNone(
            await eval_store.get_execution("eval-exec-does-not-exist")
        )
        self.assertEqual(adapter.call_count, 0)


class JudgeBindingIdentityTests(unittest.IsolatedAsyncioTestCase):
    """Judge 绑定身份必须与 Case 冻结的 evaluator 引用精确一致。"""

    async def test_judge_binding_version_mismatch_fails_closed(self) -> None:
        registry = DefinitionRegistry()
        registry.register(AgentDefinition.for_adapter(
            definition_id="assistant", version="1.0",
            instructions="subject",
            model_adapter=DeterministicModelAdapter(responses=("ok",)),
            tools=[],
        ))
        # Case 冻结 judge-eval@1.0，绑定却声明 2.0：必须 fail-closed，
        # 而不是让绑定身份静默替换被评估的 evaluator 版本。
        binding = JudgeBinding(
            judge=EvaluatorRef(evaluator_id="judge-eval", version="2.0"),
            definition_id="judge-agent",
            definition_version="1.0",
            requirements=EvidenceRequirements(
                evaluator=EvaluatorRef(
                    evaluator_id="judge-eval", version="2.0"
                ),
                fields=frozenset({EvidenceField.RUN_OUTPUT}),
            ),
        )
        engine = EvalExecutionEngine(
            registry=registry,
            run_store=InMemoryRunStore(PlaintextPayloadCodec()),
            eval_store=InMemoryEvalStore(),
            evaluators={},
            projection_policy=_policy(),
            judge=JudgeRunExecutor(
                registry=registry,
                run_store=InMemoryRunStore(PlaintextPayloadCodec()),
            ),
            judge_bindings={"judge-eval": binding},
        )
        suite = EvalSuite(
            suite_id="suite-binding", version="1.0",
            cases=(EvalCase(
                case_id="case-binding", input="hi",
                variant=AgentVariant(
                    variant_id="variant-a", definition_id="assistant",
                    definition_version="1.0",
                ),
                fixture_bundle=_bundle(),
                execution_protocol=ExecutionProtocol(deterministic=True),
                evaluators=(EvaluatorRef(
                    evaluator_id="judge-eval", version="1.0"
                ),),
            ),),
            variants=(AgentVariant(
                variant_id="variant-a", definition_id="assistant",
                definition_version="1.0",
            ),),
        )
        with self.assertRaises(EvalError):
            await engine.run_suite(suite)


class NonTerminalObservationTests(unittest.IsolatedAsyncioTestCase):
    """非终态 subject Run 绝不作为 INCONCLUSIVE 观察被 append-only 固化。"""

    async def test_waiting_subject_run_fails_closed_without_persisting(self) -> None:
        clock = FakeClock()
        model = _ToolCallModel()
        tool = DeterministicTool(
            name="side_effect_tool",
            effect=ToolEffect.NON_IDEMPOTENT,
            handler=lambda request: ToolOutcome.success(
                call_id=request.call_id,
                tool_name=request.tool_name,
                result="written",
            ),
        )
        run_store = InMemoryRunStore(
            payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        registry = DefinitionRegistry()
        registry.register(AgentDefinition.for_adapter(
            definition_id="waiting-assistant", version="1.0",
            instructions="subject", model_adapter=model, tools=[tool],
        ))
        suite = EvalSuite(
            suite_id="suite-waiting", version="1.0",
            cases=(EvalCase(
                case_id="case-waiting", input="hi",
                variant=AgentVariant(
                    variant_id="variant-a", definition_id="waiting-assistant",
                    definition_version="1.0",
                ),
                fixture_bundle=FixtureBundle.build(
                    bundle_id="bundle-1", facts=(),
                    declared_external_effects=("side_effect_tool",),
                    expected_evidence_ids=(),
                ),
                execution_protocol=ExecutionProtocol(deterministic=True),
                evaluators=(EvaluatorRef(
                    evaluator_id="output-match", version="1.0"
                ),),
            ),),
            variants=(AgentVariant(
                variant_id="variant-a", definition_id="waiting-assistant",
                definition_version="1.0",
            ),),
        )
        execution_id = EvalExecutionEngine.execution_identity(suite)
        item = suite.expand()[0]
        run_id = EvalExecutor.subject_run_id(execution_id, item)

        # 预置 WAITING run：非幂等工具 dispatch 后崩溃，租约过期后续跑
        # 进入 WAITING（UNCERTAIN_NON_IDEMPOTENT，等待应用 resolution）。
        def crash_hook(point: CrashPoint, crashed_run_id: str) -> None:
            if point is CrashPoint.BEFORE_TOOL_CHECKPOINT:
                raise RuntimeError("injected crash after tool dispatch")

        crashed_runner = Runner(
            registry=registry, store=run_store, crash_hook=crash_hook
        )
        await crashed_runner.create_run(
            "waiting-assistant", "1.0", "hi", run_id=run_id
        )
        with self.assertRaises(RuntimeError):
            await crashed_runner.start_run(run_id)
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        resumed = await Runner(
            registry=registry, store=run_store
        ).resume_run(run_id)
        self.assertIs(resumed.status, RunStatus.WAITING)

        # 引擎面对 WAITING run：fail-closed——一旦把 INCONCLUSIVE 观察
        # append-only 落盘，后续任何 resolution 都无法再被观察到。
        eval_store = InMemoryEvalStore()
        engine = EvalExecutionEngine(
            registry=registry, run_store=run_store, eval_store=eval_store,
            evaluators={"output-match": OutputMatchesEvaluator(
                evaluator_id="output-match", version="1.0", expected="ok",
            )},
            projection_policy=_policy(),
        )
        with self.assertRaises(EvalError):
            await engine.run_suite(suite)
        observation_id = EvalExecutor.observation_id(execution_id, item)
        self.assertIsNone(await eval_store.get_observation(observation_id))


class ExecutionLinkAtomicityTests(unittest.IsolatedAsyncioTestCase):
    """observation 与 execution 关联的崩溃窗口由幂等重放修复。"""

    async def test_observation_replay_reestablishes_missing_execution_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            database = os.path.join(tmpdir, "link-recovery.db")
            observation = EvalObservation(
                observation_id="obs-link-1",
                mode=EvalMode.EXECUTE,
                subject_run_id="run-1",
                completeness=EvidenceCompleteness.COMPLETE,
                reason_code=REASON_SUBJECT_TERMINAL,
                execution_id="exec-link-1",
            )
            store = SQLiteEvalStore(database)
            try:
                await store.record_execution(EvalExecutionRecord(
                    execution_id="exec-link-1",
                    suite_id="suite-1",
                    suite_version="1.0",
                    suite_digest="sha256:" + "0" * 64,
                    mode=EvalMode.EXECUTE.value,
                    item_ids=("item-1",),
                ))
                await store.record_observation(observation)
                view = await store.execution_view("exec-link-1")
                self.assertEqual(view.observation_ids, ("obs-link-1",))
            finally:
                store.close()

            # 模拟旧版双事务实现留下的崩溃窗口：observation 已落盘、
            # execution 关联丢失（进程在两个 commit 之间硬退出）。
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "DELETE FROM eval_execution_observations"
                    " WHERE observation_id=?",
                    ("obs-link-1",),
                )
                connection.commit()
            finally:
                connection.close()

            # 同内容 observation 幂等重放必须补建关联（对账修复），
            # 同 id 异内容仍确定性冲突。
            replay = SQLiteEvalStore(database)
            try:
                await replay.record_observation(observation)
                view = await replay.execution_view("exec-link-1")
            finally:
                replay.close()
            self.assertEqual(view.observation_ids, ("obs-link-1",))


class SessionClaimRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """「Turn 已提交、Observation 未落盘」的崩溃窗口可幂等恢复。"""

    async def test_committed_turn_without_observation_recovers_idempotently(self) -> None:
        adapter = DeterministicModelAdapter(responses=("ok",))
        registry = DefinitionRegistry()
        registry.register(_definition(adapter))
        session_store = InMemorySessionStore()
        executor = EvalExecutor(
            registry=registry,
            run_store=InMemoryRunStore(PlaintextPayloadCodec()),
            session_store=session_store,
        )
        suite = _multi_case_suite(1)
        item = suite.expand()[0]
        # 第一次执行：Run SUCCEEDED、Turn 已提交、claim 已被 commit
        # 清除——「崩溃」发生在提交与 Observation 落盘之间。
        first = await executor.execute_item(
            suite=suite, item=item, execution_id="exec-claim-1"
        )
        # 恢复重放：绝不因「该 run 已提交过 Turn」的 fail-closed 而
        # 永久失败（旧实现在此抛 SessionClaimConflictError）。
        second = await executor.execute_item(
            suite=suite, item=item, execution_id="exec-claim-1"
        )
        self.assertEqual(second.observation_id, first.observation_id)
        self.assertEqual(second.completeness.value, "COMPLETE")
        # subject Run 不重复 dispatch；Session 历史幂等去重。
        self.assertEqual(adapter.call_count, 1)
        scope = EvalExecutor.session_scope("exec-claim-1")
        session_id = EvalExecutor.session_id(item)
        snapshot = await session_store.read_snapshot(scope, session_id)
        self.assertEqual(len(snapshot.turns), 1)
        self.assertIsNone(await session_store.get_claim(scope, session_id))
