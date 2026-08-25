"""InMemory SessionStore：Session Companion 契约的进程内参考实现。

用于确定性测试、本地实验与 Ticket 12 的 scoped Session 对话。所有
方法体内没有 await 间隙，在 asyncio 事件循环下每个操作原子完成；
Scope 以 ``(SessionScope, session_id)`` 复合键隔离，跨 Scope 访问与
"不存在"不可区分。Session Payload 的独立保护与 SQLite 持久化属于
Ticket 13，本实现不做 payload 编码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .._clock import Clock, SystemClock
from ._session import (
    DuplicateSessionError,
    SessionClaimConflictError,
    SessionCommitResult,
    SessionCommitStatus,
    SessionNotFoundError,
    SessionRecord,
    SessionRunClaim,
    SessionScope,
    SessionSnapshot,
    SessionTurn,
    SessionVersionConflictError,
)


@dataclass
class _StoredSession:
    """Session 的进程内权威状态（Store 私有）。"""

    session_id: str
    version: int = 0
    claim: SessionRunClaim | None = None
    turns: list[SessionTurn] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class InMemorySessionStore:
    """进程内 SessionStore，实现 :class:`m_agent.companion.SessionStore`。

    :param clock: 可注入时钟（测试用 FakeClock 确定性驱动记录时间戳）。
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock if clock is not None else SystemClock()
        self._sessions: dict[tuple[SessionScope, str], _StoredSession] = {}

    # -- 内部辅助 -------------------------------------------------------

    def _lookup(
        self, scope: SessionScope, session_id: str
    ) -> _StoredSession:
        stored = self._sessions.get((scope, session_id))
        if stored is None:
            # 跨 Scope 访问与不存在不可区分（fail-closed）。
            raise SessionNotFoundError(
                f"session {session_id!r} not found in the provided scope"
            )
        return stored

    @staticmethod
    def _record(stored: _StoredSession) -> SessionRecord:
        return SessionRecord(
            session_id=stored.session_id,
            version=stored.version,
            claim=stored.claim,
            created_at=stored.created_at,  # type: ignore[arg-type]
            updated_at=stored.updated_at,  # type: ignore[arg-type]
        )

    def _existing_turn(
        self, stored: _StoredSession, run_id: str
    ) -> SessionTurn | None:
        for turn in stored.turns:
            if turn.run_id == run_id:
                return turn
        return None

    # -- 生命周期 -------------------------------------------------------

    async def create_session(
        self, scope: SessionScope, session_id: str
    ) -> SessionRecord:
        if not session_id or not session_id.strip():
            raise ValueError("session_id must be a non-blank identifier")
        key = (scope, session_id)
        if key in self._sessions:
            raise DuplicateSessionError(
                f"session {session_id!r} already exists in the provided scope"
            )
        now = self._clock.now()
        stored = _StoredSession(
            session_id=session_id,
            version=0,
            claim=None,
            turns=[],
            created_at=now,
            updated_at=now,
        )
        self._sessions[key] = stored
        return self._record(stored)

    async def get_session(
        self, scope: SessionScope, session_id: str
    ) -> SessionRecord | None:
        stored = self._sessions.get((scope, session_id))
        if stored is None:
            return None
        return self._record(stored)

    # -- Snapshot / 历史 --------------------------------------------------

    async def read_snapshot(
        self,
        scope: SessionScope,
        session_id: str,
        *,
        expected_version: int | None = None,
        after: int = 0,
        limit: int | None = None,
    ) -> SessionSnapshot:
        if after < 0:
            raise ValueError("after must be a non-negative cursor")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be a positive page size")
        stored = self._lookup(scope, session_id)
        if expected_version is not None and expected_version != stored.version:
            raise SessionVersionConflictError(
                f"session {session_id!r} version conflict: expected "
                f"{expected_version}, authoritative version is "
                f"{stored.version}"
            )
        total = len(stored.turns)
        window = stored.turns[after:] if limit is None else stored.turns[after : after + limit]
        next_cursor = after + len(window)
        return SessionSnapshot(
            session_id=stored.session_id,
            version=stored.version,
            turns=tuple(window),
            next_cursor=next_cursor if next_cursor < total else None,
        )

    # -- Claim -------------------------------------------------------------

    async def get_claim(
        self, scope: SessionScope, session_id: str
    ) -> SessionRunClaim | None:
        stored = self._sessions.get((scope, session_id))
        if stored is None:
            return None
        return stored.claim

    async def claim_run(
        self,
        scope: SessionScope,
        session_id: str,
        run_id: str,
        *,
        expected_version: int,
    ) -> SessionRunClaim:
        stored = self._lookup(scope, session_id)
        if stored.claim is not None:
            raise SessionClaimConflictError(
                f"session {session_id!r} is already claimed by run "
                f"{stored.claim.run_id!r}"
            )
        if self._existing_turn(stored, run_id) is not None:
            raise SessionClaimConflictError(
                f"run {run_id!r} already committed a turn to session "
                f"{session_id!r}"
            )
        if stored.version != expected_version:
            raise SessionVersionConflictError(
                f"session {session_id!r} version conflict: expected "
                f"{expected_version}, authoritative version is "
                f"{stored.version}; no claim was created"
            )
        claim = SessionRunClaim(
            session_id=stored.session_id,
            run_id=run_id,
            session_version=stored.version,
            claimed_at=self._clock.now(),
        )
        stored.claim = claim
        stored.updated_at = self._clock.now()
        return claim

    async def release_claim(
        self, scope: SessionScope, session_id: str, run_id: str
    ) -> None:
        stored = self._lookup(scope, session_id)
        if stored.claim is None:
            # 无 active claim：稳定 no-op（崩溃恢复的幂等重放）。
            return
        if stored.claim.run_id != run_id:
            raise SessionClaimConflictError(
                f"session {session_id!r} claim is held by run "
                f"{stored.claim.run_id!r}, not {run_id!r}"
            )
        stored.claim = None
        stored.updated_at = self._clock.now()

    # -- 提交 --------------------------------------------------------------

    async def commit_turn(
        self,
        scope: SessionScope,
        session_id: str,
        turn: SessionTurn,
        *,
        expected_version: int,
    ) -> SessionCommitResult:
        stored = self._lookup(scope, session_id)
        if turn.session_id != session_id:
            raise ValueError(
                "turn session binding does not match the target session"
            )
        existing = self._existing_turn(stored, turn.run_id)
        if existing is not None:
            # run identity 幂等键：重复提交去重，无任何 mutation。
            return SessionCommitResult(
                status=SessionCommitStatus.COMMITTED,
                turn=existing,
                version=stored.version,
            )
        claim = stored.claim
        if claim is None or claim.run_id != turn.run_id:
            return SessionCommitResult(
                status=SessionCommitStatus.CONFLICT,
                turn=None,
                version=stored.version,
            )
        if stored.version != expected_version:
            # 冲突保留 claim，禁止自动 merge。
            return SessionCommitResult(
                status=SessionCommitStatus.CONFLICT,
                turn=None,
                version=stored.version,
            )
        stored.turns.append(turn)
        stored.claim = None
        stored.version += 1
        stored.updated_at = self._clock.now()
        return SessionCommitResult(
            status=SessionCommitStatus.COMMITTED,
            turn=turn,
            version=stored.version,
        )

    async def find_turn_by_run(
        self, scope: SessionScope, session_id: str, run_id: str
    ) -> SessionTurn | None:
        stored = self._lookup(scope, session_id)
        return self._existing_turn(stored, run_id)
