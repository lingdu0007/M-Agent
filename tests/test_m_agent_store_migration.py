"""Compatibility with unmodified databases emitted by the published wheel."""

from __future__ import annotations

import asyncio
import sqlite3
import tarfile
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from fixtures.run_isolation_worker import InputProvider, make_definition

from m_agent import DefinitionRegistry, Runner, RunStatus
from m_agent.adapters import FakeClock, PlaintextPayloadCodec, SQLiteRunStore
from m_agent.runtime import MAgentError, RunStoreIntegrityError

ARCHIVE = Path(__file__).parent / "fixtures" / "run_store_v050.tar.gz"


def restore_database(name: str, destination: Path) -> None:
    with tarfile.open(ARCHIVE) as archive:
        member = archive.extractfile(name)
        assert member is not None
        with member:
            destination.write_bytes(member.read())


class HistoricalRunStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_old_checkpoint_survives_new_run_and_resume(self) -> None:
        cases = (
            ("legacy.db", "legacy", 1),
            ("explicit.db", "explicit", 7),
            ("compression.db", "compression", 1),
            ("pending.db", "legacy", 2),
            ("pending-compression.db", "compression", 1),
        )
        for name, mode, provider_calls in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "runs.db"
                restore_database(name, path)
                definition = make_definition(mode)
                registry = DefinitionRegistry()
                registry.register(definition)
                clock = FakeClock()
                with SQLiteRunStore(
                    path, payload_codec=PlaintextPayloadCodec(), clock=clock
                ) as store:
                    runner = Runner(registry=registry, store=store)
                    before = await runner.inspect_run("old-a")
                    await runner.create_run(
                        definition.definition_id, "1", input="new-b", run_id="new-b"
                    )
                    self.assertEqual(
                        (await runner.start_run("new-b")).status, RunStatus.SUCCEEDED
                    )
                    self.assertEqual(await runner.inspect_run("old-a"), before)
                    assert before.run.lease_expires_at is not None
                    clock.advance(
                        before.run.lease_expires_at - clock.now() + timedelta(seconds=1)
                    )
                    self.assertEqual(
                        (await runner.resume_run("old-a")).status, RunStatus.SUCCEEDED
                    )
                    after = await runner.inspect_run("old-a")
                    self.assertEqual(after.run.snapshot, before.run.snapshot)
                    for checkpoint in before.checkpoints:
                        self.assertIn(checkpoint, after.checkpoints)
                    for step in before.steps:
                        self.assertIn(step.step_id, [s.step_id for s in after.steps])
                    assert isinstance(definition.context_provider, InputProvider)
                    self.assertEqual(
                        definition.context_provider.call_count, provider_calls
                    )

    async def test_historical_cross_run_overwrite_is_rejected_without_mutation(
        self,
    ) -> None:
        for name in ("corrupt.db", "compression-collision.db"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "runs.db"
                restore_database(name, path)
                before = path.read_bytes()
                with (
                    self.assertRaisesRegex(MAgentError, "integrity"),
                    SQLiteRunStore(path, payload_codec=PlaintextPayloadCodec()),
                ):
                    pass
                self.assertEqual(path.read_bytes(), before)

    async def test_migration_preserves_payload_bytes_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.db"
            restore_database("compression.db", path)
            # This assertion concerns the historical storage format itself.
            with sqlite3.connect(path) as connection:
                original = connection.execute(
                    "SELECT run_id, field, encoded FROM run_payloads ORDER BY rowid"
                ).fetchall()
            with SQLiteRunStore(path, payload_codec=PlaintextPayloadCodec()) as store:
                before = await Runner(
                    registry=DefinitionRegistry(), store=store
                ).inspect_run("old-a")
                for run_id, field, encoded in original:
                    self.assertEqual(store.raw_payload_bytes(run_id, field), encoded)
            migrated = path.read_bytes()
            with SQLiteRunStore(path, payload_codec=PlaintextPayloadCodec()) as store:
                self.assertEqual(
                    await Runner(
                        registry=DefinitionRegistry(), store=store
                    ).inspect_run("old-a"),
                    before,
                )
            self.assertEqual(path.read_bytes(), migrated)

    async def test_concurrent_openers_migrate_once_without_losing_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.db"
            restore_database("compression.db", path)

            def inspect():
                with SQLiteRunStore(
                    path, payload_codec=PlaintextPayloadCodec()
                ) as store:
                    return asyncio.run(
                        Runner(registry=DefinitionRegistry(), store=store).inspect_run(
                            "old-a"
                        )
                    )

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(inspect) for _ in range(4)]
                results = [future.result(timeout=15) for future in futures]
            for result in results:
                self.assertEqual(result, results[0])
                self.assertEqual(len(result.checkpoints), 2)

    async def test_unsupported_schema_rolls_back_all_migration_steps(self) -> None:
        for change in (
            "ALTER TABLE step_attempts ADD COLUMN custom_evidence TEXT",
            (
                "ALTER TABLE steps ADD COLUMN custom_evidence TEXT "
                "GENERATED ALWAYS AS (run_id || step_id) VIRTUAL"
            ),
            "PRAGMA user_version=999",
        ):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "runs.db"
                restore_database("compression.db", path)
                with sqlite3.connect(path) as connection:
                    connection.execute(change)
                original = path.read_bytes()
                with (
                    self.assertRaises(RunStoreIntegrityError),
                    SQLiteRunStore(path, payload_codec=PlaintextPayloadCodec()),
                ):
                    pass
                self.assertEqual(path.read_bytes(), original)

    async def test_custom_unique_constraint_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.db"
            restore_database("compression.db", path)
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE custom_steps (
                        step_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        step_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error_code TEXT,
                        created_at TEXT NOT NULL,
                        UNIQUE (run_id, step_type)
                    );
                    INSERT INTO custom_steps
                        (step_id, run_id, step_type, status, error_code, created_at)
                        SELECT step_id, run_id, step_type, status, error_code, created_at
                        FROM steps;
                    DROP TABLE steps;
                    ALTER TABLE custom_steps RENAME TO steps;
                """)
            original = path.read_bytes()
            with (
                self.assertRaises(RunStoreIntegrityError),
                SQLiteRunStore(path, payload_codec=PlaintextPayloadCodec()),
            ):
                pass
            self.assertEqual(path.read_bytes(), original)
