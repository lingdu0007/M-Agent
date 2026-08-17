"""Ticket 08 主行为测试：流式观察 Run Update 与协作式取消。

验收要求（.scratch/durable-run/issues/08-stream-and-cancel-run.md）：

- 公开 Runner API 订阅 Run Updates，无需访问内部执行对象（AC 1）；
- 模型流式增量携带 run_id / step_id / attempt_id（AC 2）；
- 增量不是 checkpoint；只有完整模型响应才 checkpoint（AC 3-4）；
- 失败流式尝试重试使用新 attempt_id，消费者可替换废弃部分输出
  （AC 5）；
- 订阅者断开或处理失败不改变 Run 执行与权威状态（AC 6）；
- 重连后从 RunStore 查询权威状态，无持久回放承诺（AC 7）；
- 取消阻止新 Step 启动并在安全边界转 CANCELLED（AC 8）；
- 取消不宣称强制中断或撤销已发出的模型/工具调用（AC 9）；
- 公开测试覆盖成功流式、部分输出后重试、订阅者丢失、Step 前取消、
  in-flight adapter 调用时取消（AC 10）。

所有断言只通过公开 Runner API（subscribe_run / cancel_run /
inspect_run / get_run）与外部可观测记录驱动，不断言私有实现细节。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
    DeterministicModelAdapter,
    DeterministicStreamingModelAdapter,
    DeterministicTool,
    FailureClassification,
    IllegalRunTransitionError,
    InMemoryRunStore,
    LeaseNotHeldError,
    ModelCapabilities,
    ModelDelta,
    ModelFailure,
    ModelRequest,
    ModelResponse,
    PlaintextPayloadCodec,
    RetryPolicy,
    Runner,
    RunStatus,
    RunUpdate,
    RunUpdateType,
    StaleRunVersionError,
    SQLiteRunStore,
    StepStatus,
    StepType,
    ToolCall,
    ToolEffect,
    ToolFailure,
    ToolOutcome,
    ToolRequest,
    deserialize_model_response,
)
from m_agent.runtime import ModelRequirements, ToolCallingMode


# -- fake 流式模型（确定性，可注入失败/阻塞） --------------------------


class ObservationOnlyModelState:
    """Explicitly exclude test synchronization probes from model behavior."""

    def _fingerprint_excluded_state(self) -> frozenset[str]:
        return super()._fingerprint_excluded_state() | {
            "closed",
            "gate",
            "release",
            "second_started",
            "started",
        }


class StreamToolThenFinal(DeterministicStreamingModelAdapter):
    """第一次流式请求工具 lookup；第二次流式返回最终内容。"""

    def __init__(self) -> None:
        super().__init__(
            chunks=("req",),
            tool_calls=(
                ToolCall(call_id="t1", tool_name="lookup", arguments="{}"),
            ),
        )

    async def stream(
        self, request: ModelRequest,
    ) -> "asyncio.AsyncIterator[ModelDelta | ModelResponse]":
        self.call_count += 1
        self._last_request = request
        if self.call_count == 1:
            yield ModelDelta(content="req")
            yield ModelResponse(content="", tool_calls=self._tool_calls)
        else:
            yield ModelDelta(content="final")
            yield ModelResponse(content="final answer")


class StreamFailsThenSucceeds(DeterministicStreamingModelAdapter):
    """第一次尝试发出 ``fail_after_delta`` 个 delta 后抛 TRANSIENT，
    第二次尝试完整成功（模拟部分输出后失败并重试）。"""

    def __init__(
        self, chunks: tuple[str, ...] = ("A", "B", "C"), fail_after_delta: int = 2
    ) -> None:
        super().__init__(chunks=chunks)
        self._fail_after = fail_after_delta

    async def stream(
        self, request: ModelRequest,
    ) -> "asyncio.AsyncIterator[ModelDelta | ModelResponse]":
        self.call_count += 1
        self._last_request = request
        if self.call_count == 1:
            emitted = 0
            for chunk in self._chunks:
                if emitted >= self._fail_after:
                    raise ModelFailure(
                        FailureClassification.TRANSIENT,
                        "stream_interrupted",
                        "stream broke after partial output",
                    )
                emitted += 1
                yield ModelDelta(content=chunk)
            # chunks 少于 fail_after 时的兜底：流结束也失败。
            raise ModelFailure(
                FailureClassification.TRANSIENT,
                "stream_interrupted",
                "stream ended before completion",
            )
        for chunk in self._chunks:
            yield ModelDelta(content=chunk)
        yield ModelResponse(content="".join(self._chunks))


class GatedDeltaStream(ObservationOnlyModelState, DeterministicStreamingModelAdapter):
    """第一个 delta 立即发出；第二个 delta 前阻塞在 gate。

    用于"运行中检查 checkpoints 为空"与"in-flight adapter 调用时取消"。
    ``closed`` 在流被协作关闭（GeneratorExit / 正常结束）时 set，
    用于证明取消是协作中断而不是强杀。
    """

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__(chunks=("A", "B"))
        self.gate = gate
        self.closed = asyncio.Event()

    async def stream(
        self, request: ModelRequest,
    ) -> "asyncio.AsyncIterator[ModelDelta | ModelResponse]":
        self.call_count += 1
        self._last_request = request
        try:
            yield ModelDelta(content="A")
            await self.gate.wait()
            yield ModelDelta(content="B")
            yield ModelResponse(content="AB")
        finally:
            self.closed.set()


class BlockingNonStreamingModel(ObservationOnlyModelState, DeterministicModelAdapter):
    """已 dispatch 后阻塞的非流式 Adapter。

    测试通过 ``started`` 观察 Adapter 已进入真实 ``generate`` 调用，再经
    公开 ``Runner.cancel_run`` 提交取消；``release`` 后调用仍正常返回，
    用来检验完整响应证据与 Run 终态的协作式取消边界。
    """

    def __init__(self, response: ModelResponse) -> None:
        super().__init__(
            responses=("unused",),
            capabilities=ModelCapabilities(
                tool_calling=(
                    ToolCallingMode.NATIVE
                    if response.tool_calls
                    else ToolCallingMode.NONE
                ),
            ),
        )
        self._response = response
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        if self.call_count == 1:
            self.started.set()
            await self.release.wait()
            return self._response
        return ModelResponse(content="unexpected follow-up")


class BlockingNonStreamingFailureModel(
    ObservationOnlyModelState, DeterministicModelAdapter
):
    """已 dispatch 后阻塞，并返回结构化永久失败的非流式 Adapter。"""

    def __init__(self) -> None:
        super().__init__(responses=("unused",))
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.started.set()
        await self.release.wait()
        raise ModelFailure(
            FailureClassification.PERMANENT,
            "model_rejected",
            "provider rejected the request",
        )


class ToolThenBlockedModel(
    ObservationOnlyModelState, DeterministicStreamingModelAdapter
):
    """第一次流式请求工具；第二次流式阻塞在 gate（用于 Step 间取消：
    第二次模型调用必须被取消阻止，绝不开始）。"""

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__(
            chunks=("req",),
            tool_calls=(
                ToolCall(call_id="t1", tool_name="lookup", arguments="{}"),
            ),
        )
        self.gate = gate
        self.second_started = asyncio.Event()

    async def stream(
        self, request: ModelRequest,
    ) -> "asyncio.AsyncIterator[ModelDelta | ModelResponse]":
        self.call_count += 1
        self._last_request = request
        if self.call_count == 1:
            yield ModelDelta(content="req")
            yield ModelResponse(content="", tool_calls=self._tool_calls)
            return
        self.second_started.set()
        await self.gate.wait()
        yield ModelDelta(content="never")
        yield ModelResponse(content="never")


class BlockingTool(DeterministicTool):
    """工具调用开始时 set ``started``，阻塞到 ``release`` 被 set。

    用于验证：in-flight 工具调用不被取消打断，完整执行并 checkpoint。
    """

    def __init__(self) -> None:
        super().__init__(name="lookup", effect=ToolEffect.READ_ONLY)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.call_count = 0

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        return ToolOutcome.success(request.call_id, self.name, "order-42")


class BlockingUncertainNonIdempotentTool(DeterministicTool):
    """取消期间才返回不确定结果的非幂等 Tool。"""

    def __init__(self) -> None:
        super().__init__(name="notify", effect=ToolEffect.NON_IDEMPOTENT)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.call_count = 0

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        raise ToolFailure(
            FailureClassification.UNCERTAIN,
            "effect_unconfirmed",
            "notification may have been delivered",
        )


class BlockingFailingTool(DeterministicTool):
    """已 dispatch 后返回可确认失败的 READ_ONLY Tool。"""

    def __init__(self) -> None:
        super().__init__(name="lookup", effect=ToolEffect.READ_ONLY)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.call_count = 0

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        raise ToolFailure(
            FailureClassification.PERMANENT,
            "lookup_rejected",
            "lookup rejected the request",
        )


class DispatchGateStore(InMemoryRunStore):
    """在 Model 最后 dispatch guard 处阻塞的公开 RunStore fake。"""

    def __init__(self) -> None:
        super().__init__(payload_codec=PlaintextPayloadCodec())
        self._assert_count = 0
        self.final_dispatch_guard = asyncio.Event()
        self.release_dispatch_guard = asyncio.Event()

    async def assert_lease(
        self,
        run_id: str,
        expected_version: int,
        owner: str,
    ) -> None:
        await super().assert_lease(run_id, expected_version, owner)
        self._assert_count += 1
        if self._assert_count == 2:
            self.final_dispatch_guard.set()
            await self.release_dispatch_guard.wait()


class ReservationGateStore(InMemoryRunStore):
    """Pause immediately after the durable model reservation is recorded."""

    def __init__(self, **kwargs) -> None:
        super().__init__(payload_codec=PlaintextPayloadCodec(), **kwargs)
        self.reserved = asyncio.Event()
        self.release_reservation = asyncio.Event()

    async def reserve_model_attempt(self, *args, **kwargs) -> bool:
        result = await super().reserve_model_attempt(*args, **kwargs)
        if result:
            self.reserved.set()
            await self.release_reservation.wait()
        return result


def instant_tool(name: str = "lookup") -> DeterministicTool:
    tool = DeterministicTool(
        name=name,
        effect=ToolEffect.READ_ONLY,
        handler=lambda r: ToolOutcome.success(r.call_id, name, "order-42"),
    )
    tool.call_count = 0  # 测试可观测的调用计数（公开记录）。
    original = tool.invoke

    async def counting_invoke(request: ToolRequest) -> ToolOutcome:
        tool.call_count += 1
        return await original(request)

    tool.invoke = counting_invoke  # type: ignore[method-assign]
    return tool


def make_runner(
    model: DeterministicModelAdapter,
    tools: tuple[DeterministicTool, ...] = (),
    retry_policy: RetryPolicy | None = None,
    store: InMemoryRunStore | SQLiteRunStore | None = None,
) -> tuple[Runner, DefinitionRegistry, InMemoryRunStore | SQLiteRunStore]:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id="assistant",
            version="1.0",
            instructions="Answer deterministically.",
            model_requirements=ModelRequirements(
                capabilities=model.capabilities
            ),
            model_adapter=model,
            tools=tools,
            retry_policy=retry_policy,
        )
    )
    if store is None:
        store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
    return Runner(registry=registry, store=store), registry, store


def model_checkpoints(inspection) -> list:
    return [
        c for c in inspection.checkpoints if c.step_type is StepType.MODEL
    ]


class StreamingRunUpdateTests(unittest.IsolatedAsyncioTestCase):
    """AC 1-4, 6, 7：公开订阅、delta 标识、非 checkpoint、完整响应。"""

    async def test_subscribe_via_public_runner_api(self) -> None:
        # AC 1：通过公开 Runner API 订阅即可观察完整生命周期事件，
        # 不需要任何内部执行对象。
        model = DeterministicStreamingModelAdapter(chunks=("Hel", "lo"))
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")

        seen: list[RunUpdate] = []

        async def collect() -> None:
            async for update in runner.subscribe_run(created.run_id):
                seen.append(update)
                if (
                    update.update_type is RunUpdateType.STATUS_CHANGED
                    and update.status is RunStatus.SUCCEEDED
                ):
                    break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)  # 让订阅先注册
        result = await runner.start_run(created.run_id)
        await collector

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        types = [u.update_type for u in seen]
        # 生命周期可观察：RUNNING -> STEP_STARTED -> MODEL_DELTA* ->
        # STEP_COMPLETED -> SUCCEEDED。
        self.assertIn(RunUpdateType.STATUS_CHANGED, types)
        self.assertIn(RunUpdateType.STEP_STARTED, types)
        self.assertIn(RunUpdateType.MODEL_DELTA, types)
        self.assertIn(RunUpdateType.STEP_COMPLETED, types)
        delta_contents = [
            u.content for u in seen
            if u.update_type is RunUpdateType.MODEL_DELTA
        ]
        self.assertEqual(delta_contents, ["Hel", "lo"])

    async def test_deltas_carry_run_step_attempt_identity(self) -> None:
        # AC 2：delta 关联 run_id / step_id / attempt_id，且 attempt_id
        # 与权威记录中的成功 Attempt 一致。
        model = DeterministicStreamingModelAdapter(chunks=("a", "b"))
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")
        deltas: list[RunUpdate] = []

        async def collect() -> None:
            async for update in runner.subscribe_run(created.run_id):
                if update.update_type is RunUpdateType.MODEL_DELTA:
                    deltas.append(update)
                if (
                    update.update_type is RunUpdateType.STATUS_CHANGED
                    and update.status is RunStatus.SUCCEEDED
                ):
                    break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await runner.start_run(created.run_id)
        await collector

        self.assertEqual(len(deltas), 2)
        for delta in deltas:
            self.assertEqual(delta.run_id, created.run_id)
            self.assertIsNotNone(delta.step_id)
            self.assertIsNotNone(delta.attempt_id)
            self.assertEqual(delta.step_type, StepType.MODEL)
        # 同一 Model Step 的同一成功 Attempt 内，delta 共享 step/attempt。
        self.assertEqual(
            {d.step_id for d in deltas},
            {deltas[0].step_id},
        )
        inspection = await runner.inspect_run(created.run_id)
        model_steps = [
            s for s in inspection.steps if s.step_type is StepType.MODEL
        ]
        self.assertEqual(len(model_steps), 1)
        succeeded = [
            a for a in inspection.attempts
            if a.status is StepStatus.SUCCEEDED
        ]
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(deltas[0].attempt_id, succeeded[0].attempt_id)

    async def test_deltas_are_not_checkpointed_while_streaming(self) -> None:
        # AC 3：流式进行中（Run 尚未完成），delta 已发布但 RunStore 中
        # 没有任何 MODEL checkpoint——增量不是已完成 checkpoint。
        gate = asyncio.Event()
        model = GatedDeltaStream(gate)
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")
        first_delta = asyncio.Event()

        async def collect() -> None:
            async for update in runner.subscribe_run(created.run_id):
                if update.update_type is RunUpdateType.MODEL_DELTA:
                    first_delta.set()
                if (
                    update.update_type is RunUpdateType.STATUS_CHANGED
                    and update.status is RunStatus.SUCCEEDED
                ):
                    break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)
        task = asyncio.create_task(runner.start_run(created.run_id))
        await first_delta.wait()
        # 第一个 delta 已发布、第二个 delta 前阻塞：权威记录无 checkpoint。
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(model_checkpoints(inspection), [])
        self.assertEqual(len(inspection.attempts), 1)
        gate.set()  # 释放流，允许完整响应 checkpoint。
        result = await task
        await collector
        self.assertEqual(result.status, RunStatus.SUCCEEDED)

    async def test_full_response_is_single_checkpoint(self) -> None:
        # AC 3/4：完成后只有一个 MODEL checkpoint，其 output 是完整
        # 响应（delta 拼接的结果），不是任何增量片段。
        model = DeterministicStreamingModelAdapter(chunks=("Hel", "lo ", "world"))
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")
        result = await runner.start_run(created.run_id)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.output, "Hello world")
        inspection = await runner.inspect_run(created.run_id)
        checkpoints = model_checkpoints(inspection)
        self.assertEqual(len(checkpoints), 1)
        restored = deserialize_model_response(checkpoints[0].output)
        self.assertEqual(restored.content, "Hello world")
        self.assertEqual(restored.tool_calls, ())

    async def test_full_response_checkpointed_before_dependent_work(
        self,
    ) -> None:
        # AC 4：完整模型响应 checkpoint 先于依赖它的 Tool Step；工具
        # 只在该 checkpoint 落盘后执行。checkpoint 顺序必须为
        # MODEL -> TOOL -> MODEL(final)。
        model = StreamToolThenFinal()
        tool = instant_tool()
        runner, _, _ = make_runner(model, tools=(tool,))
        created = await runner.create_run("assistant", "1.0", input="hi")
        result = await runner.start_run(created.run_id)

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.output, "final answer")
        self.assertEqual(tool.call_count, 1)
        inspection = await runner.inspect_run(created.run_id)
        checkpoint_types = [c.step_type for c in inspection.checkpoints]
        self.assertEqual(
            checkpoint_types,
            [StepType.MODEL, StepType.TOOL, StepType.MODEL],
        )
        # 第一个 MODEL checkpoint 携带完整响应（含工具请求）而不是 delta
        # 片段；TOOL checkpoint 记录工具结果。模型把文本放进 delta，
        # 完整响应的 content 为空、只声明工具调用——checkpoint 保存的
        # 是"完整响应"本身，实时文本由 delta 提供。
        first = inspection.checkpoints[0]
        restored = deserialize_model_response(first.output)
        self.assertEqual(restored.content, "")
        self.assertEqual(len(restored.tool_calls), 1)
        self.assertEqual(inspection.checkpoints[1].step_type, StepType.TOOL)

    async def test_tool_step_events_correlate_with_authoritative_attempt(
        self,
    ) -> None:
        # 工具 Step 的 STEP_STARTED / STEP_COMPLETED 与权威 checkpoint
        # 使用同一 attempt_id：订阅者可关联实时事件与 RunStore 记录。
        model = StreamToolThenFinal()
        tool = instant_tool()
        runner, _, _ = make_runner(model, tools=(tool,))
        created = await runner.create_run("assistant", "1.0", input="hi")
        tool_events: list[RunUpdate] = []

        async def collect() -> None:
            async for update in runner.subscribe_run(created.run_id):
                if update.step_type is StepType.TOOL:
                    tool_events.append(update)
                if (
                    update.update_type is RunUpdateType.STATUS_CHANGED
                    and update.status is RunStatus.SUCCEEDED
                ):
                    break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await runner.start_run(created.run_id)
        await collector

        started = [
            u for u in tool_events
            if u.update_type is RunUpdateType.STEP_STARTED
        ]
        completed = [
            u for u in tool_events
            if u.update_type is RunUpdateType.STEP_COMPLETED
        ]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(completed), 1)
        self.assertEqual(started[0].attempt_id, completed[0].attempt_id)
        inspection = await runner.inspect_run(created.run_id)
        tool_checkpoints = [
            c for c in inspection.checkpoints if c.step_type is StepType.TOOL
        ]
        self.assertEqual(len(tool_checkpoints), 1)
        self.assertEqual(
            tool_checkpoints[0].attempt_id, started[0].attempt_id
        )

    async def test_subscriber_disconnect_does_not_affect_run(self) -> None:
        # AC 6：订阅者断开（break）不改变 Run 执行与权威状态。
        model = DeterministicStreamingModelAdapter(chunks=("x", "y"))
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")
        received: list[RunUpdate] = []

        async def disconnect_early() -> None:
            async for update in runner.subscribe_run(created.run_id):
                received.append(update)
                break  # 收到第一条后立即断开

        collector = asyncio.create_task(disconnect_early())
        await asyncio.sleep(0)
        result = await runner.start_run(created.run_id)
        await collector

        # Run 正常完成，权威状态完整，与订阅者是否存活无关。
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.output, "xy")
        self.assertEqual(len(received), 1)
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(len(model_checkpoints(inspection)), 1)

    async def test_subscriber_error_does_not_affect_run(self) -> None:
        # AC 6：订阅者处理失败（抛异常）同样不影响 Run。
        model = DeterministicStreamingModelAdapter(chunks=("x",))
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")

        async def buggy_subscriber() -> None:
            async for _ in runner.subscribe_run(created.run_id):
                raise RuntimeError("subscriber bug")

        collector = asyncio.create_task(buggy_subscriber())
        await asyncio.sleep(0)
        result = await runner.start_run(created.run_id)
        # 订阅者异常在 collector 中结束，不影响 Run。
        try:
            await collector
        except RuntimeError:
            pass
        self.assertEqual(result.status, RunStatus.SUCCEEDED)

    async def test_no_replay_after_reconnect_runstore_is_authoritative(
        self,
    ) -> None:
        # AC 7：重连后订阅只收到新事件（无持久回放）；权威事实从
        # RunStore 重建（inspect_run）。
        gate = asyncio.Event()
        model = GatedDeltaStream(gate)
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")
        first_session: list[RunUpdate] = []

        async def session_one() -> None:
            # 模拟断线：收到第一个 delta（A）后立即断开。
            async for update in runner.subscribe_run(created.run_id):
                first_session.append(update)
                if update.update_type is RunUpdateType.MODEL_DELTA:
                    break

        collector1 = asyncio.create_task(session_one())
        await asyncio.sleep(0)
        task = asyncio.create_task(runner.start_run(created.run_id))
        await collector1
        self.assertEqual(
            [u.content for u in first_session if u.update_type is RunUpdateType.MODEL_DELTA],
            ["A"],
        )

        # 重连：第二个会话从当前点开始，只收到尚未发生的新事件
        # （delta B 与完成事件），绝不重放历史 delta A。
        second_session: list[RunUpdate] = []

        async def session_two() -> None:
            async for update in runner.subscribe_run(created.run_id):
                second_session.append(update)
                if (
                    update.update_type is RunUpdateType.STATUS_CHANGED
                    and update.status is RunStatus.SUCCEEDED
                ):
                    break

        collector2 = asyncio.create_task(session_two())
        await asyncio.sleep(0)
        gate.set()  # 释放流，让剩余 delta 与完整响应发生。
        result = await task
        await collector2

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        deltas_two = [
            u.content for u in second_session
            if u.update_type is RunUpdateType.MODEL_DELTA
        ]
        self.assertEqual(deltas_two, ["B"])  # 无 "A" 回放

        # 权威事实从 RunStore 查询：完整输出与 checkpoint 都在。
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(inspection.run.status, RunStatus.SUCCEEDED)
        self.assertEqual(inspection.run.output, "AB")
        self.assertEqual(len(model_checkpoints(inspection)), 1)


class StreamingRetryReplacementTests(unittest.IsolatedAsyncioTestCase):
    """AC 5：部分输出失败后重试，新 attempt_id 允许替换废弃输出。"""

    async def test_partial_stream_retry_new_attempt_replaces_output(
        self,
    ) -> None:
        model = StreamFailsThenSucceeds(
            chunks=("A", "B", "C"), fail_after_delta=2
        )
        runner, _, _ = make_runner(
            model, retry_policy=RetryPolicy(max_attempts=2)
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        deltas: list[RunUpdate] = []

        async def collect() -> None:
            async for update in runner.subscribe_run(created.run_id):
                if update.update_type is RunUpdateType.MODEL_DELTA:
                    deltas.append(update)
                if (
                    update.update_type is RunUpdateType.STATUS_CHANGED
                    and update.status is not None
                    and update.status.is_terminal
                ):
                    break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)
        result = await runner.start_run(created.run_id)
        await collector

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.output, "ABC")

        # AC 5：两次 attempt 的 attempt_id 互异；第一次只产生部分输出
        # （A, B），第二次产生完整输出（A, B, C）——消费者按 attempt_id
        # 分组即可替换废弃的部分输出。
        attempt_ids = [d.attempt_id for d in deltas]
        self.assertEqual(len(set(attempt_ids)), 2)
        first_attempt, second_attempt = attempt_ids[0], attempt_ids[-1]
        self.assertNotEqual(first_attempt, second_attempt)
        self.assertEqual(
            [d.content for d in deltas if d.attempt_id == first_attempt],
            ["A", "B"],
        )
        self.assertEqual(
            [d.content for d in deltas if d.attempt_id == second_attempt],
            ["A", "B", "C"],
        )

        # 权威记录：checkpoint 只属于成功的第二次 attempt 且为完整
        # 响应；失败的第一次 attempt 无 output（部分输出从不落盘）。
        inspection = await runner.inspect_run(created.run_id)
        checkpoints = model_checkpoints(inspection)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0].attempt_id, second_attempt)
        self.assertEqual(
            deserialize_model_response(checkpoints[0].output).content,
            "ABC",
        )
        failed = [
            a for a in inspection.attempts if a.status is StepStatus.FAILED
        ]
        self.assertEqual(len(failed), 1)
        self.assertIsNone(failed[0].output)
        self.assertEqual(
            failed[0].classification, FailureClassification.TRANSIENT
        )
        # AC 5 回归保护：失败的权威 Attempt 与失败 delta 使用同一
        # attempt_id——订阅者可据此把废弃的部分输出与失败事件关联并替换。
        self.assertEqual(failed[0].attempt_id, first_attempt)


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    """AC 8-10：开始前 / Step 间 / in-flight adapter 取消与重复取消。"""

    async def test_cancel_before_start_never_invokes_model(self) -> None:
        # AC 8/10：CREATED（开始前）取消——从未执行，直接 CANCELLED，
        # 模型零调用。
        model = DeterministicStreamingModelAdapter(chunks=("A",))
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")
        cancelled = await runner.cancel_run(created.run_id)

        self.assertEqual(cancelled.status, RunStatus.CANCELLED)
        self.assertTrue(cancelled.status.is_terminal)
        self.assertEqual(model.call_count, 0)
        self.assertEqual(
            (await runner.get_run(created.run_id)).status, RunStatus.CANCELLED
        )

    async def test_cancel_running_without_active_advancer_fails_closed(
        self,
    ) -> None:
        # RUNNING 但本 Runner 不掌握推进循环：即使租约可获取，也无法
        # 证明旧 owner 没有 in-flight 外部调用，不能抢写 CANCELLED。
        store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        runner, _, _ = make_runner(
            DeterministicStreamingModelAdapter(chunks=("A",)), store=store
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        # 模拟遗留 RUNNING 记录（无推进者）：直接推进状态机。
        running = await store.transition_run(
            created.run_id,
            expected_version=created.version,
            status=RunStatus.RUNNING,
        )
        self.assertEqual(running.status, RunStatus.RUNNING)
        with self.assertRaises(LeaseNotHeldError):
            await runner.cancel_run(created.run_id)
        self.assertEqual(
            (await runner.get_run(created.run_id)).status,
            RunStatus.RUNNING,
        )

    async def test_stale_cancel_request_preserves_authoritative_record(
        self,
    ) -> None:
        # AC 10：过期 optimistic version 的取消必须显式失败，不能终结或
        # 覆盖调用方刚刚读取到的 CREATED 记录。
        model = DeterministicStreamingModelAdapter(chunks=("A",))
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")

        with self.assertRaises(StaleRunVersionError):
            await runner.cancel_run(
                created.run_id, expected_version=created.version + 1
            )

        authoritative = await runner.get_run(created.run_id)
        self.assertEqual(authoritative.status, RunStatus.CREATED)
        self.assertEqual(authoritative.version, created.version)
        self.assertEqual(model.call_count, 0)

    async def test_cancel_between_steps_blocks_next_step(self) -> None:
        # AC 8/9/10：Step 间取消——第一个模型 Step（请求工具）已完成
        # checkpoint，工具调用 in-flight 时请求取消；工具完整执行并
        # checkpoint（已发生的副作用被记录、绝不撤销），下一个模型
        # Step 被阻止，Run 在安全边界转 CANCELLED。
        gate = asyncio.Event()
        model = ToolThenBlockedModel(gate)
        tool = BlockingTool()
        runner, _, _ = make_runner(model, tools=(tool,))
        created = await runner.create_run("assistant", "1.0", input="hi")

        task = asyncio.create_task(runner.start_run(created.run_id))
        await tool.started.wait()  # 工具调用已发出（in-flight）
        await runner.cancel_run(created.run_id)  # 推进者持有租约 -> 登记
        tool.release.set()  # 工具正常完成（不被打断）
        result = await task

        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertEqual(model.call_count, 1)  # 第二个模型 Step 被阻止
        self.assertFalse(model.second_started.is_set())  # 从未开始
        self.assertEqual(tool.call_count, 1)  # in-flight 调用完整执行
        # 已发生的工具副作用被记录为权威 checkpoint（AC 9）。
        inspection = await runner.inspect_run(created.run_id)
        tool_checkpoints = [
            c for c in inspection.checkpoints if c.step_type is StepType.TOOL
        ]
        self.assertEqual(len(tool_checkpoints), 1)
        # 权威状态可核对：Run 已 CANCELLED，没有不完整的模型输出。
        self.assertEqual(
            (await runner.get_run(created.run_id)).status, RunStatus.CANCELLED
        )

    async def test_cancel_during_uncertain_non_idempotent_tool_stays_waiting(
        self,
    ) -> None:
        # Ticket 07 边界：取消请求不能把已 dispatch、结果仍不确定的
        # NON_IDEMPOTENT Tool 伪造成 CANCELLED；只有 Resolution 才能处置。
        class RequestNotificationModel(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(
                    responses=("unused",),
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    ),
                )

            async def generate(self, request: ModelRequest) -> ModelResponse:
                self.call_count += 1
                self._last_request = request
                return ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            call_id="notify-1",
                            tool_name="notify",
                            arguments="{}",
                        ),
                    ),
                )

        model = RequestNotificationModel()
        tool = BlockingUncertainNonIdempotentTool()
        runner, _, _ = make_runner(model, tools=(tool,))
        created = await runner.create_run("assistant", "1.0", input="hi")

        advancing = asyncio.create_task(runner.start_run(created.run_id))
        await tool.started.wait()
        requested = await runner.cancel_run(created.run_id)
        self.assertEqual(requested.status, RunStatus.RUNNING)
        tool.release.set()
        result = await advancing

        self.assertEqual(result.status, RunStatus.WAITING)
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(model.call_count, 1)
        inspection = await runner.inspect_run(created.run_id)
        tool_attempts = [
            attempt
            for attempt in inspection.attempts
            if attempt.step_id == result.waiting_step_id
        ]
        self.assertEqual(len(tool_attempts), 1)
        self.assertEqual(tool_attempts[0].status, StepStatus.FAILED)
        self.assertEqual(
            tool_attempts[0].classification, FailureClassification.UNCERTAIN
        )
        self.assertEqual(
            [
                checkpoint
                for checkpoint in inspection.checkpoints
                if checkpoint.step_type is StepType.TOOL
            ],
            [],
        )

    async def test_cancel_after_known_tool_failure_preserves_evidence_but_never_fails_run(
        self,
    ) -> None:
        class RequestLookupModel(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(
                    responses=("unused",),
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    ),
                )

            async def generate(self, request: ModelRequest) -> ModelResponse:
                self.call_count += 1
                self._last_request = request
                return ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            call_id="lookup-1",
                            tool_name="lookup",
                            arguments="{}",
                        ),
                    ),
                )

        model = RequestLookupModel()
        tool = BlockingFailingTool()
        runner, _, _ = make_runner(model, tools=(tool,))
        created = await runner.create_run("assistant", "1.0", input="hi")

        advancing = asyncio.create_task(runner.start_run(created.run_id))
        await tool.started.wait()
        await runner.cancel_run(created.run_id)
        tool.release.set()
        result = await advancing

        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(tool.call_count, 1)
        inspection = await runner.inspect_run(created.run_id)
        tool_steps = [
            step for step in inspection.steps if step.step_type is StepType.TOOL
        ]
        self.assertEqual(len(tool_steps), 1)
        self.assertEqual(tool_steps[0].status, StepStatus.FAILED)
        tool_attempts = [
            attempt
            for attempt in inspection.attempts
            if attempt.step_id == tool_steps[0].step_id
        ]
        self.assertEqual(len(tool_attempts), 1)
        self.assertEqual(tool_attempts[0].status, StepStatus.FAILED)
        self.assertEqual(tool_attempts[0].error_code, "lookup_rejected")

    async def test_cancel_at_final_model_dispatch_guard_prevents_provider_call(
        self,
    ) -> None:
        # 仅经公开 Runner + RunStore seam 构造最后 guard 竞态；取消发生在
        # STEP_STARTED 之后、Adapter.generate 之前，因而没有真实调用证据。
        store = DispatchGateStore()
        model = DeterministicModelAdapter(responses=("never",))
        runner, _, _ = make_runner(model, store=store)
        created = await runner.create_run("assistant", "1.0", input="hi")

        advancing = asyncio.create_task(runner.start_run(created.run_id))
        await store.final_dispatch_guard.wait()
        requested = await runner.cancel_run(created.run_id)
        self.assertEqual(requested.status, RunStatus.RUNNING)
        store.release_dispatch_guard.set()
        result = await advancing

        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertEqual(model.call_count, 0)
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(inspection.steps, [])
        self.assertEqual(inspection.attempts, [])
        self.assertEqual(inspection.checkpoints, [])

    async def test_cancel_after_model_reservation_prevents_provider_call(
        self,
    ) -> None:
        store = ReservationGateStore()
        model = DeterministicModelAdapter(responses=("never",))
        runner, _, _ = make_runner(model, store=store)
        created = await runner.create_run("assistant", "1.0", input="hi")

        advancing = asyncio.create_task(runner.start_run(created.run_id))
        await store.reserved.wait()
        requested = await runner.cancel_run(created.run_id)
        self.assertEqual(requested.status, RunStatus.RUNNING)
        store.release_reservation.set()
        result = await advancing

        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertEqual(model.call_count, 0)
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(len(inspection.attempts), 1)
        self.assertEqual(inspection.attempts[0].status, StepStatus.FAILED)
        self.assertEqual(
            inspection.attempts[0].error_code, "MODEL_DISPATCH_CANCELLED"
        )
        self.assertEqual(len(inspection.steps), 1)
        self.assertEqual(inspection.steps[0].status, StepStatus.FAILED)
        self.assertEqual(
            inspection.steps[0].error_code, "MODEL_DISPATCH_CANCELLED"
        )

    async def test_lease_expiry_after_model_reservation_prevents_provider_call(
        self,
    ) -> None:
        from datetime import timedelta

        from m_agent import DEFAULT_LEASE_TTL, FakeClock

        clock = FakeClock()
        store = ReservationGateStore(clock=clock)
        model = DeterministicModelAdapter(responses=("never",))
        runner, _, _ = make_runner(model, store=store)
        created = await runner.create_run("assistant", "1.0", input="hi")

        advancing = asyncio.create_task(runner.start_run(created.run_id))
        await store.reserved.wait()
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        store.release_reservation.set()
        with self.assertRaises(LeaseNotHeldError):
            await advancing

        self.assertEqual(model.call_count, 0)

    async def test_cancel_during_inflight_streaming_adapter(self) -> None:
        # AC 9/10：adapter in-flight 时请求取消——流式调用在 delta 之间
        # 被协作中断（aclose -> GeneratorExit，adapter finally 观察到），
        # 不完整输出绝不 checkpoint，Run 转 CANCELLED。
        gate = asyncio.Event()
        model = GatedDeltaStream(gate)
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")

        async def collect() -> None:
            async for update in runner.subscribe_run(created.run_id):
                if (
                    update.update_type is RunUpdateType.MODEL_DELTA
                    and update.content == "A"
                ):
                    # 收到第一个 delta 后请求取消并释放流。
                    await runner.cancel_run(created.run_id)
                    gate.set()
                if (
                    update.update_type is RunUpdateType.STATUS_CHANGED
                    and update.status is RunStatus.CANCELLED
                ):
                    break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)
        task = asyncio.create_task(runner.start_run(created.run_id))
        result = await task
        await collector

        self.assertEqual(result.status, RunStatus.CANCELLED)
        # 协作中断：adapter 的 finally 被执行（GeneratorExit），而不是
        # 被强杀或假装"撤销"调用。
        self.assertTrue(model.closed.is_set())
        # 不完整响应没有成为 checkpoint。
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(model_checkpoints(inspection), [])
        model_steps = [
            step for step in inspection.steps if step.step_type is StepType.MODEL
        ]
        self.assertEqual([step.status for step in model_steps], [StepStatus.FAILED])
        model_attempts = [
            attempt
            for attempt in inspection.attempts
            if attempt.step_id == model_steps[0].step_id
        ]
        self.assertEqual(
            [attempt.status for attempt in model_attempts], [StepStatus.FAILED]
        )

    async def test_cancel_after_terminal_is_rejected(self) -> None:
        # AC 10：终态后重复取消被显式拒绝，权威记录不被改动。
        model = DeterministicStreamingModelAdapter(chunks=("A",))
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        with self.assertRaises(IllegalRunTransitionError):
            await runner.cancel_run(created.run_id)
        # 重复取消同样被拒（CANCELLED 是终态）。
        cancelled = await runner.cancel_run(
            (await runner.create_run("assistant", "1.0", input="x")).run_id
        )
        self.assertEqual(cancelled.status, RunStatus.CANCELLED)
        with self.assertRaises(IllegalRunTransitionError):
            await runner.cancel_run(cancelled.run_id)
        self.assertEqual(
            (await runner.get_run(cancelled.run_id)).status, RunStatus.CANCELLED
        )

    async def test_cancel_waiting_run_reuses_resolution(self) -> None:
        # WAITING Run 的取消复用 CANCEL_RUN resolution（Ticket 07 语义）。
        from m_agent import (
            REASON_UNCERTAIN_NON_IDEMPOTENT,
            ToolFailure,
        )

        class UncertainTool(DeterministicTool):
            def __init__(self) -> None:
                super().__init__(
                    name="uncertain", effect=ToolEffect.NON_IDEMPOTENT
                )

            async def invoke(self, request: ToolRequest) -> ToolOutcome:
                raise ToolFailure(
                    FailureClassification.UNCERTAIN,
                    "effect_unconfirmed",
                    "uncertain side effect",
                )

        class RequestToolModel(DeterministicStreamingModelAdapter):
            def __init__(self) -> None:
                super().__init__(
                    chunks=("req",),
                    tool_calls=(
                        ToolCall(
                            call_id="u1", tool_name="uncertain", arguments="{}"
                        ),
                    ),
                )

            async def stream(
                self, request: ModelRequest,
            ) -> "asyncio.AsyncIterator[ModelDelta | ModelResponse]":
                self.call_count += 1
                self._last_request = request
                yield ModelDelta(content="req")
                yield ModelResponse(content="", tool_calls=self._tool_calls)

        tool = UncertainTool()
        model = RequestToolModel()
        runner, _, _ = make_runner(model, tools=(tool,))
        created = await runner.create_run("assistant", "1.0", input="hi")
        waiting = await runner.start_run(created.run_id)
        self.assertEqual(waiting.status, RunStatus.WAITING)
        self.assertEqual(
            waiting.waiting_reason, REASON_UNCERTAIN_NON_IDEMPOTENT
        )
        cancelled = await runner.cancel_run(created.run_id)
        self.assertEqual(cancelled.status, RunStatus.CANCELLED)


class NonStreamingCancellationTests(unittest.IsolatedAsyncioTestCase):
    """非流式 in-flight 取消必须在完整 checkpoint 后阻止终态提交。"""

    async def test_cancel_after_non_streaming_final_response_checkpoints_but_never_succeeds(
        self,
    ) -> None:
        model = BlockingNonStreamingModel(ModelResponse(content="complete"))
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")
        updates: list[RunUpdate] = []

        async def collect() -> None:
            async for update in runner.subscribe_run(created.run_id):
                updates.append(update)
                if (
                    update.update_type is RunUpdateType.STATUS_CHANGED
                    and update.status in (RunStatus.SUCCEEDED, RunStatus.CANCELLED)
                ):
                    return

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)
        advancing = asyncio.create_task(runner.start_run(created.run_id))
        await model.started.wait()

        requested = await runner.cancel_run(created.run_id)
        self.assertEqual(requested.status, RunStatus.RUNNING)
        model.release.set()
        result = await advancing
        await collector

        self.assertEqual(model.call_count, 1)
        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertEqual(
            (await runner.get_run(created.run_id)).status, RunStatus.CANCELLED
        )
        self.assertFalse(
            any(
                update.update_type is RunUpdateType.STATUS_CHANGED
                and update.status is RunStatus.SUCCEEDED
                for update in updates
            )
        )
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(len(inspection.steps), 1)
        self.assertEqual(len(inspection.attempts), 1)
        checkpoints = model_checkpoints(inspection)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(inspection.steps[0].step_type, StepType.MODEL)
        self.assertEqual(inspection.steps[0].status, StepStatus.SUCCEEDED)
        self.assertEqual(inspection.attempts[0].status, StepStatus.SUCCEEDED)
        self.assertEqual(
            inspection.attempts[0].attempt_id, checkpoints[0].attempt_id
        )
        self.assertEqual(
            deserialize_model_response(checkpoints[0].output).content,
            "complete",
        )

    async def test_cancel_after_non_streaming_failure_preserves_evidence_but_never_fails_run(
        self,
    ) -> None:
        model = BlockingNonStreamingFailureModel()
        runner, _, _ = make_runner(model)
        created = await runner.create_run("assistant", "1.0", input="hi")
        updates: list[RunUpdate] = []

        async def collect() -> None:
            async for update in runner.subscribe_run(created.run_id):
                updates.append(update)
                if (
                    update.update_type is RunUpdateType.STATUS_CHANGED
                    and update.status in (RunStatus.CANCELLED, RunStatus.FAILED)
                ):
                    return

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)
        advancing = asyncio.create_task(runner.start_run(created.run_id))
        await model.started.wait()
        await runner.cancel_run(created.run_id)
        model.release.set()
        result = await advancing
        await collector

        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertEqual(model.call_count, 1)
        self.assertFalse(
            any(
                update.update_type is RunUpdateType.STATUS_CHANGED
                and update.status is RunStatus.FAILED
                for update in updates
            )
        )
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(len(inspection.steps), 1)
        self.assertEqual(inspection.steps[0].status, StepStatus.FAILED)
        self.assertEqual(len(inspection.attempts), 1)
        self.assertEqual(inspection.attempts[0].status, StepStatus.FAILED)
        self.assertEqual(inspection.attempts[0].error_code, "model_rejected")
        self.assertEqual(model_checkpoints(inspection), [])

    async def test_cancel_after_non_streaming_tool_request_never_dispatches_tool(
        self,
    ) -> None:
        requested_call = ToolCall(
            call_id="lookup-1", tool_name="lookup", arguments="{}"
        )
        model = BlockingNonStreamingModel(
            ModelResponse(content="", tool_calls=(requested_call,))
        )
        tool = instant_tool()
        runner, _, _ = make_runner(model, tools=(tool,))
        created = await runner.create_run("assistant", "1.0", input="hi")
        updates: list[RunUpdate] = []

        async def collect() -> None:
            async for update in runner.subscribe_run(created.run_id):
                updates.append(update)
                if (
                    update.update_type is RunUpdateType.STATUS_CHANGED
                    and update.status in (RunStatus.SUCCEEDED, RunStatus.CANCELLED)
                ):
                    return

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)
        advancing = asyncio.create_task(runner.start_run(created.run_id))
        await model.started.wait()

        requested = await runner.cancel_run(created.run_id)
        self.assertEqual(requested.status, RunStatus.RUNNING)
        model.release.set()
        result = await advancing
        await collector

        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(tool.call_count, 0)
        self.assertFalse(
            any(
                update.update_type is RunUpdateType.STATUS_CHANGED
                and update.status is RunStatus.SUCCEEDED
                for update in updates
            )
        )
        self.assertFalse(
            any(
                update.update_type is RunUpdateType.STEP_STARTED
                and update.step_type is StepType.TOOL
                for update in updates
            )
        )
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(
            [step.step_type for step in inspection.steps], [StepType.MODEL]
        )
        self.assertEqual(
            [attempt.step_id for attempt in inspection.attempts],
            [inspection.steps[0].step_id],
        )
        checkpoints = model_checkpoints(inspection)
        self.assertEqual(len(checkpoints), 1)
        checkpointed = deserialize_model_response(checkpoints[0].output)
        self.assertEqual(checkpointed.tool_calls, (requested_call,))


class Ticket08SQLiteTests(unittest.IsolatedAsyncioTestCase):
    """同一流式与取消语义在 SQLiteRunStore 上成立（持久化权威）。"""

    def _sqlite_store(self, tmp: str) -> SQLiteRunStore:
        return SQLiteRunStore(
            os.path.join(tmp, "ticket08.db"),
            payload_codec=PlaintextPayloadCodec(),
        )

    async def test_streaming_completion_persisted_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._sqlite_store(tmp)
            model = DeterministicStreamingModelAdapter(chunks=("persist", "ed"))
            runner, _, _ = make_runner(model, store=store)
            created = await runner.create_run("assistant", "1.0", input="hi")
            result = await runner.start_run(created.run_id)

            self.assertEqual(result.status, RunStatus.SUCCEEDED)
            self.assertEqual(result.output, "persisted")
            inspection = await runner.inspect_run(created.run_id)
            checkpoints = model_checkpoints(inspection)
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(
                deserialize_model_response(checkpoints[0].output).content,
                "persisted",
            )
            store.close()

    async def test_cancel_before_start_persisted_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._sqlite_store(tmp)
            model = DeterministicStreamingModelAdapter(chunks=("A",))
            runner, _, _ = make_runner(model, store=store)
            created = await runner.create_run("assistant", "1.0", input="hi")
            cancelled = await runner.cancel_run(created.run_id)
            self.assertEqual(cancelled.status, RunStatus.CANCELLED)
            self.assertEqual(model.call_count, 0)
            # 重开连接：CANCELLED 是持久化终态。
            reopened = self._sqlite_store(tmp)
            record = await reopened.get_run(created.run_id)
            self.assertEqual(record.status, RunStatus.CANCELLED)
            reopened.close()
            store.close()


if __name__ == "__main__":
    unittest.main()
