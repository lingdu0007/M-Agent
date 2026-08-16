"""Ticket 08 public Runner contracts for typed model bindings."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path


_WORKER = Path(__file__).parent / "fixtures" / "crash_worker.py"
_CRASH_EXIT_CODE = 17


class TypedModelContractRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_boolean_capabilities_and_definition_field_are_rejected(
        self,
    ) -> None:
        from pydantic import ValidationError

        from m_agent.adapters import DeterministicModelAdapter
        from m_agent.runtime import AgentDefinition, ModelCapabilities

        with self.assertRaises(ValidationError):
            ModelCapabilities(tool_calling=True)
        with self.assertRaises(ValidationError):
            AgentDefinition(
                definition_id="legacy-capabilities",
                version="1",
                instructions="Reply.",
                required_capabilities=ModelCapabilities(),
                model_adapter=DeterministicModelAdapter(),
            )

    async def test_runner_freezes_a_typed_primary_binding(self) -> None:
        """A public Runner Run persists the exact checked primary Contract."""
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelContract,
            ModelExecutionBudget,
            ModelLimits,
            ModelPurpose,
            ModelRequirements,
            RevisionStability,
            Runner,
        )

        contract = ModelContract(
            contract_id="deterministic-text",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:text",
            capabilities=ModelCapabilities(),
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
            fingerprint="deterministic-text-v1",
        )
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition(
                definition_id="typed-contract",
                version="1",
                instructions="Reply deterministically.",
                model_requirements=ModelRequirements(
                    min_context_window_tokens=64,
                    min_output_tokens=16,
                ),
                model_execution_budget=ModelExecutionBudget(
                    run_max_attempts=1,
                    primary_max_attempts=1,
                    context_compression_max_attempts=0,
                    output_repair_max_attempts=0,
                ),
                model_adapter=DeterministicModelAdapter(
                    ("accepted",), model_contract=contract
                ),
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run("typed-contract", "1", "hello")
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)

        self.assertEqual(terminal.output, "accepted")
        assert inspection.run.snapshot is not None
        primary = inspection.run.snapshot.model_bindings.for_purpose(
            ModelPurpose.PRIMARY
        )
        self.assertEqual(primary.contract, contract)
        self.assertEqual(primary.requirements.min_context_window_tokens, 64)

    async def test_incompatible_requirements_fail_before_any_model_dispatch(
        self,
    ) -> None:
        from m_agent.adapters import DeterministicModelAdapter
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelCapabilityError,
            ModelRequirements,
            ToolCallingMode,
        )

        adapter = DeterministicModelAdapter(("unreachable",))
        requirements = ModelRequirements(
            capabilities=ModelCapabilities(tool_calling=ToolCallingMode.NATIVE)
        )
        match = requirements.match(adapter.model_contract)

        self.assertFalse(match.compatible)
        self.assertEqual(match.reason.value, "TOOL_CALLING_UNSUPPORTED")
        registry = DefinitionRegistry()
        with self.assertRaisesRegex(ModelCapabilityError, "TOOL_CALLING_UNSUPPORTED"):
            registry.register(
                AgentDefinition(
                    definition_id="requires-tools",
                    version="1",
                    instructions="Never dispatch.",
                    model_requirements=requirements,
                    model_adapter=adapter,
                )
            )
        self.assertEqual(adapter.call_count, 0)

    async def test_run_budget_stops_a_second_primary_dispatch(self) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            DeterministicTool,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelExecutionBudget,
            ModelRequirements,
            ModelResponse,
            Runner,
            RunStatus,
            ToolCall,
            ToolCallingMode,
            ToolEffect,
            ToolOutcome,
        )

        class ToolThenAnswer(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    )
                )

            async def generate(self, request):
                self.call_count += 1
                self._last_request = request
                if not request.tool_outcomes:
                    return ModelResponse(
                        tool_calls=(
                            ToolCall(
                                call_id="tool-1",
                                tool_name="lookup",
                                arguments="{}",
                            ),
                        )
                    )
                return ModelResponse(content="must not be called")

        adapter = ToolThenAnswer()
        tool = DeterministicTool(
            name="lookup",
            effect=ToolEffect.READ_ONLY,
            handler=lambda request: ToolOutcome.success(
                request.call_id, request.tool_name, "found"
            ),
        )
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition(
                definition_id="bounded-tool-loop",
                version="1",
                instructions="Use the tool once.",
                model_requirements=ModelRequirements(
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    )
                ),
                model_execution_budget=ModelExecutionBudget(
                    run_max_attempts=1,
                    primary_max_attempts=1,
                    context_compression_max_attempts=0,
                    output_repair_max_attempts=0,
                ),
                model_adapter=adapter,
                tools=(tool,),
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run("bounded-tool-loop", "1", "lookup")
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_EXECUTION_BUDGET_EXCEEDED")
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(
            len([attempt for attempt in inspection.attempts if attempt.model_purpose]),
            1,
        )

    async def test_usage_keeps_unavailable_and_rejects_missing_required_fields(
        self,
    ) -> None:
        from m_agent import deserialize_model_response
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelContract,
            ModelLimits,
            ModelUsageGuarantees,
            RevisionStability,
            Runner,
            RunStatus,
            UsageFieldGuarantee,
            UsageProvenance,
        )

        class UsageAdapter(DeterministicModelAdapter):
            def __init__(self, provenance: UsageProvenance) -> None:
                super().__init__(("accepted",))
                self._provenance = provenance

            async def generate(self, request):
                response = await super().generate(request)
                from m_agent.runtime import ModelUsage, ModelResponse

                return ModelResponse(
                    content=response.content,
                    usage=ModelUsage(
                        input_tokens=11,
                        output_tokens=7,
                        provenance=self._provenance,
                    ),
                )

        def contract(guarantees: ModelUsageGuarantees) -> ModelContract:
            return ModelContract(
                contract_id="usage-contract",
                version="1",
                revision_stability=RevisionStability.PINNED,
                model_identity="deterministic:usage",
                limits=ModelLimits(
                    context_window_tokens=128, max_output_tokens=32
                ),
                input_sizer_id="deterministic-v1",
                serialization_id="deterministic-text-v1",
                usage_guarantees=guarantees,
                fingerprint=f"usage-{guarantees.input_tokens.value}",
            )

        async def run(
            guarantees: ModelUsageGuarantees,
            adapter: DeterministicModelAdapter | None = None,
        ):
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition(
                    definition_id="usage",
                    version=guarantees.input_tokens.value,
                    instructions="Reply.",
                    model_adapter=(
                        adapter
                        if adapter is not None
                        else DeterministicModelAdapter(
                            ("accepted",),
                            model_contract=contract(guarantees),
                        )
                    ),
                )
            )
            runner = Runner(
                registry=registry,
                store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
            )
            created = await runner.create_run(
                "usage", guarantees.input_tokens.value, "hello"
            )
            terminal = await runner.start_run(created.run_id)
            return terminal, await runner.inspect_run(created.run_id)

        optional_terminal, optional_inspection = await run(
            ModelUsageGuarantees()
        )
        self.assertIs(optional_terminal.status, RunStatus.SUCCEEDED)
        response = deserialize_model_response(optional_inspection.attempts[0].output)
        assert response.usage is not None
        self.assertIs(response.usage.provenance, UsageProvenance.UNAVAILABLE)
        self.assertIsNone(response.usage.input_tokens)

        provider_terminal, provider_inspection = await run(
            ModelUsageGuarantees(),
            UsageAdapter(UsageProvenance.PROVIDER_REPORTED),
        )
        self.assertIs(provider_terminal.status, RunStatus.SUCCEEDED)
        provider_response = deserialize_model_response(
            provider_inspection.attempts[0].output
        )
        assert provider_response.usage is not None
        self.assertIs(
            provider_response.usage.provenance,
            UsageProvenance.PROVIDER_REPORTED,
        )

        sized_terminal, sized_inspection = await run(
            ModelUsageGuarantees(),
            UsageAdapter(UsageProvenance.RUNTIME_SIZED),
        )
        self.assertIs(sized_terminal.status, RunStatus.SUCCEEDED)
        sized_response = deserialize_model_response(
            sized_inspection.attempts[0].output
        )
        assert sized_response.usage is not None
        self.assertIs(
            sized_response.usage.provenance,
            UsageProvenance.RUNTIME_SIZED,
        )

        required_terminal, required_inspection = await run(
            ModelUsageGuarantees(input_tokens=UsageFieldGuarantee.REQUIRED)
        )
        self.assertIs(required_terminal.status, RunStatus.FAILED)
        self.assertEqual(
            required_terminal.error_code, "MODEL_CONTRACT_VIOLATION"
        )
        self.assertEqual(len(required_inspection.attempts), 1)

    async def test_recovery_rejects_changed_frozen_model_contract_before_dispatch(
        self,
    ) -> None:
        from m_agent import DEFAULT_LEASE_TTL, CrashPoint, FakeClock
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelContract,
            ModelLimits,
            RevisionStability,
            Runner,
        )

        def contract(fingerprint: str) -> ModelContract:
            return ModelContract(
                contract_id="pinned-model",
                version="1",
                revision_stability=RevisionStability.PINNED,
                model_identity="deterministic:pinned",
                limits=ModelLimits(
                    context_window_tokens=128, max_output_tokens=32
                ),
                input_sizer_id="deterministic-v1",
                serialization_id="deterministic-text-v1",
                fingerprint=fingerprint,
            )

        first_adapter = DeterministicModelAdapter(
            ("first",), model_contract=contract("first-revision")
        )
        first_registry = DefinitionRegistry()
        first_registry.register(
            AgentDefinition(
                definition_id="frozen-binding",
                version="1",
                instructions="Reply.",
                model_adapter=first_adapter,
            )
        )
        clock = FakeClock()
        store = InMemoryRunStore(
            payload_codec=PlaintextPayloadCodec(), clock=clock
        )

        def crash_before_checkpoint(point: CrashPoint, run_id: str) -> None:
            if point is CrashPoint.BEFORE_MODEL_CHECKPOINT:
                raise RuntimeError("injected crash")

        first_runner = Runner(
            registry=first_registry,
            store=store,
            crash_hook=crash_before_checkpoint,
        )
        created = await first_runner.create_run("frozen-binding", "1", "hi")
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            await first_runner.start_run(created.run_id)
        self.assertEqual(first_adapter.call_count, 1)

        changed_adapter = DeterministicModelAdapter(
            ("must not dispatch",), model_contract=contract("second-revision")
        )
        changed_registry = DefinitionRegistry()
        changed_registry.register(
            AgentDefinition(
                definition_id="frozen-binding",
                version="1",
                instructions="Reply.",
                model_adapter=changed_adapter,
            )
        )
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        with self.assertRaisesRegex(RuntimeError, "snapshot Model Contract"):
            await Runner(registry=changed_registry, store=store).resume_run(
                created.run_id
            )
        self.assertEqual(changed_adapter.call_count, 0)

    async def test_sqlite_recovery_consumes_crashed_pre_dispatch_reservation(
        self,
    ) -> None:
        from m_agent import FakeClock
        from m_agent.adapters import (
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelExecutionBudget,
            Runner,
            RunStatus,
        )
        from fixtures.crash_worker import LoggingModelAdapter

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "run.db")
            model_log = os.path.join(tmp, "model-calls.log")
            environment = {
                **os.environ,
                "M_AGENT_TEST_MODEL_BUDGET": "1",
            }
            proc = subprocess.run(
                [
                    sys.executable,
                    str(_WORKER),
                    db_path,
                    model_log,
                    "after_model_attempt_reservation",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=environment,
            )
            self.assertEqual(proc.returncode, _CRASH_EXIT_CODE)
            run_id = next(
                line.split("=", 1)[1]
                for line in proc.stdout.splitlines()
                if line.startswith("RUN_ID=")
            )
            self.assertFalse(os.path.exists(model_log))

            probe = SQLiteRunStore(
                db_path, payload_codec=PlaintextPayloadCodec()
            )
            try:
                crashed = await probe.get_run(run_id)
                assert crashed is not None
                assert crashed.lease_expires_at is not None
                restart = FakeClock(
                    start=crashed.lease_expires_at + timedelta(seconds=1)
                )
            finally:
                probe.close()

            adapter = LoggingModelAdapter(model_log, ("must not dispatch",))
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition(
                    definition_id="assistant",
                    version="1.0",
                    instructions="Answer deterministically.",
                    model_execution_budget=ModelExecutionBudget(
                        run_max_attempts=1,
                        primary_max_attempts=1,
                        context_compression_max_attempts=0,
                        output_repair_max_attempts=0,
                    ),
                    model_adapter=adapter,
                )
            )
            store = SQLiteRunStore(
                db_path,
                payload_codec=PlaintextPayloadCodec(),
                clock=restart,
            )
            try:
                terminal = await Runner(registry=registry, store=store).resume_run(
                    run_id
                )
                inspection = await Runner(
                    registry=registry, store=store
                ).inspect_run(run_id)
            finally:
                store.close()

            self.assertIs(terminal.status, RunStatus.FAILED)
            self.assertEqual(
                terminal.error_code, "MODEL_EXECUTION_BUDGET_EXCEEDED"
            )
            self.assertEqual(adapter.call_count, 0)
            self.assertFalse(os.path.exists(model_log))
            self.assertEqual(
                len(
                    [
                        attempt
                        for attempt in inspection.attempts
                        if attempt.model_purpose is not None
                    ]
                ),
                1,
            )
