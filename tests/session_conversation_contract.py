"""SessionRunner 组合行为共享契约测试（实现无关）。

Ticket 12 AC 5-8 的组合层契约：Snapshot 一次性冻结为 Conversation
History、claim 原子占用、非成功释放、WAITING 保留与禁止越过、幂等
提交、Core Run Status 与 Session Commit Status 分离。本 mixin 只依赖
公开 seam（SessionRunner / SessionStore / Runner 公开 API），子类只
提供 ``make_session_store()``；Ticket 13 的 SQLite 实现直接复用。
"""

from __future__ import annotations

import asyncio

from m_agent.adapters import (
    DeterministicModelAdapter,
    DeterministicTool,
    InMemoryRunStore,
    PlaintextPayloadCodec,
)
from m_agent.companion import (
    SessionClaimConflictError,
    SessionCommitStatus,
    SessionNotFoundError,
    SessionRunner,
    SessionScope,
    SessionStore,
    SessionTurn,
)
from m_agent.runtime import (
    AgentDefinition,
    ConversationMessage,
    ConversationRole,
    DefinitionNotFoundError,
    DefinitionRegistry,
    FailureClassification,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelRequirements,
    PolicyAction,
    PolicyDecision,
    PolicyGate,
    ResolutionAction,
    RunNotFoundError,
    RunResolution,
    RunStatus,
    Runner,
    StaticRunPolicy,
    ToolCallingMode,
    ToolCall,
    ToolEffect,
    ToolFailure,
    ToolOutcome,
)

_SCOPE = SessionScope(token="app-scope")
_OTHER_SCOPE = SessionScope(token="other-scope")


class RecordingModelAdapter(DeterministicModelAdapter):
    """确定性模型：记录每次 ModelRequest，返回固定最终响应。"""

    deterministic: bool = True

    def __init__(self) -> None:
        super().__init__(responses=("session answer",))
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        return ModelResponse(content="session answer")


class YieldingModelAdapter(DeterministicModelAdapter):
    """确定性模型：响应前显式让出事件循环，制造确定性交错。"""

    deterministic: bool = True

    def __init__(self) -> None:
        super().__init__(responses=("session answer",))

    async def generate(self, request: ModelRequest) -> ModelResponse:
        await asyncio.sleep(0)
        self.call_count += 1
        self._last_request = request
        return ModelResponse(content="session answer")


class ExplodingModelAdapter(DeterministicModelAdapter):
    """确定性模型：总是抛异常，把 Run 推向 FAILED 终态。"""

    deterministic: bool = True

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        raise RuntimeError("provider exploded")


class ToolWaitingModelAdapter(DeterministicModelAdapter):
    """确定性模型：先请求 NON_IDEMPOTENT 工具，收到 outcome 后收尾。"""

    deterministic: bool = True

    def __init__(self) -> None:
        super().__init__(
            capabilities=ModelCapabilities(tool_calling=ToolCallingMode.NATIVE)
        )
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
        return ModelResponse(content="final: " + request.tool_outcomes[0].result)


class UncertainEffectTool(DeterministicTool):
    """NON_IDEMPOTENT 工具：效果无法确认 → Run 进入 WAITING。"""

    deterministic: bool = True

    def __init__(self) -> None:
        super().__init__(name="notify", effect=ToolEffect.NON_IDEMPOTENT)
        self.call_count = 0

    async def invoke(self, request) -> ToolOutcome:
        self.call_count += 1
        raise ToolFailure(
            FailureClassification.UNCERTAIN,
            "effect_unconfirmed",
            "delivery outcome is unknown",
        )


class FlakyCommitSessionStore:
    """装饰任意 SessionStore：``commit_turn`` 前 N 次抛异常模拟故障。

    用于验证「SessionStore 提交失败不改写 Core 终态」的 PENDING 语义；
    除 ``commit_turn`` 外全部委托给内层 store。
    """

    def __init__(self, inner: SessionStore, failures: int = 1) -> None:
        self._inner = inner
        self._failures = failures
        self.commit_calls = 0

    async def commit_turn(self, scope, session_id, turn, *, expected_version):
        self.commit_calls += 1
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("session store temporarily unavailable")
        return await self._inner.commit_turn(
            scope, session_id, turn, expected_version=expected_version
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TransientGetRunRunner:
    """装饰任意 Runner：``get_run`` 前 N 次抛瞬态异常（非
    RunNotFoundError）模拟 RunStore 故障。

    用于验证 ADR 0020：瞬态故障既未确认终态也未确认「从未创建」，
    claim 只能保留；除 ``get_run`` 外全部委托给内层 Runner。
    """

    def __init__(self, inner: Runner, failures: int = 1) -> None:
        self._inner = inner
        self._failures = failures

    async def get_run(self, run_id: str):
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("run store temporarily unavailable")
        return await self._inner.get_run(run_id)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class CountingRunStore(InMemoryRunStore):
    """计数 create_run 调用，用于断言失败路径不产生孤儿 Run。"""

    def __init__(self, payload_codec) -> None:
        super().__init__(payload_codec=payload_codec)
        self.created_runs = 0

    async def create_run(self, run):
        self.created_runs += 1
        return await super().create_run(run)


class SessionConversationHarness:
    """组合层 fixture：确定性 Runner + 待测 SessionStore。"""

    def __init__(self, session_store) -> None:
        self.run_store = CountingRunStore(PlaintextPayloadCodec())
        self.registry = DefinitionRegistry()
        self.model = RecordingModelAdapter()

        self.registry.register(
            AgentDefinition.for_adapter(
                definition_id="assistant",
                version="1.0",
                instructions="be brief",
                model_adapter=self.model,
            )
        )
        self.registry.register(
            AgentDefinition.for_adapter(
                definition_id="broken",
                version="1.0",
                instructions="explode",
                model_adapter=ExplodingModelAdapter(responses=("ignored",)),
            )
        )
        self.registry.register(
            AgentDefinition.for_adapter(
                definition_id="gated",
                version="1.0",
                instructions="always rejected",
                model_adapter=RecordingModelAdapter(),
                run_policy=StaticRunPolicy(
                    policy_id="input-deny",
                    version="1",
                    decisions={
                        PolicyGate.INPUT: PolicyDecision(
                            action=PolicyAction.REJECT,
                            reason_code="INPUT_DENIED",
                        )
                    },
                ),
            )
        )
        self.registry.register(
            AgentDefinition.for_adapter(
                definition_id="slow",
                version="1.0",
                instructions="yield once",
                model_adapter=YieldingModelAdapter(),
            )
        )
        self.waiting_model = ToolWaitingModelAdapter()
        self.waiting_tool = UncertainEffectTool()
        self.registry.register(
            AgentDefinition.for_adapter(
                definition_id="careful",
                version="1.0",
                instructions="use the tool",
                model_requirements=ModelRequirements(
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    )
                ),
                model_adapter=self.waiting_model,
                tools=(self.waiting_tool,),
            )
        )

        self.runner = Runner(registry=self.registry, store=self.run_store)
        self.session_runner = SessionRunner(
            runner=self.runner, session_store=session_store
        )
        self.session_store = session_store


class SessionConversationContractMixin:
    """SessionRunner 组合行为契约。子类必须同时继承
    ``unittest.IsolatedAsyncioTestCase`` 并实现
    ``make_session_store()``（返回全新 SessionStore 实例）。
    """

    def make_session_store(self):  # pragma: no cover
        raise NotImplementedError

    def make_harness(self, session_store=None) -> SessionConversationHarness:
        return SessionConversationHarness(
            session_store if session_store is not None
            else self.make_session_store()
        )

    async def _create_session(self, harness, session_id="session-1"):
        return await harness.session_store.create_session(_SCOPE, session_id)

    # -- AC 1 / AC 6：一条成功会话 --------------------------------------

    async def test_successful_conversation_appends_immutable_turns(self) -> None:
        harness = self.make_harness()
        store = harness.session_store
        await self._create_session(harness)

        first = await harness.session_runner.submit(
            _SCOPE, "session-1", "assistant", "1.0", "first message"
        )
        self.assertIs(first.run.status, RunStatus.SUCCEEDED)
        self.assertIs(first.commit_status, SessionCommitStatus.COMMITTED)
        self.assertIsNotNone(first.turn)
        self.assertEqual(first.turn.user_input, "first message")
        self.assertEqual(first.turn.assistant_output, "session answer")
        self.assertEqual(first.turn.run_id, first.run.run_id)
        self.assertEqual(first.turn.definition_id, "assistant")
        self.assertEqual(first.turn.definition_version, "1.0")
        self.assertIsNotNone(first.turn.created_at)

        second = await harness.session_runner.submit(
            _SCOPE, "session-1", "assistant", "1.0", "second message"
        )
        self.assertIs(second.commit_status, SessionCommitStatus.COMMITTED)

        snapshot = await store.read_snapshot(_SCOPE, "session-1")
        self.assertEqual(snapshot.version, 2)
        self.assertEqual(
            [turn.user_input for turn in snapshot.turns],
            ["first message", "second message"],
        )
        # 提交完成后 claim 被清除，Session 可继续下一条消息。
        self.assertIsNone(await store.get_claim(_SCOPE, "session-1"))

    async def test_committed_turn_schema_is_minimal(self) -> None:
        # Session Turn 只承载最小对话事实：不允许夹带任意 metadata、
        # 中间输出或工具轨迹（PRD Session Turn schema）。
        self.assertEqual(
            set(SessionTurn.model_fields),
            {
                "turn_id",
                "session_id",
                "run_id",
                "definition_id",
                "definition_version",
                "user_input",
                "assistant_output",
                "created_at",
            },
        )

    # -- AC 5：Snapshot 一次性冻结为受保护 Conversation History ---------

    async def test_snapshot_converts_once_into_frozen_model_history(self) -> None:
        harness = self.make_harness()
        await self._create_session(harness)
        await harness.session_runner.submit(
            _SCOPE, "session-1", "assistant", "1.0", "first message"
        )

        # 第二条消息的模型请求只看到冻结的历史：第一条 Turn 转换成的
        # USER/ASSISTANT Conversation History + 当前输入。
        second = await harness.session_runner.submit(
            _SCOPE, "session-1", "assistant", "1.0", "second message"
        )
        last_request = harness.model.requests[-1]
        self.assertEqual(
            last_request.history,
            (
                ConversationMessage(role=ConversationRole.USER, content="first message"),
                ConversationMessage(
                    role=ConversationRole.ASSISTANT, content="session answer"
                ),
            ),
        )
        self.assertEqual(last_request.input, "second message")

        # 历史作为受保护 Run Payload 冻结在权威 Run 记录中。
        stored_run = await harness.runner.get_run(second.run.run_id)
        self.assertIsNotNone(stored_run)
        self.assertEqual(len(stored_run.history), 2)
        self.assertEqual(stored_run.history[0].content, "first message")

    async def test_submit_and_resume_never_reread_the_session_store(self) -> None:
        harness = self.make_harness()
        store = harness.session_store
        await self._create_session(harness)
        readings = {"snapshot": 0}

        class SnapshotCountingStore:
            def __init__(self, inner) -> None:
                self._inner = inner

            async def read_snapshot(self, scope, session_id, **kwargs):
                readings["snapshot"] += 1
                return await self._inner.read_snapshot(scope, session_id, **kwargs)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        counting = SnapshotCountingStore(store)
        harness.session_runner = SessionRunner(
            runner=harness.runner, session_store=counting
        )

        await harness.session_runner.submit(
            _SCOPE, "session-1", "assistant", "1.0", "message"
        )
        # 一次 submit 恰好读取一次 Snapshot（claim 前的那次），
        # start/resume/commit 都不重读 Session Store。
        self.assertEqual(readings["snapshot"], 1)

        # claim 已随提交清除：resume 稳定拒绝，且不重读 Session Store。
        with self.assertRaises(SessionClaimConflictError):
            await harness.session_runner.resume(_SCOPE, "session-1")
        self.assertEqual(readings["snapshot"], 1)

    async def test_sessionless_run_semantics_are_unchanged(self) -> None:
        harness = self.make_harness()
        # 不带 history / run_id 的既有 create_run 语义保持不变。
        created = await harness.runner.create_run("assistant", "1.0", "plain input")
        self.assertEqual(created.history, ())
        self.assertNotEqual(created.run_id, "")
        terminal = await harness.runner.start_run(created.run_id)
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(harness.model.requests[-1].history, ())
        self.assertEqual(harness.model.requests[-1].input, "plain input")

    # -- AC 2：跨 Scope fail-closed -------------------------------------

    async def test_submit_with_wrong_scope_fails_closed(self) -> None:
        harness = self.make_harness()
        await self._create_session(harness)

        with self.assertRaises(SessionNotFoundError):
            await harness.session_runner.submit(
                _OTHER_SCOPE, "session-1", "assistant", "1.0", "sneaky message"
            )
        # 失败路径不产生孤儿 Run，也不改动原 Scope 状态。
        self.assertEqual(harness.run_store.created_runs, 0)
        snapshot = await harness.session_store.read_snapshot(_SCOPE, "session-1")
        self.assertEqual(snapshot.turns, ())
        self.assertIsNone(await harness.session_store.get_claim(_SCOPE, "session-1"))

    # -- AC 4 / AC 7：claim 竞争与禁止越过 ------------------------------

    async def test_second_message_cannot_overtake_unresolved_work(self) -> None:
        harness = self.make_harness()
        store = harness.session_store
        await self._create_session(harness)

        waiting = await harness.session_runner.submit(
            _SCOPE, "session-1", "careful", "1.0", "needs approval"
        )
        self.assertIs(waiting.run.status, RunStatus.WAITING)
        # WAITING Run 保留 claim。
        claim = await store.get_claim(_SCOPE, "session-1")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.run_id, waiting.run.run_id)
        self.assertIs(waiting.commit_status, SessionCommitStatus.NOT_READY)

        # 另一条消息不得越过未解决工作获得 Session slot。
        with self.assertRaises(SessionClaimConflictError):
            await harness.session_runner.submit(
                _SCOPE, "session-1", "assistant", "1.0", "overtaking message"
            )
        self.assertEqual(
            (await store.get_claim(_SCOPE, "session-1")) or claim, claim
        )
        # 被拒绝的消息没有创建任何 Run。
        self.assertEqual(harness.run_store.created_runs, 1)

    async def test_concurrent_submits_admit_exactly_one_run(self) -> None:
        harness = self.make_harness()
        await self._create_session(harness)

        # 使用会让出事件循环的确定性模型：第一个 submit 在执行中让出，
        # 第二个 submit 在其未终态期间发起 claim 竞争 → 稳定冲突。
        results = await asyncio.gather(
            harness.session_runner.submit(
                _SCOPE, "session-1", "slow", "1.0", "message a"
            ),
            harness.session_runner.submit(
                _SCOPE, "session-1", "slow", "1.0", "message b"
            ),
            return_exceptions=True,
        )
        conflicts = [
            result
            for result in results
            if isinstance(result, SessionClaimConflictError)
        ]
        successes = [
            result
            for result in results
            if not isinstance(result, BaseException)
        ]
        # claim 竞争失败路径稳定复现：恰好一个成功、一个竞争失败。
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(len(successes), 1)
        self.assertEqual(harness.run_store.created_runs, 1)
        snapshot = await harness.session_store.read_snapshot(_SCOPE, "session-1")
        self.assertEqual(len(snapshot.turns), 1)

    # -- AC 7：非成功终态释放 claim --------------------------------------

    async def test_failed_run_releases_claim_without_turn(self) -> None:
        harness = self.make_harness()
        store = harness.session_store
        await self._create_session(harness)

        result = await harness.session_runner.submit(
            _SCOPE, "session-1", "broken", "1.0", "will fail"
        )
        self.assertIs(result.run.status, RunStatus.FAILED)
        self.assertIs(result.commit_status, SessionCommitStatus.NOT_READY)
        self.assertIsNone(result.turn)
        # FAILED：claim 释放、历史为空、版本为零。
        self.assertIsNone(await store.get_claim(_SCOPE, "session-1"))
        snapshot = await store.read_snapshot(_SCOPE, "session-1")
        self.assertEqual(snapshot.version, 0)
        self.assertEqual(snapshot.turns, ())

        # claim 释放后下一条消息可以正常占用 slot。
        followup = await harness.session_runner.submit(
            _SCOPE, "session-1", "assistant", "1.0", "try again"
        )
        self.assertIs(followup.commit_status, SessionCommitStatus.COMMITTED)

    async def test_rejected_run_releases_claim_without_turn(self) -> None:
        harness = self.make_harness()
        store = harness.session_store
        await self._create_session(harness)

        result = await harness.session_runner.submit(
            _SCOPE, "session-1", "gated", "1.0", "denied input"
        )
        self.assertIs(result.run.status, RunStatus.REJECTED)
        self.assertIs(result.commit_status, SessionCommitStatus.NOT_READY)
        self.assertIsNone(await store.get_claim(_SCOPE, "session-1"))
        snapshot = await store.read_snapshot(_SCOPE, "session-1")
        self.assertEqual(snapshot.turns, ())

    async def test_cancelled_waiting_run_releases_claim_without_turn(self) -> None:
        harness = self.make_harness()
        store = harness.session_store
        await self._create_session(harness)

        waiting = await harness.session_runner.submit(
            _SCOPE, "session-1", "careful", "1.0", "needs approval"
        )
        cancelled = await harness.runner.resolve_run(
            waiting.run.run_id,
            RunResolution.cancel_run(reason="user cancelled"),
            expected_version=waiting.run.version,
        )
        self.assertIs(cancelled.status, RunStatus.CANCELLED)

        # 恢复入口依据权威终态释放 claim，不产生 Turn。
        result = await harness.session_runner.resume(_SCOPE, "session-1")
        self.assertIs(result.run.status, RunStatus.CANCELLED)
        self.assertIs(result.commit_status, SessionCommitStatus.NOT_READY)
        self.assertIsNone(await store.get_claim(_SCOPE, "session-1"))
        snapshot = await store.read_snapshot(_SCOPE, "session-1")
        self.assertEqual(snapshot.turns, ())

    async def test_waiting_run_resume_after_confirmation_commits_turn(self) -> None:
        harness = self.make_harness()
        store = harness.session_store
        await self._create_session(harness)

        waiting = await harness.session_runner.submit(
            _SCOPE, "session-1", "careful", "1.0", "needs approval"
        )
        self.assertIs(waiting.run.status, RunStatus.WAITING)
        self.assertEqual(
            (await store.read_snapshot(_SCOPE, "session-1")).turns, ()
        )

        confirmed = await harness.runner.resolve_run(
            waiting.run.run_id,
            RunResolution.confirm_step(result="confirmed-effect"),
            expected_version=waiting.run.version,
        )
        self.assertIs(confirmed.status, RunStatus.SUCCEEDED)

        # 恢复入口读取权威 RunStore 终态并完成 Session 提交。
        result = await harness.session_runner.resume(_SCOPE, "session-1")
        self.assertIs(result.run.status, RunStatus.SUCCEEDED)
        self.assertIs(result.commit_status, SessionCommitStatus.COMMITTED)
        self.assertIsNotNone(result.turn)
        self.assertEqual(result.turn.assistant_output, "final: confirmed-effect")
        self.assertIsNone(await store.get_claim(_SCOPE, "session-1"))
        snapshot = await store.read_snapshot(_SCOPE, "session-1")
        self.assertEqual(snapshot.version, 1)

    # -- AC 6 / AC 8：幂等提交与提交状态分离 ------------------------------

    async def test_store_outage_maps_to_pending_and_retry_commits_once(self) -> None:
        session_store = self.make_session_store()
        flaky = FlakyCommitSessionStore(session_store, failures=1)
        harness = self.make_harness(session_store=flaky)
        await self._create_session(harness)

        result = await harness.session_runner.submit(
            _SCOPE, "session-1", "assistant", "1.0", "message"
        )
        # Core 已成功；SessionStore 故障不得改写该终态：提交状态为
        # PENDING，claim 保留，等待幂等重试。
        self.assertIs(result.run.status, RunStatus.SUCCEEDED)
        self.assertIs(result.commit_status, SessionCommitStatus.PENDING)
        self.assertIsNone(result.turn)
        claim = await flaky.get_claim(_SCOPE, "session-1")
        self.assertIsNotNone(claim)
        self.assertEqual(flaky.commit_calls, 1)

        # 通过公开查询观察 PENDING。
        self.assertEqual(
            await harness.session_runner.commit_status(
                _SCOPE, "session-1", result.run.run_id
            ),
            SessionCommitStatus.PENDING,
        )

        # 恢复入口重试提交：以 run identity 幂等去重，只产生一条 Turn。
        retried = await harness.session_runner.resume(_SCOPE, "session-1")
        self.assertIs(retried.commit_status, SessionCommitStatus.COMMITTED)
        snapshot = await session_store.read_snapshot(_SCOPE, "session-1")
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(len(snapshot.turns), 1)
        self.assertEqual(snapshot.turns[0].run_id, result.run.run_id)
        self.assertEqual(flaky.commit_calls, 2)

    async def test_commit_status_is_separate_from_core_run_status(self) -> None:
        session_store = self.make_session_store()
        flaky = FlakyCommitSessionStore(session_store, failures=1)
        harness = self.make_harness(session_store=flaky)
        await self._create_session(harness)

        pending = await harness.session_runner.submit(
            _SCOPE, "session-1", "assistant", "1.0", "message"
        )
        run_id = pending.run.run_id
        # Core 终态权威且不被 SessionStore 状态改写。
        authoritative = await harness.runner.get_run(run_id)
        self.assertIs(authoritative.status, RunStatus.SUCCEEDED)
        self.assertEqual(
            await harness.session_runner.commit_status(
                _SCOPE, "session-1", run_id
            ),
            SessionCommitStatus.PENDING,
        )

        # 人工释放 claim 后另一条消息成功提交：stale Run 的提交路径
        # 进入 CONFLICT，但 Core 终态保持 SUCCEEDED。
        await flaky.release_claim(_SCOPE, "session-1", run_id)
        followup = await harness.session_runner.submit(
            _SCOPE, "session-1", "assistant", "1.0", "another message"
        )
        self.assertIs(followup.commit_status, SessionCommitStatus.COMMITTED)
        self.assertEqual(
            await harness.session_runner.commit_status(
                _SCOPE, "session-1", run_id
            ),
            SessionCommitStatus.CONFLICT,
        )
        authoritative = await harness.runner.get_run(run_id)
        self.assertIs(authoritative.status, RunStatus.SUCCEEDED)

        # 会话历史只有后来者的一条 Turn；stale Run 不产生重复 Turn。
        snapshot = await session_store.read_snapshot(_SCOPE, "session-1")
        self.assertEqual(len(snapshot.turns), 1)
        self.assertEqual(snapshot.turns[0].run_id, followup.run.run_id)

    async def test_commit_status_not_ready_before_success(self) -> None:
        harness = self.make_harness()
        await self._create_session(harness)
        waiting = await harness.session_runner.submit(
            _SCOPE, "session-1", "careful", "1.0", "needs approval"
        )
        # 未到权威 SUCCEEDED：无可提交 Turn，NOT_READY 可观察。
        self.assertEqual(
            await harness.session_runner.commit_status(
                _SCOPE, "session-1", waiting.run.run_id
            ),
            SessionCommitStatus.NOT_READY,
        )
        with self.assertRaises(RunNotFoundError):
            await harness.session_runner.commit_status(
                _SCOPE, "session-1", "run-that-never-existed"
            )

    # -- 组合负路径 -------------------------------------------------------

    async def test_submit_releases_claim_when_run_creation_fails(self) -> None:
        harness = self.make_harness()
        store = harness.session_store
        await self._create_session(harness)

        with self.assertRaises(DefinitionNotFoundError):
            await harness.session_runner.submit(
                _SCOPE, "session-1", "missing-definition", "9.9", "message"
            )
        # Run 创建失败（定义缺失）：claim 被回滚释放，Session 不被卡死。
        self.assertIsNone(await store.get_claim(_SCOPE, "session-1"))
        self.assertEqual(harness.run_store.created_runs, 0)
        followup = await harness.session_runner.submit(
            _SCOPE, "session-1", "assistant", "1.0", "works now"
        )
        self.assertIs(followup.commit_status, SessionCommitStatus.COMMITTED)

    async def test_resume_requires_an_active_claim(self) -> None:
        harness = self.make_harness()
        await self._create_session(harness)
        with self.assertRaises(SessionClaimConflictError):
            await harness.session_runner.resume(_SCOPE, "session-1")

    async def test_resume_releases_claim_when_run_was_never_created(self) -> None:
        # 预分配 run identity 后进程在 create_run 之前失败：权威 RunStore
        # 中不存在该 Run。resume 确认「从未创建」后清理 claim（ADR 0020）。
        harness = self.make_harness()
        store = harness.session_store
        await self._create_session(harness)
        await store.claim_run(_SCOPE, "session-1", "ghost-run", expected_version=0)

        with self.assertRaises(RunNotFoundError):
            await harness.session_runner.resume(_SCOPE, "session-1")
        self.assertIsNone(await store.get_claim(_SCOPE, "session-1"))

    async def test_resume_keeps_claim_when_run_store_fails_transiently(self) -> None:
        # 瞬态 RunStore 故障既未确认终态也未确认「从未创建」（ADR
        # 0020）：claim 必须保留、异常原样传播，另一条消息不得越过
        # 未解决工作。
        harness = self.make_harness()
        store = harness.session_store
        await self._create_session(harness)
        waiting = await harness.session_runner.submit(
            _SCOPE, "session-1", "careful", "1.0", "needs approval"
        )
        self.assertIs(waiting.run.status, RunStatus.WAITING)

        flaky = SessionRunner(
            runner=TransientGetRunRunner(harness.runner, failures=1),
            session_store=store,
        )
        with self.assertRaises(RuntimeError):
            await flaky.resume(_SCOPE, "session-1")

        claim = await store.get_claim(_SCOPE, "session-1")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.run_id, waiting.run.run_id)
        with self.assertRaises(SessionClaimConflictError):
            await harness.session_runner.submit(
                _SCOPE, "session-1", "assistant", "1.0", "overtake attempt"
            )
