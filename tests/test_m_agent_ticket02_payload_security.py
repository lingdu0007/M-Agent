"""Ticket 02 regression tests for protected snapshot and failure payloads."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
    DeterministicModelAdapter,
    FailureClassification,
    ModelFailure,
    ModelRequest,
    ModelResponse,
    Runner,
    SQLiteRunStore,
    StepStatus,
)

from fixtures.sentinel_payload_worker import SentinelPayloadCodec

_WORKER = Path(__file__).parent / "fixtures" / "sentinel_payload_worker.py"
_CRASH_EXIT_CODE = 17
_INSTRUCTIONS = "INSTRUCTION-SENTINEL-02"
_OUTPUT = "MODEL-OUTPUT-SENTINEL-02"
_ERROR = "PROVIDER-RAW-ERROR-SENTINEL-02"


def _worker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_WORKER), *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _run_id(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("RUN_ID="):
            return line.split("=", 1)[1]
    raise AssertionError(f"worker did not emit a run id: {stdout!r}")


def _metadata_text(db_path: str) -> str:
    connection = sqlite3.connect(db_path)
    try:
        statements = connection.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
        values: list[object] = list(statements)
        for table in ("runs", "steps", "step_attempts", "step_checkpoints"):
            values.extend(connection.execute(f"SELECT * FROM {table}").fetchall())
        return repr(values)
    finally:
        connection.close()


class _ExplodingAdapter(DeterministicModelAdapter):
    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        raise ModelFailure(
            FailureClassification.PERMANENT, "provider_test_error", _ERROR
        )


class Ticket02PayloadSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_process_reuses_checkpoint_and_decodes_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "run.db")
            log_path = os.path.join(tmp, "model.log")
            crashed = _worker("crash", db_path, log_path, _INSTRUCTIONS, _OUTPUT)
            self.assertEqual(crashed.returncode, _CRASH_EXIT_CODE, crashed.stderr)
            run_id = _run_id(crashed.stdout)

            resumed = _worker(
                "resume", db_path, log_path, run_id, _INSTRUCTIONS, _OUTPUT
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertIn("STATUS=SUCCEEDED", resumed.stdout)
            self.assertIn(f"OUTPUT={_OUTPUT}", resumed.stdout)
            self.assertIn(f"INSTRUCTIONS={_INSTRUCTIONS}", resumed.stdout)
            self.assertIn("MODEL_CALLS=0", resumed.stdout)
            with open(log_path, encoding="utf-8") as fh:
                self.assertEqual(fh.read().splitlines(), ["model-call"])

            metadata = _metadata_text(db_path)
            self.assertNotIn(_INSTRUCTIONS, metadata)
            self.assertNotIn(_OUTPUT, metadata)
            self.assertNotIn("sentinel input", metadata)

    async def test_failed_attempt_detail_is_codec_protected_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "run.db")
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="failing-assistant",
                    version="1.0",
                    instructions=_INSTRUCTIONS,
                    model_adapter=_ExplodingAdapter(responses=("unused",)),
                )
            )
            store = SQLiteRunStore(db_path, payload_codec=SentinelPayloadCodec())
            try:
                runner = Runner(registry=registry, store=store)
                created = await runner.create_run(
                    "failing-assistant", "1.0", input="sentinel input"
                )
                terminal = await runner.start_run(created.run_id)
                self.assertEqual(terminal.status.value, "FAILED")
            finally:
                store.close()

            metadata = _metadata_text(db_path)
            self.assertNotIn(_INSTRUCTIONS, metadata)
            self.assertNotIn(_ERROR, metadata)

            reopened = SQLiteRunStore(
                db_path, payload_codec=SentinelPayloadCodec()
            )
            try:
                restored = await reopened.get_run(created.run_id)
                attempts = await reopened.get_attempts(created.run_id)
            finally:
                reopened.close()

            self.assertEqual(restored.snapshot.instructions, _INSTRUCTIONS)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].status, StepStatus.FAILED)
            self.assertEqual(
                attempts[0].classification, FailureClassification.PERMANENT
            )
            self.assertEqual(attempts[0].error_code, "provider_test_error")
            self.assertNotIn(_ERROR, attempts[0].error or "")


if __name__ == "__main__":
    unittest.main()
