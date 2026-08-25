"""Official Runtime Companion namespace.

Session Companion（Ticket 12 / ADR 0018-0021）：应用通过 SessionStore
显式创建受 opaque Scope 隔离的 Session，SessionRunner 把一次 Session
Snapshot 一次性冻结为 Conversation History 并组合 Runtime Core 的
Runner 完成一条 scoped 对话。Companion 只通过公开端口组合 Core，
绝不给 Runner 注入 hook；Runtime Core 不导入本命名空间。
"""

from ._in_memory_session_store import InMemorySessionStore
from ._session import (
    DuplicateSessionError,
    SessionClaimConflictError,
    SessionCommitResult,
    SessionCommitStatus,
    SessionError,
    SessionNotFoundError,
    SessionRecord,
    SessionRunClaim,
    SessionScope,
    SessionSnapshot,
    SessionStore,
    SessionTurn,
    SessionVersionConflictError,
)
from ._session_runner import SessionRunResult, SessionRunner

__all__ = [
    "DuplicateSessionError",
    "InMemorySessionStore",
    "SessionClaimConflictError",
    "SessionCommitResult",
    "SessionCommitStatus",
    "SessionError",
    "SessionNotFoundError",
    "SessionRecord",
    "SessionRunClaim",
    "SessionRunResult",
    "SessionRunner",
    "SessionScope",
    "SessionSnapshot",
    "SessionStore",
    "SessionTurn",
    "SessionVersionConflictError",
]
