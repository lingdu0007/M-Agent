"""Independent Runs retain their own durable Context invocation evidence."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures.run_isolation_worker import InputProvider, make_definition

from m_agent import AgentDefinition, DefinitionRegistry, Runner, RunStatus
from m_agent.adapters import (
    DeterministicContextProvider,
    DeterministicModelAdapter,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent.runtime import (
    ContextItem,
    ContextRequest,
    LeaseNotHeldError,
    RunInspection,
    StepAttempt,
    StepCheckpoint,
    StepRecord,
    StepType,
)


def run_worker(
    path: Path,
    mode: str,
    run_id: str,
    action: str,
    *options: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from fixtures.run_isolation_worker import main; main()",
            str(path),
            mode,
            run_id,
            action,
            *options,
        ],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (str(Path(__file__).parent), str(Path(__file__).parents[1] / "src"))
            ),
            "M_AGENT_RUN_LIVE_TESTS": "0",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


class GatedProvider(InputProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def provide(self, request: ContextRequest) -> list[ContextItem]:
        if request.input == "run-a" and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
        return await super().provide(request)


class SharedSQLiteContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_sequential_runs_preserve_context_evidence(self) -> None:
        provider = DeterministicContextProvider(
            (ContextItem(item_id="source", content="synthetic", source="fixture"),)
        )
        model = DeterministicModelAdapter(responses=("answer",))
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="shared-context",
                version="1",
                instructions="Use the supplied synthetic context.",
                model_adapter=model,
                context_provider=provider,
            )
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            SQLiteRunStore(
                Path(directory) / "runs.db", payload_codec=PlaintextPayloadCodec()
            ) as store,
        ):
            runner = Runner(registry=registry, store=store)
            await runner.create_run("shared-context", "1", input="A", run_id="run-a")
            result_a = await runner.start_run("run-a")
            self.assertEqual(result_a.status, RunStatus.SUCCEEDED)
            original_a = await runner.inspect_run("run-a")
            self.assertEqual(
                [step.step_type for step in original_a.steps],
                [StepType.CONTEXT, StepType.MODEL],
            )

            await runner.create_run("shared-context", "1", input="B", run_id="run-b")
            result_b = await runner.start_run("run-b")

            self.assertEqual(result_b.status, RunStatus.SUCCEEDED)
            self.assertEqual(await runner.inspect_run("run-a"), original_a)
            inspection_b = await runner.inspect_run("run-b")
            self.assertEqual(
                [step.step_type for step in inspection_b.steps],
                [StepType.CONTEXT, StepType.MODEL],
            )
            for inspection, run_id in (
                (original_a, "run-a"),
                (inspection_b, "run-b"),
            ):
                records: tuple[StepRecord | StepAttempt | StepCheckpoint, ...] = (
                    *inspection.steps,
                    *inspection.attempts,
                    *inspection.checkpoints,
                )
                for record in records:
                    self.assertEqual(record.run_id, run_id)
                self.assertEqual(len(inspection.attempts), 2)
                self.assertEqual(len(inspection.checkpoints), 2)
            self.assertEqual(provider.call_count, 2)
            self.assertEqual(model.call_count, 2)

    async def test_two_connections_advance_context_runs_without_cross_run_writes(
        self,
    ) -> None:
        for mode in ("legacy", "explicit", "compression"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "runs.db"
                provider = GatedProvider()
                definition = make_definition(mode).model_copy(
                    update={"context_provider": provider}
                )
                registry = DefinitionRegistry()
                registry.register(definition)
                with (
                    SQLiteRunStore(
                        path, payload_codec=PlaintextPayloadCodec()
                    ) as store_a,
                    SQLiteRunStore(
                        path, payload_codec=PlaintextPayloadCodec()
                    ) as store_b,
                ):
                    runner_a = Runner(registry=registry, store=store_a)
                    runner_b = Runner(registry=registry, store=store_b)
                    for runner, run_id in ((runner_a, "run-a"), (runner_b, "run-b")):
                        await runner.create_run(
                            definition.definition_id, "1", input=run_id, run_id=run_id
                        )
                    task_a = asyncio.create_task(runner_a.start_run("run-a"))
                    try:
                        await asyncio.wait_for(provider.entered.wait(), timeout=5)
                        inflight_a = await runner_a.inspect_run("run-a")
                        with self.assertRaises(LeaseNotHeldError):
                            await runner_b.resume_run("run-a")
                        result_b = await runner_b.start_run("run-b")
                        self.assertEqual(result_b.status, RunStatus.SUCCEEDED)
                        self.assertEqual(
                            await runner_a.inspect_run("run-a"), inflight_a
                        )
                        original_b = await runner_b.inspect_run("run-b")
                        provider.release.set()
                        self.assertEqual(
                            (await asyncio.wait_for(task_a, timeout=5)).status,
                            RunStatus.SUCCEEDED,
                        )
                        self.assertEqual(
                            await runner_b.inspect_run("run-b"), original_b
                        )
                        for run_id in ("run-a", "run-b"):
                            inspection = await runner_b.inspect_run(run_id)
                            self.assert_owned_evidence(inspection, run_id)
                            if mode == "explicit":
                                scopes = {
                                    json.loads(checkpoint.output)["scope"]
                                    for checkpoint in inspection.checkpoints
                                    if checkpoint.step_type is StepType.CONTEXT
                                }
                                self.assertEqual(
                                    scopes, {"RUN_INPUT", "TOOL_OUTCOME", "MODEL_STEP"}
                                )
                            if mode == "compression":
                                self.assertEqual(
                                    len(
                                        [
                                            s
                                            for s in inspection.steps
                                            if s.step_id
                                            == "compression:shared-compression:1"
                                        ]
                                    ),
                                    1,
                                )
                    finally:
                        provider.release.set()
                        if not task_a.done():
                            task_a.cancel()
                        await asyncio.gather(task_a, return_exceptions=True)

    def assert_owned_evidence(self, inspection: RunInspection, run_id: str) -> None:
        self.assertEqual(inspection.run.run_id, run_id)
        steps = {step.step_id for step in inspection.steps}
        attempts = {attempt.attempt_id: attempt for attempt in inspection.attempts}
        records: tuple[StepRecord | StepAttempt | StepCheckpoint, ...] = (
            *inspection.steps,
            *inspection.attempts,
            *inspection.checkpoints,
        )
        for record in records:
            self.assertEqual(record.run_id, run_id)
        for checkpoint in inspection.checkpoints:
            self.assertIn(checkpoint.step_id, steps)
            self.assertIn(checkpoint.attempt_id, attempts)
            self.assertEqual(
                attempts[checkpoint.attempt_id].step_id, checkpoint.step_id
            )
            self.assertEqual(attempts[checkpoint.attempt_id].output, checkpoint.output)
            if checkpoint.step_type is StepType.CONTEXT:
                result = json.loads(checkpoint.output)
                for output in result["output_items"]:
                    self.assertEqual(output["item"]["metadata"]["input"], run_id)
                    self.assertEqual(
                        output["provenance"]["stage_id"], result["stage_id"]
                    )

    async def test_process_restart_preserves_invocations_and_completed_providers(
        self,
    ) -> None:
        windows = (
            ("legacy", "during-provider", 2),
            ("legacy", "after-context", 1),
            ("explicit", "after-context", 4),
            ("compression", "during-compression", 1),
            ("compression", "after-compression", 1),
        )
        for mode, window, provider_calls in windows:
            for repetition in range(3):
                with (
                    self.subTest(mode=mode, window=window, repetition=repetition),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    path = Path(directory) / "runs.db"
                    journal = Path(directory) / "dispatch.log"
                    crashed = run_worker(
                        path,
                        mode,
                        "run-a",
                        "start",
                        "--crash",
                        window,
                        "--journal",
                        str(journal),
                    )
                    self.assertEqual(crashed.returncode, 17, crashed.stderr)
                    with SQLiteRunStore(
                        path, payload_codec=PlaintextPayloadCodec()
                    ) as store:
                        before = await Runner(
                            registry=DefinitionRegistry(), store=store
                        ).inspect_run("run-a")
                    other = run_worker(path, mode, "run-b", "start")
                    self.assertEqual(other.returncode, 0, other.stderr)
                    original_b = RunInspection.model_validate_json(other.stdout)
                    resumed = run_worker(
                        path, mode, "run-a", "resume", "--journal", str(journal)
                    )
                    self.assertEqual(resumed.returncode, 0, resumed.stderr)
                    after = RunInspection.model_validate_json(resumed.stdout)
                    self.assertEqual(after.run.status, RunStatus.SUCCEEDED)
                    self.assertEqual(after.run.snapshot, before.run.snapshot)
                    self.assert_owned_evidence(after, "run-a")
                    for checkpoint in before.checkpoints:
                        self.assertIn(checkpoint, after.checkpoints)
                    self.assertTrue(
                        {step.step_id for step in before.steps}
                        <= {step.step_id for step in after.steps}
                    )
                    self.assertTrue(
                        {attempt.attempt_id for attempt in before.attempts}
                        <= {attempt.attempt_id for attempt in after.attempts}
                    )
                    if window.startswith("during-"):
                        interrupted = before.steps[-1].step_id
                        self.assertEqual(
                            len(
                                [a for a in after.attempts if a.step_id == interrupted]
                            ),
                            2,
                        )
                    calls = journal.read_text().splitlines()
                    self.assertEqual(calls.count("provider"), provider_calls)
                    if mode == "compression":
                        self.assertEqual(
                            calls.count("compression"),
                            2 if window == "during-compression" else 1,
                        )
                    reread = run_worker(path, mode, "run-b", "inspect")
                    self.assertEqual(reread.returncode, 0, reread.stderr)
                    self.assertEqual(
                        RunInspection.model_validate_json(reread.stdout), original_b
                    )
