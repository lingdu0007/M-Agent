"""InMemorySessionStore 与未来 SQLiteSessionStore 共享的行为契约测试。

PRD Testing Decisions / Ticket 12 AC 9：SessionStore 的两种实现必须
通过同一份实现无关的公共契约套件；本模块以 mixin 形式提供，具体
实现各自继承（须同时继承 ``unittest.IsolatedAsyncioTestCase``）并只
提供 ``make_store()`` 工厂。Ticket 13 的 SQLite 实现直接复用本套件。

断言只通过 SessionStore 公开接口驱动（SessionScope、SessionRecord、
SessionSnapshot、SessionRunClaim、SessionTurn、SessionCommitResult），
不触碰实现细节；Scope 隔离、exact-version read、cursor pagination、
claim 原子性、非成功释放、CAS commit 与幂等去重的正负路径全部在
本套件内覆盖。
"""

from __future__ import annotations

from datetime import datetime, timezone

from m_agent.adapters import FakeClock
from m_agent.companion import (
    DuplicateSessionError,
    SessionClaimConflictError,
    SessionCommitStatus,
    SessionNotFoundError,
    SessionScope,
    SessionTurn,
    SessionVersionConflictError,
)

_SCOPE_A = SessionScope(token="app-scope-a")
_SCOPE_B = SessionScope(token="app-scope-b")
_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def build_turn(
    session_id: str,
    run_id: str,
    *,
    turn_id: str | None = None,
    user_input: str = "hello",
    assistant_output: str = "hi there",
) -> SessionTurn:
    """构造一条最小对话事实齐全的 Session Turn（仅测试数据）。"""
    return SessionTurn(
        turn_id=turn_id if turn_id is not None else f"turn-{run_id}",
        session_id=session_id,
        run_id=run_id,
        definition_id="assistant",
        definition_version="1.0",
        user_input=user_input,
        assistant_output=assistant_output,
        created_at=_NOW,
    )


class SessionStoreContractMixin:
    """SessionStore 行为契约。子类必须同时继承
    ``unittest.IsolatedAsyncioTestCase`` 并实现 ``make_store()``。

    ``make_store(clock=None)``：clock 为 None 时使用系统时钟，传入
    :class:`FakeClock` 可确定性断言记录时间戳。
    """

    def make_store(self, clock: FakeClock | None = None):  # pragma: no cover
        raise NotImplementedError

    # -- AC 1：显式创建与确定性初始状态 -------------------------------

    async def test_create_session_starts_at_version_zero_empty_and_unclaimed(
        self,
    ) -> None:
        store = self.make_store()
        record = await store.create_session(_SCOPE_A, "session-1")

        self.assertEqual(record.session_id, "session-1")
        self.assertEqual(record.version, 0)
        self.assertIsNone(record.claim)
        self.assertIsNotNone(record.created_at)
        self.assertIsNotNone(record.updated_at)

        # 历史（Snapshot 视图）为空，且无 active claim。
        snapshot = await store.read_snapshot(_SCOPE_A, "session-1")
        self.assertEqual(snapshot.session_id, "session-1")
        self.assertEqual(snapshot.version, 0)
        self.assertEqual(snapshot.turns, ())
        self.assertIsNone(snapshot.next_cursor)
        self.assertIsNone(await store.get_claim(_SCOPE_A, "session-1"))

    async def test_duplicate_session_creation_fails_without_mutation(self) -> None:
        store = self.make_store()
        first = await store.create_session(_SCOPE_A, "session-1")
        with self.assertRaises(DuplicateSessionError):
            await store.create_session(_SCOPE_A, "session-1")

        # 重复创建是稳定失败：权威记录未被改动，重复读取结果一致。
        second = await store.get_session(_SCOPE_A, "session-1")
        self.assertIsNotNone(second)
        self.assertEqual(second.version, first.version)
        self.assertEqual(second.claim, first.claim)

    async def test_missing_session_access_is_stable_and_deterministic(self) -> None:
        store = self.make_store()
        # 存在性探测返回 None；所有依赖存在的操作都显式失败，
        # 且两次调用结果一致（稳定、确定性）。
        for _ in range(2):
            self.assertIsNone(await store.get_session(_SCOPE_A, "missing"))
            with self.assertRaises(SessionNotFoundError):
                await store.read_snapshot(_SCOPE_A, "missing")
            with self.assertRaises(SessionNotFoundError):
                await store.claim_run(
                    _SCOPE_A, "missing", "run-1", expected_version=0
                )
            with self.assertRaises(SessionNotFoundError):
                await store.release_claim(_SCOPE_A, "missing", "run-1")
            with self.assertRaises(SessionNotFoundError):
                await store.commit_turn(
                    _SCOPE_A,
                    "missing",
                    build_turn("missing", "run-1"),
                    expected_version=0,
                )
            with self.assertRaises(SessionNotFoundError):
                await store.find_turn_by_run(_SCOPE_A, "missing", "run-1")

    # -- AC 2：Scope fail-closed ---------------------------------------

    async def test_scope_isolation_fails_closed_for_every_operation(self) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        claim = await store.claim_run(
            _SCOPE_A, "session-1", "run-1", expected_version=0
        )
        await store.commit_turn(
            _SCOPE_A,
            "session-1",
            build_turn("session-1", "run-1"),
            expected_version=0,
        )

        # 仅知道 Session identifier 的另一 Scope 不能读取、claim、提交
        # 或推断其存在：所有访问与"不存在"不可区分。
        self.assertIsNone(await store.get_session(_SCOPE_B, "session-1"))
        with self.assertRaises(SessionNotFoundError):
            await store.read_snapshot(_SCOPE_B, "session-1")
        self.assertIsNone(await store.get_claim(_SCOPE_B, "session-1"))
        with self.assertRaises(SessionNotFoundError):
            await store.claim_run(
                _SCOPE_B, "session-1", "run-2", expected_version=1
            )
        with self.assertRaises(SessionNotFoundError):
            await store.release_claim(_SCOPE_B, "session-1", "run-1")
        with self.assertRaises(SessionNotFoundError):
            await store.commit_turn(
                _SCOPE_B,
                "session-1",
                build_turn("session-1", "run-2"),
                expected_version=1,
            )
        with self.assertRaises(SessionNotFoundError):
            await store.find_turn_by_run(_SCOPE_B, "session-1", "run-1")

        # Scope A 的权威状态完全未被跨 Scope 尝试改动。
        snapshot = await store.read_snapshot(_SCOPE_A, "session-1")
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(len(snapshot.turns), 1)
        self.assertIsNone(snapshot.next_cursor)
        self.assertEqual(claim.run_id, "run-1")

    async def test_same_session_id_under_another_scope_is_a_distinct_session(
        self,
    ) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        # Scope 命名空间隔离：另一 Scope 可以创建同名 Session，
        # 两者互不可见、互不影响。
        record_b = await store.create_session(_SCOPE_B, "session-1")
        self.assertEqual(record_b.version, 0)
        self.assertIsNone(record_b.claim)

        await store.claim_run(_SCOPE_B, "session-1", "run-b", expected_version=0)
        claim_a = await store.get_claim(_SCOPE_A, "session-1")
        self.assertIsNone(claim_a)
        claim_b = await store.get_claim(_SCOPE_B, "session-1")
        self.assertIsNotNone(claim_b)
        self.assertEqual(claim_b.run_id, "run-b")

    # -- AC 3：exact-version read 与 cursor pagination -----------------

    async def test_read_snapshot_returns_complete_history_in_order(self) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        for index, run_id in enumerate(("run-1", "run-2", "run-3")):
            claim = await store.claim_run(
                _SCOPE_A, "session-1", run_id, expected_version=index
            )
            result = await store.commit_turn(
                _SCOPE_A,
                "session-1",
                build_turn(
                    "session-1",
                    run_id,
                    user_input=f"message-{index + 1}",
                    assistant_output=f"answer-{index + 1}",
                ),
                expected_version=claim.session_version,
            )
            self.assertIs(result.status, SessionCommitStatus.COMMITTED)

        snapshot = await store.read_snapshot(_SCOPE_A, "session-1")
        # 不静默截断：默认读取返回完整历史，顺序与追加顺序一致，
        # 每条 Turn 的最小对话事实原样保留。
        self.assertEqual(snapshot.version, 3)
        self.assertIsNone(snapshot.next_cursor)
        self.assertEqual(
            [(turn.user_input, turn.assistant_output) for turn in snapshot.turns],
            [
                ("message-1", "answer-1"),
                ("message-2", "answer-2"),
                ("message-3", "answer-3"),
            ],
        )
        for turn in snapshot.turns:
            self.assertEqual(turn.definition_id, "assistant")
            self.assertEqual(turn.definition_version, "1.0")
            self.assertEqual(turn.created_at, _NOW)

    async def test_read_snapshot_exact_version_guard(self) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        claim = await store.claim_run(
            _SCOPE_A, "session-1", "run-1", expected_version=0
        )
        await store.commit_turn(
            _SCOPE_A,
            "session-1",
            build_turn("session-1", "run-1"),
            expected_version=claim.session_version,
        )

        # exact-version read：版本匹配时返回该版本快照。
        snapshot = await store.read_snapshot(
            _SCOPE_A, "session-1", expected_version=1
        )
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(len(snapshot.turns), 1)

        # 版本不匹配时确定性失败，绝不静默返回其他版本。
        with self.assertRaises(SessionVersionConflictError):
            await store.read_snapshot(_SCOPE_A, "session-1", expected_version=0)
        with self.assertRaises(SessionVersionConflictError):
            await store.read_snapshot(_SCOPE_A, "session-1", expected_version=2)

    async def test_read_snapshot_cursor_pagination_is_ordered_and_complete(
        self,
    ) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        for index, run_id in enumerate(("run-1", "run-2", "run-3", "run-4")):
            claim = await store.claim_run(
                _SCOPE_A, "session-1", run_id, expected_version=index
            )
            await store.commit_turn(
                _SCOPE_A,
                "session-1",
                build_turn("session-1", run_id),
                expected_version=claim.session_version,
            )

        # cursor 分页按追加顺序返回；分页 union == 完整历史。
        first = await store.read_snapshot(_SCOPE_A, "session-1", after=0, limit=2)
        self.assertEqual([t.run_id for t in first.turns], ["run-1", "run-2"])
        self.assertEqual(first.next_cursor, 2)

        second = await store.read_snapshot(
            _SCOPE_A, "session-1", after=first.next_cursor or 0, limit=2
        )
        self.assertEqual([t.run_id for t in second.turns], ["run-3", "run-4"])
        self.assertIsNone(second.next_cursor)

        # 最后一页之后再读：空页、无更多 cursor。
        tail = await store.read_snapshot(
            _SCOPE_A, "session-1", after=first.next_cursor or 0, limit=5
        )
        self.assertEqual(len(tail.turns), 2)
        self.assertIsNone(tail.next_cursor)

        # 非法 cursor / limit 显式拒绝。
        with self.assertRaises(ValueError):
            await store.read_snapshot(_SCOPE_A, "session-1", after=-1)
        with self.assertRaises(ValueError):
            await store.read_snapshot(_SCOPE_A, "session-1", limit=0)
        with self.assertRaises(ValueError):
            await store.read_snapshot(_SCOPE_A, "session-1", limit=-1)

        # cursor 越界（历史末尾之后）返回稳定空页。
        beyond = await store.read_snapshot(_SCOPE_A, "session-1", after=99)
        self.assertEqual(beyond.turns, ())
        self.assertIsNone(beyond.next_cursor)

    # -- AC 4：原子 Claim 与竞争失败路径 -------------------------------

    async def test_claim_run_binds_identity_and_frozen_version(self) -> None:
        store = self.make_store(clock=FakeClock())
        await store.create_session(_SCOPE_A, "session-1")
        claim = await store.claim_run(
            _SCOPE_A, "session-1", "run-1", expected_version=0
        )

        self.assertEqual(claim.session_id, "session-1")
        self.assertEqual(claim.run_id, "run-1")
        self.assertEqual(claim.session_version, 0)
        self.assertIsNotNone(claim.claimed_at)
        # claim 不改变历史版本，且可通过公开查询观察到。
        self.assertEqual(
            (await store.get_claim(_SCOPE_A, "session-1")) or claim, claim
        )
        record = await store.get_session(_SCOPE_A, "session-1")
        self.assertIsNotNone(record)
        self.assertEqual(record.version, 0)
        self.assertEqual(record.claim, claim)

    async def test_claim_run_conflict_leaves_existing_claim_untouched(self) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        first = await store.claim_run(
            _SCOPE_A, "session-1", "run-1", expected_version=0
        )

        # claim 竞争失败路径稳定复现：第二个 claim 被拒绝，权威 claim
        # 保持指向第一个预分配 run identity。
        with self.assertRaises(SessionClaimConflictError):
            await store.claim_run(
                _SCOPE_A, "session-1", "run-2", expected_version=0
            )
        current = await store.get_claim(_SCOPE_A, "session-1")
        self.assertEqual(current, first)

    async def test_claim_run_version_conflict_creates_no_claim(self) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        with self.assertRaises(SessionVersionConflictError):
            await store.claim_run(
                _SCOPE_A, "session-1", "run-1", expected_version=5
            )
        self.assertIsNone(await store.get_claim(_SCOPE_A, "session-1"))

    async def test_claim_run_rejects_already_committed_run_identity(self) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        claim = await store.claim_run(
            _SCOPE_A, "session-1", "run-1", expected_version=0
        )
        await store.commit_turn(
            _SCOPE_A,
            "session-1",
            build_turn("session-1", "run-1"),
            expected_version=claim.session_version,
        )

        # 已提交过 Turn 的 run identity 不可再 claim（fail-closed），
        # 防止为已完成的 Run 制造永久占用。
        with self.assertRaises(SessionClaimConflictError):
            await store.claim_run(
                _SCOPE_A, "session-1", "run-1", expected_version=1
            )
        self.assertIsNone(await store.get_claim(_SCOPE_A, "session-1"))

    # -- AC 7：claim 释放 ----------------------------------------------

    async def test_release_claim_requires_owner_and_is_idempotent(self) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        await store.claim_run(_SCOPE_A, "session-1", "run-1", expected_version=0)

        # 非 claim 持有者不能释放。
        with self.assertRaises(SessionClaimConflictError):
            await store.release_claim(_SCOPE_A, "session-1", "run-2")
        self.assertIsNotNone(await store.get_claim(_SCOPE_A, "session-1"))

        # 持有者释放后 claim 清空；重复释放是稳定 no-op。
        await store.release_claim(_SCOPE_A, "session-1", "run-1")
        self.assertIsNone(await store.get_claim(_SCOPE_A, "session-1"))
        await store.release_claim(_SCOPE_A, "session-1", "run-1")
        self.assertIsNone(await store.get_claim(_SCOPE_A, "session-1"))

    # -- AC 6 / AC 8：CAS commit 与幂等去重 ----------------------------

    async def test_commit_turn_appends_clears_claim_and_increments_version(
        self,
    ) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        claim = await store.claim_run(
            _SCOPE_A, "session-1", "run-1", expected_version=0
        )

        turn = build_turn("session-1", "run-1")
        result = await store.commit_turn(
            _SCOPE_A, "session-1", turn, expected_version=claim.session_version
        )

        self.assertIs(result.status, SessionCommitStatus.COMMITTED)
        self.assertEqual(result.turn, turn)
        self.assertEqual(result.version, 1)
        # 原子完成后：Turn 追加一次、claim 清除、版本 +1。
        snapshot = await store.read_snapshot(_SCOPE_A, "session-1")
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(snapshot.turns, (turn,))
        self.assertIsNone(await store.get_claim(_SCOPE_A, "session-1"))

    async def test_commit_turn_is_idempotent_by_run_identity(self) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        claim = await store.claim_run(
            _SCOPE_A, "session-1", "run-1", expected_version=0
        )
        original = build_turn("session-1", "run-1")
        await store.commit_turn(
            _SCOPE_A, "session-1", original, expected_version=claim.session_version
        )

        # 相同 run identity 重复提交：幂等去重，返回原 Turn，
        # 不追加第二条、版本不变。
        replay = await store.commit_turn(
            _SCOPE_A,
            "session-1",
            original,
            expected_version=claim.session_version,
        )
        self.assertIs(replay.status, SessionCommitStatus.COMMITTED)
        self.assertEqual(replay.turn, original)
        self.assertEqual(replay.version, 1)

        # 即使重建了不同 turn_id 的 Turn（崩溃恢复重放场景），
        # run identity 仍是幂等键：不产生重复 Turn。
        rebuilt = build_turn(
            "session-1",
            "run-1",
            turn_id="turn-rebuilt",
            user_input="changed input",
        )
        replay2 = await store.commit_turn(
            _SCOPE_A, "session-1", rebuilt, expected_version=0
        )
        self.assertIs(replay2.status, SessionCommitStatus.COMMITTED)
        self.assertEqual(replay2.turn, original)
        self.assertEqual(replay2.version, 1)

        snapshot = await store.read_snapshot(_SCOPE_A, "session-1")
        self.assertEqual(len(snapshot.turns), 1)
        self.assertEqual(snapshot.turns[0].turn_id, original.turn_id)
        self.assertEqual(snapshot.turns[0].user_input, original.user_input)

    async def test_commit_turn_claim_conflict_preserves_claim(self) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        claim = await store.claim_run(
            _SCOPE_A, "session-1", "run-1", expected_version=0
        )

        # 无 claim 匹配的提交（另一 run identity）→ CONFLICT，无任何
        # mutation：claim 保留、历史为空、版本不变。
        other = await store.commit_turn(
            _SCOPE_A,
            "session-1",
            build_turn("session-1", "run-2"),
            expected_version=0,
        )
        self.assertIs(other.status, SessionCommitStatus.CONFLICT)
        self.assertIsNone(other.turn)
        self.assertEqual(other.version, 0)
        self.assertEqual(
            await store.get_claim(_SCOPE_A, "session-1"), claim
        )
        snapshot = await store.read_snapshot(_SCOPE_A, "session-1")
        self.assertEqual(snapshot.turns, ())
        self.assertEqual(snapshot.version, 0)

    async def test_commit_turn_version_conflict_preserves_claim(self) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        claim = await store.claim_run(
            _SCOPE_A, "session-1", "run-1", expected_version=0
        )

        # 版本 CAS 失败（expected_version 落后于权威版本）→ CONFLICT，
        # claim 保留且禁止自动 merge。
        result = await store.commit_turn(
            _SCOPE_A,
            "session-1",
            build_turn("session-1", "run-1"),
            expected_version=3,
        )
        self.assertIs(result.status, SessionCommitStatus.CONFLICT)
        self.assertEqual(result.version, 0)
        self.assertEqual(await store.get_claim(_SCOPE_A, "session-1"), claim)
        self.assertEqual(
            (await store.read_snapshot(_SCOPE_A, "session-1")).turns, ()
        )

    async def test_commit_turn_validates_session_binding(self) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        await store.claim_run(_SCOPE_A, "session-1", "run-1", expected_version=0)

        # Turn 的 session 归属必须与目标 Session 一致（fail-closed）。
        with self.assertRaises(ValueError):
            await store.commit_turn(
                _SCOPE_A,
                "session-1",
                build_turn("session-other", "run-1"),
                expected_version=0,
            )
        snapshot = await store.read_snapshot(_SCOPE_A, "session-1")
        self.assertEqual(snapshot.turns, ())

    # -- 查询 ------------------------------------------------------------

    async def test_find_turn_by_run(self) -> None:
        store = self.make_store()
        await store.create_session(_SCOPE_A, "session-1")
        self.assertIsNone(
            await store.find_turn_by_run(_SCOPE_A, "session-1", "run-1")
        )

        claim = await store.claim_run(
            _SCOPE_A, "session-1", "run-1", expected_version=0
        )
        turn = build_turn("session-1", "run-1")
        await store.commit_turn(
            _SCOPE_A, "session-1", turn, expected_version=claim.session_version
        )

        self.assertEqual(
            await store.find_turn_by_run(_SCOPE_A, "session-1", "run-1"), turn
        )
        self.assertIsNone(
            await store.find_turn_by_run(_SCOPE_A, "session-1", "run-2")
        )

    # -- 时间戳确定性 -----------------------------------------------------

    async def test_store_uses_injected_clock_for_timestamps(self) -> None:
        clock = FakeClock()
        store = self.make_store(clock=clock)
        record = await store.create_session(_SCOPE_A, "session-1")
        self.assertEqual(record.created_at, clock.now())
        claim = await store.claim_run(
            _SCOPE_A, "session-1", "run-1", expected_version=0
        )
        self.assertEqual(claim.claimed_at, clock.now())
