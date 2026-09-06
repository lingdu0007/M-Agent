"""Public-API synthetic Runs, also executable against the published 0.5 wheel."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

from m_agent import AgentDefinition, DefinitionRegistry, Runner
from m_agent.adapters import (
    DeterministicContextProvider,
    DeterministicModelAdapter,
    DeterministicTool,
    FakeClock,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent.runtime import (
    CompressionContract,
    ContextItem,
    ContextPlan,
    ContextRequest,
    ContextScope,
    ContextStage,
    ContextStageIdentity,
    ContextTransformType,
    ModelBinding,
    ModelBindingSet,
    ModelCapabilities,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
    RetryPolicy,
    StepCheckpoint,
    StepType,
    ToolCall,
    ToolCallingMode,
    ToolEffect,
    ToolOutcome,
    ToolRequest,
)

CRASH = ""
JOURNAL: Path | None = None


def observe(kind: str) -> None:
    if JOURNAL is not None:
        with JOURNAL.open("a") as stream:
            stream.write(kind + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    if CRASH == f"during-{kind}":
        os._exit(17)


class InputProvider(DeterministicContextProvider):
    async def provide(self, request: ContextRequest) -> list[ContextItem]:
        self.call_count += 1
        observe("provider")
        return [
            ContextItem(
                item_id="source",
                content=(f"synthetic source for {request.input}. " * 20),
                source="fixture",
                metadata={"input": request.input},
            )
        ]


class EchoTool(DeterministicTool):
    def __init__(self) -> None:
        super().__init__(
            name="echo", description="Synthetic echo.", effect=ToolEffect.READ_ONLY
        )

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        return ToolOutcome.success(request.call_id, self.name, result="synthetic")


class ObservingModel(DeterministicModelAdapter):
    def __init__(self, kind: str) -> None:
        super().__init__(
            capabilities=ModelCapabilities(
                tool_calling=(
                    ToolCallingMode.NATIVE
                    if kind == "explicit"
                    else ToolCallingMode.NONE
                )
            )
        )
        self.kind = kind

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        observe("compression" if self.kind == "compression" else "model")
        if self.kind == "compression":
            return ModelResponse(
                content=json.dumps(
                    {
                        "items": [
                            {
                                "item_id": "summary",
                                "content": request.context_items[0].content[:45],
                                "source_item_ids": ["source"],
                            }
                        ]
                    }
                )
            )
        if self.kind == "explicit" and not request.tool_outcomes:
            return ModelResponse(
                tool_calls=(
                    ToolCall(call_id="echo-1", tool_name="echo", arguments="{}"),
                )
            )
        return ModelResponse(content=f"answer for {request.input}")


def make_definition(mode: str, stage_suffix: str = "") -> AgentDefinition:
    provider = InputProvider()
    business = ObservingModel("explicit" if mode == "explicit" else "business")
    plan = ContextPlan()
    if mode != "legacy":
        scopes = (
            tuple(ContextScope) if mode == "explicit" else (ContextScope.RUN_INPUT,)
        )
        plan = ContextPlan(
            stages=tuple(
                ContextStage(
                    identity=ContextStageIdentity(
                        stage_id=f"source-{scope.value}{stage_suffix}",
                        scope=scope,
                        transform_type=ContextTransformType.PROVIDE,
                    )
                )
                for scope in scopes
            )
        )
    definition = AgentDefinition.for_adapter(
        definition_id=f"isolation-{mode}{stage_suffix}",
        version="1",
        instructions="Use synthetic context.",
        model_adapter=business,
        context_provider=provider,
        context_plan=plan,
        tools=(EchoTool(),) if mode == "explicit" else (),
        retry_policy=RetryPolicy(max_attempts=3),
    )
    if mode != "compression":
        return definition
    compression = ObservingModel("compression")
    bindings = tuple(
        binding
        for binding in definition.model_bindings.bindings
        if binding.purpose is not ModelPurpose.CONTEXT_COMPRESSION
    )
    return definition.model_copy(
        update={
            "model_bindings": ModelBindingSet(
                bindings=(
                    *bindings,
                    ModelBinding(
                        purpose=ModelPurpose.CONTEXT_COMPRESSION,
                        contract=compression.model_contract,
                    ),
                )
            ),
            "model_adapters": {ModelPurpose.CONTEXT_COMPRESSION: compression},
            "compression_contract": CompressionContract(
                contract_id="shared-compression",
                version="1",
                instructions="Summarize synthetic context.",
                allowed_sources=("fixture",),
                retained_categories=("facts",),
                omitted_categories=("repetition",),
                derived_categories=("summary",),
                max_output_items=1,
            ),
        }
    )


class ExitingStore(SQLiteRunStore):
    async def record_checkpoint(
        self,
        checkpoint: StepCheckpoint,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> StepCheckpoint:
        result = await super().record_checkpoint(
            checkpoint, expected_version=expected_version, lease_owner=lease_owner
        )
        kind = (
            "context"
            if checkpoint.step_type is StepType.CONTEXT
            else "compression"
            if checkpoint.step_id.startswith("compression:")
            else "model"
        )
        if CRASH == f"after-{kind}":
            os._exit(17)
        return result


async def execute(args: argparse.Namespace) -> None:
    global CRASH, JOURNAL
    CRASH = args.crash
    JOURNAL = Path(args.journal) if args.journal else None
    definition = make_definition(args.mode, args.stage_suffix)
    registry = DefinitionRegistry()
    registry.register(definition)
    clock = FakeClock()
    with SQLiteRunStore(args.db, payload_codec=PlaintextPayloadCodec()) as probe:
        record = await probe.get_run(args.run_id)
        if record is not None and record.lease_expires_at is not None:
            clock = FakeClock(start=record.lease_expires_at + timedelta(seconds=1))
    with ExitingStore(
        args.db, payload_codec=PlaintextPayloadCodec(), clock=clock
    ) as store:
        runner = Runner(registry=registry, store=store)
        if args.action == "start":
            await runner.create_run(
                definition.definition_id, "1", input=args.run_id, run_id=args.run_id
            )
            await runner.start_run(args.run_id)
        elif args.action == "resume":
            await runner.resume_run(args.run_id)
        inspection = await runner.inspect_run(args.run_id)
        print(inspection.model_dump_json())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("db")
    parser.add_argument("mode", choices=("legacy", "explicit", "compression"))
    parser.add_argument("run_id")
    parser.add_argument("action", choices=("start", "resume", "inspect"))
    parser.add_argument("--crash", default="")
    parser.add_argument("--journal")
    parser.add_argument("--stage-suffix", default="")
    asyncio.run(execute(parser.parse_args()))
