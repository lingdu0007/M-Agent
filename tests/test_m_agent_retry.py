"""Ticket 06 主行为测试：结构化失败分类与有界 Retry Policy 恢复。

验收要求（.scratch/durable-run/issues/06-bounded-retry.md）：

- Model / Tool Adapter 失败契约可报告 TRANSIENT / PERMANENT /
  UNCERTAIN，分类来自结构化异常而非解析异常消息（AC 1）；
- 失败分类、错误标识与时间证据保留在失败 Step Attempt（AC 2）；
- TRANSIENT + 适用 Retry Policy -> 新 Step Attempt，恢复后完成 Run
  （AC 3）；
- 重试在策略显式上限停止，产生确定性终态 / WAITING（AC 4）；
- 无 Retry Policy -> 同一瞬时失败只尝试一次，不自动重放（AC 5）；
- PERMANENT 即使有策略也不重试（AC 6）；
- 重试尊重 Tool Effect，UNCERTAIN NON_IDEMPOTENT Tool Step 绝不
  自动重放（AC 7）；
- Retry Policy 来自冻结的 Definition Snapshot，运行中修改定义不
  影响已有 Run（AC 8）；
- 所有断言通过公开 Runner API（inspect_run / get_run）检查 attempt
  identifiers 与 stored outcomes，不断言私有循环细节（AC 9）。

不在本 Ticket 实现四种应用 resolution；UNCERTAIN NON_IDEMPOTENT
路径只安全停住（WAITING + 机器可读 reason），为 Ticket 07 留状态。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from m_agent.runtime import (
    REASON_UNCERTAIN_NON_IDEMPOTENT,
    AgentDefinition,
    CrashPoint,
    DefinitionRegistry,
    FailureClassification,
    ModelCapabilities,
    ModelFailure,
    ModelRequest,
    ModelResponse,
    RetryPolicy,
    Runner,
    RunRecord,
    RunStatus,
    StepStatus,
    StepType,
    ToolCall,
    ToolEffect,
    ToolFailure,
    ToolOutcome,
    ToolRequest,
)
from m_agent.adapters import (
    DeterministicModelAdapter,
    DeterministicTool,
    FakeClock,
    InMemoryRunStore,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
    Runner,
    RunRecord,
    RunStatus,
)
from m_agent.runtime import ModelRequirements, ToolCallingMode

TOOL_CALLING = ModelCapabilities(tool_calling=ToolCallingMode.NATIVE)


# -- fake 模型（确定性，结构化失败分类） --------------------------------


class TransientThenSuccessModel(DeterministicModelAdapter):
    """前 ``transient_failures`` 次调用抛 TRANSIENT，之后成功。"""

    def __init__(
        self, transient_failures: int = 1, *, code: str = "rate_limited"
    ) -> None:
        super().__init__(capabilities=TOOL_CALLING)
        self._transient_failures = transient_failures
        self._code = code
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        if self.call_count <= self._transient_failures:
            raise ModelFailure(
                FailureClassification.TRANSIENT,
                self._code,
                f"transient failure #{self.call_count}",
            )
        return ModelResponse(content=f"final-after-{self.call_count}")


class AlwaysTransientModel(DeterministicModelAdapter):
    """每次调用都抛 TRANSIENT（用于重试耗尽）。"""

    def __init__(self) -> None:
        super().__init__(capabilities=TOOL_CALLING)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        raise ModelFailure(
            FailureClassification.TRANSIENT,
            "upstream_unavailable",
            "always transient",
        )


class AlwaysPermanentModel(DeterministicModelAdapter):
    """每次调用都抛 PERMANENT（有策略也不重试）。"""

    def __init__(self) -> None:
        super().__init__(capabilities=TOOL_CALLING)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        raise ModelFailure(
            FailureClassification.PERMANENT,
            "invalid_request",
            "request is permanently invalid",
        )


class UncertainOnceModel(DeterministicModelAdapter):
    """第一次抛 UNCERTAIN，第二次成功（模型无外部副作用可重试）。"""

    def __init__(self) -> None:
        super().__init__(capabilities=TOOL_CALLING)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        if self.call_count == 1:
            raise ModelFailure(
                FailureClassification.UNCERTAIN,
                "response_lost",
                "cannot confirm whether the request was processed",
            )
        return ModelResponse(content="recovered-after-uncertain")


class BareExceptionModel(DeterministicModelAdapter):
    """抛裸异常（未结构化分类）：fail-closed 为 PERMANENT 不重试。"""

    def __init__(self) -> None:
        super().__init__(capabilities=TOOL_CALLING)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        raise RuntimeError("provider exploded")


class RequestToolModel(DeterministicModelAdapter):
    """第一次请求工具（名字可配置）；之后带 outcome 返回最终内容。"""

    def __init__(self, tool_name: str = "retryable_tool") -> None:
        super().__init__(capabilities=TOOL_CALLING)
        self.requests: list[ModelRequest] = []
        self._tool_name = tool_name

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        if not request.tool_outcomes:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="r1",
                        tool_name=self._tool_name,
                        arguments="{}",
                    ),
                )
            )
        return ModelResponse(
            content=f"done with {len(request.tool_outcomes)} outcome(s)"
        )


class RequestUncertainToolModel(DeterministicModelAdapter):
    """第一次请求工具 uncertain_tool；不应有任何后续模型调用。"""

    def __init__(self) -> None:
        super().__init__(capabilities=TOOL_CALLING)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        return ModelResponse(
            tool_calls=(
                ToolCall(call_id="u1", tool_name="uncertain_tool", arguments="{}"),
            )
        )


# -- fake 工具（确定性，结构化失败分类） --------------------------------


class RetryableTool(DeterministicTool):
    """前 ``transient_failures`` 次抛 ToolFailure(TRANSIENT)，之后成功。

    ``effect`` 可配置以验证 Tool Effect 对重试决策的影响。
    """

    def __init__(
        self,
        transient_failures: int = 1,
        *,
        effect: ToolEffect = ToolEffect.READ_ONLY,
    ) -> None:
        super().__init__(name="retryable_tool", effect=effect)
        self._transient_failures = transient_failures
        self.call_count = 0

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        self.call_count += 1
        if self.call_count <= self._transient_failures:
            raise ToolFailure(
                FailureClassification.TRANSIENT,
                "upstream_down",
                f"tool transient #{self.call_count}",
            )
        return ToolOutcome.success(request.call_id, self.name, result="order-42")


class AlwaysFailingTool(DeterministicTool):
    """每次调用都抛 ToolFailure(TRANSIENT)（工具重试耗尽）。"""

    def __init__(self) -> None:
        super().__init__(name="retryable_tool", effect=ToolEffect.READ_ONLY)
        self.call_count = 0

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        self.call_count += 1
        raise ToolFailure(
            FailureClassification.TRANSIENT,
            "upstream_down",
            "always failing",
        )


class UncertainTool(DeterministicTool):
    """抛 ToolFailure(UNCERTAIN)；``succeed_after`` 次后可转成功。

    ``effect`` 可配置以验证 Tool Effect 对 UNCERTAIN 重试决策的影响
    （NON_IDEMPOTENT 禁止自动重放，READ_ONLY/IDEMPOTENT 允许）。
    """

    def __init__(
        self,
        *,
        effect: ToolEffect = ToolEffect.NON_IDEMPOTENT,
        succeed_after: int | None = None,
    ) -> None:
        super().__init__(name="uncertain_tool", effect=effect)
        self.call_count = 0
        self._succeed_after = succeed_after

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        self.call_count += 1
        if self._succeed_after is not None and self.call_count > self._succeed_after:
            return ToolOutcome.success(request.call_id, self.name, result="order-42")
        raise ToolFailure(
            FailureClassification.UNCERTAIN,
            "effect_unconfirmed",
            "cannot confirm whether the side effect occurred",
        )


# -- 公共构造 -----------------------------------------------------------


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
                capabilities=(TOOL_CALLING if tools else ModelCapabilities())
            ),
            model_adapter=model,
            tools=tools,
            retry_policy=retry_policy,
        )
    )
    if store is None:
        store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
    return Runner(registry=registry, store=store), registry, store


def tool_attempts(inspection):
    """按工具 Step 过滤全部 Attempt（公开 inspect 结果）。"""
    tool_step_ids = {
        s.step_id for s in inspection.steps if s.step_type is StepType.TOOL
    }
    return [a for a in inspection.attempts if a.step_id in tool_step_ids]


class ModelRetryTests(unittest.IsolatedAsyncioTestCase):
    """AC 1/2/3/4/5/6/8：模型失败分类与重试。"""

    async def test_transient_failure_recovers_with_new_attempts(self) -> None:
        # AC 3：TRANSIENT 失败 + 适用策略 -> 每次重试都是新 Step Attempt，
        # 恢复后 Run 成功完成；分类 / 错误标识 / 时间证据保留在失败 Attempt。
        model = TransientThenSuccessModel(transient_failures=2)
        runner, _, _ = make_runner(model, retry_policy=RetryPolicy(max_attempts=3))
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(terminal.output, "final-after-3")
        inspection = await runner.inspect_run(created.run_id)

        model_steps = [s for s in inspection.steps if s.step_type is StepType.MODEL]
        self.assertEqual(len(model_steps), 1)  # 同一 Model Step，多次 Attempt
        attempts = [
            a for a in inspection.attempts if a.step_id == model_steps[0].step_id
        ]
        # AC 2/9：3 个 attempt（2 失败 + 1 成功），id 互异，证据完整。
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len({a.attempt_id for a in attempts}), 3)
        self.assertEqual(
            [a.status for a in attempts],
            [StepStatus.FAILED, StepStatus.FAILED, StepStatus.SUCCEEDED],
        )
        for failed in attempts[:2]:
            self.assertEqual(failed.classification, FailureClassification.TRANSIENT)
            self.assertEqual(failed.error_code, "rate_limited")
            self.assertNotIn("transient failure", failed.error or "")
            self.assertIsNotNone(failed.created_at)
        self.assertIsNotNone(attempts[0].created_at)
        self.assertIsNotNone(attempts[1].created_at)

    async def test_retry_stops_at_explicit_bound(self) -> None:
        # AC 4：策略显式上限 max_attempts=3 -> 恰好 3 次尝试后停止，
        # 产生确定性终态 FAILED（模型调用 3 次，最后一次不再重试）。
        model = AlwaysTransientModel()
        runner, _, _ = make_runner(model, retry_policy=RetryPolicy(max_attempts=3))
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.FAILED)
        self.assertTrue(terminal.status.is_terminal)
        self.assertEqual(model.call_count, 3)
        inspection = await runner.inspect_run(created.run_id)
        attempts = [a for a in inspection.attempts if a.status is StepStatus.FAILED]
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len({a.attempt_id for a in attempts}), 3)

    async def test_no_policy_means_single_attempt(self) -> None:
        # AC 5：无 Retry Policy -> 同一瞬时失败只尝试一次，不自动重放。
        model = TransientThenSuccessModel(transient_failures=1)
        runner, _, _ = make_runner(model)  # 无策略
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.FAILED)
        self.assertEqual(model.call_count, 1)  # 绝不重放
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(len(inspection.attempts), 1)
        self.assertEqual(inspection.attempts[0].status, StepStatus.FAILED)

    async def test_permanent_failure_is_never_retried(self) -> None:
        # AC 6：PERMANENT 即使存在策略也不重试（1 次尝试即终态 FAILED）。
        model = AlwaysPermanentModel()
        runner, _, _ = make_runner(model, retry_policy=RetryPolicy(max_attempts=5))
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.FAILED)
        self.assertEqual(model.call_count, 1)
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(len(inspection.attempts), 1)
        self.assertEqual(
            inspection.attempts[0].classification,
            FailureClassification.PERMANENT,
        )
        self.assertEqual(inspection.attempts[0].error_code, "invalid_request")

    async def test_unclassified_bare_exception_fails_closed(self) -> None:
        # AC 1 fail-closed：裸异常未携带结构化分类，绝不解析异常消息，
        # 默认按 PERMANENT 处理——即使存在策略也不自动重试。
        model = BareExceptionModel()
        runner, _, _ = make_runner(model, retry_policy=RetryPolicy(max_attempts=3))
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.FAILED)
        self.assertEqual(model.call_count, 1)
        inspection = await runner.inspect_run(created.run_id)
        attempt = inspection.attempts[0]
        self.assertEqual(attempt.classification, FailureClassification.PERMANENT)
        self.assertEqual(attempt.error_code, "UNCLASSIFIED_FAILURE")
        self.assertEqual(attempt.error, "unclassified adapter exception: RuntimeError")

    async def test_uncertain_model_failure_is_not_automatically_retried(self) -> None:
        # 即使模型没有工具副作用，UNCERTAIN 也不是 TRANSIENT 的同义词；
        # 只有适配器明确分类为 TRANSIENT 才可自动重试。
        model = UncertainOnceModel()
        runner, _, _ = make_runner(model, retry_policy=RetryPolicy(max_attempts=2))
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.FAILED)
        self.assertEqual(model.call_count, 1)
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(len(inspection.attempts), 1)
        self.assertEqual(
            inspection.attempts[0].classification,
            FailureClassification.UNCERTAIN,
        )

    async def test_retry_policy_frozen_in_snapshot(self) -> None:
        # AC 8：Retry Policy 来自 Run 启动时冻结的 Definition Snapshot。
        # 运行中注册新版本（修改定义）不能改变已有 Run 的重试行为。
        model = TransientThenSuccessModel(transient_failures=2)
        runner, registry, _ = make_runner(
            model, retry_policy=RetryPolicy(max_attempts=3)
        )
        created = await runner.create_run("assistant", "1.0", input="hi")

        # "运行中修改 Agent Definition"：注册同 id 新版本，策略改为 1 次。
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="assistant",
                version="2.0",
                instructions="changed",
                model_adapter=DeterministicModelAdapter(responses=("x",)),
                retry_policy=RetryPolicy(max_attempts=1),
            )
        )
        terminal = await runner.start_run(created.run_id)

        # Run 仍按冻结的 v1 策略（max_attempts=3）执行：3 次尝试后成功。
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(terminal.snapshot.retry_policy.max_attempts, 3)
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(len(inspection.attempts), 3)
        # 快照不可变：再次检查策略未被改写。
        self.assertEqual(
            (await runner.get_run(created.run_id)).snapshot.retry_policy,
            RetryPolicy(max_attempts=3),
        )


class ToolRetryTests(unittest.IsolatedAsyncioTestCase):
    """AC 3/4/7：工具重试与 Tool Effect 约束。"""

    async def test_tool_transient_failure_recovers(self) -> None:
        # 工具 READ_ONLY + TRANSIENT：2 次失败后第 3 次成功，同一 Tool
        # Step 保留 3 个 Attempt；模型只调用 2 次（请求 + 带 outcome 收尾）。
        tool = RetryableTool(transient_failures=2)
        model = RequestToolModel()
        runner, _, _ = make_runner(
            model, tools=(tool,), retry_policy=RetryPolicy(max_attempts=3)
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(tool.call_count, 3)
        self.assertEqual(model.call_count, 2)
        inspection = await runner.inspect_run(created.run_id)
        tool_steps = [s for s in inspection.steps if s.step_type is StepType.TOOL]
        self.assertEqual(len(tool_steps), 1)
        attempts = [
            a for a in inspection.attempts if a.step_id == tool_steps[0].step_id
        ]
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len({a.attempt_id for a in attempts}), 3)
        self.assertEqual(
            [a.status for a in attempts],
            [StepStatus.FAILED, StepStatus.FAILED, StepStatus.SUCCEEDED],
        )
        self.assertEqual(attempts[0].classification, FailureClassification.TRANSIENT)
        self.assertEqual(attempts[0].error_code, "upstream_down")
        # 最终模型响应确实收到了工具 outcome（公开数据断言）。
        self.assertEqual(terminal.output, "done with 1 outcome(s)")

    async def test_tool_retry_exhaustion_fails_run(self) -> None:
        # 工具 TRANSIENT 重试在 max_attempts 上限停止 -> 确定性 FAILED。
        tool = AlwaysFailingTool()
        model = RequestToolModel()
        runner, _, _ = make_runner(
            model, tools=(tool,), retry_policy=RetryPolicy(max_attempts=2)
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.FAILED)
        self.assertEqual(tool.call_count, 2)
        self.assertEqual(model.call_count, 1)  # 失败后不再回到模型
        inspection = await runner.inspect_run(created.run_id)
        failed = [a for a in inspection.attempts if a.status is StepStatus.FAILED]
        self.assertEqual(len(failed), 2)

    async def test_uncertain_non_idempotent_never_replayed(self) -> None:
        # AC 7：UNCERTAIN + NON_IDEMPOTENT Tool Step 绝不自动重放——
        # 即使配置了策略也进入 WAITING，工具只调用一次，等待应用处置
        # （Ticket 07）；无自动重放 = 无重复外部副作用。
        tool = UncertainTool(effect=ToolEffect.NON_IDEMPOTENT)
        model = RequestUncertainToolModel()
        runner, _, _ = make_runner(
            model, tools=(tool,), retry_policy=RetryPolicy(max_attempts=5)
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.WAITING)
        self.assertFalse(terminal.status.is_terminal)
        self.assertEqual(terminal.waiting_reason, REASON_UNCERTAIN_NON_IDEMPOTENT)
        self.assertEqual(tool.call_count, 1)  # 无自动重放证据
        self.assertEqual(model.call_count, 1)  # 无后续模型调用
        inspection = await runner.inspect_run(created.run_id)
        # WAITING 时 Step 未完成（无 StepRecord，Ticket 07 处置后补齐），
        # 失败证据直接来自 Attempt 记录。
        failed = [a for a in inspection.attempts if a.status is StepStatus.FAILED]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].classification, FailureClassification.UNCERTAIN)
        self.assertEqual(failed[0].error_code, "effect_unconfirmed")
        # 无 TOOL checkpoint：失败未落盘为 outcome。
        self.assertEqual(
            [c for c in inspection.checkpoints if c.step_type is StepType.TOOL],
            [],
        )

    async def test_transient_non_idempotent_can_retry(self) -> None:
        # AC 7 边界：只有 UNCERTAIN + NON_IDEMPOTENT 禁止自动重放；
        # TRANSIENT + NON_IDEMPOTENT（瞬时失败，副作用未发生）允许重试。
        tool = RetryableTool(transient_failures=1, effect=ToolEffect.NON_IDEMPOTENT)
        model = RequestToolModel()
        runner, _, _ = make_runner(
            model, tools=(tool,), retry_policy=RetryPolicy(max_attempts=3)
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(tool.call_count, 2)
        inspection = await runner.inspect_run(created.run_id)
        tool_steps = [s for s in inspection.steps if s.step_type is StepType.TOOL]
        attempts = [
            a for a in inspection.attempts if a.step_id == tool_steps[0].step_id
        ]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            [a.status for a in attempts],
            [StepStatus.FAILED, StepStatus.SUCCEEDED],
        )

    async def test_uncertain_read_only_is_not_automatically_retried(self) -> None:
        # AC 7 边界：UNCERTAIN 不会因为当前 effect 是 READ_ONLY 就被当成
        # TRANSIENT 自动重试；非幂等的不确定结果另走 WAITING。
        tool = UncertainTool(effect=ToolEffect.READ_ONLY, succeed_after=1)
        model = RequestToolModel(tool_name="uncertain_tool")
        runner, _, _ = make_runner(
            model, tools=(tool,), retry_policy=RetryPolicy(max_attempts=2)
        )
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status, RunStatus.FAILED)
        self.assertEqual(tool.call_count, 1)
        inspection = await runner.inspect_run(created.run_id)
        failed = [a for a in inspection.attempts if a.status is StepStatus.FAILED]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].classification, FailureClassification.UNCERTAIN)

    async def test_missing_or_ambiguous_frozen_tool_effect_fails_closed(self) -> None:
        """恢复不能由当前 Tool callable 猜测缺失快照的 effect。"""
        for declarations in ((), "duplicate"):
            with self.subTest(declarations=declarations):
                model = RequestToolModel()
                tool = RetryableTool(
                    transient_failures=0,
                    effect=ToolEffect.READ_ONLY,
                )
                runner, registry, store = make_runner(model, tools=(tool,))
                definition = registry.resolve("assistant", "1.0")
                snapshot = definition.frozen_snapshot()
                frozen_declarations = (
                    () if declarations == () else snapshot.tool_declarations * 2
                )
                await store.create_run(
                    RunRecord(
                        run_id=f"missing-effect-{declarations}",
                        definition_id="assistant",
                        definition_version="1.0",
                        input="hi",
                        status=RunStatus.RUNNING,
                        snapshot=snapshot.model_copy(
                            update={"tool_declarations": frozen_declarations}
                        ),
                    )
                )

                terminal = await runner.resume_run(f"missing-effect-{declarations}")

                self.assertEqual(terminal.status, RunStatus.FAILED)
                self.assertEqual(model.call_count, 1)
                self.assertEqual(tool.call_count, 0)
                inspection = await runner.inspect_run(terminal.run_id)
                failed_tool_attempt = next(
                    attempt
                    for attempt in inspection.attempts
                    if attempt.step_id
                    in {
                        step.step_id
                        for step in inspection.steps
                        if step.step_type is StepType.TOOL
                    }
                )
                self.assertEqual(
                    failed_tool_attempt.error_code,
                    "FROZEN_TOOL_DECLARATION_UNAVAILABLE",
                )


class RetryWithSQLiteTests(unittest.IsolatedAsyncioTestCase):
    """同一重试语义在 SQLiteRunStore 上成立（持久化分类与证据）。"""

    def _sqlite_store(self, tmp: str) -> SQLiteRunStore:
        return SQLiteRunStore(
            os.path.join(tmp, "retry.db"),
            payload_codec=PlaintextPayloadCodec(),
        )

    async def _interrupt_after_failed_model_attempt(
        self,
        db: str,
        retry_policy: RetryPolicy | None,
    ) -> tuple[str, str, datetime]:
        """经公开 Runner 入口留下一个失败但尚未 retry 的 Model Step。"""
        clock = FakeClock()

        def hook(point: CrashPoint, run_id: str) -> None:
            if point is CrashPoint.AFTER_ATTEMPT_FAILED:
                raise RuntimeError("interrupt after failed model attempt")

        store = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec(), clock=clock)
        model = TransientThenSuccessModel(transient_failures=1)
        _, registry, _ = make_runner(
            model,
            retry_policy=retry_policy,
            store=store,
        )
        runner = Runner(registry=registry, store=store, crash_hook=hook)
        created = await runner.create_run("assistant", "1.0", input="hi")
        with self.assertRaisesRegex(RuntimeError, "failed model attempt"):
            await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)
        step = next(
            item for item in inspection.steps if item.step_type is StepType.MODEL
        )
        run = await runner.get_run(created.run_id)
        assert run.lease_expires_at is not None
        self.assertEqual(model.call_count, 1)
        self.assertEqual(
            [attempt.status for attempt in inspection.attempts],
            [StepStatus.FAILED],
        )
        store.close()
        return created.run_id, step.step_id, run.lease_expires_at

    async def test_transient_recovery_persisted_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._sqlite_store(tmp)
            model = TransientThenSuccessModel(transient_failures=1)
            runner, _, _ = make_runner(
                model, retry_policy=RetryPolicy(max_attempts=2), store=store
            )
            created = await runner.create_run("assistant", "1.0", input="hi")
            terminal = await runner.start_run(created.run_id)
            self.assertEqual(terminal.status, RunStatus.SUCCEEDED)

            inspection = await runner.inspect_run(created.run_id)
            self.assertEqual(len(inspection.attempts), 2)
            self.assertEqual(
                inspection.attempts[0].classification,
                FailureClassification.TRANSIENT,
            )
            self.assertEqual(inspection.attempts[0].error_code, "rate_limited")
            self.assertEqual(inspection.attempts[1].status, StepStatus.SUCCEEDED)
            store.close()

    async def test_tool_recovery_does_not_exceed_frozen_budget_after_dispatch_crash(
        self,
    ) -> None:
        """未 checkpoint 的 READ_ONLY dispatch 也计入冻结的总预算。"""
        for policy in (None, RetryPolicy(max_attempts=1)):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as tmp:
                db = os.path.join(tmp, "retry.db")
                clock = FakeClock()

                def hook(point: CrashPoint, run_id: str) -> None:
                    if point is CrashPoint.BEFORE_TOOL_CHECKPOINT:
                        raise RuntimeError("interrupt after tool dispatch")

                first_store = SQLiteRunStore(
                    db,
                    payload_codec=PlaintextPayloadCodec(),
                    clock=clock,
                )
                first_tool = RetryableTool(
                    transient_failures=0,
                    effect=ToolEffect.READ_ONLY,
                )
                _, first_registry, _ = make_runner(
                    RequestToolModel(),
                    tools=(first_tool,),
                    retry_policy=policy,
                    store=first_store,
                )
                first_runner = Runner(
                    registry=first_registry,
                    store=first_store,
                    crash_hook=hook,
                )
                created = await first_runner.create_run("assistant", "1.0", input="hi")
                with self.assertRaisesRegex(RuntimeError, "tool dispatch"):
                    await first_runner.start_run(created.run_id)

                interrupted = await first_runner.inspect_run(created.run_id)
                tool_step = next(
                    step
                    for step in interrupted.steps
                    if step.step_type is StepType.TOOL
                )
                attempts_before_restart = [
                    attempt
                    for attempt in interrupted.attempts
                    if attempt.step_id == tool_step.step_id
                ]
                self.assertEqual(len(attempts_before_restart), 1)
                self.assertEqual(attempts_before_restart[0].status, StepStatus.RUNNING)
                self.assertEqual(first_tool.call_count, 1)
                interrupted_run = await first_runner.get_run(created.run_id)
                assert interrupted_run.lease_expires_at is not None
                first_store.close()

                recovered_store = SQLiteRunStore(
                    db,
                    payload_codec=PlaintextPayloadCodec(),
                    clock=FakeClock(
                        start=(interrupted_run.lease_expires_at + timedelta(seconds=1))
                    ),
                )
                try:
                    recovered_tool = RetryableTool(
                        transient_failures=0,
                        effect=ToolEffect.READ_ONLY,
                    )
                    recovered_runner, _, _ = make_runner(
                        RequestToolModel(),
                        tools=(recovered_tool,),
                        # 恢复进程的宽松策略不能改变原 Run 的预算。
                        retry_policy=RetryPolicy(max_attempts=3),
                        store=recovered_store,
                    )
                    terminal = await recovered_runner.resume_run(created.run_id)

                    self.assertEqual(terminal.status, RunStatus.FAILED)
                    self.assertEqual(recovered_tool.call_count, 0)
                    recovered = await recovered_runner.inspect_run(created.run_id)
                    attempts = [
                        attempt
                        for attempt in recovered.attempts
                        if attempt.step_id == tool_step.step_id
                    ]
                    self.assertEqual(
                        [attempt.attempt_id for attempt in attempts],
                        [attempts_before_restart[0].attempt_id],
                    )
                    self.assertEqual(
                        [attempt.status for attempt in attempts],
                        [StepStatus.FAILED],
                    )
                    self.assertEqual(
                        attempts[0].classification,
                        FailureClassification.UNCERTAIN,
                    )
                    self.assertEqual(
                        [
                            checkpoint
                            for checkpoint in recovered.checkpoints
                            if checkpoint.step_type is StepType.TOOL
                        ],
                        [],
                    )
                finally:
                    recovered_store.close()

    async def test_uncertain_waiting_persisted_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._sqlite_store(tmp)
            tool = UncertainTool(effect=ToolEffect.NON_IDEMPOTENT)
            model = RequestUncertainToolModel()
            runner, _, _ = make_runner(
                model,
                tools=(tool,),
                retry_policy=RetryPolicy(max_attempts=5),
                store=store,
            )
            created = await runner.create_run("assistant", "1.0", input="hi")
            terminal = await runner.start_run(created.run_id)
            self.assertEqual(terminal.status, RunStatus.WAITING)
            self.assertEqual(terminal.waiting_reason, REASON_UNCERTAIN_NON_IDEMPOTENT)
            self.assertEqual(tool.call_count, 1)

            inspection = await runner.inspect_run(created.run_id)
            failed = [a for a in inspection.attempts if a.status is StepStatus.FAILED]
            self.assertEqual(len(failed), 1)
            self.assertEqual(
                failed[0].classification,
                FailureClassification.UNCERTAIN,
            )
            self.assertEqual(failed[0].error_code, "effect_unconfirmed")
            store.close()

    async def test_model_resume_uses_persisted_attempt_budget_and_step(self) -> None:
        """一次失败落盘后中断，恢复只能使用同一 Model Step 的余量。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "retry.db")
            clock = FakeClock()

            def hook(point: CrashPoint, run_id: str) -> None:
                if point is CrashPoint.AFTER_ATTEMPT_FAILED:
                    raise RuntimeError("interrupt after failed model attempt")

            first_store = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            first_model = TransientThenSuccessModel(transient_failures=1)
            _, first_registry, _ = make_runner(
                first_model,
                retry_policy=RetryPolicy(max_attempts=2),
                store=first_store,
            )
            first_runner = Runner(
                registry=first_registry,
                store=first_store,
                crash_hook=hook,
            )
            created = await first_runner.create_run("assistant", "1.0", input="hi")
            with self.assertRaisesRegex(RuntimeError, "failed model attempt"):
                await first_runner.start_run(created.run_id)

            interrupted = await first_runner.inspect_run(created.run_id)
            model_steps = [
                step for step in interrupted.steps if step.step_type is StepType.MODEL
            ]
            self.assertEqual(len(model_steps), 1)
            interrupted_attempts = [
                attempt
                for attempt in interrupted.attempts
                if attempt.step_id == model_steps[0].step_id
            ]
            self.assertEqual(len(interrupted_attempts), 1)
            self.assertEqual(interrupted_attempts[0].status, StepStatus.FAILED)
            self.assertEqual(
                interrupted_attempts[0].classification,
                FailureClassification.TRANSIENT,
            )
            self.assertEqual(first_model.call_count, 1)
            interrupted_run = await first_runner.get_run(created.run_id)
            assert interrupted_run.lease_expires_at is not None
            first_store.close()

            recovered_store = SQLiteRunStore(
                db,
                payload_codec=PlaintextPayloadCodec(),
                clock=FakeClock(
                    start=interrupted_run.lease_expires_at + timedelta(seconds=1)
                ),
            )
            try:
                recovered_model = TransientThenSuccessModel(transient_failures=1)
                # The fake's configured behavior is frozen; only its
                # test-observation counter reflects the prior process call.
                recovered_model.call_count = 1
                recovered_runner, _, _ = make_runner(
                    recovered_model,
                    retry_policy=RetryPolicy(max_attempts=2),
                    store=recovered_store,
                )
                terminal = await recovered_runner.resume_run(created.run_id)

                self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
                self.assertEqual(len(recovered_model.requests), 1)
                recovered = await recovered_runner.inspect_run(created.run_id)
                recovered_steps = [
                    step for step in recovered.steps if step.step_type is StepType.MODEL
                ]
                self.assertEqual(
                    [step.step_id for step in recovered_steps],
                    [model_steps[0].step_id],
                )
                attempts = [
                    attempt
                    for attempt in recovered.attempts
                    if attempt.step_id == model_steps[0].step_id
                ]
                self.assertEqual(
                    [attempt.status for attempt in attempts],
                    [StepStatus.FAILED, StepStatus.SUCCEEDED],
                )
                self.assertEqual(len({attempt.attempt_id for attempt in attempts}), 2)
                self.assertEqual(
                    [checkpoint.step_id for checkpoint in recovered.checkpoints],
                    [model_steps[0].step_id],
                )
            finally:
                recovered_store.close()

    async def test_tool_resume_uses_frozen_non_idempotent_effect(self) -> None:
        """恢复时当前 Tool 漂移为 READ_ONLY 也不能重放原副作用。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "retry.db")
            clock = FakeClock()

            def hook(point: CrashPoint, run_id: str) -> None:
                if point is CrashPoint.AFTER_ATTEMPT_FAILED:
                    raise RuntimeError("interrupt after failed tool attempt")

            first_store = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            first_tool = RetryableTool(
                transient_failures=1,
                effect=ToolEffect.NON_IDEMPOTENT,
            )
            _, first_registry, _ = make_runner(
                RequestToolModel(),
                tools=(first_tool,),
                retry_policy=RetryPolicy(max_attempts=3),
                store=first_store,
            )
            first_runner = Runner(
                registry=first_registry,
                store=first_store,
                crash_hook=hook,
            )
            created = await first_runner.create_run("assistant", "1.0", input="hi")
            with self.assertRaisesRegex(RuntimeError, "failed tool attempt"):
                await first_runner.start_run(created.run_id)

            interrupted = await first_runner.inspect_run(created.run_id)
            tool_step = next(
                step for step in interrupted.steps if step.step_type is StepType.TOOL
            )
            attempts_before_restart = [
                attempt
                for attempt in interrupted.attempts
                if attempt.step_id == tool_step.step_id
            ]
            self.assertEqual(
                [attempt.status for attempt in attempts_before_restart],
                [StepStatus.FAILED],
            )
            self.assertEqual(first_tool.call_count, 1)
            interrupted_run = await first_runner.get_run(created.run_id)
            assert interrupted_run.lease_expires_at is not None
            first_store.close()

            recovered_store = SQLiteRunStore(
                db,
                payload_codec=PlaintextPayloadCodec(),
                clock=FakeClock(
                    start=interrupted_run.lease_expires_at + timedelta(seconds=1)
                ),
            )
            try:
                # 同一 definition_id/version 的新进程实现发生 effect 漂移。
                recovered_tool = RetryableTool(
                    transient_failures=0,
                    effect=ToolEffect.READ_ONLY,
                )
                recovered_runner, _, _ = make_runner(
                    RequestToolModel(),
                    tools=(recovered_tool,),
                    retry_policy=RetryPolicy(max_attempts=3),
                    store=recovered_store,
                )

                waiting = await recovered_runner.resume_run(created.run_id)

                self.assertEqual(waiting.status, RunStatus.WAITING)
                self.assertEqual(
                    waiting.waiting_reason,
                    REASON_UNCERTAIN_NON_IDEMPOTENT,
                )
                self.assertEqual(waiting.waiting_step_id, tool_step.step_id)
                self.assertEqual(first_tool.call_count, 1)
                self.assertEqual(recovered_tool.call_count, 0)
                recovered = await recovered_runner.inspect_run(created.run_id)
                attempts = [
                    attempt
                    for attempt in recovered.attempts
                    if attempt.step_id == tool_step.step_id
                ]
                self.assertEqual(len(attempts), 1)
                self.assertEqual(
                    attempts[0].classification,
                    FailureClassification.TRANSIENT,
                )
            finally:
                recovered_store.close()

    async def test_model_resume_without_policy_does_not_auto_replay(self) -> None:
        """冻结的无策略在重启后仍只允许已经发生的一次调用。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "retry.db")
            (
                run_id,
                step_id,
                lease_expires_at,
            ) = await self._interrupt_after_failed_model_attempt(db, None)
            store = SQLiteRunStore(
                db,
                payload_codec=PlaintextPayloadCodec(),
                clock=FakeClock(start=lease_expires_at + timedelta(seconds=1)),
            )
            try:
                recovered_model = TransientThenSuccessModel(transient_failures=1)
                runner, _, _ = make_runner(
                    recovered_model,
                    # 第二进程试图放宽策略也不能影响原 Run。
                    retry_policy=RetryPolicy(max_attempts=3),
                    store=store,
                )
                terminal = await runner.resume_run(run_id)

                self.assertEqual(terminal.status, RunStatus.FAILED)
                self.assertEqual(recovered_model.call_count, 0)
                inspection = await runner.inspect_run(run_id)
                self.assertEqual([step.step_id for step in inspection.steps], [step_id])
                self.assertEqual(len(inspection.attempts), 1)
                self.assertEqual(inspection.checkpoints, [])
            finally:
                store.close()

    async def test_model_resume_ignores_stricter_registered_policy(self) -> None:
        """冻结的两次预算不会被恢复进程的单次策略收紧。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "retry.db")
            (
                run_id,
                step_id,
                lease_expires_at,
            ) = await self._interrupt_after_failed_model_attempt(
                db, RetryPolicy(max_attempts=2)
            )
            store = SQLiteRunStore(
                db,
                payload_codec=PlaintextPayloadCodec(),
                clock=FakeClock(start=lease_expires_at + timedelta(seconds=1)),
            )
            try:
                recovered_model = TransientThenSuccessModel(transient_failures=1)
                # Preserve the fake's first-process observation without
                # changing its frozen behavior configuration.
                recovered_model.call_count = 1
                runner, _, _ = make_runner(
                    recovered_model,
                    retry_policy=RetryPolicy(max_attempts=1),
                    store=store,
                )
                terminal = await runner.resume_run(run_id)

                self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
                self.assertEqual(len(recovered_model.requests), 1)
                inspection = await runner.inspect_run(run_id)
                attempts = [
                    attempt
                    for attempt in inspection.attempts
                    if attempt.step_id == step_id
                ]
                self.assertEqual(len(attempts), 2)
                self.assertEqual(
                    [attempt.status for attempt in attempts],
                    [StepStatus.FAILED, StepStatus.SUCCEEDED],
                )
            finally:
                store.close()

    async def test_read_only_tool_resume_uses_persisted_budget_and_step(self) -> None:
        """SQLite 重开后，READ_ONLY Tool 用同一 Step 的剩余预算恢复。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "retry.db")
            clock = FakeClock()

            def hook(point: CrashPoint, run_id: str) -> None:
                if point is CrashPoint.AFTER_ATTEMPT_FAILED:
                    raise RuntimeError("interrupt after failed tool attempt")

            first_store = SQLiteRunStore(
                db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            first_tool = RetryableTool(
                transient_failures=1,
                effect=ToolEffect.READ_ONLY,
            )
            _, first_registry, _ = make_runner(
                RequestToolModel(),
                tools=(first_tool,),
                retry_policy=RetryPolicy(max_attempts=2),
                store=first_store,
            )
            first_runner = Runner(
                registry=first_registry,
                store=first_store,
                crash_hook=hook,
            )
            created = await first_runner.create_run("assistant", "1.0", input="hi")
            with self.assertRaisesRegex(RuntimeError, "failed tool attempt"):
                await first_runner.start_run(created.run_id)

            interrupted = await first_runner.inspect_run(created.run_id)
            tool_step = next(
                step for step in interrupted.steps if step.step_type is StepType.TOOL
            )
            first_attempt = next(
                attempt
                for attempt in interrupted.attempts
                if attempt.step_id == tool_step.step_id
            )
            self.assertEqual(first_attempt.status, StepStatus.FAILED)
            self.assertEqual(
                first_attempt.classification, FailureClassification.TRANSIENT
            )
            self.assertEqual(first_tool.call_count, 1)
            interrupted_run = await first_runner.get_run(created.run_id)
            assert interrupted_run.lease_expires_at is not None
            first_store.close()

            recovered_store = SQLiteRunStore(
                db,
                payload_codec=PlaintextPayloadCodec(),
                clock=FakeClock(
                    start=interrupted_run.lease_expires_at + timedelta(seconds=1)
                ),
            )
            try:
                recovered_tool = RetryableTool(
                    transient_failures=0,
                    effect=ToolEffect.READ_ONLY,
                )
                recovered_runner, _, _ = make_runner(
                    RequestToolModel(),
                    tools=(recovered_tool,),
                    retry_policy=RetryPolicy(max_attempts=1),
                    store=recovered_store,
                )
                terminal = await recovered_runner.resume_run(created.run_id)

                self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
                self.assertEqual(first_tool.call_count + recovered_tool.call_count, 2)
                recovered = await recovered_runner.inspect_run(created.run_id)
                tool_steps = [
                    step for step in recovered.steps if step.step_type is StepType.TOOL
                ]
                self.assertEqual(
                    [step.step_id for step in tool_steps], [tool_step.step_id]
                )
                attempts = [
                    attempt
                    for attempt in recovered.attempts
                    if attempt.step_id == tool_step.step_id
                ]
                self.assertEqual(
                    [attempt.status for attempt in attempts],
                    [StepStatus.FAILED, StepStatus.SUCCEEDED],
                )
                self.assertEqual(len({attempt.attempt_id for attempt in attempts}), 2)
                self.assertEqual(
                    [
                        checkpoint.step_id
                        for checkpoint in recovered.checkpoints
                        if checkpoint.step_type is StepType.TOOL
                    ],
                    [tool_step.step_id],
                )
            finally:
                recovered_store.close()


if __name__ == "__main__":
    unittest.main()
