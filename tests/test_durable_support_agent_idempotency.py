"""Ticket 11: idempotent support-ticket updates have one external effect."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLE_DIR = os.path.abspath(
    os.path.join(_HERE, "..", "examples", "durable_support_agent")
)
if _EXAMPLE_DIR not in sys.path:
    sys.path.insert(0, _EXAMPLE_DIR)

from m_agent.runtime import (
    ERROR_EFFECT_UNCONFIRMED,
    DefinitionRegistry,
    FailureClassification,
    Runner,
    StepStatus,
    StepType,
    ToolRequest,
    deserialize_tool_outcome,
)
from m_agent.adapters import (
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent import (
    DefinitionRegistry,
    Runner,
)
from support_agent import TICKET_ID, TicketUpdateTool, journal_count

_WORKER = os.path.join(_EXAMPLE_DIR, "worker.py")


def _run_worker(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, _WORKER, *args],
        cwd=_EXAMPLE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def _run_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if line.startswith("RUN_ID="):
            return line.split("=", 1)[1].strip()
    return None


class TicketUpdateIdempotencyTests(unittest.TestCase):
    def test_same_ticket_note_has_one_external_update_and_stable_outcome(self) -> None:
        """A repeated operation must not append a second external effect."""
        with tempfile.TemporaryDirectory() as tmp:
            journal = os.path.join(tmp, "ticket-update.journal")
            tool = TicketUpdateTool(journal)
            request = ToolRequest(
                call_id="ticket-update-idempotency-check",
                tool_name="ticket_update",
                arguments=(
                    '{"ticket_id": "' + TICKET_ID + '", '
                    '"note": "order status verified by lookup"}'
                ),
            )

            first = asyncio.run(tool.invoke(request))
            repeated = asyncio.run(tool.invoke(request))
            replay_request = request.model_copy(
                update={"call_id": "ticket-update-recovery-attempt"}
            )
            replayed = asyncio.run(tool.invoke(replay_request))

            self.assertEqual(first, repeated)
            self.assertEqual(first.status, replayed.status)
            self.assertEqual(first.tool_name, replayed.tool_name)
            self.assertEqual(first.result, replayed.result)
            self.assertEqual(journal_count(journal), 1)

    def test_recovery_replays_ticket_update_without_second_external_effect(
        self,
    ) -> None:
        """A public cross-process resume retains both attempts and one update."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "ticket-update-replay.sqlite")
            journal = os.path.join(tmp, "ticket-update-replay.journal")
            logs_dir = os.path.join(tmp, "logs")
            first = _run_worker(
                [
                    db_path,
                    os.path.join(tmp, "notify.journal"),
                    journal,
                    logs_dir,
                    "ticket-update-and-crash",
                ]
            )
            run_id = _run_id(first.stdout)
            self.assertEqual(first.returncode, 17, first.stderr)
            self.assertIsNotNone(run_id, first.stdout)
            self.assertEqual(journal_count(journal), 1)

            resumed = _run_worker(
                [
                    db_path,
                    os.path.join(tmp, "notify.journal"),
                    journal,
                    logs_dir,
                    "resume-after-ticket-update",
                    str(run_id),
                ]
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertIn("STATUS=SUCCEEDED", resumed.stdout)
            self.assertEqual(journal_count(journal), 1)

            store = SQLiteRunStore(
                db_path, payload_codec=PlaintextPayloadCodec()
            )
            try:
                inspection = asyncio.run(
                    Runner(
                        registry=DefinitionRegistry(), store=store
                    ).inspect_run(str(run_id))
                )
            finally:
                store.close()
            ticket_checkpoint = next(
                checkpoint
                for checkpoint in inspection.checkpoints
                if checkpoint.step_type is StepType.TOOL
                and deserialize_tool_outcome(checkpoint.output).tool_name
                == "ticket_update"
            )
            attempts = [
                attempt
                for attempt in inspection.attempts
                if attempt.step_id == ticket_checkpoint.step_id
            ]
            self.assertEqual(len(attempts), 2)
            self.assertEqual(
                {attempt.status for attempt in attempts},
                {StepStatus.FAILED, StepStatus.SUCCEEDED},
            )
            original = next(
                attempt
                for attempt in attempts
                if attempt.status is StepStatus.FAILED
            )
            self.assertIs(
                original.classification, FailureClassification.UNCERTAIN
            )
            self.assertEqual(original.error_code, ERROR_EFFECT_UNCONFIRMED)
            self.assertNotEqual(original.attempt_id, ticket_checkpoint.attempt_id)


if __name__ == "__main__":
    unittest.main()
