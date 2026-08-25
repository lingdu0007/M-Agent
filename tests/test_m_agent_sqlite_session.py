"""Ticket 13：durable SQLite SessionStore（共享 contract kit 绑定）。

本文件把 Ticket 12 的实现无关契约套件绑定到 SQLiteSessionStore，并
覆盖 SQLite 特有的持久化行为：

- 共享 SessionStore 行为契约（Scope、version、pagination、claim、
  CAS commit、幂等去重）——与 InMemory 同一份套件；
- 共享 SessionRunner 组合行为契约——SQLite session store 绑定；
- reopen 持久化：状态、claim（无 TTL）与受保护 Turn payload 跨进程
  存活；
- Session Payload 独立保护：可搜索 metadata 不含对话正文、错误 key
  fail-closed、错误信息不泄露正文；
- SessionRunner 依据权威 RunStore 重启对账 missing / active /
  waiting / terminal 四类 Run。
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from m_agent.adapters import (
    DeterministicModelAdapter,
    FakeClock,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent.companion import (
    SessionClaimConflictError,
    SessionCommitStatus,
    SessionNotFoundError,
    SessionRunResult,
    SessionRunner,
    SessionScope,
    SessionTurn,
    SQLiteSessionStore,
)
from m_agent.runtime import (
    AgentDefinition,
    DefinitionRegistry,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelRequirements,
    RunNotFoundError,
    RunResolution,
    RunStatus,
    Runner,
    ToolCallingMode,
)
from m_agent.runtime import PayloadCodec

from session_conversation_contract import (
    SessionConversationContractMixin,
    SessionConversationHarness,
    ToolWaitingModelAdapter,
    UncertainEffectTool,
)
from session_store_contract import SessionStoreContractMixin, build_turn

_SCOPE = SessionScope(token="app-scope-a")
_OTHER_SCOPE = SessionScope(token="app-scope-b")


class KeyedSessionCodec(PayloadCodec):
    """带 key 指纹的测试 Codec：错误 key 在解码时 fail closed。"""

    name = "test-keyed-session"
    _PREFIX = b"keyed-session:"
    _FINGERPRINT_LEN = 8

    def __init__(self, key: str) -> None:
        self._key = key.encode("utf-8")
        self._fingerprint = hashlib.sha256(self._key).digest()[: self._FINGERPRINT_LEN]

    def _stream(self, size: int) -> bytes:
        repeats = size // len(self._key) + 1
        return (self._key * repeats)[:size]

    def encode(self, payload: str) -> bytes:
        data = payload.encode("utf-8")
        masked = bytes(a ^ b for a, b in zip(data, self._stream(len(data))))
        return self._PREFIX + self._fingerprint + masked

    def decode(self, encoded: bytes) -> str:
        prefix_len = len(self._PREFIX)
        if not encoded.startswith(self._PREFIX):
            raise ValueError("session payload does not use the keyed-session codec")
        fingerprint = encoded[prefix_len : prefix_len + self._FINGERPRINT_LEN]
        if fingerprint != self._fingerprint:
            raise ValueError(
                "session payload was written with a different codec key"
            )
        body = encoded[prefix_len + self._FINGERPRINT_LEN :]
        plain = bytes(a ^ b for a, b in zip(body, self._stream(len(body))))
        return plain.decode("utf-8")


class _SQLiteDirectory:
    """每个测试一个临时目录 + 递增的数据库文件名。"""

    def __init__(self, test: unittest.TestCase) -> None:
        self._directory = tempfile.TemporaryDirectory()
        test.addCleanup(self._directory.cleanup)
        self._root = Path(self._directory.name)
        self._counter = 0

    def new_path(self) -> Path:
        self._counter += 1
        return self._root / f"session-{self._counter}.sqlite3"


class SQLiteSessionStoreContractTests(
    SessionStoreContractMixin, unittest.IsolatedAsyncioTestCase
):
    """Ticket 13 AC 1：共享 SessionStore 契约套件的 SQLite 绑定。"""

    def setUp(self) -> None:
        self._paths = _SQLiteDirectory(self)

    def make_store(self, clock=None):
        store = SQLiteSessionStore(
            self._paths.new_path(), payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        self.addCleanup(store.close)
        return store


class SQLiteSessionConversationTests(
    SessionConversationContractMixin, unittest.IsolatedAsyncioTestCase
):
    """Ticket 13 AC 1：共享组合行为契约套件的 SQLite 绑定。"""

    def setUp(self) -> None:
        self._paths = _SQLiteDirectory(self)

    def make_session_store(self):
        store = SQLiteSessionStore(
            self._paths.new_path(), payload_codec=PlaintextPayloadCodec()
        )
        self.addCleanup(store.close)
        return store


class SQLiteSessionDurabilityTests(unittest.IsolatedAsyncioTestCase):
    """reopen 持久化、claim 无 TTL 与重启对账（AC 2 / AC 3）。"""

    def setUp(self) -> None:
        self._paths = _SQLiteDirectory(self)

    def _store(self, path=None, codec=None, clock=None):
        target = path if path is not None else self._paths.new_path()
        store = SQLiteSessionStore(
            target, payload_codec=codec or PlaintextPayloadCodec(), clock=clock
        )
        self.addCleanup(store.close)
        return store

    async def test_state_survives_reopen(self) -> None:
        path = self._paths.new_path()
        store = SQLiteSessionStore(path, payload_codec=PlaintextPayloadCodec())
        await store.create_session(_SCOPE, "session-1")
        claim = await store.claim_run(
            _SCOPE, "session-1", "run-1", expected_version=0
        )
        await store.commit_turn(
            _SCOPE,
            "session-1",
            build_turn("session-1", "run-1"),
            expected_version=claim.session_version,
        )
        store.close()

        reopened = SQLiteSessionStore(path, payload_codec=PlaintextPayloadCodec())
        self.addCleanup(reopened.close)
        record = await reopened.get_session(_SCOPE, "session-1")
        self.assertIsNotNone(record)
        self.assertEqual(record.version, 1)
        self.assertIsNone(record.claim)
        snapshot = await reopened.read_snapshot(_SCOPE, "session-1")
        self.assertEqual([turn.run_id for turn in snapshot.turns], ["run-1"])
        self.assertEqual(
            await reopened.find_turn_by_run(_SCOPE, "session-1", "run-1"),
            snapshot.turns[0],
        )

    async def test_claim_has_no_ttl_and_survives_reopen(self) -> None:
        path = self._paths.new_path()
        store = SQLiteSessionStore(path, payload_codec=PlaintextPayloadCodec())
        await store.create_session(_SCOPE, "session-1")
        await store.claim_run(_SCOPE, "session-1", "run-1", expected_version=0)
        store.close()

        # 重启（reopen）后 claim 原样存活：没有任何 TTL 使其失效。
        reopened = SQLiteSessionStore(path, payload_codec=PlaintextPayloadCodec())
        self.addCleanup(reopened.close)
        claim = await reopened.get_claim(_SCOPE, "session-1")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.run_id, "run-1")
        # 第二个 run 不能静默占用同一 Session。
        with self.assertRaises(SessionClaimConflictError):
            await reopened.claim_run(
                _SCOPE, "session-1", "run-2", expected_version=0
            )

    async def test_conflict_outcome_leaves_no_partial_state_across_reopen(
        self,
    ) -> None:
        path = self._paths.new_path()
        store = SQLiteSessionStore(path, payload_codec=PlaintextPayloadCodec())
        await store.create_session(_SCOPE, "session-1")
        claim = await store.claim_run(
            _SCOPE, "session-1", "run-1", expected_version=0
        )
        # 版本 CAS 冲突：CONFLICT，无任何 mutation。
        result = await store.commit_turn(
            _SCOPE,
            "session-1",
            build_turn("session-1", "run-1"),
            expected_version=claim.session_version + 5,
        )
        self.assertIs(result.status, SessionCommitStatus.CONFLICT)
        store.close()

        reopened = SQLiteSessionStore(path, payload_codec=PlaintextPayloadCodec())
        self.addCleanup(reopened.close)
        snapshot = await reopened.read_snapshot(_SCOPE, "session-1")
        self.assertEqual(snapshot.version, 0)
        self.assertEqual(snapshot.turns, ())
        self.assertEqual(
            (await reopened.get_claim(_SCOPE, "session-1")).run_id, "run-1"
        )

    async def test_reconciliation_of_missing_run_releases_claim(self) -> None:
        # Crash window 1（claim 后 Run 创建前）：权威 RunStore 确认
        # 「从未创建」→ claim 被清理；此前第二个 Run 只能冲突。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_db = root / "session.sqlite3"
            run_db = root / "runs.sqlite3"
            store = SQLiteSessionStore(
                session_db, payload_codec=PlaintextPayloadCodec()
            )
            await store.create_session(_SCOPE, "session-1")
            await store.claim_run(_SCOPE, "session-1", "ghost-run", expected_version=0)
            store.close()

            run_store = SQLiteRunStore(run_db, payload_codec=PlaintextPayloadCodec())
            self.addCleanup(run_store.close)
            session_store = SQLiteSessionStore(
                session_db, payload_codec=PlaintextPayloadCodec()
            )
            self.addCleanup(session_store.close)
            runner = SessionRunner(
                runner=self._runner_for(run_store),
                session_store=session_store,
            )

            # 第二条消息不得越过未解决工作。
            with self.assertRaises(SessionClaimConflictError):
                await runner.submit(
                    _SCOPE, "session-1", "assistant", "1.0", "overtake"
                )

            # 权威对账：Run 从未创建 → claim 释放 + RunNotFoundError。
            with self.assertRaises(RunNotFoundError):
                await runner.resume(_SCOPE, "session-1")
            self.assertIsNone(await session_store.get_claim(_SCOPE, "session-1"))

            # claim 释放后对话可继续。
            followup = await runner.submit(
                _SCOPE, "session-1", "assistant", "1.0", "next message"
            )
            self.assertIs(followup.commit_status, SessionCommitStatus.COMMITTED)

    async def test_reconciliation_resumes_active_created_run(self) -> None:
        # Crash 后 Run 处于非终态（CREATED）：重启后 resume 推进并提交。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_db = root / "session.sqlite3"
            run_db = root / "runs.sqlite3"
            run_store = SQLiteRunStore(run_db, payload_codec=PlaintextPayloadCodec())
            session_store = SQLiteSessionStore(
                session_db, payload_codec=PlaintextPayloadCodec()
            )
            core_runner = self._runner_for(run_store)
            await session_store.create_session(_SCOPE, "session-1")
            snapshot = await session_store.read_snapshot(_SCOPE, "session-1")
            await session_store.claim_run(
                _SCOPE, "session-1", "run-active", expected_version=snapshot.version
            )
            await core_runner.create_run(
                "assistant",
                "1.0",
                "crashed mid flight",
                run_id="run-active",
                history=(),
            )
            run_store.close()
            session_store.close()

            # 重启：两个 Store 都 reopen，只通过公开 API 对账。
            reopened_runs = SQLiteRunStore(
                run_db, payload_codec=PlaintextPayloadCodec()
            )
            self.addCleanup(reopened_runs.close)
            reopened_sessions = SQLiteSessionStore(
                session_db, payload_codec=PlaintextPayloadCodec()
            )
            self.addCleanup(reopened_sessions.close)
            runner = SessionRunner(
                runner=self._runner_for(reopened_runs),
                session_store=reopened_sessions,
            )
            result = await runner.resume(_SCOPE, "session-1")
            self.assertIs(result.run.status, RunStatus.SUCCEEDED)
            self.assertIs(result.commit_status, SessionCommitStatus.COMMITTED)
            self.assertIsNone(await reopened_sessions.get_claim(_SCOPE, "session-1"))

    async def test_reconciliation_keeps_claim_for_waiting_run(self) -> None:
        # 长时间 WAITING：claim 保留（无 TTL），另一条消息不得越过；
        # 显式 resolution 之后 resume 完成提交。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_db = root / "session.sqlite3"
            run_db = root / "runs.sqlite3"
            run_store = SQLiteRunStore(run_db, payload_codec=PlaintextPayloadCodec())
            session_store = SQLiteSessionStore(
                session_db, payload_codec=PlaintextPayloadCodec()
            )
            core_runner = self._waiting_runner_for(run_store)
            await session_store.create_session(_SCOPE, "session-1")
            waiting = await SessionRunner(
                runner=core_runner, session_store=session_store
            ).submit(_SCOPE, "session-1", "careful", "1.0", "needs approval")
            self.assertIs(waiting.run.status, RunStatus.WAITING)
            run_store.close()
            session_store.close()

            # 确定性接管：读取原进程 lease 到期时间，用 FakeClock 越过。
            probe = SQLiteRunStore(run_db, payload_codec=PlaintextPayloadCodec())
            lease = await probe.get_lease(waiting.run.run_id)
            self.assertIsNotNone(lease)
            clock = FakeClock(
                start=(lease.expires_at if lease is not None else None)
                + timedelta(seconds=1)
            )
            probe.close()

            reopened_runs = SQLiteRunStore(
                run_db, payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            self.addCleanup(reopened_runs.close)
            reopened_sessions = SQLiteSessionStore(
                session_db, payload_codec=PlaintextPayloadCodec()
            )
            self.addCleanup(reopened_sessions.close)
            waiting_runner = self._waiting_runner_for(reopened_runs)
            runner = SessionRunner(
                runner=waiting_runner, session_store=reopened_sessions
            )

            # 重启后 WAITING 仍保留 claim。
            claim = await reopened_sessions.get_claim(_SCOPE, "session-1")
            self.assertIsNotNone(claim)
            self.assertEqual(claim.run_id, waiting.run.run_id)
            with self.assertRaises(SessionClaimConflictError):
                await runner.submit(_SCOPE, "session-1", "assistant", "1.0", "skip")

            # resume 返回 WAITING（不提交、不释放）。
            still_waiting = await runner.resume(_SCOPE, "session-1")
            self.assertIs(still_waiting.run.status, RunStatus.WAITING)
            self.assertIs(
                still_waiting.commit_status, SessionCommitStatus.NOT_READY
            )

            resolved = await waiting_runner.resolve_run(
                waiting.run.run_id,
                RunResolution.confirm_step(result="approved"),
                expected_version=still_waiting.run.version,
            )
            self.assertIs(resolved.status, RunStatus.SUCCEEDED)
            committed = await runner.resume(_SCOPE, "session-1")
            self.assertIs(committed.commit_status, SessionCommitStatus.COMMITTED)
            snapshot = await reopened_sessions.read_snapshot(_SCOPE, "session-1")
            self.assertEqual(snapshot.version, 1)
            self.assertEqual(snapshot.turns[0].run_id, waiting.run.run_id)

    async def test_reconciliation_of_terminal_failure_releases_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_db = root / "session.sqlite3"
            run_db = root / "runs.sqlite3"
            run_store = SQLiteRunStore(run_db, payload_codec=PlaintextPayloadCodec())
            session_store = SQLiteSessionStore(
                session_db, payload_codec=PlaintextPayloadCodec()
            )
            core_runner = self._runner_for(run_store)
            await session_store.create_session(_SCOPE, "session-1")
            await core_runner.create_run(
                "assistant", "1.0", "doomed", run_id="run-failed", history=()
            )
            # 直接把 Run 推向 FAILED（模拟崩溃前已终态失败）。
            await run_store.transition_run(
                "run-failed", 1, status=RunStatus.FAILED, error_code="X"
            )
            await session_store.claim_run(
                _SCOPE, "session-1", "run-failed", expected_version=0
            )
            run_store.close()
            session_store.close()

            reopened_runs = SQLiteRunStore(
                run_db, payload_codec=PlaintextPayloadCodec()
            )
            self.addCleanup(reopened_runs.close)
            reopened_sessions = SQLiteSessionStore(
                session_db, payload_codec=PlaintextPayloadCodec()
            )
            self.addCleanup(reopened_sessions.close)
            result = await SessionRunner(
                runner=self._runner_for(reopened_runs),
                session_store=reopened_sessions,
            ).resume(_SCOPE, "session-1")
            self.assertIs(result.run.status, RunStatus.FAILED)
            self.assertIs(result.commit_status, SessionCommitStatus.NOT_READY)
            self.assertIsNone(await reopened_sessions.get_claim(_SCOPE, "session-1"))
            snapshot = await reopened_sessions.read_snapshot(_SCOPE, "session-1")
            self.assertEqual(snapshot.turns, ())

    # -- harness -------------------------------------------------------

    @staticmethod
    def _runner_for(store: SQLiteRunStore) -> Runner:
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="assistant",
                version="1.0",
                instructions="be brief",
                model_adapter=_PlainModelAdapter(),
            )
        )
        return Runner(registry=registry, store=store)

    @staticmethod
    def _waiting_runner_for(store: SQLiteRunStore) -> Runner:
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="assistant",
                version="1.0",
                instructions="be brief",
                model_adapter=_PlainModelAdapter(),
            )
        )
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="careful",
                version="1.0",
                instructions="use the tool",
                model_requirements=ModelRequirements(
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    )
                ),
                model_adapter=ToolWaitingModelAdapter(),
                tools=(UncertainEffectTool(),),
            )
        )
        return Runner(registry=registry, store=store)


class _PlainModelAdapter(DeterministicModelAdapter):
    deterministic: bool = True

    def __init__(self) -> None:
        super().__init__(responses=("session answer",))

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        return ModelResponse(content="session answer")


class SQLiteSessionPayloadProtectionTests(unittest.IsolatedAsyncioTestCase):
    """Ticket 13 AC 6：Session Payload 独立保护与 fail-closed。"""

    def setUp(self) -> None:
        self._paths = _SQLiteDirectory(self)

    async def test_searchable_metadata_never_contains_conversation_text(
        self,
    ) -> None:
        secret_input = "SECRET-INPUT-TICKET13"
        secret_output = "SECRET-OUTPUT-TICKET13"
        path = self._paths.new_path()
        codec = KeyedSessionCodec("session-secret-key")
        store = SQLiteSessionStore(path, payload_codec=codec)
        await store.create_session(_SCOPE, "session-1")
        claim = await store.claim_run(_SCOPE, "session-1", "run-1", expected_version=0)
        turn = SessionTurn(
            turn_id="turn-1",
            session_id="session-1",
            run_id="run-1",
            definition_id="assistant",
            definition_version="1.0",
            user_input=secret_input,
            assistant_output=secret_output,
            created_at=claim.claimed_at,
        )
        await store.commit_turn(
            _SCOPE, "session-1", turn, expected_version=claim.session_version
        )

        # 受保护 payload 经 Codec 落库（非明文）。
        raw = store.raw_turn_payload_bytes(_SCOPE, "session-1", "run-1")
        self.assertIsNotNone(raw)
        self.assertNotIn(secret_input.encode("utf-8"), raw)
        self.assertNotIn(secret_output.encode("utf-8"), raw)

        # 可搜索 metadata（所有非 payload 列）不含对话正文。
        metadata = self._metadata_text(path)
        self.assertNotIn(secret_input, metadata)
        self.assertNotIn(secret_output, metadata)

        # 正确 key 读取完整还原正文。
        snapshot = await store.read_snapshot(_SCOPE, "session-1")
        self.assertEqual(snapshot.turns[0].user_input, secret_input)
        self.assertEqual(snapshot.turns[0].assistant_output, secret_output)
        store.close()

    async def test_wrong_key_fails_closed_on_read(self) -> None:
        path = self._paths.new_path()
        store = SQLiteSessionStore(
            path, payload_codec=KeyedSessionCodec("correct-key")
        )
        await store.create_session(_SCOPE, "session-1")
        claim = await store.claim_run(_SCOPE, "session-1", "run-1", expected_version=0)
        await store.commit_turn(
            _SCOPE,
            "session-1",
            build_turn("session-1", "run-1"),
            expected_version=claim.session_version,
        )
        store.close()

        wrong = SQLiteSessionStore(
            path, payload_codec=KeyedSessionCodec("wrong-key")
        )
        self.addCleanup(wrong.close)
        # fail closed：错误 key 的读取确定性失败，绝不静默返回正文。
        with self.assertRaises(ValueError):
            await wrong.read_snapshot(_SCOPE, "session-1")
        with self.assertRaises(ValueError):
            await wrong.find_turn_by_run(_SCOPE, "session-1", "run-1")

        # 可搜索 metadata 仍可读取（版本/claim 不依赖 payload key）。
        record = await wrong.get_session(_SCOPE, "session-1")
        self.assertIsNotNone(record)
        self.assertEqual(record.version, 1)

        # 正确 key 仍可完整读取。
        right = SQLiteSessionStore(
            path, payload_codec=KeyedSessionCodec("correct-key")
        )
        self.addCleanup(right.close)
        snapshot = await right.read_snapshot(_SCOPE, "session-1")
        self.assertEqual(len(snapshot.turns), 1)

    async def test_error_messages_do_not_leak_conversation_text(self) -> None:
        secret = "LEAK-CANARY-TICKET13"
        store = SQLiteSessionStore(
            self._paths.new_path(), payload_codec=KeyedSessionCodec("k")
        )
        await store.create_session(_SCOPE, "session-1")
        claim = await store.claim_run(_SCOPE, "session-1", "run-1", expected_version=0)
        turn = SessionTurn(
            turn_id="turn-1",
            session_id="session-1",
            run_id="run-1",
            definition_id="assistant",
            definition_version="1.0",
            user_input=secret,
            assistant_output=secret,
            created_at=claim.claimed_at,
        )
        # 先制造一个冲突提交（claim 持有者是 run-1，版本故意错）。
        conflict = await store.commit_turn(
            _SCOPE, "session-1", turn, expected_version=claim.session_version + 9
        )
        self.assertIs(conflict.status, SessionCommitStatus.CONFLICT)

        errors: list[str] = []
        for attempt in (
            lambda: store.read_snapshot(_SCOPE, "session-1", expected_version=99),
            lambda: store.claim_run(
                _SCOPE, "session-1", "run-2", expected_version=99
            ),
            lambda: store.release_claim(_SCOPE, "session-1", "run-2"),
            lambda: store.read_snapshot(_OTHER_SCOPE, "session-1"),
        ):
            try:
                await attempt()
            except Exception as error:  # noqa: BLE001 - 收集错误文本
                errors.append(str(error))
        self.assertTrue(errors)
        for message in errors:
            self.assertNotIn(secret, message)

    @staticmethod
    def _metadata_text(path: Path) -> str:
        connection = sqlite3.connect(path)
        try:
            statements = connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
            ).fetchall()
            values: list[object] = list(statements)
            for table in ("sessions", "session_claims", "session_turns"):
                values.extend(
                    connection.execute(
                        f"SELECT turn_index, turn_id, session_id, run_id,"
                        f" definition_id, definition_version, created_at"
                        f" FROM {table}"  # noqa: S608 - fixed table/columns
                        if table == "session_turns"
                        else f"SELECT * FROM {table}"  # noqa: S608
                    ).fetchall()
                )
            return repr(values)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
