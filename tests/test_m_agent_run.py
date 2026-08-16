"""Ticket 01 主行为测试：只通过公开异步 Runner 的注册、创建、启动与
查询路径驱动，断言外部可观测的 Run 记录、状态、Step Attempt 与
Checkpoint，不触碰内部实现细节。
"""

from __future__ import annotations

import unittest

from m_agent import (
    AgentDefinition,
    DefinitionConflictError,
    DefinitionNotFoundError,
    DefinitionRegistry,
    DeterministicModelAdapter,
    IllegalRunTransitionError,
    InMemoryRunStore,
    ModelAdapter,
    ModelCapabilities,
    ModelCapabilityError,
    ModelPurpose,
    ModelRequirements,
    ModelRequest,
    ModelResponse,
    PlaintextPayloadCodec,
    RunNotFoundError,
    Runner,
    RunStatus,
    StepStatus,
    StepType,
    StructuredOutputMode,
    StreamingMode,
    ToolCallingMode,
    deserialize_model_response,
    is_terminal,
)


def make_registry() -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition(
            definition_id="assistant",
            version="1.0",
            instructions="Answer deterministically.",
            model_adapter=DeterministicModelAdapter(
                responses=("deterministic answer",)
            ),
        )
    )
    return registry


def make_runner() -> tuple[Runner, DefinitionRegistry, InMemoryRunStore]:
    registry = make_registry()
    store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
    return Runner(registry=registry, store=store), registry, store


class MAgentPackageContractTests(unittest.TestCase):
    """m_agent 可导入、公开 async-first Runner API、状态词汇完整。"""

    def test_status_vocabulary_and_terminal_statuses(self) -> None:
        # Ticket AC：公开 Run 记录使用完整状态词汇，且正确标识终态。
        expected = {
            RunStatus.CREATED,
            RunStatus.RUNNING,
            RunStatus.WAITING,
            RunStatus.SUCCEEDED,
            RunStatus.REJECTED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
        self.assertEqual(set(RunStatus), expected)
        terminal = {s for s in RunStatus if s.is_terminal}
        self.assertEqual(
            terminal,
            {
                RunStatus.SUCCEEDED,
                RunStatus.REJECTED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            },
        )
        # 字符串形式亦可判断。
        self.assertTrue(is_terminal("SUCCEEDED"))
        self.assertTrue(is_terminal(RunStatus.FAILED))
        self.assertFalse(is_terminal("WAITING"))
        self.assertFalse(is_terminal(RunStatus.RUNNING))

    def test_public_runner_api_is_async_first(self) -> None:
        # Ticket AC：m_agent 暴露 async-first Runner API（create/start/查询）。
        import inspect

        runner, _, _ = make_runner()
        self.assertTrue(inspect.iscoroutinefunction(runner.create_run))
        self.assertTrue(inspect.iscoroutinefunction(runner.start_run))
        self.assertTrue(inspect.iscoroutinefunction(runner.get_run))
        self.assertTrue(inspect.iscoroutinefunction(runner.inspect_run))

    def test_fake_and_live_adapters_are_visibly_different(self) -> None:
        # Ticket AC：fake 与 live adapter 类型与 deterministic 标记明确区分，
        # 确定性测试不会被误认为供应商兼容性验证。
        fake = DeterministicModelAdapter(responses=("x",))
        self.assertTrue(fake.deterministic)
        self.assertIsInstance(fake, ModelAdapter)
        self.assertFalse(ModelAdapter.deterministic)
        # 确定性 fake 不得被当作 live provider 兼容性证据。
        self.assertNotEqual(type(fake).__name__, "ModelAdapter")


class DefinitionRegistryTests(unittest.IsolatedAsyncioTestCase):
    """Ticket AC：按精确 id+version 注册/解析，注册时校验 Model Capabilities。"""

    def test_register_and_resolve_by_exact_id_and_version(self) -> None:
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition(
                definition_id="assistant",
                version="1.0",
                instructions="v1 instructions",
                model_adapter=DeterministicModelAdapter(responses=("a",)),
            )
        )
        registry.register(
            AgentDefinition(
                definition_id="assistant",
                version="2.0",
                instructions="v2 instructions",
                model_adapter=DeterministicModelAdapter(responses=("b",)),
            )
        )
        resolved_v1 = registry.resolve("assistant", "1.0")
        resolved_v2 = registry.resolve("assistant", "2.0")
        self.assertEqual(resolved_v1.instructions, "v1 instructions")
        self.assertEqual(resolved_v2.instructions, "v2 instructions")
        # 不可变：修改 Definition 字段必须失败。
        with self.assertRaises((ValueError, TypeError)):
            resolved_v1.instructions = "mutated"  # type: ignore[misc]

    def test_resolve_missing_definition_fails(self) -> None:
        registry = DefinitionRegistry()
        with self.assertRaises(DefinitionNotFoundError):
            registry.resolve("assistant", "9.9")

    def test_duplicate_registration_of_same_id_and_version_rejected(
        self,
    ) -> None:
        registry = DefinitionRegistry()
        definition = AgentDefinition(
            definition_id="assistant",
            version="1.0",
            instructions="v1",
            model_adapter=DeterministicModelAdapter(responses=("a",)),
        )
        registry.register(definition)
        with self.assertRaises(DefinitionConflictError):
            registry.register(definition)

    def test_registration_rejects_undeclared_capabilities(self) -> None:
        # Ticket AC：required capabilities 未被 adapter 声明 → 拒绝，无静默降级。
        registry = DefinitionRegistry()
        adapter = DeterministicModelAdapter(
            responses=("a",), capabilities=ModelCapabilities()
        )
        definition = AgentDefinition(
            definition_id="assistant",
            version="1.0",
            instructions="v1",
            model_requirements=ModelRequirements(
                capabilities=ModelCapabilities(
                    streaming=StreamingMode.DELTA,
                    tool_calling=ToolCallingMode.NATIVE,
                )
            ),
            model_adapter=adapter,
        )
        with self.assertRaises(ModelCapabilityError):
            registry.register(definition)
        # 拒绝后定义不得被注册。
        self.assertFalse(registry.is_registered("assistant", "1.0"))

    def test_registration_accepts_declared_capabilities(self) -> None:
        registry = DefinitionRegistry()
        adapter = DeterministicModelAdapter(
            responses=("a",),
            capabilities=ModelCapabilities(
                streaming=StreamingMode.DELTA,
                tool_calling=ToolCallingMode.NATIVE,
                structured_output=StructuredOutputMode.NATIVE,
            ),
        )
        definition = AgentDefinition(
            definition_id="assistant",
            version="1.0",
            instructions="v1",
            model_requirements=ModelRequirements(
                capabilities=ModelCapabilities(
                    streaming=StreamingMode.DELTA,
                    tool_calling=ToolCallingMode.NATIVE,
                )
            ),
            model_adapter=adapter,
        )
        registry.register(definition)  # 不应抛错
        self.assertTrue(registry.is_registered("assistant", "1.0"))


class RunnerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Ticket AC：通过公开 Runner 创建、启动并检查一个版本化 Model Run。"""

    async def test_create_persists_created_record_before_any_model_call(
        self,
    ) -> None:
        # Ticket AC：create 后立即得到可检查的 CREATED 记录，且无模型调用。
        runner, registry, _ = make_runner()
        adapter = registry.resolve("assistant", "1.0").model_adapter

        created = await runner.create_run("assistant", "1.0", input="hi")
        self.assertEqual(created.status, RunStatus.CREATED)
        self.assertFalse(created.status.is_terminal)
        self.assertIsNone(created.snapshot)  # 启动时才冻结
        self.assertEqual(adapter.call_count, 0)

        stored = await runner.get_run(created.run_id)
        self.assertEqual(stored.status, RunStatus.CREATED)

    async def test_create_requires_registered_definition(self) -> None:
        runner, _, _ = make_runner()
        with self.assertRaises(DefinitionNotFoundError):
            await runner.create_run("assistant", "9.9", input="hi")

    async def test_start_freezes_snapshot_and_reaches_succeeded(self) -> None:
        # Ticket AC：启动冻结 Definition Snapshot、执行一个 Model Step、
        # 记录独立 Step Attempt 与 completed checkpoint，到达 SUCCEEDED。
        runner, registry, _ = make_runner()
        definition = registry.resolve("assistant", "1.0")
        adapter = definition.model_adapter

        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        self.assertTrue(terminal.status.is_terminal)
        self.assertEqual(terminal.output, "deterministic answer")
        self.assertIsNotNone(terminal.snapshot)
        self.assertEqual(terminal.snapshot.definition_id, "assistant")
        self.assertEqual(terminal.snapshot.version, "1.0")
        self.assertEqual(
            terminal.snapshot.instructions, definition.instructions
        )
        self.assertEqual(
            terminal.snapshot.model_bindings.for_purpose(ModelPurpose.PRIMARY).requirements,
            definition.model_requirements,
        )
        self.assertEqual(
            terminal.snapshot.model_bindings.for_purpose(
                ModelPurpose.PRIMARY
            ).contract.capabilities,
            adapter.model_contract.capabilities,
        )
        self.assertEqual(adapter.call_count, 1)
        # 模型请求携带 Definition 的 Agent Instruction。
        self.assertEqual(
            adapter.last_request,
            ModelRequest(input="hi", instructions=definition.instructions),
        )

    async def test_inspect_reports_step_attempt_and_checkpoint(self) -> None:
        # Ticket AC：inspect 读到 1 个 Model Step、1 个 Step Attempt 与 checkpoint。
        runner, _, _ = make_runner()
        created = await runner.create_run("assistant", "1.0", input="hi")
        await runner.start_run(created.run_id)

        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(inspection.run.status, RunStatus.SUCCEEDED)
        self.assertEqual(len(inspection.steps), 1)
        step = inspection.steps[0]
        self.assertEqual(step.step_type, StepType.MODEL)
        self.assertEqual(step.status, StepStatus.SUCCEEDED)
        self.assertEqual(len(inspection.attempts), 1)
        attempt = inspection.attempts[0]
        self.assertEqual(attempt.step_id, step.step_id)
        self.assertEqual(attempt.status, StepStatus.SUCCEEDED)
        # Ticket 05：Model Step 的 Attempt/Checkpoint 携带完整序列化
        # 响应（含 tool_calls），供恢复精确重建执行位置；内容可还原。
        self.assertEqual(
            deserialize_model_response(attempt.output).content,
            "deterministic answer",
        )
        self.assertEqual(len(inspection.checkpoints), 1)
        checkpoint = inspection.checkpoints[0]
        self.assertEqual(checkpoint.step_id, step.step_id)
        self.assertEqual(checkpoint.attempt_id, attempt.attempt_id)
        self.assertEqual(
            deserialize_model_response(checkpoint.output).content,
            "deterministic answer",
        )

    async def test_separate_runs_are_isolated(self) -> None:
        # 同一 Definition 的多个 Run 各自独立推进。
        runner, _, _ = make_runner()
        first = await runner.create_run("assistant", "1.0", input="one")
        second = await runner.create_run("assistant", "1.0", input="two")
        self.assertNotEqual(first.run_id, second.run_id)

        await runner.start_run(first.run_id)
        await runner.start_run(second.run_id)

        first_record = await runner.get_run(first.run_id)
        second_record = await runner.get_run(second.run_id)
        self.assertEqual(first_record.status, RunStatus.SUCCEEDED)
        self.assertEqual(second_record.status, RunStatus.SUCCEEDED)
        self.assertEqual(first_record.output, "deterministic answer")
        self.assertEqual(second_record.output, "deterministic answer")

    async def test_model_failure_reaches_failed_terminal(self) -> None:
        # 模型调用异常：记录失败 Attempt，Run 到达终态 FAILED（无重试）。
        class ExplodingAdapter(DeterministicModelAdapter):
            async def generate(self, request: ModelRequest) -> ModelResponse:
                self.call_count += 1
                raise RuntimeError("provider exploded")

        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition(
                definition_id="assistant",
                version="1.0",
                instructions="x",
                model_adapter=ExplodingAdapter(responses=("ignored",)),
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.FAILED)
        self.assertTrue(terminal.status.is_terminal)
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(len(inspection.steps), 1)
        self.assertEqual(inspection.steps[0].status, StepStatus.FAILED)
        self.assertEqual(len(inspection.attempts), 1)
        self.assertEqual(inspection.attempts[0].status, StepStatus.FAILED)
        self.assertEqual(
            inspection.attempts[0].error,
            "unclassified adapter exception: RuntimeError",
        )


class IllegalCommandsTests(unittest.IsolatedAsyncioTestCase):
    """Ticket AC：非法生命周期命令显式失败且不改动权威 Run 记录。"""

    async def test_start_missing_run_fails(self) -> None:
        runner, _, _ = make_runner()
        with self.assertRaises(RunNotFoundError):
            await runner.start_run("no-such-run")

    async def test_starting_an_already_terminal_run_fails(self) -> None:
        # 重复 start：终态 Run 不接受推进命令。
        runner, _, _ = make_runner()
        created = await runner.create_run("assistant", "1.0", input="hi")
        await runner.start_run(created.run_id)
        before = await runner.get_run(created.run_id)

        with self.assertRaises(IllegalRunTransitionError):
            await runner.start_run(created.run_id)

        # 权威记录未被改动。
        after = await runner.get_run(created.run_id)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.output, before.output)


if __name__ == "__main__":
    unittest.main()
