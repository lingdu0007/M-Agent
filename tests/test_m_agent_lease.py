"""Ticket 03 并发租约测试：同一 Run 只允许一个有效推进者。

验收对照（ADR 0013 / PRD User Stories 21-23, 44）：

- 同一 Run 双 Runner 争抢 -> 一个有效 lease owner（另一个被拒）；
- 无有效租约的 Runner 不能开始下一 Step 或提交权威进度；
- 每次权威 mutation 校验期望版本 + 未过期租约，旧 owner 的迟到
  提交得到显式冲突（LeaseNotHeldError / StaleRunVersionError）；
- 租约过期通过 :class:`FakeClock` 确定性驱动（advance），无真实
  sleep；过期后新 Runner 接管并从最新安全 checkpoint 恢复；
- 接管后旧 owner 迟到提交被拒，最终 Run / Step 记录只反映有效
  推进者，模型调用次数作为可观察计数证据；
- 不同 Run 可并发推进，没有全局执行锁；
- 运行时没有引入后台扫描、自动 takeover、queue 或 scheduler。

模型调用计数：两个 Runner 的确定性 Adapter 把每次调用追加到同一个
日志文件，断言文件行数即总模型调用次数（同 Ticket 02 的证据模式）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import timedelta

from m_agent import (
    DEFAULT_LEASE_TTL,
    AgentDefinition,
    CrashPoint,
    DefinitionRegistry,
    DeterministicContextProvider,
    DeterministicModelAdapter,
    DeterministicTool,
    FakeClock,
    IllegalRunTransitionError,
    InMemoryRunStore,
    LeaseNotHeldError,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    PlaintextPayloadCodec,
    Runner,
    RunStatus,
    SQLiteRunStore,
    StaleRunVersionError,
    StepType,
    TelemetryEvent,
    TelemetryEventType,
    ToolCall,
    ToolCallingMode,
    ToolEffect,
    ToolOutcome,
    ToolRequest,
)

_ANSWER = "deterministic answer"


class GatedLoggingAdapter(DeterministicModelAdapter):
    """确定性模型：每次 generate 追加一行日志，可选地在调用中挂起。

    ``entered`` 在进入 generate 时 set（表示"已持有租约并开始模型
    调用"），``gate`` 为 None 时不挂起；否则一直等待到 gate.set()。
    这样测试可以确定性控制两个 Runner 的推进交错，无需真实 sleep。
    """

    deterministic: bool = True

    def __init__(
        self,
        log_path: str,
        responses: tuple[str, ...] = (_ANSWER,),
        entered: asyncio.Event | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        super().__init__(responses=responses)
        self._log_path = log_path
        self._entered = entered
        self._gate = gate

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        with open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{request.input}\n")
        if self._entered is not None:
            self._entered.set()
        if self._gate is not None:
            await self._gate.wait()
        index = min(self.call_count - 1, len(self._responses) - 1)
        return ModelResponse(content=self._responses[index])


def make_registry(
    log_path: str, entered: asyncio.Event | None = None, gate: asyncio.Event | None = None
) -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition(
            definition_id="assistant",
            version="1.0",
            instructions="Answer deterministically.",
            model_adapter=GatedLoggingAdapter(
                log_path=log_path, entered=entered, gate=gate
            ),
        )
    )
    return registry


def count_model_calls(log_path: str) -> int:
    if not os.path.exists(log_path):
        return 0
    with open(log_path, encoding="utf-8") as fh:
        return len([line for line in fh if line.strip()])


class ToolThenAnswerAdapter(DeterministicModelAdapter):
    """第一次请求工具，第二次给出最终响应。"""

    def __init__(self) -> None:
        super().__init__(
            capabilities=ModelCapabilities(tool_calling=ToolCallingMode.NATIVE)
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        if self.call_count == 1:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="effect-1",
                        tool_name="external_effect",
                        arguments="{}",
                    ),
                )
            )
        return ModelResponse(content=_ANSWER)


class CountingEffectTool(DeterministicTool):
    """带可观察外部效果计数的 NON_IDEMPOTENT 测试工具。"""

    def __init__(
        self,
        *,
        entered: asyncio.Event | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        super().__init__(
            name="external_effect", effect=ToolEffect.NON_IDEMPOTENT
        )
        self.call_count = 0
        self.entered = entered
        self.gate = gate

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        self.call_count += 1
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        return ToolOutcome.success(request.call_id, self.name, "effect-recorded")


def make_tool_registry(
    model: DeterministicModelAdapter,
    tool: CountingEffectTool,
) -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition(
            definition_id="assistant",
            version="1.0",
            instructions="Use the declared tool.",
            model_adapter=model,
            tools=(tool,),
        )
    )
    return registry


class ExpireLeaseOnStepStartedSink:
    """在目标 Step 的同步 telemetry 回调中令当前 lease 过期。"""

    def __init__(self, clock: FakeClock, step_type: StepType) -> None:
        self._clock = clock
        self._step_type = step_type

    def emit(self, event: TelemetryEvent) -> None:
        if (
            event.event_type is TelemetryEventType.STEP_STARTED
            and event.step_type is self._step_type
        ):
            self._clock.advance(
                DEFAULT_LEASE_TTL + timedelta(seconds=1)
            )


class SQLiteContentionTests(unittest.IsolatedAsyncioTestCase):
    """SQLite 双连接（两个 Runner 实例）争抢同一 Run。"""

    def make_contenders(
        self, db_path: str, clock: FakeClock, log_path: str
    ) -> tuple[
        Runner, Runner, SQLiteRunStore, SQLiteRunStore,
        asyncio.Event, asyncio.Event,
    ]:
        """构造两个完全独立的 registry/store/runner（模拟两个 worker）。

        Runner A 的模型在 generate 中挂起（entered/gate 控制），
        Runner B 无挂起。共享同一 clock 与模型调用日志。
        """
        entered, gate = asyncio.Event(), asyncio.Event()
        reg_a = make_registry(log_path, entered=entered, gate=gate)
        reg_b = make_registry(log_path)
        s_a = SQLiteRunStore(
            db_path, payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        s_b = SQLiteRunStore(
            db_path, payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        r_a = Runner(registry=reg_a, store=s_a)
        r_b = Runner(registry=reg_b, store=s_b)
        return r_a, r_b, s_a, s_b, entered, gate

    async def test_two_runners_contend_for_same_run_single_effective_owner(
        self,
    ) -> None:
        # AC1/AC2：同一 Run 双 Runner 争抢 -> 一个有效租约 owner；
        # 没有租约的 Runner 不能开始下一 Step（resume 被 LeaseNotHeldError
        # 拒绝，start 被状态机拒绝），也不能推进。
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            log = os.path.join(tmp, "model_calls.log")
            clock = FakeClock()
            r_a, r_b, s_a, s_b, entered, gate = self.make_contenders(
                db, clock, log
            )

            created = await r_a.create_run("assistant", "1.0", input="hi")
            task = asyncio.create_task(r_a.start_run(created.run_id))
            await entered.wait()  # A 已获取租约并挂起在模型调用中

            # 租约持久化为 A 的 owner（可验证的持久状态）。
            lease = await s_b.get_lease(created.run_id)
            self.assertIsNotNone(lease)
            self.assertEqual(lease.owner, r_a.owner)
            run_view = await s_b.get_run(created.run_id)
            self.assertEqual(run_view.lease_owner, r_a.owner)
            self.assertEqual(run_view.status, RunStatus.RUNNING)

            # B 尝试推进同一 Run：没有有效租约 -> 显式冲突。
            with self.assertRaises(LeaseNotHeldError):
                await r_b.resume_run(created.run_id)
            # B 也不能 start 一个已被推进的 Run。
            with self.assertRaises(IllegalRunTransitionError):
                await r_b.start_run(created.run_id)

            gate.set()
            terminal = await task
            self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
            # 模型只被 A 调用一次；B 一次都没有推进。
            self.assertEqual(count_model_calls(log), 1)
            inspection = await r_b.inspect_run(created.run_id)
            self.assertEqual(len(inspection.steps), 1)
            self.assertEqual(len(inspection.attempts), 1)
            s_a.close()
            s_b.close()

    async def test_expired_lease_takeover_and_stale_owner_rejected(
        self,
    ) -> None:
        # AC4/AC6：租约过期（FakeClock advance）后 B 接管并完成；
        # 旧 owner A 的迟到提交被拒，最终记录只反映 B 的推进，
        # 模型调用次数 = A 1 次（被浪费）+ B 1 次 = 2。
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            log = os.path.join(tmp, "model_calls.log")
            clock = FakeClock()
            r_a, r_b, s_a, s_b, entered, gate = self.make_contenders(
                db, clock, log
            )

            created = await r_a.create_run("assistant", "1.0", input="hi")
            task = asyncio.create_task(r_a.start_run(created.run_id))
            await entered.wait()  # A 挂起，持有租约

            # 确定性推进时钟：A 的租约过期。
            clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))

            # B 接管并推进到 SUCCEEDED（A 尚无 checkpoint，B 重新执行）。
            terminal_b = await r_b.resume_run(created.run_id)
            self.assertEqual(terminal_b.status, RunStatus.SUCCEEDED)
            self.assertEqual(terminal_b.output, _ANSWER)

            # A 恢复：所有迟到写入（Step / Attempt / Checkpoint /
            # 终态提交）都被租约检查拒绝。
            gate.set()
            with self.assertRaises(
                (LeaseNotHeldError, StaleRunVersionError)
            ):
                await task

            # 最终权威记录：Run 与 Step 只反映 B 的推进。
            final = await r_b.get_run(created.run_id)
            self.assertEqual(final.status, RunStatus.SUCCEEDED)
            self.assertEqual(final.output, _ANSWER)
            inspection = await r_b.inspect_run(created.run_id)
            self.assertEqual(len(inspection.steps), 1)
            self.assertEqual(len(inspection.attempts), 2)
            self.assertEqual(len(inspection.checkpoints), 1)
            # A 调用一次（迟到、被拒）+ B 调用一次 = 2 次模型调用，
            # 证明没有双重推进（不会出现 3+ 次或两套 Step 记录）。
            self.assertEqual(count_model_calls(log), 2)
            s_a.close()
            s_b.close()

    async def test_takeover_resumes_from_latest_checkpoint(self) -> None:
        # AC5：A 在 checkpoint 落盘后、终态前中断（崩溃注入）；
        # 租约过期后 B 接管并复用 checkpoint 完成，不重复调用模型。
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            log = os.path.join(tmp, "model_calls.log")
            clock = FakeClock()

            def crash_hook(point: CrashPoint, run_id: str) -> None:
                if point is CrashPoint.AFTER_MODEL_CHECKPOINT:
                    raise RuntimeError("injected crash")

            reg_a = make_registry(log)
            s_a = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            r_a = Runner(registry=reg_a, store=s_a, crash_hook=crash_hook)
            created = await r_a.create_run("assistant", "1.0", input="hi")
            with self.assertRaises(RuntimeError):
                await r_a.start_run(created.run_id)

            # 崩溃后：RUNNING + checkpoint 已持久化，租约仍由 A 持有。
            crashed = await s_a.get_run(created.run_id)
            self.assertEqual(crashed.status, RunStatus.RUNNING)
            self.assertIsNotNone(crashed.lease_owner)
            checkpoints = await s_a.get_checkpoints(created.run_id)
            self.assertEqual(len(checkpoints), 1)

            # 时钟越过 A 的租约过期点，B 接管并从 checkpoint 恢复。
            clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
            reg_b = make_registry(log)
            s_b = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            r_b = Runner(registry=reg_b, store=s_b)
            terminal = await r_b.resume_run(created.run_id)
            self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
            self.assertEqual(terminal.output, _ANSWER)
            # checkpoint 复用：模型只被调用 1 次（A），B 没有新调用。
            self.assertEqual(count_model_calls(log), 1)
            b_adapter = reg_b.resolve(
                "assistant", "1.0"
            ).model_adapter
            self.assertEqual(b_adapter.call_count, 0)
            # 完成后租约被 B 释放。
            self.assertIsNone(await s_b.get_lease(created.run_id))
            s_a.close()
            s_b.close()

    async def test_stale_owner_cannot_commit_terminal_transition(self) -> None:
        # AC3/AC6：状态转换合法、期望版本恰好匹配时，旧 owner 的
        # 终态提交仍被租约检查拒绝（owner 已被接管），权威记录保持
        # 新 owner 的推进状态——版本检查与租约检查互相独立、缺一不可。
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            log = os.path.join(tmp, "model_calls.log")
            clock = FakeClock()
            r_a, r_b, s_a, s_b, entered, gate = self.make_contenders(
                db, clock, log
            )
            # B 在 checkpoint 落盘前中断（模拟 B 也在推进中）：
            # 租约归 B，Run 保持 RUNNING。
            def crash_hook_b(point: CrashPoint, run_id: str) -> None:
                if point is CrashPoint.BEFORE_MODEL_CHECKPOINT:
                    raise RuntimeError("b interrupted")

            reg_b = make_registry(log)
            r_b = Runner(
                registry=reg_b, store=s_b, crash_hook=crash_hook_b
            )

            created = await r_a.create_run("assistant", "1.0", input="hi")
            task = asyncio.create_task(r_a.start_run(created.run_id))
            await entered.wait()
            clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
            with self.assertRaises(RuntimeError):
                await r_b.resume_run(created.run_id)

            # 当前权威状态：RUNNING、version=2、租约归 B。
            current = await s_a.get_run(created.run_id)
            self.assertEqual(current.status, RunStatus.RUNNING)
            self.assertEqual(current.lease_owner, r_b.owner)

            # 旧 owner A 迟到提交完成结果：版本匹配、转换合法，但
            # 租约检查拒绝。
            with self.assertRaises(LeaseNotHeldError):
                await s_a.transition_run(
                    created.run_id,
                    expected_version=current.version,
                    status=RunStatus.SUCCEEDED,
                    output="stale output",
                    lease_owner=r_a.owner,
                )
            # 权威记录未被覆盖。
            final = await s_b.get_run(created.run_id)
            self.assertEqual(final.status, RunStatus.RUNNING)
            self.assertEqual(final.output, None)
            self.assertEqual(final.lease_owner, r_b.owner)

            gate.set()
            await asyncio.gather(task, return_exceptions=True)
            s_a.close()
            s_b.close()

    async def test_expired_owner_cannot_start_tool_after_model_checkpoint(
        self,
    ) -> None:
        """AC2: Model checkpoint 后租约过期，不得发起后续 Tool Step。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            clock = FakeClock()
            model = ToolThenAnswerAdapter()
            tool = CountingEffectTool()

            def expire_after_model_checkpoint(
                point: CrashPoint, run_id: str
            ) -> None:
                if point is CrashPoint.AFTER_MODEL_CHECKPOINT:
                    clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))

            store = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            runner = Runner(
                registry=make_tool_registry(model, tool),
                store=store,
                crash_hook=expire_after_model_checkpoint,
            )
            try:
                created = await runner.create_run(
                    "assistant", "1.0", input="hi"
                )
                with self.assertRaises(LeaseNotHeldError):
                    await runner.start_run(created.run_id)

                self.assertEqual(model.call_count, 1)
                self.assertEqual(tool.call_count, 0)
                inspection = await runner.inspect_run(created.run_id)
                self.assertEqual(inspection.run.status, RunStatus.RUNNING)
                self.assertEqual(len(inspection.checkpoints), 1)
            finally:
                store.close()

    async def test_telemetry_expiry_cannot_dispatch_context(self) -> None:
        """dispatch 最后边界必须覆盖 STEP_STARTED telemetry 时间窗。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            clock = FakeClock()
            provider = DeterministicContextProvider()
            model = DeterministicModelAdapter(responses=(_ANSWER,))
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition(
                    definition_id="assistant",
                    version="1.0",
                    instructions="Answer deterministically.",
                    model_adapter=model,
                    context_provider=provider,
                )
            )
            store = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            runner = Runner(
                registry=registry,
                store=store,
                telemetry_sink=ExpireLeaseOnStepStartedSink(
                    clock, StepType.CONTEXT
                ),
            )
            try:
                created = await runner.create_run(
                    "assistant", "1.0", input="hi"
                )
                with self.assertRaises(LeaseNotHeldError):
                    await runner.start_run(created.run_id)

                self.assertEqual(provider.call_count, 0)
                self.assertEqual(model.call_count, 0)
                inspection = await runner.inspect_run(created.run_id)
                self.assertEqual(inspection.run.status, RunStatus.RUNNING)
                self.assertEqual(inspection.steps, [])
            finally:
                store.close()

    async def test_telemetry_expiry_cannot_dispatch_model(self) -> None:
        """Model 外部调用前必须再次校验 telemetry 后的 lease。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            clock = FakeClock()
            # Ticket 08 的 request preflight 会在 telemetry 前拒绝不支持
            # 工具的模型；此处需使用支持工具的 Adapter，才会覆盖 lease
            # 在 STEP_STARTED hook 之后、真正 model dispatch 之前的守卫。
            model = ToolThenAnswerAdapter()
            store = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            runner = Runner(
                registry=make_tool_registry(model, CountingEffectTool()),
                store=store,
                telemetry_sink=ExpireLeaseOnStepStartedSink(
                    clock, StepType.MODEL
                ),
            )
            try:
                created = await runner.create_run(
                    "assistant", "1.0", input="hi"
                )
                with self.assertRaises(LeaseNotHeldError):
                    await runner.start_run(created.run_id)

                self.assertEqual(model.call_count, 0)
                inspection = await runner.inspect_run(created.run_id)
                self.assertEqual(inspection.run.status, RunStatus.RUNNING)
                self.assertEqual(inspection.steps, [])
            finally:
                store.close()

    async def test_telemetry_expiry_cannot_dispatch_tool(self) -> None:
        """Tool 外部效果前必须再次校验 telemetry 后的 lease。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            clock = FakeClock()
            model = ToolThenAnswerAdapter()
            tool = CountingEffectTool()
            store = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            runner = Runner(
                registry=make_tool_registry(model, tool),
                store=store,
                telemetry_sink=ExpireLeaseOnStepStartedSink(
                    clock, StepType.TOOL
                ),
            )
            try:
                created = await runner.create_run(
                    "assistant", "1.0", input="hi"
                )
                with self.assertRaises(LeaseNotHeldError):
                    await runner.start_run(created.run_id)

                self.assertEqual(model.call_count, 1)
                self.assertEqual(tool.call_count, 0)
                inspection = await runner.inspect_run(created.run_id)
                self.assertEqual(inspection.run.status, RunStatus.RUNNING)
                self.assertEqual(len(inspection.checkpoints), 1)
            finally:
                store.close()

    async def test_expired_owner_cannot_start_model_after_tool_checkpoint(
        self,
    ) -> None:
        """AC2: Tool checkpoint 后租约过期，不得发起后续 Model Step。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            clock = FakeClock()
            model = ToolThenAnswerAdapter()
            tool = CountingEffectTool()

            def expire_after_tool_checkpoint(
                point: CrashPoint, run_id: str
            ) -> None:
                if point is CrashPoint.AFTER_TOOL_CHECKPOINT:
                    clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))

            store = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            runner = Runner(
                registry=make_tool_registry(model, tool),
                store=store,
                crash_hook=expire_after_tool_checkpoint,
            )
            try:
                created = await runner.create_run(
                    "assistant", "1.0", input="hi"
                )
                with self.assertRaises(LeaseNotHeldError):
                    await runner.start_run(created.run_id)

                self.assertEqual(tool.call_count, 1)
                self.assertEqual(model.call_count, 1)
                inspection = await runner.inspect_run(created.run_id)
                self.assertEqual(inspection.run.status, RunStatus.RUNNING)
                self.assertEqual(len(inspection.checkpoints), 2)
            finally:
                store.close()

    async def test_cancel_cannot_claim_terminal_after_inflight_lease_expires(
        self,
    ) -> None:
        """cancel/takeover race: 旧调用 in-flight 时不能抢写 CANCELLED。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            clock = FakeClock()
            entered, gate = asyncio.Event(), asyncio.Event()
            model_a = ToolThenAnswerAdapter()
            tool_a = CountingEffectTool(entered=entered, gate=gate)
            model_b = ToolThenAnswerAdapter()
            tool_b = CountingEffectTool()
            store_a = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            store_b = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            runner_a = Runner(
                registry=make_tool_registry(model_a, tool_a), store=store_a
            )
            runner_b = Runner(
                registry=make_tool_registry(model_b, tool_b), store=store_b
            )
            try:
                created = await runner_a.create_run(
                    "assistant", "1.0", input="hi"
                )
                advancing = asyncio.create_task(
                    runner_a.start_run(created.run_id)
                )
                await entered.wait()
                clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))

                with self.assertRaises(LeaseNotHeldError):
                    await runner_b.cancel_run(created.run_id)
                self.assertEqual(
                    (await runner_b.get_run(created.run_id)).status,
                    RunStatus.RUNNING,
                )

                gate.set()
                with self.assertRaises(LeaseNotHeldError):
                    await advancing
                inspection = await runner_b.inspect_run(created.run_id)
                self.assertEqual(inspection.run.status, RunStatus.RUNNING)
                self.assertEqual(tool_a.call_count, 1)
                self.assertEqual(
                    [c for c in inspection.checkpoints if c.step_type.value == "TOOL"],
                    [],
                )
            finally:
                store_a.close()
                store_b.close()


class SeparateRunConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """不同 Run 可并发推进，没有全局执行锁（AC7）。"""

    async def test_runner_blocked_on_one_run_does_not_block_another(
        self,
    ) -> None:
        # InMemory 共享同一 store：A 挂起在 run1 的模型调用中时，
        # B 能完整推进 run2 —— 证明租约按 Run 隔离，无全局执行锁。
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "model_calls.log")
            clock = FakeClock()
            store = InMemoryRunStore(
                payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            entered, gate = asyncio.Event(), asyncio.Event()
            reg_a = make_registry(log, entered=entered, gate=gate)
            reg_b = make_registry(log)
            r_a = Runner(registry=reg_a, store=store)
            r_b = Runner(registry=reg_b, store=store)

            run1 = await r_a.create_run("assistant", "1.0", input="one")
            run2 = await r_b.create_run("assistant", "1.0", input="two")
            task = asyncio.create_task(r_a.start_run(run1.run_id))
            await entered.wait()  # A 挂起在 run1

            terminal2 = await r_b.start_run(run2.run_id)
            self.assertEqual(terminal2.status, RunStatus.SUCCEEDED)
            self.assertEqual(terminal2.output, _ANSWER)
            # run1 的租约不受 run2 影响。
            lease1 = await store.get_lease(run1.run_id)
            self.assertEqual(lease1.owner, r_a.owner)

            gate.set()
            terminal1 = await task
            self.assertEqual(terminal1.status, RunStatus.SUCCEEDED)
            self.assertEqual(count_model_calls(log), 2)

    async def test_sqlite_two_connections_advance_different_runs(
        self,
    ) -> None:
        # SQLite 双连接通过公开 Runner API 并发推进两个不同 Run。
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            log = os.path.join(tmp, "model_calls.log")
            clock = FakeClock()
            reg_a = make_registry(log)
            reg_b = make_registry(log)
            s_a = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            s_b = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            r_a = Runner(registry=reg_a, store=s_a)
            r_b = Runner(registry=reg_b, store=s_b)
            try:
                run1 = await r_a.create_run("assistant", "1.0", input="one")
                run2 = await r_b.create_run("assistant", "1.0", input="two")
                results = await asyncio.gather(
                    r_a.start_run(run1.run_id), r_b.start_run(run2.run_id)
                )
                self.assertEqual(
                    [r.status for r in results],
                    [RunStatus.SUCCEEDED, RunStatus.SUCCEEDED],
                )
                # 各自一个 Step、一次模型调用。
                self.assertEqual(
                    len((await r_a.inspect_run(run1.run_id)).steps), 1
                )
                self.assertEqual(
                    len((await r_b.inspect_run(run2.run_id)).steps), 1
                )
                self.assertEqual(count_model_calls(log), 2)
            finally:
                s_a.close()
                s_b.close()


if __name__ == "__main__":
    unittest.main()
