"""Ticket 05 恢复测试：工具执行过程中断后，从 Run Store 精确恢复。

恢复语义（ADR 0003 at-least-once / Ticket 02 既有契约）扩展到 Tool
Step：

- 已确认的 Tool Step checkpoint 复用：恢复**不重复执行**外部副作用，
  Tool Outcome 从 checkpoint 还原并作为数据继续交给模型循环；
- Tool checkpoint 落盘前中断：该 Tool Step 重新执行（at-least-once，
  绝不宣称 exactly-once）；
- 同一响应内的多个工具调用部分确认：只执行缺失的调用，已确认的
  调用不重复；
- 最终 Model 响应 checkpoint 后中断：复用最终响应直接写入 SUCCEEDED，
  模型不再被调用。

所有断言只通过公开 Runner API（create/start/resume/inspect）与公开
数据模型驱动；崩溃通过 :class:`CrashPoint` crash_hook 确定性注入，
租约过期通过 :class:`FakeClock` 确定性推进（无真实 sleep）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from m_agent import (
    DEFAULT_LEASE_TTL,
    AgentDefinition,
    CrashPoint,
    DefinitionRegistry,
    DeterministicModelAdapter,
    DeterministicTool,
    FakeClock,
    InMemoryRunStore,
    ModelCapabilities,
    ModelRequirements,
    ModelRequest,
    ModelResponse,
    PlaintextPayloadCodec,
    RetryPolicy,
    Runner,
    RunStatus,
    SQLiteRunStore,
    StepStatus,
    StepType,
    ToolCall,
    ToolCallingMode,
    ToolEffect,
    ToolOutcome,
    deserialize_model_response,
    deserialize_tool_outcome,
)

TOOL_CALLING_CAPABILITIES = ModelCapabilities(
    tool_calling=ToolCallingMode.NATIVE
)


class LookupTool(DeterministicTool):
    """READ_ONLY fake 工具：每次调用计数并返回 SUCCESS。"""

    def __init__(self) -> None:
        super().__init__(
            name="lookup_order", effect=ToolEffect.READ_ONLY
        )
        self.call_count = 0

    async def invoke(self, request) -> ToolOutcome:
        self.call_count += 1
        return ToolOutcome.success(
            request.call_id, self.name, result=f"order-{request.call_id}"
        )


class ToolThenAnswerModel(DeterministicModelAdapter):
    """确定性模型：基于请求内容（是否已收到工具结果）决定响应。

    恢复场景下模型适配器不能依赖进程内 call_count（第二进程从零
    开始）；真实 LLM adapter 也是依据完整请求（含 tool_outcomes）
    独立生成响应，本 fake 模拟这一行为。
    """

    def __init__(self) -> None:
        super().__init__(capabilities=TOOL_CALLING_CAPABILITIES)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        if not request.tool_outcomes:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="call-1",
                        tool_name="lookup_order",
                        arguments="{}",
                    ),
                )
            )
        return ModelResponse(
            content="final: " + request.tool_outcomes[0].result
        )


class TwoToolsModel(DeterministicModelAdapter):
    """确定性模型：同一响应请求两个工具，收到结果后给出最终答案。"""

    def __init__(self) -> None:
        super().__init__(capabilities=TOOL_CALLING_CAPABILITIES)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        if len(request.tool_outcomes) == 0:
            return ModelResponse(
                tool_calls=(
                    ToolCall(call_id="c1", tool_name="tool_a", arguments="{}"),
                    ToolCall(call_id="c2", tool_name="tool_b", arguments="{}"),
                )
            )
        results = tuple(o.call_id for o in request.tool_outcomes)
        return ModelResponse(content=f"final: {results}")


class ThreeToolsModel(DeterministicModelAdapter):
    """同一响应请求三个工具，用于验证恢复后不会复用旧 in-flight 身份。"""

    def __init__(self) -> None:
        super().__init__(capabilities=TOOL_CALLING_CAPABILITIES)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        if not request.tool_outcomes:
            return ModelResponse(
                tool_calls=(
                    ToolCall(call_id="c1", tool_name="tool_a", arguments="{}"),
                    ToolCall(call_id="c2", tool_name="tool_b", arguments="{}"),
                    ToolCall(call_id="c3", tool_name="tool_c", arguments="{}"),
                )
            )
        return ModelResponse(
            content=f"final: {tuple(o.call_id for o in request.tool_outcomes)}"
        )


class SimpleTool(DeterministicTool):
    """按名区分的 fake 工具（call_id 前缀计数）。"""

    def __init__(self, name: str) -> None:
        super().__init__(name=name, effect=ToolEffect.READ_ONLY)
        self.call_count = 0

    async def invoke(self, request) -> ToolOutcome:
        self.call_count += 1
        return ToolOutcome.success(request.call_id, self.name, result="ok")


def build_registry(
    model: DeterministicModelAdapter,
    tools: tuple[DeterministicTool, ...],
    *,
    retry_policy: RetryPolicy | None = None,
) -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition(
            definition_id="assistant",
            version="1.0",
            instructions="Answer deterministically.",
            model_requirements=ModelRequirements(
                capabilities=TOOL_CALLING_CAPABILITIES
            ),
            model_adapter=model,
            tools=tools,
            retry_policy=retry_policy,
        )
    )
    return registry


class ToolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """InMemoryStore：工具执行各中断点的确定性恢复。"""

    def make_runner(
        self,
        model: DeterministicModelAdapter,
        tools: tuple[DeterministicTool, ...],
        crash_hook=None,
        clock: FakeClock | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> tuple[Runner, InMemoryRunStore, FakeClock]:
        registry = build_registry(model, tools, retry_policy=retry_policy)
        if clock is None:
            clock = FakeClock()
        store = InMemoryRunStore(
            payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        runner = Runner(
            registry=registry, store=store, crash_hook=crash_hook
        )
        return runner, store, clock

    async def test_dispatch_crash_leaves_original_tool_step_and_attempt(self) -> None:
        """dispatch 后、outcome checkpoint 前崩溃仍保留原始身份。"""
        def hook(p: CrashPoint, run_id: str) -> None:
            if p is CrashPoint.BEFORE_TOOL_CHECKPOINT:
                raise RuntimeError("injected crash after tool dispatch")

        clock = FakeClock()
        model = ToolThenAnswerModel()
        tool = LookupTool()
        tool.effect = ToolEffect.NON_IDEMPOTENT
        runner, store, _ = self.make_runner(
            model, (tool,), crash_hook=hook, clock=clock
        )
        created = await runner.create_run("assistant", "1.0", input="hi")

        with self.assertRaises(RuntimeError):
            await runner.start_run(created.run_id)

        inspection = await runner.inspect_run(created.run_id)
        tool_steps = [s for s in inspection.steps if s.step_type is StepType.TOOL]
        self.assertEqual(len(tool_steps), 1)
        tool_step = tool_steps[0]
        self.assertEqual(tool_step.status, StepStatus.RUNNING)
        tool_attempts = [
            a for a in inspection.attempts if a.step_id == tool_step.step_id
        ]
        self.assertEqual(len(tool_attempts), 1)
        tool_attempt = tool_attempts[0]
        self.assertEqual(tool_attempt.status, StepStatus.RUNNING)
        self.assertNotEqual(tool_attempt.attempt_id, tool_step.step_id)
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(
            [c.step_type for c in inspection.checkpoints], [StepType.MODEL]
        )

        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        resumed = await Runner(
            registry=build_registry(model, (tool,)), store=store
        ).resume_run(created.run_id)
        self.assertEqual(resumed.status, RunStatus.WAITING)
        recovered = await runner.inspect_run(created.run_id)
        recovered_step = next(
            s for s in recovered.steps if s.step_type is StepType.TOOL
        )
        recovered_attempt = next(
            a
            for a in recovered.attempts
            if a.step_id == recovered_step.step_id
        )
        self.assertEqual(recovered_step.step_id, tool_step.step_id)
        self.assertEqual(recovered_attempt.attempt_id, tool_attempt.attempt_id)
        self.assertEqual(recovered_attempt.status, StepStatus.FAILED)

    async def test_resume_after_tool_checkpoint_reuses_outcome(self) -> None:
        # 工具 checkpoint 落盘后崩溃：恢复复用 outcome，工具不再被调用，
        # 模型收到同一份数据并完成 Run。
        def hook(p: CrashPoint, run_id: str) -> None:
            if p is CrashPoint.AFTER_TOOL_CHECKPOINT:
                raise RuntimeError("injected crash after tool checkpoint")

        clock = FakeClock()
        model = ToolThenAnswerModel()
        tool = LookupTool()
        runner, store, _ = self.make_runner(
            model, (tool,), crash_hook=hook, clock=clock
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        with self.assertRaises(RuntimeError):
            await runner.start_run(created.run_id)

        # 崩溃后：RUNNING，MODEL + TOOL checkpoint 已确认，工具已调用一次。
        crashed = await store.get_run(created.run_id)
        self.assertEqual(crashed.status, RunStatus.RUNNING)
        checkpoints = await store.get_checkpoints(created.run_id)
        self.assertEqual(
            [c.step_type for c in checkpoints], [StepType.MODEL, StepType.TOOL]
        )
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(model.call_count, 1)

        # 租约过期后由新 Runner 接管并恢复（无 crash hook）。
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        resumed = await Runner(
            registry=build_registry(model, (tool,)), store=store
        ).resume_run(created.run_id)

        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        self.assertEqual(resumed.output, "final: order-call-1")
        # 工具不重复执行（side-effect counter 保持 1）；模型只补一次最终
        # 响应（第一次调用已确认，不重复）。
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(model.call_count, 2)
        # 模型收到的 outcome 来自 checkpoint（call-1 / order-call-1）。
        self.assertEqual(
            model.last_request.tool_outcomes[0].call_id, "call-1"
        )
        self.assertEqual(
            model.last_request.tool_outcomes[0].result, "order-call-1"
        )
        # Step trajectory 不重复：仍只有 1 个 TOOL Step。
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(
            [s.step_type for s in inspection.steps],
            [StepType.MODEL, StepType.TOOL, StepType.MODEL],
        )
        self.assertEqual(
            [
                c.step_type for c in inspection.checkpoints
            ],
            [StepType.MODEL, StepType.TOOL, StepType.MODEL],
        )

    async def test_resume_before_tool_checkpoint_reexecutes_tool(self) -> None:
        # 工具执行完成但 checkpoint 落盘前崩溃：at-least-once，恢复时
        # Tool Step 重新执行（外部副作用可能发生不止一次）。
        def hook(p: CrashPoint, run_id: str) -> None:
            if p is CrashPoint.BEFORE_TOOL_CHECKPOINT:
                raise RuntimeError("injected crash before tool checkpoint")

        clock = FakeClock()
        model = ToolThenAnswerModel()
        tool = LookupTool()
        runner, store, _ = self.make_runner(
            model,
            (tool,),
            crash_hook=hook,
            clock=clock,
            retry_policy=RetryPolicy(max_attempts=2),
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        with self.assertRaises(RuntimeError):
            await runner.start_run(created.run_id)

        # 工具已执行但无 TOOL checkpoint。
        self.assertEqual(tool.call_count, 1)
        checkpoints = await store.get_checkpoints(created.run_id)
        self.assertEqual(
            [c.step_type for c in checkpoints], [StepType.MODEL]
        )

        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        resumed = await Runner(
            registry=build_registry(
                model, (tool,), retry_policy=RetryPolicy(max_attempts=2)
            ),
            store=store,
        ).resume_run(created.run_id)
        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        # 工具被第二次执行（at-least-once，不宣称 exactly-once）。
        self.assertEqual(tool.call_count, 2)

    async def test_resume_after_model_checkpoint_runs_tool_then_finishes(
        self,
    ) -> None:
        # 第一次模型响应（请求工具）checkpoint 后崩溃：恢复时从 Tool
        # Step 继续，不重复已确认的模型调用。
        def hook(p: CrashPoint, run_id: str) -> None:
            if p is CrashPoint.AFTER_MODEL_CHECKPOINT:
                raise RuntimeError("injected crash after model checkpoint")

        clock = FakeClock()
        model = ToolThenAnswerModel()
        tool = LookupTool()
        runner, store, _ = self.make_runner(
            model, (tool,), crash_hook=hook, clock=clock
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        with self.assertRaises(RuntimeError):
            await runner.start_run(created.run_id)

        self.assertEqual(model.call_count, 1)
        self.assertEqual(tool.call_count, 0)
        checkpoints = await store.get_checkpoints(created.run_id)
        self.assertEqual(
            [c.step_type for c in checkpoints], [StepType.MODEL]
        )

        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        resumed = await Runner(
            registry=build_registry(model, (tool,)), store=store
        ).resume_run(created.run_id)
        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        self.assertEqual(tool.call_count, 1)
        # 已确认的第一次模型调用不重复：总共只调用两次（请求工具 + 最终）。
        self.assertEqual(model.call_count, 2)

    async def test_resume_partial_multi_tool_only_reruns_missing(self) -> None:
        # 同一响应请求 c1、c2；c1 已 checkpoint、c2 执行后未 checkpoint
        # 时崩溃：恢复只重新执行 c2，绝不重复 c1 的外部副作用。
        tool_a = SimpleTool("tool_a")
        tool_b = SimpleTool("tool_b")
        attempts = 0

        def hook(p: CrashPoint, run_id: str) -> None:
            nonlocal attempts
            if p is CrashPoint.BEFORE_TOOL_CHECKPOINT:
                attempts += 1
                if attempts == 2:  # 第二个工具（c2）checkpoint 前崩溃
                    raise RuntimeError("injected crash on second tool")

        clock = FakeClock()
        model = TwoToolsModel()
        runner, store, _ = self.make_runner(
            model,
            (tool_a, tool_b),
            crash_hook=hook,
            clock=clock,
            retry_policy=RetryPolicy(max_attempts=2),
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        with self.assertRaises(RuntimeError):
            await runner.start_run(created.run_id)

        # c1 已确认，c2 未确认。
        self.assertEqual(tool_a.call_count, 1)
        self.assertEqual(tool_b.call_count, 1)
        checkpoints = await store.get_checkpoints(created.run_id)
        self.assertEqual(
            [c.step_type for c in checkpoints],
            [StepType.MODEL, StepType.TOOL],
        )
        confirmed = deserialize_tool_outcome(checkpoints[1].output)
        self.assertEqual(confirmed.call_id, "c1")

        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        resumed = await Runner(
            registry=build_registry(
                model, (tool_a, tool_b), retry_policy=RetryPolicy(max_attempts=2)
            ),
            store=store,
        ).resume_run(created.run_id)
        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        self.assertEqual(resumed.output, "final: ('c1', 'c2')")
        # c1 不重复执行；c2 重新执行一次（at-least-once）。
        self.assertEqual(tool_a.call_count, 1)
        self.assertEqual(tool_b.call_count, 2)
        # 恢复后模型只补最终响应（第一次模型调用已确认，不重复）。
        self.assertEqual(model.call_count, 2)
        # 最终模型请求收到 c1（复用）+ c2（重试）两个 outcome。
        self.assertEqual(
            tuple(o.call_id for o in model.last_request.tool_outcomes),
            ("c1", "c2"),
        )

    async def test_recovery_gives_later_tools_fresh_identities(self) -> None:
        # c2 dispatch 后崩溃；恢复时 c2 复用原 Step 并产生新 Attempt，
        # 尚未 dispatch 的 c3 必须拥有全新的 Step / Attempt identity。
        tool_a = SimpleTool("tool_a")
        tool_b = SimpleTool("tool_b")
        tool_c = SimpleTool("tool_c")
        dispatches = 0

        def hook(p: CrashPoint, run_id: str) -> None:
            nonlocal dispatches
            if p is CrashPoint.BEFORE_TOOL_CHECKPOINT:
                dispatches += 1
                if dispatches == 2:
                    raise RuntimeError("injected crash on second tool")

        clock = FakeClock()
        model = ThreeToolsModel()
        runner, store, _ = self.make_runner(
            model,
            (tool_a, tool_b, tool_c),
            crash_hook=hook,
            clock=clock,
            retry_policy=RetryPolicy(max_attempts=2),
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        with self.assertRaises(RuntimeError):
            await runner.start_run(created.run_id)

        crashed = await runner.inspect_run(created.run_id)
        crashed_c2 = [
            step
            for step in crashed.steps
            if step.step_type is StepType.TOOL
            and step.status is StepStatus.RUNNING
        ]
        self.assertEqual(len(crashed_c2), 1)
        c2_step_id = crashed_c2[0].step_id

        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        terminal = await Runner(
            registry=build_registry(
                model,
                (tool_a, tool_b, tool_c),
                retry_policy=RetryPolicy(max_attempts=2),
            ),
            store=store,
        ).resume_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(
            (tool_a.call_count, tool_b.call_count, tool_c.call_count), (1, 2, 1)
        )
        inspection = await runner.inspect_run(created.run_id)
        tool_steps = [
            step for step in inspection.steps if step.step_type is StepType.TOOL
        ]
        self.assertEqual(len(tool_steps), 3)
        self.assertEqual(len({step.step_id for step in tool_steps}), 3)
        self.assertIn(c2_step_id, {step.step_id for step in tool_steps})
        self.assertEqual(
            [
                deserialize_tool_outcome(checkpoint.output).call_id
                for checkpoint in inspection.checkpoints
                if checkpoint.step_type is StepType.TOOL
            ],
            ["c1", "c2", "c3"],
        )

    async def test_resume_final_model_checkpoint_skips_model_call(self) -> None:
        # 最终模型响应（不含工具请求）checkpoint 后崩溃：恢复复用最终
        # 响应直接 SUCCEEDED，模型不再被调用。
        model_calls = 0

        def hook(p: CrashPoint, run_id: str) -> None:
            nonlocal model_calls
            if p is CrashPoint.AFTER_MODEL_CHECKPOINT:
                model_calls += 1
                if model_calls == 2:  # 最终响应 checkpoint 后崩溃
                    raise RuntimeError("injected crash after final checkpoint")

        clock = FakeClock()
        model = ToolThenAnswerModel()
        tool = LookupTool()
        runner, store, _ = self.make_runner(
            model, (tool,), crash_hook=hook, clock=clock
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        with self.assertRaises(RuntimeError):
            await runner.start_run(created.run_id)

        # 全部步骤已 checkpoint，但终态未写入。
        self.assertEqual(model.call_count, 2)
        checkpoints = await store.get_checkpoints(created.run_id)
        self.assertEqual(
            [c.step_type for c in checkpoints],
            [StepType.MODEL, StepType.TOOL, StepType.MODEL],
        )

        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        resumed = await Runner(
            registry=build_registry(model, (tool,)), store=store
        ).resume_run(created.run_id)
        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        self.assertEqual(resumed.output, "final: order-call-1")
        # 模型与工具都不再被调用。
        self.assertEqual(model.call_count, 2)
        self.assertEqual(tool.call_count, 1)


class SQLiteToolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """SQLiteRunStore：工具中断后的持久化恢复（跨进程语义同进程验证）。"""

    async def test_sqlite_reopen_preserves_dispatch_identity_before_checkpoint(
        self,
    ) -> None:
        # 外部效果已 dispatch、outcome checkpoint 尚未提交时崩溃：重开
        # SQLite 后仍能读到原始 Step / Attempt，并由 recovery 标记同一
        # Attempt 为不确定失败，而不是新造一份无来源记录。
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "tool_run.db")

            def hook(p: CrashPoint, run_id: str) -> None:
                if p is CrashPoint.BEFORE_TOOL_CHECKPOINT:
                    raise RuntimeError("injected crash after tool dispatch")

            clock = FakeClock()
            tool = LookupTool()
            tool.effect = ToolEffect.NON_IDEMPOTENT
            store = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            runner = Runner(
                registry=build_registry(ToolThenAnswerModel(), (tool,)),
                store=store,
                crash_hook=hook,
            )
            created = await runner.create_run("assistant", "1.0", input="hi")
            with self.assertRaises(RuntimeError):
                await runner.start_run(created.run_id)
            crashed = await runner.inspect_run(created.run_id)
            crashed_step = next(
                step
                for step in crashed.steps
                if step.step_type is StepType.TOOL
            )
            crashed_attempt = next(
                attempt
                for attempt in crashed.attempts
                if attempt.step_id == crashed_step.step_id
            )
            self.assertEqual(crashed_step.status, StepStatus.RUNNING)
            self.assertEqual(crashed_attempt.status, StepStatus.RUNNING)
            self.assertEqual(tool.call_count, 1)
            store.close()

            probe = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
            try:
                crashed_run = await probe.get_run(created.run_id)
                assert crashed_run is not None
                assert crashed_run.lease_expires_at is not None
                restart = crashed_run.lease_expires_at + timedelta(seconds=1)
            finally:
                probe.close()

            recovered_tool = LookupTool()
            recovered_tool.effect = ToolEffect.NON_IDEMPOTENT
            store2 = SQLiteRunStore(
                db,
                payload_codec=PlaintextPayloadCodec(),
                clock=FakeClock(start=restart),
            )
            try:
                runner2 = Runner(
                    registry=build_registry(
                        ToolThenAnswerModel(), (recovered_tool,)
                    ),
                    store=store2,
                )
                before_resume = await runner2.inspect_run(created.run_id)
                before_step = next(
                    step
                    for step in before_resume.steps
                    if step.step_type is StepType.TOOL
                )
                before_attempt = next(
                    attempt
                    for attempt in before_resume.attempts
                    if attempt.step_id == before_step.step_id
                )
                self.assertEqual(before_step.step_id, crashed_step.step_id)
                self.assertEqual(before_attempt.attempt_id, crashed_attempt.attempt_id)
                self.assertEqual(before_attempt.status, StepStatus.RUNNING)

                waiting = await runner2.resume_run(created.run_id)

                self.assertEqual(waiting.status, RunStatus.WAITING)
                self.assertEqual(recovered_tool.call_count, 0)
                recovered = await runner2.inspect_run(created.run_id)
                failed_attempt = next(
                    attempt
                    for attempt in recovered.attempts
                    if attempt.step_id == waiting.waiting_step_id
                )
                self.assertEqual(waiting.waiting_step_id, crashed_step.step_id)
                self.assertEqual(failed_attempt.attempt_id, crashed_attempt.attempt_id)
                self.assertEqual(failed_attempt.status, StepStatus.FAILED)
            finally:
                store2.close()

    async def test_resume_from_sqlite_reuses_tool_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "tool_run.db")

            def hook(p: CrashPoint, run_id: str) -> None:
                if p is CrashPoint.AFTER_TOOL_CHECKPOINT:
                    raise RuntimeError("injected crash after tool checkpoint")

            model = ToolThenAnswerModel()
            tool = LookupTool()
            registry = build_registry(model, (tool,))
            clock = FakeClock()
            store = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            runner = Runner(
                registry=registry, store=store, crash_hook=hook
            )
            created = await runner.create_run("assistant", "1.0", input="hi")
            with self.assertRaises(RuntimeError):
                await runner.start_run(created.run_id)
            store.close()

            # 重开数据库（第二进程语义）：仅从持久化状态恢复。
            clock2 = FakeClock(
                start=(await self._crashed_expiry(db, created.run_id))
                + timedelta(seconds=1)
            )
            store2 = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock2
            )
            try:
                model2 = ToolThenAnswerModel()
                tool2 = LookupTool()
                registry2 = build_registry(model2, (tool2,))
                resumed = await Runner(
                    registry=registry2, store=store2
                ).resume_run(created.run_id)

                self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
                self.assertEqual(resumed.output, "final: order-call-1")
                # 新进程中的工具实例一次都没有被调用（outcome 从
                # checkpoint 复用）；模型只补最终响应。
                self.assertEqual(tool2.call_count, 0)
                self.assertEqual(model2.call_count, 1)
                # 权威记录完整：MODEL -> TOOL -> MODEL。
                inspection = await Runner(
                    registry=registry2, store=store2
                ).inspect_run(created.run_id)
                self.assertEqual(
                    [s.step_type for s in inspection.steps],
                    [StepType.MODEL, StepType.TOOL, StepType.MODEL],
                )
                tool_ckpts = [
                    c
                    for c in inspection.checkpoints
                    if c.step_type is StepType.TOOL
                ]
                self.assertEqual(len(tool_ckpts), 1)
                self.assertEqual(
                    deserialize_tool_outcome(tool_ckpts[0].output).result,
                    "order-call-1",
                )
            finally:
                store2.close()

    @staticmethod
    async def _crashed_expiry(db: str, run_id: str) -> datetime:
        probe = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
        try:
            crashed = await probe.get_run(run_id)
            assert crashed is not None
            assert crashed.lease_expires_at is not None
            return crashed.lease_expires_at
        finally:
            probe.close()

if __name__ == "__main__":
    unittest.main()
