"""Contract tests for the synchronous facade over Runner."""

from __future__ import annotations

import asyncio
import threading
import unittest

from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
    DeterministicModelAdapter,
    DeterministicTool,
    FailureClassification,
    InMemoryRunStore,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    PlaintextPayloadCodec,
    RunResolution,
    RunStatus,
    Runner,
    SyncRunner,
    ToolCall,
    ToolEffect,
    ToolFailure,
    ToolOutcome,
    ToolRequest,
)
from m_agent.runtime import ModelRequirements, ToolCallingMode


class FailingModel(DeterministicModelAdapter):
    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        raise RuntimeError("deliberate failure")


class UncertainTool(DeterministicTool):
    def __init__(self) -> None:
        super().__init__(name="notify", effect=ToolEffect.NON_IDEMPOTENT)
        self.calls = 0

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        self.calls += 1
        raise ToolFailure(FailureClassification.UNCERTAIN, "effect_unconfirmed", "unknown")


class ToolRequestingModel(DeterministicModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            capabilities=ModelCapabilities(tool_calling=ToolCallingMode.NATIVE)
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        if not request.tool_outcomes:
            return ModelResponse(
                tool_calls=(ToolCall(call_id="call-1", tool_name="notify", arguments="{}"),)
            )
        return ModelResponse(content="completed")


def make_sync(model, tools=()) -> SyncRunner:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id="assistant",
            version="1.0",
            instructions="be deterministic",
            model_requirements=ModelRequirements(
                capabilities=(
                    ModelCapabilities(tool_calling=ToolCallingMode.NATIVE)
                    if tools
                    else ModelCapabilities()
                )
            ),
            model_adapter=model,
            tools=tuple(tools),
        )
    )
    return SyncRunner(
        Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )
    )


class SyncRunnerTests(unittest.TestCase):
    def test_success_and_failure_share_async_semantics(self) -> None:
        with make_sync(DeterministicModelAdapter(responses=("ok",))) as runner:
            created = runner.create_run("assistant", "1.0", "hi")
            self.assertEqual(runner.start_run(created.run_id).status, RunStatus.SUCCEEDED)

        with make_sync(FailingModel()) as runner:
            created = runner.create_run("assistant", "1.0", "hi")
            self.assertEqual(runner.start_run(created.run_id).status, RunStatus.FAILED)

    def test_waiting_resolution_does_not_reexecute_uncertain_tool(self) -> None:
        tool = UncertainTool()
        with make_sync(ToolRequestingModel(), (tool,)) as runner:
            created = runner.create_run("assistant", "1.0", "notify")
            waiting = runner.start_run(created.run_id)
            self.assertEqual(waiting.status, RunStatus.WAITING)
            terminal = runner.resolve_run(
                created.run_id,
                RunResolution.confirm_step("confirmed"),
                waiting.version,
            )
            self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
            self.assertEqual(tool.calls, 1)

    def test_cancellation_is_cooperative(self) -> None:
        class BlockingModel(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.started = threading.Event()
                self.release = threading.Event()

            def _fingerprint_excluded_state(self) -> frozenset[str]:
                return super()._fingerprint_excluded_state() | {
                    "started",
                    "release",
                }

            async def generate(self, request: ModelRequest) -> ModelResponse:
                self.call_count += 1
                self.started.set()
                await asyncio.to_thread(self.release.wait)
                return ModelResponse(content="done")

        model = BlockingModel()
        with make_sync(model) as runner:
            created = runner.create_run("assistant", "1.0", "hi")
            result = {}
            worker = threading.Thread(
                target=lambda: result.setdefault("record", runner.start_run(created.run_id))
            )
            worker.start()
            self.assertTrue(model.started.wait(2))
            runner.cancel_run(created.run_id)
            model.release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result["record"].status, RunStatus.CANCELLED)

    def test_closed_runner_rejects_new_commands(self) -> None:
        runner = make_sync(DeterministicModelAdapter())
        runner.close()
        with self.assertRaises(RuntimeError):
            runner.create_run("assistant", "1.0", "hi")
