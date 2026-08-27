"""SQLiteSessionStore：跨进程 durable 的 SessionStore 参考实现。

ADR 0018-0021：Session 对话历史的权威持久化边界，与
InMemorySessionStore 共享同一份 :class:`~m_agent.companion.SessionStore`
行为契约（Scope 隔离、exact-version read、cursor pagination、原子
claim、CAS commit、run identity 幂等去重）。

持久化与保护边界（对齐 ADR 0033 的 Run Store 做法）：

- 可查询 metadata（Session 版本、claim、turn 标识、Definition 引用、
  时间）与对话正文 payload 分离存储；正文（user_input /
  assistant_output）只经上层应用显式配置的 Session Payload
  :class:`~m_agent.runtime.PayloadCodec` 编码后存入
  ``session_turns.encoded_payload``，任何存取路径都不绕过 Codec。
  Codec 独立于 RunStore 的 PayloadCodec 配置；明文 Codec 仅用于
  开发与测试。
- 每个进程实例持有自己的连接；所有 mutation 在单个
  ``BEGIN IMMEDIATE`` 事务内校验并写入、返回前 commit——
  :meth:`commit_turn` 的「校验 claim → CAS version → append-once →
  清除 claim → version +1」要么全部落盘、要么全部回滚，进程在事务
  中途硬退出（含 ``os._exit``）由 SQLite journal 自动回滚，绝不留下
  半提交状态（ADR 0021 的 Turn 追加与 Claim 清理原子边界）。
- Scope 以 ``(scope_token, session_id)`` 复合键隔离；跨 Scope 访问
  与「不存在」不可区分（fail-closed），数据库行不跨 Scope 泄漏。
- Session Run Claim 无 TTL：claim 行只能被权威 RunStore 对账语义
  （release / commit）清理，本 Store 从不基于时间回收。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .._clock import Clock, SystemClock
from .._codec import PayloadCodec
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    scope_token TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    version     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (scope_token, session_id)
);
CREATE TABLE IF NOT EXISTS session_claims (
    scope_token     TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    session_version INTEGER NOT NULL,
    claimed_at      TEXT NOT NULL,
    PRIMARY KEY (scope_token, session_id)
);
CREATE TABLE IF NOT EXISTS session_turns (
    scope_token        TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    turn_index         INTEGER NOT NULL,
    turn_id            TEXT NOT NULL,
    run_id             TEXT NOT NULL,
    definition_id      TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    encoded_payload    BLOB NOT NULL,
    PRIMARY KEY (scope_token, session_id, turn_index),
    UNIQUE (scope_token, session_id, run_id)
);
"""


def _payload_json(turn: SessionTurn) -> str:
    """Turn 的受保护正文（只含对话文本；其余列是可搜索 metadata）。"""
    return json.dumps(
        {"user_input": turn.user_input, "assistant_output": turn.assistant_output},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _turn_from_row(row: sqlite3.Row, payload: str) -> SessionTurn:
    body = json.loads(payload)
    return SessionTurn(
        turn_id=row["turn_id"],
        session_id=row["session_id"],
        run_id=row["run_id"],
        definition_id=row["definition_id"],
        definition_version=row["definition_version"],
        user_input=body["user_input"],
        assistant_output=body["assistant_output"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class SQLiteSessionStore:
    """把 Session 对话历史持久化到单个 SQLite 文件的 SessionStore。

    :param path: 数据库文件路径；父目录必须已存在。
    :param payload_codec: Session 正文的保护边界（独立于 RunStore 的
        PayloadCodec）；明文 Codec 仅用于开发与测试。
    :param clock: 记录时间戳使用的时钟（默认系统时钟；测试用
        :class:`~m_agent.adapters.FakeClock` 确定性驱动）。
    """

    def __init__(
        self,
        path: str | Path,
        payload_codec: PayloadCodec,
        clock: Clock | None = None,
    ) -> None:
        self._path = str(path)
        self._codec = payload_codec
        self._clock = clock if clock is not None else SystemClock()
        self._conn = sqlite3.connect(self._path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteSessionStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def path(self) -> str:
        return self._path

    # -- 内部辅助 -------------------------------------------------------

    def _lookup(
        self, scope: SessionScope, session_id: str
    ) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE scope_token=? AND session_id=?",
            (scope.token, session_id),
        ).fetchone()
        if row is None:
            # 跨 Scope 访问与不存在不可区分（fail-closed）。
            raise SessionNotFoundError(
                f"session {session_id!r} not found in the provided scope"
            )
        return row

    def _claim_row(
        self, scope: SessionScope, session_id: str
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM session_claims WHERE scope_token=? AND session_id=?",
            (scope.token, session_id),
        ).fetchone()

    @staticmethod
    def _claim_from_row(row: sqlite3.Row) -> SessionRunClaim:
        return SessionRunClaim(
            session_id=row["session_id"],
            run_id=row["run_id"],
            session_version=row["session_version"],
            claimed_at=datetime.fromisoformat(row["claimed_at"]),
        )

    def _record_from_row(
        self, scope: SessionScope, row: sqlite3.Row
    ) -> SessionRecord:
        claim_row = self._claim_row(scope, row["session_id"])
        return SessionRecord(
            session_id=row["session_id"],
            version=row["version"],
            claim=self._claim_from_row(claim_row) if claim_row is not None else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _turn_row_by_run(
        self, scope: SessionScope, session_id: str, run_id: str
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM session_turns WHERE scope_token=? AND session_id=?"
            " AND run_id=?",
            (scope.token, session_id, run_id),
        ).fetchone()

    def _decode_turn(self, row: sqlite3.Row) -> SessionTurn:
        # 正文只经 Codec 读取：错误 key / 损坏 payload 在此 fail closed。
        return _turn_from_row(row, self._codec.decode(row["encoded_payload"]))

    # -- 生命周期 -------------------------------------------------------

    async def create_session(
        self, scope: SessionScope, session_id: str
    ) -> SessionRecord:
        if not session_id or not session_id.strip():
            raise ValueError("session_id must be a non-blank identifier")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            existing = self._conn.execute(
                "SELECT 1 FROM sessions WHERE scope_token=? AND session_id=?",
                (scope.token, session_id),
            ).fetchone()
            if existing is not None:
                raise DuplicateSessionError(
                    f"session {session_id!r} already exists in the provided scope"
                )
            now = self._clock.now()
            self._conn.execute(
                "INSERT INTO sessions (scope_token, session_id, version,"
                " created_at, updated_at) VALUES (?,?,?,?,?)",
                (scope.token, session_id, 0, now.isoformat(), now.isoformat()),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return SessionRecord(
            session_id=session_id,
            version=0,
            claim=None,
            created_at=now,
            updated_at=now,
        )

    async def get_session(
        self, scope: SessionScope, session_id: str
    ) -> SessionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE scope_token=? AND session_id=?",
            (scope.token, session_id),
        ).fetchone()
        if row is None:
            return None
        return self._record_from_row(scope, row)

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
        row = self._lookup(scope, session_id)
        if expected_version is not None and expected_version != row["version"]:
            raise SessionVersionConflictError(
                f"session {session_id!r} version conflict: expected "
                f"{expected_version}, authoritative version is {row['version']}"
            )
        total = self._conn.execute(
            "SELECT COUNT(*) FROM session_turns WHERE scope_token=?"
            " AND session_id=?",
            (scope.token, session_id),
        ).fetchone()[0]
        if limit is None:
            query = (
                "SELECT * FROM session_turns WHERE scope_token=? AND session_id=?"
                " AND turn_index>=? ORDER BY turn_index"
            )
            params: tuple[object, ...] = (scope.token, session_id, after)
        else:
            query = (
                "SELECT * FROM session_turns WHERE scope_token=? AND session_id=?"
                " AND turn_index>=? ORDER BY turn_index LIMIT ?"
            )
            params = (scope.token, session_id, after, limit)
        window = [
            self._decode_turn(item)
            for item in self._conn.execute(query, params).fetchall()
        ]
        next_cursor = after + len(window)
        return SessionSnapshot(
            session_id=session_id,
            version=row["version"],
            turns=tuple(window),
            next_cursor=next_cursor if next_cursor < total else None,
        )

    # -- Claim -------------------------------------------------------------

    async def get_claim(
        self, scope: SessionScope, session_id: str
    ) -> SessionRunClaim | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE scope_token=? AND session_id=?",
            (scope.token, session_id),
        ).fetchone()
        if row is None:
            return None
        claim_row = self._claim_row(scope, session_id)
        return self._claim_from_row(claim_row) if claim_row is not None else None

    async def claim_run(
        self,
        scope: SessionScope,
        session_id: str,
        run_id: str,
        *,
        expected_version: int,
    ) -> SessionRunClaim:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._lookup(scope, session_id)
            claim_row = self._claim_row(scope, session_id)
            if claim_row is not None:
                raise SessionClaimConflictError(
                    f"session {session_id!r} is already claimed by run "
                    f"{claim_row['run_id']!r}"
                )
            if self._turn_row_by_run(scope, session_id, run_id) is not None:
                raise SessionClaimConflictError(
                    f"run {run_id!r} already committed a turn to session "
                    f"{session_id!r}"
                )
            if row["version"] != expected_version:
                raise SessionVersionConflictError(
                    f"session {session_id!r} version conflict: expected "
                    f"{expected_version}, authoritative version is "
                    f"{row['version']}; no claim was created"
                )
            claimed_at = self._clock.now()
            self._conn.execute(
                "INSERT INTO session_claims (scope_token, session_id, run_id,"
                " session_version, claimed_at) VALUES (?,?,?,?,?)",
                (
                    scope.token,
                    session_id,
                    run_id,
                    row["version"],
                    claimed_at.isoformat(),
                ),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at=? WHERE scope_token=?"
                " AND session_id=?",
                (self._clock.now().isoformat(), scope.token, session_id),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return SessionRunClaim(
            session_id=session_id,
            run_id=run_id,
            session_version=expected_version,
            claimed_at=claimed_at,
        )

    async def release_claim(
        self, scope: SessionScope, session_id: str, run_id: str
    ) -> None:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._lookup(scope, session_id)
            claim_row = self._claim_row(scope, session_id)
            if claim_row is None:
                # 无 active claim：稳定 no-op（崩溃恢复的幂等重放）。
                self._conn.commit()
                return
            if claim_row["run_id"] != run_id:
                raise SessionClaimConflictError(
                    f"session {session_id!r} claim is held by run "
                    f"{claim_row['run_id']!r}, not {run_id!r}"
                )
            self._conn.execute(
                "DELETE FROM session_claims WHERE scope_token=? AND session_id=?",
                (scope.token, session_id),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at=? WHERE scope_token=?"
                " AND session_id=?",
                (self._clock.now().isoformat(), scope.token, session_id),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    # -- 提交 --------------------------------------------------------------

    async def commit_turn(
        self,
        scope: SessionScope,
        session_id: str,
        turn: SessionTurn,
        *,
        expected_version: int,
    ) -> SessionCommitResult:
        if turn.session_id != session_id:
            raise ValueError(
                "turn session binding does not match the target session"
            )
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._lookup(scope, session_id)
            existing = self._turn_row_by_run(scope, session_id, turn.run_id)
            if existing is not None:
                # run identity 幂等键：重复提交去重，无任何 mutation。
                existing_turn = self._decode_turn(existing)
                self._conn.commit()
                return SessionCommitResult(
                    status=SessionCommitStatus.COMMITTED,
                    turn=existing_turn,
                    version=row["version"],
                )
            claim_row = self._claim_row(scope, session_id)
            if claim_row is None or claim_row["run_id"] != turn.run_id:
                self._conn.commit()
                return SessionCommitResult(
                    status=SessionCommitStatus.CONFLICT,
                    turn=None,
                    version=row["version"],
                )
            if row["version"] != expected_version:
                # 冲突保留 claim，禁止自动 merge。
                self._conn.commit()
                return SessionCommitResult(
                    status=SessionCommitStatus.CONFLICT,
                    turn=None,
                    version=row["version"],
                )
            # 正文在事务内经 Codec 编码（Codec 故障 → 整个事务回滚，
            # 绝不留下 Turn 追加而 claim 未清理的半提交状态）。
            encoded = self._codec.encode(_payload_json(turn))
            self._conn.execute(
                "INSERT INTO session_turns (scope_token, session_id,"
                " turn_index, turn_id, run_id, definition_id,"
                " definition_version, created_at, encoded_payload)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    scope.token,
                    session_id,
                    row["version"],
                    turn.turn_id,
                    turn.run_id,
                    turn.definition_id,
                    turn.definition_version,
                    turn.created_at.isoformat(),
                    encoded,
                ),
            )
            self._conn.execute(
                "DELETE FROM session_claims WHERE scope_token=? AND session_id=?",
                (scope.token, session_id),
            )
            self._conn.execute(
                "UPDATE sessions SET version=?, updated_at=?"
                " WHERE scope_token=? AND session_id=?",
                (
                    row["version"] + 1,
                    self._clock.now().isoformat(),
                    scope.token,
                    session_id,
                ),
            )
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        return SessionCommitResult(
            status=SessionCommitStatus.COMMITTED,
            turn=turn,
            version=row["version"] + 1,
        )

    async def find_turn_by_run(
        self, scope: SessionScope, session_id: str, run_id: str
    ) -> SessionTurn | None:
        self._lookup(scope, session_id)
        row = self._turn_row_by_run(scope, session_id, run_id)
        return self._decode_turn(row) if row is not None else None

    # -- 测试辅助 ---------------------------------------------------------

    def raw_turn_payload_bytes(
        self, scope: SessionScope, session_id: str, run_id: str
    ) -> bytes | None:
        """返回 Turn 正文未经解码的编码字节（仅测试：验证确实经过 Codec）。"""
        row = self._turn_row_by_run(scope, session_id, run_id)
        return bytes(row["encoded_payload"]) if row is not None else None
