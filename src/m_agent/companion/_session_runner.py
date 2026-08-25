"""SessionRunner：把 SessionStore 与 Runtime Core Runner 显式组合。

ADR 0019-0021 的组合边界：SessionRunner 在 Agent Run 创建之前读取
一次 Session Snapshot，把它一次性转换成不可变 Conversation History
并显式交给 ``Runner.create_run`` 冻结为受保护 Run Payload；start /
resume / 恢复不重读 Session Store。Run 结束后依据权威 RunStore 终态
决定：仅 ``SUCCEEDED`` 以 ``run_id`` 幂等键提交最小对话事实 Turn，
``REJECTED`` / ``FAILED`` / ``CANCELLED`` 释放 claim，``WAITING`` 保留
claim（另一条消息不得越过未解决工作）。Core Run Status 与 Session
Commit Status 分离且分别可观察；SessionStore 故障不得改写 Core 终态。

SessionRunner 是 Runtime Companion：它只通过公开端口组合 Core，
不给 Runner 注入任何 hook。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from .._clock import Clock, SystemClock
from .._errors import RunNotFoundError
from .._history import ConversationMessage, ConversationRole
from .._run import RunRecord
from .._runner import Runner
from .._status import RunStatus
from pydantic import BaseModel, ConfigDict

from ._session import (
    SessionClaimConflictError,
    SessionCommitStatus,
    SessionError,
    SessionNotFoundError,
    SessionRunClaim,
    SessionScope,
    SessionStore,
    SessionTurn,
)


def new_session_id() -> str:
    """SessionRunner 侧新标识（预分配 run identity / turn identity）。"""
    return uuid.uuid4().hex


class SessionRunResult(BaseModel, frozen=True):
    """一次 Session 对话推进的可观测结果。

    ``run`` 是权威 RunStore 记录（终态或当前态）；``claim`` 是本次
    绑定的占用声明（提交/释放后 Store 内已清除，这里是绑定证据）；
    ``commit_status`` 与 Core Run Status 分离——Core 成功 + 提交
    pending/conflict 是合法且可观察的中间状态。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run: RunRecord
    claim: SessionRunClaim
    commit_status: SessionCommitStatus
    turn: SessionTurn | None


class SessionRunner:
    """把一条 scoped Session 消息推进为一个 Agent Run 并回写结果。

    :param runner: Runtime Core 的公开 Runner（sessionless；本组件不
        给它注入任何 hook）。
    :param session_store: SessionStore 实现（InMemory / SQLite 共享同一
        行为契约）。
    :param clock: 可注入时钟（Turn 时间戳的确定性来源）。
    """

    def __init__(
        self,
        *,
        runner: Runner,
        session_store: SessionStore,
        clock: Clock | None = None,
    ) -> None:
        self._runner = runner
        self._store = session_store
        self._clock = clock if clock is not None else SystemClock()

    # -- 公开组合入口 -----------------------------------------------------

    async def submit(
        self,
        scope: SessionScope,
        session_id: str,
        definition_id: str,
        definition_version: str,
        user_input: str,
    ) -> SessionRunResult:
        """提交一条新消息：占用 Session slot 并完成一次 Agent Run。

        1. 读取一次 Session Snapshot（exact 当前版本）；
        2. 一次性转换为不可变 Conversation History；
        3. 预分配 run identity 并以一次原子 claim 绑定冻结版本；
        4. 以该 identity 创建 Run（history 作为受保护 Run Payload 冻结）
           并推进到终态或 WAITING；
        5. 依据权威终态提交 Turn / 释放 claim / 保留 claim。

        Session 不存在（含跨 Scope）抛 :class:`SessionNotFoundError`；
        claim 竞争失败抛 :class:`SessionClaimConflictError`（此时未创建
        任何 Run）。Run 创建失败时回滚释放 claim；Run 执行异常时保留
        claim 并传播异常（Run 已存在，可由 :meth:`resume` 恢复）。
        """
        snapshot = await self._store.read_snapshot(scope, session_id)
        history = _to_conversation_history(snapshot.turns)
        run_id = new_session_id()
        claim = await self._store.claim_run(
            scope, session_id, run_id, expected_version=snapshot.version
        )
        try:
            await self._runner.create_run(
                definition_id,
                definition_version,
                user_input,
                run_id=run_id,
                history=history,
            )
        except BaseException:
            # Run 从未创建：回滚 claim，避免 Session 被幽灵 identity 卡死。
            # 释放自身的失败不影响传播原始错误（残余 claim 交给对账）。
            try:
                await self._store.release_claim(scope, session_id, run_id)
            except SessionError:
                pass
            raise
        run = await self._runner.start_run(run_id)
        return await self._complete(scope, session_id, run, claim)

    async def resume(
        self, scope: SessionScope, session_id: str
    ) -> SessionRunResult:
        """恢复当前 claimed Run 并依据权威状态完成 Session 生命周期。

        - 无 active claim 抛 :class:`SessionClaimConflictError`；
        - claimed Run 在权威 RunStore 中不存在（claim 后、create 前
          崩溃）：确认"从未创建"后清理 claim 并抛
          :class:`m_agent.runtime.RunNotFoundError`（ADR 0020）；
        - 非终态 Run 走公开 resume_run 推进；终态 Run 直接完成提交或
          释放（例如提交曾因 Store 故障处于 PENDING，重试以 run
          identity 幂等去重）。
        """
        record = await self._store.get_session(scope, session_id)
        if record is None:
            raise SessionNotFoundError(
                f"session {session_id!r} not found in the provided scope"
            )
        claim = record.claim
        if claim is None:
            raise SessionClaimConflictError(
                f"session {session_id!r} has no active claim to resume"
            )
        try:
            run = await self._runner.get_run(claim.run_id)
        except RunNotFoundError:
            # 权威 RunStore 确认该 identity 从未创建：清理 claim 后
            # 传播原始错误。瞬态 RunStore 故障（非 RunNotFoundError）
            # 既未确认终态也未确认"从未创建"，保留 claim 并原样传播
            # （ADR 0020：claim 只依据权威状态对账清理）。
            await self._store.release_claim(scope, session_id, claim.run_id)
            raise
        if not run.status.is_terminal:
            run = await self._runner.resume_run(claim.run_id)
        return await self._complete(scope, session_id, run, claim)

    async def commit_status(
        self, scope: SessionScope, session_id: str, run_id: str
    ) -> SessionCommitStatus:
        """查询一个 Run 的 Turn 提交状态（只读，不改动任何 Store）。

        - ``COMMITTED``：历史中已有该 run identity 的 Turn；
        - ``NOT_READY``：Run 未进入权威 ``SUCCEEDED``（不存在可提交
          的 Turn）；
        - ``PENDING``：Run 已成功、Turn 未提交且 claim 完整（安全
          幂等重试窗口）；
        - ``CONFLICT``：Run 已成功但 claim 缺失 / 被其他 run 持有 /
          版本已前进——需要应用显式对账，禁止自动 merge。

        Session 不存在抛 :class:`SessionNotFoundError`；Run 不在权威
        RunStore 中抛 :class:`m_agent.runtime.RunNotFoundError`。
        """
        record = await self._store.get_session(scope, session_id)
        if record is None:
            raise SessionNotFoundError(
                f"session {session_id!r} not found in the provided scope"
            )
        turn = await self._store.find_turn_by_run(scope, session_id, run_id)
        if turn is not None:
            return SessionCommitStatus.COMMITTED
        run = await self._runner.get_run(run_id)
        if run.status is not RunStatus.SUCCEEDED:
            return SessionCommitStatus.NOT_READY
        claim = record.claim
        if claim is None or claim.run_id != run_id:
            return SessionCommitStatus.CONFLICT
        if record.version != claim.session_version:
            return SessionCommitStatus.CONFLICT
        return SessionCommitStatus.PENDING

    # -- 内部 -------------------------------------------------------------

    async def _complete(
        self,
        scope: SessionScope,
        session_id: str,
        run: RunRecord,
        claim: SessionRunClaim,
    ) -> SessionRunResult:
        """依据权威 Run 终态完成 Session 生命周期（提交 / 释放 / 保留）。"""
        if run.status is RunStatus.SUCCEEDED:
            return await self._commit_successful_run(
                scope, session_id, run, claim
            )
        if run.status.is_terminal:
            # REJECTED / FAILED / CANCELLED：只释放 claim，不产生 Turn。
            await self._store.release_claim(scope, session_id, run.run_id)
            return SessionRunResult(
                run=run,
                claim=claim,
                commit_status=SessionCommitStatus.NOT_READY,
                turn=None,
            )
        # 非终态（WAITING 等）：保留 claim，另一条消息不得越过。
        return SessionRunResult(
            run=run,
            claim=claim,
            commit_status=SessionCommitStatus.NOT_READY,
            turn=None,
        )

    async def _commit_successful_run(
        self,
        scope: SessionScope,
        session_id: str,
        run: RunRecord,
        claim: SessionRunClaim,
    ) -> SessionRunResult:
        turn = SessionTurn(
            turn_id=new_session_id(),
            session_id=session_id,
            run_id=run.run_id,
            definition_id=run.definition_id,
            definition_version=run.definition_version,
            user_input=run.input,
            assistant_output=run.output or "",
            created_at=self._clock.now(),
        )
        try:
            result = await self._store.commit_turn(
                scope,
                session_id,
                turn,
                expected_version=claim.session_version,
            )
        except Exception:
            # SessionStore 暂不可用 / 提交未完成：不改写 Core 终态，
            # 状态为 PENDING，claim 保留，等待幂等重试（run identity
            # 是幂等键）。
            return SessionRunResult(
                run=run,
                claim=claim,
                commit_status=SessionCommitStatus.PENDING,
                turn=None,
            )
        if result.status is SessionCommitStatus.COMMITTED:
            return SessionRunResult(
                run=run,
                claim=claim,
                commit_status=SessionCommitStatus.COMMITTED,
                turn=result.turn,
            )
        return SessionRunResult(
            run=run,
            claim=claim,
            commit_status=SessionCommitStatus.CONFLICT,
            turn=None,
        )


def _to_conversation_history(
    turns: Sequence[SessionTurn],
) -> tuple[ConversationMessage, ...]:
    """把 Session Snapshot 一次性转换为不可变 Conversation History。

    每条 Turn 展开为一对 USER/ASSISTANT 消息；转换只发生在这里——
    Run 创建后历史即冻结，恢复绝不重读 Session Store。
    """
    history: list[ConversationMessage] = []
    for turn in turns:
        history.append(
            ConversationMessage(
                role=ConversationRole.USER, content=turn.user_input
            )
        )
        history.append(
            ConversationMessage(
                role=ConversationRole.ASSISTANT, content=turn.assistant_output
            )
        )
    return tuple(history)
