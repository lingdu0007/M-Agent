"""Official Runtime Companion namespace.

Session Companion（ADR 0018-0021）：应用通过 SessionStore
显式创建受 opaque Scope 隔离的 Session，SessionRunner 把一次 Session
Snapshot 一次性冻结为 Conversation History 并组合 Runtime Core 的
Runner 完成一条 scoped 对话。Companion 只通过公开端口组合 Core，
绝不给 Runner 注入 hook；Runtime Core 不导入本命名空间。

SQLiteSessionStore 提供跨进程 durable 的同一行为契约实现，
对话正文经独立的 Session Payload Codec 保护，与可搜索 metadata 分离。

Eval Companion（ADR 0029，m_agent.companion.eval）：隔离 EXECUTE 与
严格只读 OBSERVE 的最小证据评估能力。

Model Routing Companion（ADR 0041，m_agent.companion.routing）：
在 ``create_run`` 前用冻结 Catalog、版本化 Routing Policy、
Deployment Constraints 与只读快照确定性选择完整 Agent Variant。
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
from . import eval
from . import routing
from ._sqlite_session_store import SQLiteSessionStore

__all__ = [
    "DuplicateSessionError",
    "InMemorySessionStore",
    "SQLiteSessionStore",
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
