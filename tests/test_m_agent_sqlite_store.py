"""SQLiteRunStore 行为契约与持久化专属测试。

- 共享契约：与 InMemoryRunStore 相同的生命周期、Step、Step Attempt、
  Checkpoint、版本控制与 Payload 处理契约（PRD Testing Decisions）；
- SQLite 专属：进程重启（关闭连接后重开）数据仍完整、Metadata 与
  Payload 分表存储、明文编码字节带显式标记、无 Codec 构造被拒绝、
  原始 payload 字节不可绕过 Codec 读取。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest

from m_agent import (
    InMemoryRunStore,
    PlaintextPayloadCodec,
    RunStatus,
    SQLiteRunStore,
    StaleRunVersionError,
)

from store_contract import RunStoreContractMixin, created_record, snapshot


def make_db_path() -> str:
    return tempfile.mktemp(prefix="m-agent-contract-", suffix=".db")


class SQLiteRunStoreContractTests(
    RunStoreContractMixin, unittest.IsolatedAsyncioTestCase
):
    def make_store(  # type: ignore[override]
        self, codec=None, clock=None
    ):
        self._db = make_db_path()
        return SQLiteRunStore(
            path=self._db,
            payload_codec=codec or PlaintextPayloadCodec(),
            clock=clock,
        )


class SQLiteRunStorePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_and_reopen_preserves_all_records(self) -> None:
        # 进程重启等价路径：关闭连接、重开同一数据库文件，数据完整。
        db = make_db_path()
        store = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
        created = await store.create_run(created_record())
        running = await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
            snapshot=snapshot(),
        )
        await store.transition_run(
            "run-1",
            expected_version=running.version,
            status=RunStatus.SUCCEEDED,
            output="persisted output",
        )
        store.close()

        reopened = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
        try:
            record = await reopened.get_run("run-1")
            self.assertIsNotNone(record)
            self.assertEqual(record.status, RunStatus.SUCCEEDED)
            self.assertEqual(record.output, "persisted output")
            self.assertEqual(record.input, "hi")
            self.assertEqual(record.snapshot.definition_id, "assistant")
            self.assertEqual(record.version, 3)
        finally:
            reopened.close()

    async def test_metadata_and_payload_are_separate_tables(self) -> None:
        # ADR 0033：可查询 Metadata 与编码 Payload 分表；runs 表不含
        # 内容字段，内容只出现在 run_payloads 表（BLOB）。
        db = make_db_path()
        store = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
        await store.create_run(created_record())
        store.close()

        conn = sqlite3.connect(db)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("runs", tables)
            self.assertIn("run_payloads", tables)
            runs_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(runs)")
            }
            # metadata 表不承载任何内容字段；内容列只存在于 payload 表。
            self.assertNotIn("input", runs_cols)
            self.assertNotIn("output", runs_cols)
            payload_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(run_payloads)")
            }
            self.assertIn("encoded", payload_cols)
            # 运行一次完整 Run 后：内容只出现在 payload 表，且为编码字节。
            store = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
            try:
                created = await store.get_run("run-1")
                running = await store.transition_run(
                    "run-1",
                    expected_version=created.version,
                    status=RunStatus.RUNNING,
                )
                await store.transition_run(
                    "run-1",
                    expected_version=running.version,
                    status=RunStatus.SUCCEEDED,
                    output="final content",
                )
            finally:
                store.close()
            metadata_content = conn.execute(
                "SELECT snapshot_json, waiting_reason, status FROM runs"
            ).fetchall()
            self.assertNotIn("final content", str(metadata_content))
            self.assertNotIn("hi", str(metadata_content))
            raw = conn.execute(
                "SELECT encoded FROM run_payloads WHERE field='run:output'"
            ).fetchone()
            self.assertIsNotNone(raw)
            self.assertEqual(
                bytes(raw[0]),
                PlaintextPayloadCodec().encode("final content"),
            )
        finally:
            conn.close()

    async def test_plaintext_bytes_are_marked_and_rejected_by_wrong_codec(
        self,
    ) -> None:
        # 持久化字节带明文标记；换用其他 Codec 读取会显式失败，
        # 证明不存在绕过 Codec 的读取路径。
        db = make_db_path()
        store = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
        await store.create_run(created_record())
        raw = store.raw_payload_bytes("run-1", "run:input")
        self.assertEqual(raw, PlaintextPayloadCodec().encode("hi"))
        store.close()

        class OtherCodec(PlaintextPayloadCodec):
            _PREFIX = b"other-codec:"

        reopened = SQLiteRunStore(db, payload_codec=OtherCodec())
        try:
            with self.assertRaises(ValueError):
                await reopened.get_run("run-1")
        finally:
            reopened.close()

    async def test_two_connections_stale_version_is_rejected_atomically(
        self,
    ) -> None:
        # 两个连接基于同一期望版本各自提交：第二个必须被 SQL 层的
        # WHERE version=? 拒绝（StaleRunVersionError），且失败后该连接
        # 仍可继续正常操作（事务已回滚）。
        db = make_db_path()
        s1 = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
        s2 = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
        try:
            created = await s1.create_run(created_record())
            # s2 在 s1 推进之前读到 version=1 的 CREATED 记录。
            stale_view = await s2.get_run("run-1")
            self.assertEqual(stale_view.version, 1)

            await s1.transition_run(
                "run-1",
                expected_version=created.version,
                status=RunStatus.RUNNING,
                snapshot=snapshot(),
            )
            # s2 用过期版本提交：必须失败，且权威记录不被改动。
            with self.assertRaises(StaleRunVersionError):
                await s2.transition_run(
                    "run-1",
                    expected_version=stale_view.version,
                    status=RunStatus.SUCCEEDED,
                )
            authoritative = await s1.get_run("run-1")
            self.assertEqual(authoritative.status, RunStatus.RUNNING)
            self.assertEqual(authoritative.version, 2)

            # 失败后连接仍然可用（回滚未留下半开事务）。
            running = await s2.get_run("run-1")
            succeeded = await s2.transition_run(
                "run-1",
                expected_version=running.version,
                status=RunStatus.SUCCEEDED,
                output="done",
            )
            self.assertEqual(succeeded.status, RunStatus.SUCCEEDED)
        finally:
            s1.close()
            s2.close()

    def test_store_construction_requires_explicit_codec(self) -> None:
        # plaintext 不是默认：构造时必须显式传入 Payload Codec。
        with self.assertRaises(TypeError):
            InMemoryRunStore()  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            SQLiteRunStore(make_db_path())  # type: ignore[call-arg]

    def test_plaintext_codec_is_explicitly_a_dev_test_choice(self) -> None:
        codec = PlaintextPayloadCodec()
        # 编码带可识别标记，便于断言存储内容确实经 Codec 处理。
        self.assertEqual(codec.encode("secret"), b"m-agent-plaintext:secret")
        self.assertEqual(codec.decode(b"m-agent-plaintext:secret"), "secret")
        with self.assertRaises(ValueError):
            codec.decode(b"not-ours:secret")


if __name__ == "__main__":
    unittest.main()
