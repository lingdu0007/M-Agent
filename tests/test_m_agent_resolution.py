"""Ticket 07：裁决不确定的非幂等通知副作用（ADR 0008 / PRD US 13, 37-42）。

核心安全故事：NON_IDEMPOTENT 通知的外部效果已经发生（journal 证据，
独立于 RunStore），但 Tool Step checkpoint 未提交；第二进程恢复时
运行时**不得再次通知**，而是进入 WAITING，等待上层应用显式 resolution。

- 机器可读 WAITING 记录：reason / 目标 Step / 合法 resolution actions
  （AC 4）；
- 四种 resolution 逐一覆盖：CONFIRM_STEP（不执行工具，AC 5）、
  RETRY_STEP（新 Step Attempt，显式授权后重执行，AC 6）、FAIL_RUN /
  CANCEL_RUN（不再次调用工具，AC 7）；
- 模型无 resolution 权限（AC 8）；
- 重复 / 过期 / 非法命令明确失败且不破坏 Run（AC 9）；
- 端到端（含真实子进程 crash/restart）验证 notification effect count
  （AC 1-3, 10）。

断言只通过公开 Runner API（create/start/resume/resolve/inspect）与
公开数据模型驱动；崩溃通过 CrashPoint crash_hook 确定性注入；租约
过期通过 FakeClock 确定性推进（无真实 sleep）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from m_agent import (
    DEFAULT_LEASE_TTL,
    AgentDefinition,
    CrashPoint,
    DefinitionRegistry,
    DeterministicModelAdapter,
    DeterministicTool,
    FakeClock,
    FailureClassification,
    IllegalRunTransitionError,
    InMemoryRunStore,
    LeaseNotHeldError,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    PlaintextPayloadCodec,
    REASON_DEFINITION_UNAVAILABLE,
    REASON_UNCERTAIN_NON_IDEMPOTENT,
    ResolutionAction,
    ResolutionNotAllowedError,
    Runner,
    RunResolution,
    RunStatus,
    SQLiteRunStore,
    StaleRunVersionError,
    StepStatus,
    StepType,
    ToolCall,
    ToolEffect,
    ToolFailure,
    ToolOutcome,
    allowed_resolutions,
    deserialize_tool_outcome,
)

_TOOL_CALLING = ModelCapabilities(tool_calling=True)

_WORKER = Path(__file__).parent / "fixtures" / "notification_worker.py"
_CRASH_EXIT_CODE = 17


# -- 确定性 fake：模型与工具 ----------------------------------------


class RequestNotifyModel(DeterministicModelAdapter):
    """确定性模型：第一次请求 notify 工具，收到 outcome 后给出最终响应。"""

    deterministic: bool = True

    def __init__(self) -> None:
        super().__init__(capabilities=_TOOL_CALLING)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        if not request.tool_outcomes:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="call-notify",
                        tool_name="notify",
                        arguments="{}",
                    ),
                )
            )
        return ModelResponse(
            content="final: " + request.tool_outcomes[0].result
        )


class CountingNotifier(DeterministicTool):
    """NON_IDEMPOTENT fake：每次调用计数并返回 SUCCESS outcome。"""

    deterministic: bool = True

    def __init__(self) -> None:
        super().__init__(name="notify", effect=ToolEffect.NON_IDEMPOTENT)
        self.call_count = 0

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        self.call_count += 1
        return ToolOutcome.success(
            request.call_id, self.name, result=f"notification-{self.call_count}"
        )


class UncertainNotifier(CountingNotifier):
    """NON_IDEMPOTENT fake：执行后抛 UNCERTAIN（外部效果无法确认）。

    模拟真实通知场景：副作用可能已发生，但适配器无法确认结果。
    ``succeed_after`` 指定前 N 次失败后转为成功（用于验证 RETRY_STEP
    显式授权后重试可以完成 Run）。
    """

    deterministic: bool = True

    def __init__(self, succeed_after: int | None = None) -> None:
        super().__init__()
        self._succeed_after = succeed_after

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        self.call_count += 1
        if (
            self._succeed_after is not None
            and self.call_count > self._succeed_after
        ):
            return ToolOutcome.success(
                request.call_id,
                self.name,
                result=f"notification-{self.call_count}",
            )
        raise ToolFailure(
            FailureClassification.UNCERTAIN,
            "effect_unconfirmed",
            "delivery outcome is unknown",
        )


def build_registry(
    model: DeterministicModelAdapter,
    tool: CountingNotifier,
) -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition(
            definition_id="support_agent",
            version="1.0",
            instructions="Notify the customer deterministically.",
            required_capabilities=_TOOL_CALLING,
            model_adapter=model,
            tools=(tool,),
        )
    )
    return registry


# -- InMemory 单进程测试（同一 Runner 续约） -------------------------


class InMemoryResolutionTests(unittest.IsolatedAsyncioTestCase):
    """UNCERTAIN NON_IDEMPOTENT -> WAITING 后的四种 resolution 与约束。"""

    def make_runner(
        self,
        model: DeterministicModelAdapter,
        tool: CountingNotifier,
        crash_hook=None,
        clock: FakeClock | None = None,
    ) -> tuple[Runner, InMemoryRunStore, FakeClock]:
        registry = build_registry(model, tool)
        if clock is None:
            clock = FakeClock()
        store = InMemoryRunStore(
            payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        runner = Runner(
            registry=registry, store=store, crash_hook=crash_hook
        )
        return runner, store, clock

    async def _waiting_run(
        self,
        runner: Runner,
        store: InMemoryRunStore,
        tool: CountingNotifier,
        model: DeterministicModelAdapter,
    ) -> str:
        created = await runner.create_run("support_agent", "1.0", input="hi")
        waiting = await runner.start_run(created.run_id)
        self.assertEqual(waiting.status, RunStatus.WAITING)
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(model.call_count, 1)
        return created.run_id

    async def _resolve_current(
        self, runner: Runner, run_id: str, resolution: RunResolution
    ):
        """以应用刚观察到的权威 version 提交 resolution 命令。"""
        current = await runner.get_run(run_id)
        return await runner.resolve_run(
            run_id,
            resolution,
            expected_version=current.version,
        )

    async def test_waiting_record_is_machine_readable(self) -> None:
        # AC 3/4：WAITING 记录暴露机器可读 reason、目标 Step Attempt 与
        # 合法 resolution actions；工具被调用一次，无 TOOL checkpoint。
        tool = UncertainNotifier()
        model = RequestNotifyModel()
        runner, store, _ = self.make_runner(model, tool)
        run_id = await self._waiting_run(runner, store, tool, model)

        waiting = await runner.get_run(run_id)
        self.assertEqual(
            waiting.waiting_reason, REASON_UNCERTAIN_NON_IDEMPOTENT
        )
        self.assertIsNotNone(waiting.waiting_step_id)
        self.assertEqual(
            tuple(a for a in allowed_resolutions(waiting)),
            (
                ResolutionAction.RETRY_STEP,
                ResolutionAction.CONFIRM_STEP,
                ResolutionAction.FAIL_RUN,
                ResolutionAction.CANCEL_RUN,
            ),
        )
        # 目标 Step Attempt 可检查：UNCERTAIN 失败 Attempt 指向同一 step。
        inspection = await runner.inspect_run(run_id)
        failed = [
            a for a in inspection.attempts if a.status is StepStatus.FAILED
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].step_id, waiting.waiting_step_id)
        self.assertEqual(
            failed[0].classification, FailureClassification.UNCERTAIN
        )
        self.assertEqual(failed[0].error_code, "effect_unconfirmed")
        self.assertEqual(
            [c for c in inspection.checkpoints if c.step_type is StepType.TOOL],
            [],
        )
        # WAITING 非终态。
        self.assertFalse(waiting.status.is_terminal)

    async def test_confirm_step_checkpoints_without_reexecution(self) -> None:
        # AC 5：CONFIRM_STEP 接受应用结果，不执行工具即写入 Tool Step
        # 的 Attempt + Checkpoint，并允许 Run 继续到 SUCCEEDED。
        tool = UncertainNotifier()
        model = RequestNotifyModel()
        runner, store, _ = self.make_runner(model, tool)
        run_id = await self._waiting_run(runner, store, tool, model)

        terminal = await self._resolve_current(
            runner, run_id, RunResolution.confirm_step("acknowledged-by-app")
        )
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(terminal.output, "final: acknowledged-by-app")
        # 工具没有被再次调用（CONFIRM 不执行工具）。
        self.assertEqual(tool.call_count, 1)
        inspection = await runner.inspect_run(run_id)
        tool_ckpts = [
            c for c in inspection.checkpoints if c.step_type is StepType.TOOL
        ]
        self.assertEqual(len(tool_ckpts), 1)
        outcome = deserialize_tool_outcome(tool_ckpts[0].output)
        self.assertEqual(outcome.call_id, "call-notify")
        self.assertEqual(outcome.result, "acknowledged-by-app")
        # 模型第二次请求收到应用确认的结果（作为外部数据）。
        self.assertEqual(model.call_count, 2)
        self.assertEqual(
            model.last_request.tool_outcomes[0].result,
            "acknowledged-by-app",
        )

    async def test_retry_step_creates_new_attempt_and_reexecutes(self) -> None:
        # AC 6：RETRY_STEP 创建新的 Step Attempt（同一 step_id）并在显式
        # 授权后重新执行工具；历史 UNCERTAIN Attempt 保留。应用授权后
        # 重试成功（工具第二次调用转成功），Run 继续到 SUCCEEDED。
        tool = UncertainNotifier(succeed_after=1)
        model = RequestNotifyModel()
        runner, store, _ = self.make_runner(model, tool)
        run_id = await self._waiting_run(runner, store, tool, model)
        waiting = await runner.get_run(run_id)
        target_step = waiting.waiting_step_id

        terminal = await self._resolve_current(
            runner, run_id, RunResolution.retry_step()
        )
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        # 显式授权后工具被第二次执行（重试 = 应用判断副作用安全）。
        self.assertEqual(tool.call_count, 2)
        self.assertEqual(terminal.output, "final: notification-2")
        inspection = await runner.inspect_run(run_id)
        attempts = [
            a for a in inspection.attempts if a.step_id == target_step
        ]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            [a.status for a in attempts],
            [StepStatus.FAILED, StepStatus.SUCCEEDED],
        )
        self.assertEqual(
            attempts[0].classification, FailureClassification.UNCERTAIN
        )
        self.assertEqual(
            attempts[1].classification, None  # 成功 Attempt 无分类
        )
        # 新 Attempt 使用不同 attempt_id。
        self.assertNotEqual(attempts[0].attempt_id, attempts[1].attempt_id)

    async def test_fail_run_and_cancel_run_do_not_invoke_tool(self) -> None:
        # AC 7：FAIL_RUN -> FAILED，CANCEL_RUN -> CANCELLED，均不再调用
        # 工具；WAITING 记录的目标 Step 不产生新 checkpoint。
        for action in (
            ResolutionAction.FAIL_RUN,
            ResolutionAction.CANCEL_RUN,
        ):
            with self.subTest(action=action.value):
                tool = UncertainNotifier()
                model = RequestNotifyModel()
                runner, store, _ = self.make_runner(model, tool)
                run_id = await self._waiting_run(runner, store, tool, model)

                terminal = await self._resolve_current(
                    runner, run_id, RunResolution(action=action)
                )
                expected = (
                    RunStatus.FAILED
                    if action is ResolutionAction.FAIL_RUN
                    else RunStatus.CANCELLED
                )
                self.assertEqual(terminal.status, expected)
                self.assertTrue(terminal.status.is_terminal)
                # 工具仍只调用一次（终结命令绝不再次调用工具）。
                self.assertEqual(tool.call_count, 1)
                inspection = await runner.inspect_run(run_id)
                self.assertEqual(
                    [
                        c
                        for c in inspection.checkpoints
                        if c.step_type is StepType.TOOL
                    ],
                    [],
                )
                # 终态后 WAITING 字段清空（Store 不变量）。
                self.assertIsNone(terminal.waiting_reason)
                self.assertIsNone(terminal.waiting_step_id)

    async def test_model_cannot_issue_resolution(self) -> None:
        # AC 8：模型没有任何 resolution 权限。ModelResponse 契约只有
        # content / tool_calls——模型无法请求、无法合成 resolution；
        # 唯一入口是 Runner.resolve_run。即使模型输出看起来像 resolution
        # 文本，也只是普通最终内容，Run 不会自动 WAITING 或重试。
        tool = CountingNotifier()
        model = RequestNotifyModel()
        runner, store, _ = self.make_runner(model, tool)
        created = await runner.create_run("support_agent", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        # 模型响应中不存在 resolution 字段。
        self.assertFalse(
            hasattr(model.last_request, "resolution")
            or "resolution" in model.last_request.model_dump()
        )
        # resolve_run 只能显式调用；对非 WAITING Run 一律拒绝。
        with self.assertRaises(IllegalRunTransitionError):
            await self._resolve_current(
                runner, created.run_id, RunResolution.confirm_step("x")
            )

    async def test_duplicate_resolution_fails_predictably(self) -> None:
        # AC 9：重复命令明确失败。CONFIRM 成功后 Run 已离开 WAITING，
        # 再次 resolve 抛 IllegalRunTransitionError 且不破坏终态记录。
        tool = UncertainNotifier()
        model = RequestNotifyModel()
        runner, store, _ = self.make_runner(model, tool)
        run_id = await self._waiting_run(runner, store, tool, model)

        terminal = await self._resolve_current(
            runner, run_id, RunResolution.confirm_step("ok")
        )
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        with self.assertRaises(IllegalRunTransitionError):
            await self._resolve_current(
                runner, run_id, RunResolution.fail_run()
            )
        # 权威记录未被破坏。
        after = await runner.get_run(run_id)
        self.assertEqual(after.status, RunStatus.SUCCEEDED)

    async def test_wrong_waiting_step_id_fails_without_mutation(self) -> None:
        # AC 9：应用命令可携带其观察到的 waiting_step_id。若该目标已
        # 不匹配当前 WAITING Step，Runner 必须显式拒绝，且不创建
        # checkpoint / Attempt、不调用模型或工具。
        tool = UncertainNotifier()
        model = RequestNotifyModel()
        runner, store, _ = self.make_runner(model, tool)
        run_id = await self._waiting_run(runner, store, tool, model)
        before = await runner.inspect_run(run_id)

        with self.assertRaises(ResolutionNotAllowedError):
            await self._resolve_current(
                runner,
                run_id,
                RunResolution(
                    action=ResolutionAction.CONFIRM_STEP,
                    result="application-result",
                    waiting_step_id="wrong-step-id",
                ),
            )

        after = await runner.inspect_run(run_id)
        self.assertEqual(after.run, before.run)
        self.assertEqual(after.steps, before.steps)
        self.assertEqual(after.attempts, before.attempts)
        self.assertEqual(after.checkpoints, before.checkpoints)
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(model.call_count, 1)

    async def test_resolution_requires_expected_version(self) -> None:
        # AC 9：resolution 是带 expected_version 的应用命令。调用方不
        # 提供其观察到的版本必须在任何 lease / Run mutation 前失败。
        tool = UncertainNotifier()
        model = RequestNotifyModel()
        runner, store, _ = self.make_runner(model, tool)
        run_id = await self._waiting_run(runner, store, tool, model)
        before = await runner.inspect_run(run_id)

        with self.assertRaises(TypeError):
            await runner.resolve_run(
                run_id, RunResolution.confirm_step("application-result")
            )

        after = await runner.inspect_run(run_id)
        self.assertEqual(after.run, before.run)
        self.assertEqual(after.steps, before.steps)
        self.assertEqual(after.attempts, before.attempts)
        self.assertEqual(after.checkpoints, before.checkpoints)
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(model.call_count, 1)

    async def test_stale_expected_version_resolution_fails(self) -> None:
        # AC 9：调用方传入过期的 expected_version -> StaleRunVersionError，
        # 命令不改动任何记录；最新版本命令仍可正常完成。
        tool = UncertainNotifier()
        model = RequestNotifyModel()
        runner, store, _ = self.make_runner(model, tool)
        run_id = await self._waiting_run(runner, store, tool, model)
        stale_version = (await runner.get_run(run_id)).version
        # 同 owner 再做一次失败重试：WAITING -> RUNNING -> WAITING，
        # 权威版本推进（+2），Run 仍 WAITING。
        again = await self._resolve_current(
            runner, run_id, RunResolution.retry_step()
        )
        self.assertEqual(again.status, RunStatus.WAITING)
        self.assertGreater(again.version, stale_version)
        # 基于过期版本的命令被明确拒绝。
        with self.assertRaises(StaleRunVersionError):
            await runner.resolve_run(
                run_id,
                RunResolution.confirm_step("x"),
                expected_version=stale_version,
            )
        # 权威记录未被破坏：仍 WAITING，最新版本命令正常完成。
        terminal = await self._resolve_current(
            runner, run_id, RunResolution.confirm_step("ok")
        )
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)

    async def test_stale_version_cannot_take_expired_lease(self) -> None:
        # AC 9：过期 owner 的 lease 已可被接管时，带旧 version 的
        # resolution 仍必须在获取 lease 前失败。失败命令不能把 Run 的
        # lease_owner 改成自己或清空既有权威记录。
        tool = UncertainNotifier()
        model = RequestNotifyModel()
        registry = build_registry(model, tool)
        clock = FakeClock()
        store = InMemoryRunStore(
            payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        original = Runner(registry=registry, store=store)
        run_id = await self._waiting_run(original, store, tool, model)
        waiting = await original.get_run(run_id)
        before = await original.inspect_run(run_id)
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        contender = Runner(registry=registry, store=store)

        with self.assertRaises(StaleRunVersionError):
            await contender.resolve_run(
                run_id,
                RunResolution.fail_run(
                    waiting_step_id=waiting.waiting_step_id
                ),
                expected_version=waiting.version - 1,
            )

        after = await original.inspect_run(run_id)
        self.assertEqual(after.run, before.run)
        self.assertEqual(after.steps, before.steps)
        self.assertEqual(after.attempts, before.attempts)
        self.assertEqual(after.checkpoints, before.checkpoints)
        self.assertEqual(tool.call_count, 1)
        self.assertEqual(model.call_count, 1)

    async def test_stale_resolution_after_takeover_fails(self) -> None:
        # AC 9（+ADR 0013）：过期命令失败。另一 owner 在租约过期后完成
        # resolution 后，旧 owner 的迟到命令被明确拒绝（状态/租约校验），
        # 权威记录保持终态、不被覆盖。
        tool = UncertainNotifier()
        model = RequestNotifyModel()
        registry = build_registry(model, tool)
        clock = FakeClock()
        store = InMemoryRunStore(
            payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        owner1 = Runner(registry=registry, store=store)
        run_id = await self._waiting_run(owner1, store, tool, model)

        owner2 = Runner(registry=registry, store=store)
        # owner1 的租约仍有效：owner2 的并发命令被明确拒绝。
        with self.assertRaises(LeaseNotHeldError):
            await self._resolve_current(owner2, run_id, RunResolution.fail_run())
        # 租约过期后 owner2 接管并完成 FAIL_RUN。
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        terminal = await self._resolve_current(
            owner2, run_id, RunResolution.fail_run()
        )
        self.assertEqual(terminal.status, RunStatus.FAILED)
        # owner1 迟到的 resolution：Run 已离开 WAITING，明确失败。
        with self.assertRaises(IllegalRunTransitionError):
            await self._resolve_current(
                owner1, run_id, RunResolution.cancel_run()
            )
        # 权威记录未被破坏。
        after = await owner1.get_run(run_id)
        self.assertEqual(after.status, RunStatus.FAILED)
        self.assertEqual(tool.call_count, 1)

    async def test_resolution_requires_active_lease(self) -> None:
        # AC 9（+ADR 0013）：无有效租约的 Runner 不能 resolution；
        # 租约过期后新 owner 才能接管。
        tool = UncertainNotifier()
        model = RequestNotifyModel()
        registry = build_registry(model, tool)
        clock = FakeClock()
        store = InMemoryRunStore(
            payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        owner1 = Runner(registry=registry, store=store)
        run_id = await self._waiting_run(owner1, store, tool, model)

        # 另一个 owner 在租约有效期内被拒绝。
        owner2 = Runner(registry=registry, store=store)
        with self.assertRaises(LeaseNotHeldError):
            await self._resolve_current(owner2, run_id, RunResolution.fail_run())
        # 租约过期后新 owner 可接管并 resolution。
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        terminal = await self._resolve_current(
            owner2, run_id, RunResolution.fail_run()
        )
        self.assertEqual(terminal.status, RunStatus.FAILED)
        self.assertEqual(tool.call_count, 1)

    async def test_illegal_action_for_reason_fails(self) -> None:
        # AC 9：对 DEFINITION_UNAVAILABLE 的 WAITING Run 提交
        # RETRY_STEP / CONFIRM_STEP（无目标 Step 可处置）被明确拒绝；
        # 但 FAIL_RUN / CANCEL_RUN 合法。权威记录不被改动。
        registry = build_registry(RequestNotifyModel(), CountingNotifier())
        clock = FakeClock()
        store = InMemoryRunStore(
            payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        runner = Runner(registry=registry, store=store)
        created = await runner.create_run(
            "support_agent", "1.0", input="hi"
        )
        # 崩溃在模型 checkpoint 之后（Run 保持 RUNNING，无后续步骤）。
        def hook(p: CrashPoint, run_id: str) -> None:
            if p is CrashPoint.AFTER_MODEL_CHECKPOINT:
                raise RuntimeError("injected crash after model checkpoint")

        crashing = Runner(registry=registry, store=store, crash_hook=hook)
        with self.assertRaises(RuntimeError):
            await crashing.start_run(created.run_id)
        # 租约过期后，用"不注册该定义"的 registry 恢复：精确解析失败 ->
        # WAITING(DEFINITION_UNAVAILABLE)，绝不回退到最新定义。
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        ghost = Runner(registry=DefinitionRegistry(), store=store)
        waiting = await ghost.resume_run(created.run_id)
        self.assertEqual(waiting.status, RunStatus.WAITING)
        self.assertEqual(
            waiting.waiting_reason, REASON_DEFINITION_UNAVAILABLE
        )
        self.assertEqual(
            tuple(a for a in allowed_resolutions(waiting)),
            (ResolutionAction.FAIL_RUN, ResolutionAction.CANCEL_RUN),
        )
        with self.assertRaises(ResolutionNotAllowedError):
            await self._resolve_current(
                ghost,
                created.run_id, RunResolution.retry_step()
            )
        with self.assertRaises(ResolutionNotAllowedError):
            await self._resolve_current(
                ghost,
                created.run_id, RunResolution.confirm_step("x")
            )
        terminal = await self._resolve_current(
            ghost,
            created.run_id, RunResolution.fail_run()
        )
        self.assertEqual(terminal.status, RunStatus.FAILED)

    async def test_confirm_without_result_fails(self) -> None:
        # AC 9：CONFIRM_STEP 缺少应用确认结果被明确拒绝。
        tool = UncertainNotifier()
        model = RequestNotifyModel()
        runner, store, _ = self.make_runner(model, tool)
        run_id = await self._waiting_run(runner, store, tool, model)
        with self.assertRaises(ResolutionNotAllowedError):
            await self._resolve_current(
                runner,
                run_id,
                RunResolution(action=ResolutionAction.CONFIRM_STEP),
            )
        # 记录未被改动，仍可正常 resolution。
        terminal = await self._resolve_current(
            runner, run_id, RunResolution.confirm_step("late-result")
        )
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)

    async def test_resolution_on_non_waiting_fails(self) -> None:
        # AC 9：对非 WAITING（CREATED / RUNNING / 终态）Run 的 resolution
        # 一律明确失败。
        tool = CountingNotifier()
        model = RequestNotifyModel()
        runner, store, _ = self.make_runner(model, tool)
        created = await runner.create_run("support_agent", "1.0", input="hi")
        with self.assertRaises(IllegalRunTransitionError):
            await self._resolve_current(
                runner, created.run_id, RunResolution.fail_run()
            )
        await runner.start_run(created.run_id)
        with self.assertRaises(IllegalRunTransitionError):
            await self._resolve_current(
                runner, created.run_id, RunResolution.fail_run()
            )

    async def test_retry_failure_reenters_waiting_then_confirm(self) -> None:
        # 增强：RETRY_STEP 后工具再次不确定 -> 再次 WAITING（不自动
        # 重放），应用随后可用 CONFIRM_STEP 完成；工具只被显式调用两次。
        attempts = 0

        class FailTwiceNotifier(CountingNotifier):
            async def invoke(self, request: ToolRequest) -> ToolOutcome:
                nonlocal attempts
                self.call_count += 1
                attempts += 1
                if attempts <= 2:  # 前两次 UNCERTAIN，第三次成功
                    raise ToolFailure(
                        FailureClassification.UNCERTAIN,
                        "effect_unconfirmed",
                        "still unknown",
                    )
                return ToolOutcome.success(
                    request.call_id,
                    self.name,
                    result=f"notification-{self.call_count}",
                )

        tool = FailTwiceNotifier()
        model = RequestNotifyModel()
        runner, store, _ = self.make_runner(model, tool)
        run_id = await self._waiting_run(runner, store, tool, model)
        # RETRY_STEP -> 又失败 -> 再次 WAITING（不自动重放）。
        again = await self._resolve_current(
            runner, run_id, RunResolution.retry_step()
        )
        self.assertEqual(again.status, RunStatus.WAITING)
        self.assertEqual(tool.call_count, 2)
        # 第二次显式 RETRY_STEP -> 成功完成。
        terminal = await self._resolve_current(
            runner, run_id, RunResolution.retry_step()
        )
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(tool.call_count, 3)
        inspection = await runner.inspect_run(run_id)
        attempts_for_step = [
            a
            for a in inspection.attempts
            if a.step_id == again.waiting_step_id
        ]
        # 三次 Attempt：两次 UNCERTAIN 失败 + 一次成功，全在同一 step_id。
        self.assertEqual(len(attempts_for_step), 3)

    async def test_retry_crash_normalizes_latest_attempt_to_uncertain(
        self,
    ) -> None:
        # 已授权 RETRY_STEP 的工具已经执行成功但 checkpoint 前崩溃时，
        # 该次新 Attempt 也必须被恢复为 UNCERTAIN；旧 Attempt 的失败
        # 证据不能掩盖它。这一流程只通过 Runner 的公共命令恢复。
        tool = UncertainNotifier(succeed_after=1)

        def hook(point: CrashPoint, run_id: str) -> None:
            if (
                point is CrashPoint.BEFORE_TOOL_CHECKPOINT
                and tool.call_count == 2
            ):
                raise RuntimeError("crash after authorized retry dispatch")

        model = RequestNotifyModel()
        runner, store, _ = self.make_runner(model, tool, crash_hook=hook)
        run_id = await self._waiting_run(runner, store, tool, model)
        initial_waiting = await runner.get_run(run_id)
        target_step_id = initial_waiting.waiting_step_id

        with self.assertRaisesRegex(RuntimeError, "authorized retry"):
            await self._resolve_current(
                runner, run_id, RunResolution.retry_step()
            )
        self.assertEqual(tool.call_count, 2)

        # A new Runner represents recovery after the retrying process exits.
        recovery_tool = UncertainNotifier(succeed_after=1)
        recovery = Runner(
            registry=build_registry(RequestNotifyModel(), recovery_tool),
            store=store,
        )
        waiting = await recovery.resume_run(run_id)
        self.assertEqual(waiting.status, RunStatus.WAITING)
        self.assertEqual(
            waiting.waiting_reason, REASON_UNCERTAIN_NON_IDEMPOTENT
        )
        self.assertEqual(waiting.waiting_step_id, target_step_id)
        self.assertEqual(recovery_tool.call_count, 0)

        inspection = await recovery.inspect_run(run_id)
        attempts = [
            attempt
            for attempt in inspection.attempts
            if attempt.step_id == target_step_id
        ]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            [attempt.status for attempt in attempts],
            [StepStatus.FAILED, StepStatus.FAILED],
        )
        self.assertTrue(
            all(
                attempt.classification is FailureClassification.UNCERTAIN
                and attempt.error_code == "effect_unconfirmed"
                for attempt in attempts
            )
        )
        self.assertEqual(
            [
                checkpoint
                for checkpoint in inspection.checkpoints
                if checkpoint.step_type is StepType.TOOL
            ],
            [],
        )


# -- SQLite：崩溃后恢复进入 WAITING（跨进程语义同进程验证） ----------


class SQLiteCrashResolutionTests(unittest.IsolatedAsyncioTestCase):
    """BEFORE_TOOL_CHECKPOINT 崩溃 -> 第二进程恢复 -> WAITING -> resolution。"""

    async def test_crash_before_tool_checkpoint_resumes_to_waiting(self) -> None:
        # AC 1/2/3：通知效果已发生（外部 journal +1），崩溃在 checkpoint
        # 提交前；恢复绝不再次通知，进入 WAITING 且机器可读。
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            journal = os.path.join(tmp, "notify.journal")

            def hook(p: CrashPoint, run_id: str) -> None:
                if p is CrashPoint.BEFORE_TOOL_CHECKPOINT:
                    raise RuntimeError("injected crash before tool checkpoint")

            clock = FakeClock()
            model = RequestNotifyModel()
            tool = CountingNotifier()
            registry = build_registry(model, tool)
            store = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            runner = Runner(registry=registry, store=store, crash_hook=hook)
            created = await runner.create_run(
                "support_agent", "1.0", input="hi"
            )
            with self.assertRaises(RuntimeError):
                await runner.start_run(created.run_id)
            store.close()

            # 第二进程（重开 db + 时钟越过崩溃租约）恢复。
            probe = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec()
            )
            crashed = await probe.get_run(created.run_id)
            probe.close()
            assert crashed is not None and crashed.lease_expires_at is not None
            clock2 = FakeClock(
                start=crashed.lease_expires_at + timedelta(seconds=1)
            )
            store2 = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock2
            )
            try:
                tool2 = CountingNotifier()
                runner2 = Runner(
                    registry=build_registry(RequestNotifyModel(), tool2),
                    store=store2,
                )
                waiting = await runner2.resume_run(created.run_id)
                self.assertEqual(waiting.status, RunStatus.WAITING)
                self.assertEqual(
                    waiting.waiting_reason, REASON_UNCERTAIN_NON_IDEMPOTENT
                )
                self.assertIsNotNone(waiting.waiting_step_id)
                # 第二进程的工具一次都没被调用：无重复通知。
                self.assertEqual(tool2.call_count, 0)
                # UNCERTAIN 失败 Attempt 持久化（恢复路径机器可读）。
                inspection = await runner2.inspect_run(created.run_id)
                failed = [
                    a
                    for a in inspection.attempts
                    if a.status is StepStatus.FAILED
                ]
                self.assertEqual(len(failed), 1)
                self.assertEqual(
                    failed[0].classification, FailureClassification.UNCERTAIN
                )
                self.assertEqual(failed[0].error_code, "effect_unconfirmed")
                self.assertEqual(
                    failed[0].step_id, waiting.waiting_step_id
                )
            finally:
                store2.close()

    async def test_sqlite_confirm_after_crash_completes_run(self) -> None:
        # AC 5 + 外部 journal 证据：效果已发生（journal=1）且 checkpoint
        # 未提交；恢复 WAITING 不重复通知；CONFIRM_STEP 后完成，通知仍
        # 只一次。
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            journal = os.path.join(tmp, "notify.journal")

            def hook(p: CrashPoint, run_id: str) -> None:
                if p is CrashPoint.BEFORE_TOOL_CHECKPOINT:
                    raise RuntimeError("injected crash before checkpoint")

            clock = FakeClock()
            tool = JournalCountingNotifier(journal)
            model = RequestNotifyModel()
            registry = build_registry(model, tool)
            store = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            runner = Runner(registry=registry, store=store, crash_hook=hook)
            created = await runner.create_run(
                "support_agent", "1.0", input="hi"
            )
            with self.assertRaises(RuntimeError):
                await runner.start_run(created.run_id)
            store.close()
            # 外部效果已发生，checkpoint 未提交。
            self.assertEqual(count_journal(journal), 1)

            # 第二进程：越过崩溃租约后接管。
            probe = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
            crashed = await probe.get_run(created.run_id)
            probe.close()
            assert crashed is not None and crashed.lease_expires_at is not None
            clock2 = FakeClock(
                start=crashed.lease_expires_at + timedelta(seconds=1)
            )
            store2 = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock2
            )
            try:
                tool2 = JournalCountingNotifier(journal)
                runner2 = Runner(
                    registry=build_registry(RequestNotifyModel(), tool2),
                    store=store2,
                )
                waiting = await runner2.resume_run(created.run_id)
                self.assertEqual(waiting.status, RunStatus.WAITING)
                self.assertEqual(count_journal(journal), 1)  # 无重复通知
                terminal = await runner2.resolve_run(
                    created.run_id,
                    RunResolution.confirm_step("seen"),
                    expected_version=waiting.version,
                )
                self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
                self.assertEqual(terminal.output, "final: seen")
                # 通知效果计数保持 1（CONFIRM 不执行工具）。
                self.assertEqual(count_journal(journal), 1)
            finally:
                store2.close()


class JournalCountingNotifier(CountingNotifier):
    """NON_IDEMPOTENT fake：把效果追加到独立 journal 文件（外部证据）。"""

    deterministic: bool = True

    def __init__(self, journal_path: str) -> None:
        super().__init__()
        self._journal_path = journal_path

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        outcome = await super().invoke(request)
        with open(self._journal_path, "a", encoding="utf-8") as fh:
            fh.write(f"notify:{self.call_count}\n")
        return outcome


def count_journal(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        return len([line for line in fh if line.strip()])


# -- 真实子进程：crash / restart / resolve ---------------------------


def run_worker(
    db_path: str,
    journal_path: str,
    model_journal_path: str,
    mode: str,
    run_id: str,
    action: str | None = None,
    result: str | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(_WORKER),
        db_path,
        journal_path,
        model_journal_path,
        mode,
        run_id,
    ]
    if action is not None:
        cmd.append(action)
    if result is not None:
        cmd.append(result)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def parse_run_id(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("RUN_ID="):
            return line.split("=", 1)[1]
    raise AssertionError(f"worker did not print RUN_ID; stdout={stdout!r}")


def parse_line(stdout: str, key: str) -> str:
    prefix = f"{key}="
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1]
    raise AssertionError(
        f"worker did not print {key}; stdout={stdout!r}"
    )


def parse_json_line(stdout: str, key: str) -> dict[str, object]:
    """读取 worker 公开 Runner inspection 输出的单行 JSON 证据。"""
    return json.loads(parse_line(stdout, key))


class SubprocessResolutionTests(unittest.IsolatedAsyncioTestCase):
    """真实子进程：通知效果 -> 硬崩溃 -> 第二进程恢复 WAITING（不重复
    通知）-> 四种 resolution，外部 journal 计数作为副作用证据。"""

    async def _crash_and_get_run(
        self, tmp: str
    ) -> tuple[str, str, str, str]:
        db = os.path.join(tmp, "run.db")
        journal = os.path.join(tmp, "notify.journal")
        model_journal = os.path.join(tmp, "model.journal")
        proc = run_worker(
            db,
            journal,
            model_journal,
            "notify-and-crash",
            "unused",
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.returncode, _CRASH_EXIT_CODE)
        run_id = parse_run_id(proc.stdout)
        # 通知效果已发生：外部 journal 有一行。
        self.assertEqual(count_journal(journal), 1)
        return db, journal, model_journal, run_id

    def _assert_waiting_evidence(
        self, evidence: dict[str, object]
    ) -> tuple[str, str]:
        """断言第二进程未重放通知时的公开 inspection 事实。"""
        waiting = evidence["waiting"]
        assert isinstance(waiting, dict)
        self.assertEqual(waiting["status"], "WAITING")
        self.assertEqual(
            waiting["waiting_reason"], REASON_UNCERTAIN_NON_IDEMPOTENT
        )
        step_id = waiting["waiting_step_id"]
        self.assertIsInstance(step_id, str)
        self.assertTrue(step_id)
        self.assertEqual(
            waiting["allowed_actions"],
            [
                "RETRY_STEP",
                "CONFIRM_STEP",
                "FAIL_RUN",
                "CANCEL_RUN",
            ],
        )
        attempts = waiting["attempts"]
        checkpoints = waiting["checkpoints"]
        assert isinstance(attempts, list)
        assert isinstance(checkpoints, list)
        tool_attempts = [a for a in attempts if a["step_id"] == step_id]
        self.assertEqual(len(tool_attempts), 1)
        original = tool_attempts[0]
        self.assertEqual(original["status"], "FAILED")
        self.assertEqual(original["classification"], "UNCERTAIN")
        self.assertEqual(original["error_code"], "effect_unconfirmed")
        self.assertEqual(
            [c for c in checkpoints if c["step_type"] == "TOOL"], []
        )
        self.assertEqual(evidence["waiting_model_call_count"], 1)
        self.assertEqual(evidence["waiting_notification_count"], 1)
        attempt_id = original["attempt_id"]
        self.assertIsInstance(attempt_id, str)
        return step_id, attempt_id

    def _assert_completed_tool_evidence(
        self,
        evidence: dict[str, object],
        *,
        expected_notifications: int,
    ) -> None:
        """CONFIRM / RETRY 的同一 Tool Step 轨迹应保留原 Attempt。"""
        step_id, original_attempt_id = self._assert_waiting_evidence(evidence)
        terminal = evidence["terminal"]
        assert isinstance(terminal, dict)
        self.assertEqual(terminal["status"], "SUCCEEDED")
        attempts = terminal["attempts"]
        checkpoints = terminal["checkpoints"]
        assert isinstance(attempts, list)
        assert isinstance(checkpoints, list)
        tool_attempts = [a for a in attempts if a["step_id"] == step_id]
        self.assertEqual(len(tool_attempts), 2)
        failed, succeeded = tool_attempts
        self.assertEqual(failed["attempt_id"], original_attempt_id)
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(succeeded["status"], "SUCCEEDED")
        self.assertNotEqual(succeeded["attempt_id"], original_attempt_id)
        tool_checkpoints = [
            c for c in checkpoints if c["step_type"] == "TOOL"
        ]
        self.assertEqual(len(tool_checkpoints), 1)
        self.assertEqual(tool_checkpoints[0]["step_id"], step_id)
        self.assertEqual(
            tool_checkpoints[0]["attempt_id"], succeeded["attempt_id"]
        )
        self.assertEqual(evidence["model_call_count"], 2)
        self.assertEqual(evidence["notification_count"], expected_notifications)

    def _assert_terminated_without_tool_evidence(
        self, evidence: dict[str, object], expected_status: str
    ) -> None:
        """FAIL / CANCEL 保留不确定 Attempt，且不执行后续 Model / Tool。"""
        step_id, original_attempt_id = self._assert_waiting_evidence(evidence)
        terminal = evidence["terminal"]
        assert isinstance(terminal, dict)
        self.assertEqual(terminal["status"], expected_status)
        attempts = terminal["attempts"]
        checkpoints = terminal["checkpoints"]
        assert isinstance(attempts, list)
        assert isinstance(checkpoints, list)
        tool_attempts = [a for a in attempts if a["step_id"] == step_id]
        self.assertEqual(len(tool_attempts), 1)
        self.assertEqual(tool_attempts[0]["attempt_id"], original_attempt_id)
        self.assertEqual(tool_attempts[0]["status"], "FAILED")
        self.assertEqual(
            [c for c in checkpoints if c["step_type"] == "TOOL"], []
        )
        self.assertEqual(evidence["model_call_count"], 1)
        self.assertEqual(evidence["notification_count"], 1)

    async def test_subprocess_resume_enters_waiting_without_duplicate(
        self,
    ) -> None:
        # AC 1/2/3：第二进程恢复 -> WAITING，机器可读记录，通知只一次。
        with tempfile.TemporaryDirectory() as tmp:
            db, journal, model_journal, run_id = await self._crash_and_get_run(tmp)
            proc = run_worker(db, journal, model_journal, "resume", run_id)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(parse_line(proc.stdout, "STATUS"), "WAITING")
            self.assertEqual(
                parse_line(proc.stdout, "WAITING_REASON"),
                REASON_UNCERTAIN_NON_IDEMPOTENT,
            )
            self.assertTrue(parse_line(proc.stdout, "STEP_ID"))
            self.assertEqual(
                parse_line(proc.stdout, "ALLOWED"),
                "RETRY_STEP,CONFIRM_STEP,FAIL_RUN,CANCEL_RUN",
            )
            self.assertEqual(count_journal(journal), 1)
            self._assert_waiting_evidence(
                parse_json_line(proc.stdout, "EVIDENCE")
            )

    async def test_subprocess_confirm_step(self) -> None:
        # AC 5/10：CONFIRM_STEP(result) -> SUCCEEDED，通知仍只一次。
        with tempfile.TemporaryDirectory() as tmp:
            db, journal, model_journal, run_id = await self._crash_and_get_run(tmp)
            proc = run_worker(
                db,
                journal,
                model_journal,
                "resolve",
                run_id,
                "CONFIRM_STEP",
                "app-seen",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(parse_line(proc.stdout, "STATUS"), "SUCCEEDED")
            self.assertEqual(count_journal(journal), 1)
            self._assert_completed_tool_evidence(
                parse_json_line(proc.stdout, "EVIDENCE"),
                expected_notifications=1,
            )

    async def test_subprocess_retry_step(self) -> None:
        # AC 6/10：RETRY_STEP -> 显式授权后重试，通知计数 +1。
        with tempfile.TemporaryDirectory() as tmp:
            db, journal, model_journal, run_id = await self._crash_and_get_run(tmp)
            proc = run_worker(
                db, journal, model_journal, "resolve", run_id, "RETRY_STEP"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(parse_line(proc.stdout, "STATUS"), "SUCCEEDED")
            self.assertEqual(count_journal(journal), 2)
            self._assert_completed_tool_evidence(
                parse_json_line(proc.stdout, "EVIDENCE"),
                expected_notifications=2,
            )

    async def test_subprocess_fail_run(self) -> None:
        # AC 7/10：FAIL_RUN -> FAILED，通知不再增加。
        with tempfile.TemporaryDirectory() as tmp:
            db, journal, model_journal, run_id = await self._crash_and_get_run(tmp)
            proc = run_worker(
                db, journal, model_journal, "resolve", run_id, "FAIL_RUN"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(parse_line(proc.stdout, "STATUS"), "FAILED")
            self.assertEqual(count_journal(journal), 1)
            self._assert_terminated_without_tool_evidence(
                parse_json_line(proc.stdout, "EVIDENCE"), "FAILED"
            )

    async def test_subprocess_cancel_run(self) -> None:
        # AC 7/10：CANCEL_RUN -> CANCELLED，通知不再增加。
        with tempfile.TemporaryDirectory() as tmp:
            db, journal, model_journal, run_id = await self._crash_and_get_run(tmp)
            proc = run_worker(
                db, journal, model_journal, "resolve", run_id, "CANCEL_RUN"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(parse_line(proc.stdout, "STATUS"), "CANCELLED")
            self.assertEqual(count_journal(journal), 1)
            self._assert_terminated_without_tool_evidence(
                parse_json_line(proc.stdout, "EVIDENCE"), "CANCELLED"
            )


if __name__ == "__main__":
    unittest.main()
