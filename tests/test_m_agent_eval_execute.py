"""Ticket 17 AC 2：EXECUTE 只在隔离的 Eval Store 与 fixture boundary 内执行。

可复现证据：

- 生产 Store 探针记录 EXECUTE 全程零调用（读/写均无），写入前后
  状态对比一致；subject Run 只出现在隔离 Eval RunStore；
- fixture boundary fail-closed：非确定性 Model Adapter（live 语义）
  或 fixture 未声明的外部效果工具在创建任何 Run 之前即被拒绝，
  隔离 Store 无写入、模型零 dispatch；
- 静态能力门槛缺失时产生 UNSUPPORTED Observation：零 Run、零 dispatch；
- 隔离 SessionStore 只承接 eval 自己的 Turn 提交。
"""

from __future__ import annotations

import unittest
from typing import Any

from m_agent.adapters import (
    DeterministicModelAdapter,
    DeterministicTool,
    InMemoryRunStore,
    PlaintextPayloadCodec,
)
from m_agent.companion import InMemorySessionStore
from m_agent.companion.eval import (
    REASON_MODEL_CAPABILITIES_MISSING,
    AgentVariant,
    EvalCase,
    EvalExecutor,
    EvalFixtureBoundaryError,
    EvalMode,
    EvalSuite,
    EvaluatorRef,
    ExecutionProtocol,
    FixtureBundle,
    FixtureFact,
    InMemoryEvalStore,
)
from m_agent.runtime import (
    AgentDefinition,
    DefinitionRegistry,
    DuplicateRunError,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    RunStatus,
    Runner,
    ToolCallingMode,
    ToolEffect,
)


class _LiveLikeAdapter(DeterministicModelAdapter):
    """模拟 live 语义的 Adapter：deterministic=False（访问外部系统）。"""

    deterministic = False

    def __init__(self) -> None:
        super().__init__(responses=("live-response",))


class _ProbeStore:
    """记录调用并区分读/写的探针 Store（测试本地证据装置）。"""

    def __init__(self, inner):  # noqa: ANN001 - 测试装置
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "read_calls", [])
        object.__setattr__(self, "write_calls", [])

    def __getattr__(self, name):  # noqa: ANN001
        inner = object.__getattribute__(self, "_inner")
        reads = object.__getattribute__(self, "read_calls")
        writes = object.__getattribute__(self, "write_calls")

        async def call(*args, **kwargs):
            attr = getattr(inner, name)
            if name.startswith(("get_", "list_")):
                reads.append(name)
            else:
                writes.append(name)
            return await attr(*args, **kwargs)

        return call

    async def state_snapshot(self, run_ids):  # noqa: ANN001
        return {
            run_id: await object.__getattribute__(self, "_inner").get_run(run_id)
            for run_id in run_ids
        }


def _definition(
    adapter: DeterministicModelAdapter,
    *,
    version: str = "1.0",
    tools=(),  # noqa: ANN001
) -> AgentDefinition:
    return AgentDefinition.for_adapter(
        definition_id="assistant",
        version=version,
        instructions="assistant instructions",
        model_adapter=adapter,
        tools=list(tools),
    )


def _bundle(effects=("ledger_write",)) -> FixtureBundle:
    return FixtureBundle.build(
        bundle_id="bundle-1",
        facts=(FixtureFact(fact_id="order", payload='{"status":"shipped"}'),),
        declared_external_effects=effects,
        expected_evidence_ids=("ledger",),
    )


def _case(case_id: str = "case-1", **overrides) -> EvalCase:
    values: dict[str, Any] = dict(
        case_id=case_id,
        input="check the order",
        variant=AgentVariant(
            variant_id="variant-a", definition_id="assistant",
            definition_version="1.0",
        ),
        fixture_bundle=_bundle(),
        execution_protocol=ExecutionProtocol(deterministic=True),
        evaluators=(EvaluatorRef(evaluator_id="output-match", version="1.0"),),
        tags=("isolation",),
    )
    values.update(overrides)
    return EvalCase(**values)


def _suite(case: EvalCase) -> EvalSuite:
    return EvalSuite(
        suite_id="suite-1",
        version="1.0",
        cases=(case,),
        variants=(case.variant,),
    )


class ExecuteIsolationTests(unittest.IsolatedAsyncioTestCase):
    """AC 2：隔离执行与生产 Store 零污染证据。"""

    async def test_execute_creates_subject_run_only_in_isolated_store(self) -> None:
        adapter = DeterministicModelAdapter(responses=("order is shipped",))
        registry = DefinitionRegistry()
        registry.register(_definition(adapter))

        production_probe = _ProbeStore(InMemoryRunStore(PlaintextPayloadCodec()))
        production_runner = Runner(registry=registry, store=production_probe)
        # 生产 Store 中已有一条 Run：EXECUTE 前后状态必须一致。
        await production_runner.create_run(
            "assistant", "1.0", "production input", run_id="production-run"
        )
        before = await production_probe.state_snapshot(["production-run"])
        read_marker = len(production_probe.read_calls)
        write_marker = len(production_probe.write_calls)

        eval_probe = _ProbeStore(InMemoryRunStore(PlaintextPayloadCodec()))
        executor = EvalExecutor(registry=registry, run_store=eval_probe)
        suite = _suite(_case())
        item = suite.expand()[0]

        observation = await executor.execute_item(
            suite=suite, item=item, execution_id="exec-1"
        )

        self.assertEqual(observation.mode, EvalMode.EXECUTE)
        self.assertEqual(observation.completeness.value, "COMPLETE")
        run_id = EvalExecutor.subject_run_id("exec-1", item)
        self.assertEqual(observation.subject_run_id, run_id)
        run = await object.__getattribute__(eval_probe, "_inner").get_run(run_id)
        self.assertIsNotNone(run)
        self.assertEqual(run.status, RunStatus.SUCCEEDED)
        self.assertEqual(run.output, "order is shipped")

        # 生产 Store：EXECUTE 全程零调用，状态前后一致（可复现证据）。
        self.assertEqual(production_probe.write_calls[write_marker:], [])
        self.assertEqual(production_probe.read_calls[read_marker:], [])
        after = await production_probe.state_snapshot(["production-run"])
        self.assertEqual(before, after)

    async def test_execute_fails_closed_on_nondeterministic_adapter(self) -> None:
        adapter = _LiveLikeAdapter()
        registry = DefinitionRegistry()
        registry.register(_definition(adapter))
        eval_probe = _ProbeStore(InMemoryRunStore(PlaintextPayloadCodec()))
        executor = EvalExecutor(registry=registry, run_store=eval_probe)
        suite = _suite(_case())

        with self.assertRaises(EvalFixtureBoundaryError):
            await executor.execute_item(
                suite=suite, item=suite.expand()[0], execution_id="exec-1"
            )
        # fail closed 发生在任何 Run 创建与模型 dispatch 之前。
        self.assertEqual(eval_probe.write_calls, [])
        self.assertEqual(adapter.call_count, 0)

    async def test_execute_fails_closed_on_undeclared_external_tool(self) -> None:
        adapter = DeterministicModelAdapter(
            responses=("done",),
            capabilities=ModelCapabilities(
                tool_calling=ToolCallingMode.NATIVE
            ),
        )
        ledger = DeterministicTool(
            name="ledger_write",
            effect=ToolEffect.NON_IDEMPOTENT,
            handler=lambda request: "written",
        )
        registry = DefinitionRegistry()
        registry.register(_definition(adapter, tools=[ledger]))

        strict_bundle = FixtureBundle.build(
            bundle_id="bundle-1", facts=(),
            declared_external_effects=(), expected_evidence_ids=(),
        )
        case = _case(fixture_bundle=strict_bundle)
        suite = _suite(case)
        eval_probe = _ProbeStore(InMemoryRunStore(PlaintextPayloadCodec()))
        executor = EvalExecutor(registry=registry, run_store=eval_probe)

        with self.assertRaises(EvalFixtureBoundaryError):
            await executor.execute_item(
                suite=suite, item=suite.expand()[0], execution_id="exec-1"
            )
        self.assertEqual(eval_probe.write_calls, [])
        self.assertEqual(adapter.call_count, 0)

        # fixture 声明该外部效果后，同一 Definition 可以在边界内执行。
        declared = _suite(_case())
        observation = await executor.execute_item(
            suite=declared, item=declared.expand()[0], execution_id="exec-2"
        )
        self.assertEqual(observation.completeness.value, "COMPLETE")

    async def test_execute_unsupported_capabilities_short_circuits(self) -> None:
        adapter = DeterministicModelAdapter(responses=("plain",))
        registry = DefinitionRegistry()
        registry.register(_definition(adapter))
        case = _case(
            required_capabilities=ModelCapabilities(structured_output=True)
        )
        suite = _suite(case)
        eval_probe = _ProbeStore(InMemoryRunStore(PlaintextPayloadCodec()))
        executor = EvalExecutor(registry=registry, run_store=eval_probe)

        observation = await executor.execute_item(
            suite=suite, item=suite.expand()[0], execution_id="exec-1"
        )

        self.assertEqual(observation.completeness.value, "UNSUPPORTED")
        self.assertEqual(
            observation.reason_code, REASON_MODEL_CAPABILITIES_MISSING
        )
        # UNSUPPORTED：零 Run 创建、零 provider 请求（静态短路）。
        self.assertEqual(eval_probe.write_calls, [])
        self.assertEqual(adapter.call_count, 0)


class ExecuteIdentityAndSessionTests(unittest.IsolatedAsyncioTestCase):
    """AC 2：确定性 run identity 与隔离 SessionStore 提交。"""

    def _executor(self, **kwargs):  # noqa: ANN003
        adapter = DeterministicModelAdapter(responses=("ok",))
        registry = DefinitionRegistry()
        registry.register(_definition(adapter))
        values: dict[str, Any] = dict(
            registry=registry,
            run_store=InMemoryRunStore(PlaintextPayloadCodec()),
        )
        values.update(kwargs)
        return EvalExecutor(**values)

    async def test_subject_run_identity_is_deterministic(self) -> None:
        executor = self._executor()
        suite = _suite(_case())
        item = suite.expand()[0]
        self.assertEqual(
            EvalExecutor.subject_run_id("exec-1", item),
            EvalExecutor.subject_run_id("exec-1", item),
        )
        await executor.execute_item(
            suite=suite, item=item, execution_id="exec-1"
        )
        # 同一 execution 下重放同一 item：确定性身份冲突而非重复执行。
        with self.assertRaises(DuplicateRunError):
            await executor.execute_item(
                suite=suite, item=item, execution_id="exec-1"
            )

    async def test_isolated_session_store_commits_only_eval_turns(self) -> None:
        session_store = InMemorySessionStore()
        executor = self._executor(session_store=session_store)
        suite = _suite(_case())
        item = suite.expand()[0]

        observation = await executor.execute_item(
            suite=suite, item=item, execution_id="exec-1"
        )

        scope = EvalExecutor.session_scope("exec-1")
        session_id = EvalExecutor.session_id(item)
        snapshot = await session_store.read_snapshot(scope, session_id)
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(len(snapshot.turns), 1)
        self.assertEqual(
            snapshot.turns[0].run_id, observation.subject_run_id
        )
        self.assertEqual(snapshot.turns[0].user_input, "check the order")
        self.assertIsNone(
            await session_store.get_claim(scope, session_id)
        )


class ExecuteSuiteRecordingTests(unittest.IsolatedAsyncioTestCase):
    """AC 2 + AC 8：execute_suite 记录 execution 与 observations。"""

    async def test_execute_suite_records_execution_and_observations(self) -> None:
        adapter = DeterministicModelAdapter(responses=("ok",))
        registry = DefinitionRegistry()
        registry.register(_definition(adapter))
        executor = EvalExecutor(
            registry=registry,
            run_store=InMemoryRunStore(PlaintextPayloadCodec()),
        )
        store = InMemoryEvalStore()
        suite = _suite(_case())

        result = await executor.execute_suite(suite, store=store)

        items = suite.expand()
        self.assertEqual(result.execution.suite_id, "suite-1")
        self.assertEqual(result.execution.suite_version, "1.0")
        self.assertEqual(
            result.execution.suite_digest, suite.content_digest()
        )
        self.assertEqual(result.execution.mode, EvalMode.EXECUTE)
        self.assertEqual(
            result.execution.item_ids, tuple(i.item_id for i in items)
        )
        self.assertEqual(len(result.observations), len(items))
        for observation in result.observations:
            self.assertEqual(observation.completeness.value, "COMPLETE")
            self.assertEqual(
                await store.get_observation(observation.observation_id),
                observation,
            )
        self.assertEqual(
            await store.get_execution(result.execution.execution_id),
            result.execution,
        )
        view = await store.execution_view(result.execution.execution_id)
        self.assertIsNotNone(view)
        assert view is not None  # type narrowing for mypy
        self.assertEqual(
            tuple(view.observation_ids),
            tuple(o.observation_id for o in result.observations),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
