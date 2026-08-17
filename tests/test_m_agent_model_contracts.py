"""Ticket 08 public Runner contracts for typed model bindings."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path


_WORKER = Path(__file__).parent / "fixtures" / "crash_worker.py"
_CRASH_EXIT_CODE = 17


class TypedModelContractRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_boolean_capabilities_and_definition_field_remain_supported(
        self,
    ) -> None:
        from m_agent import (
            AgentDefinition,
            DeterministicModelAdapter,
            DefinitionRegistry,
            InMemoryRunStore,
            ModelCapabilities,
            PlaintextPayloadCodec,
            Runner,
            RunStatus,
        )
        from m_agent.runtime import ModelPurpose, ToolCallingMode

        capabilities = ModelCapabilities(tool_calling=True)
        self.assertIs(capabilities.tool_calling, True)
        self.assertIs(ModelCapabilities().tool_calling, False)
        self.assertEqual(
            json.loads(ModelCapabilities(streaming=True).model_dump_json()),
            {
                "streaming": True,
                "tool_calling": False,
                "structured_output": False,
                "usage_reporting": False,
            },
        )
        self.assertEqual(
            json.loads(
                ModelCapabilities(streaming=True).model_dump_json(
                    include={"streaming"}
                )
            ),
            {"streaming": True},
        )
        self.assertIs(
            ModelCapabilities.model_validate(
                {"streaming": True, "legacy_extension": "ignored"}
            ).streaming,
            True,
        )
        self.assertEqual(
            len(
                ModelCapabilities(
                    streaming=True, tool_calling=True
                ).supported_combinations
            ),
            0,
        )
        adapter = DeterministicModelAdapter(
            ("answer",), capabilities=capabilities
        )
        definition = AgentDefinition(
            definition_id="legacy-capabilities",
            version="1",
            instructions="Reply.",
            required_capabilities=capabilities,
            model_adapter=adapter,
        )
        registry = DefinitionRegistry()
        registry.register(definition)
        runner = Runner(
            registry, InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        )

        created = await runner.create_run("legacy-capabilities", "1", "hello")
        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        assert created.snapshot is not None
        self.assertIs(
            created.snapshot.model_bindings.for_purpose(
                ModelPurpose.PRIMARY
            ).requirements.capabilities.tool_calling,
            ToolCallingMode.NATIVE,
        )

    async def test_legacy_model_adapter_can_construct_and_run(self) -> None:
        from m_agent.adapters import InMemoryRunStore, PlaintextPayloadCodec
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelAdapter,
            ModelCapabilities,
            ModelResponse,
            Runner,
            RunStatus,
        )

        class LegacyAdapter(ModelAdapter):
            capabilities = ModelCapabilities()

            async def generate(self, request) -> ModelResponse:
                return ModelResponse(content="legacy answer")

        unconfigured = AgentDefinition(
            definition_id="legacy-adapter-unconfigured",
            version="1",
            instructions="Reply.",
            model_adapter=LegacyAdapter(),
        )
        self.assertIsNotNone(unconfigured.model_bindings)

        class RegisteredLegacyAdapter(LegacyAdapter):
            def definition_contract_fingerprint(self) -> str:
                return "legacy-configuration-v1"

        definition = AgentDefinition(
            definition_id="legacy-adapter",
            version="1",
            instructions="Reply.",
            model_adapter=RegisteredLegacyAdapter(),
        )
        self.assertIsNotNone(definition.model_bindings)
        registry = DefinitionRegistry()
        registry.register(definition)
        runner = Runner(
            registry, InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        )
        created = await runner.create_run("legacy-adapter", "1", input="hello")
        terminal = await runner.start_run(created.run_id)
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(terminal.output, "legacy answer")

    async def test_legacy_sqlite_snapshot_is_migrated_and_runs(self) -> None:
        import json
        import sqlite3

        from m_agent.adapters import (
            DeterministicModelAdapter,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            Runner,
            RunStatus,
        )

        capabilities = ModelCapabilities(tool_calling=True)
        adapter = DeterministicModelAdapter(
            ("answer",), capabilities=capabilities
        )
        definition = AgentDefinition(
            definition_id="legacy-sqlite-snapshot",
            version="1",
            instructions="Reply.",
            required_capabilities=capabilities,
            model_adapter=adapter,
        )
        registry = DefinitionRegistry()
        registry.register(definition)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "legacy-snapshot.db")
            store = SQLiteRunStore(db_path, payload_codec=PlaintextPayloadCodec())
            try:
                created = await Runner(registry, store).create_run(
                    "legacy-sqlite-snapshot", "1", "hello"
                )
            finally:
                store.close()

            legacy_snapshot = {
                "definition_id": definition.definition_id,
                "version": definition.version,
                "instructions": definition.instructions,
                "required_capabilities": {
                    "streaming": False,
                    "tool_calling": True,
                    "structured_output": False,
                    "usage_reporting": False,
                },
                "adapter_capabilities": {
                    "streaming": False,
                    "tool_calling": True,
                    "structured_output": False,
                    "usage_reporting": False,
                },
                "adapter_contract_fingerprint": (
                    adapter.definition_contract_fingerprint()
                ),
                "has_context_provider": False,
                "tool_declarations": [],
                "retry_policy": None,
            }
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    "UPDATE runs SET snapshot_json=? WHERE run_id=?",
                    (json.dumps(legacy_snapshot), created.run_id),
                )
                connection.execute(
                    "DELETE FROM run_payloads WHERE run_id=? AND field=?",
                    (created.run_id, "run:snapshot"),
                )
                connection.commit()
            finally:
                connection.close()

            reopened = SQLiteRunStore(
                db_path, payload_codec=PlaintextPayloadCodec()
            )
            try:
                migrated = await reopened.get_run(created.run_id)
                assert migrated is not None
                assert migrated.snapshot is not None
                self.assertIsNone(migrated.snapshot.model_bindings)
                terminal = await Runner(registry, reopened).start_run(created.run_id)
            finally:
                reopened.close()

        self.assertIs(terminal.status, RunStatus.SUCCEEDED)

    async def test_legacy_snapshot_without_fingerprint_preserves_0_2_resume(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            DefinitionSnapshot,
            ModelCapabilities,
            RunRecord,
            Runner,
            RunStatus,
        )

        adapter = DeterministicModelAdapter(("DRIFTED",))
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="unverified-legacy-snapshot",
                version="1",
                instructions="Reply.",
                model_adapter=adapter,
            )
        )
        snapshot = DefinitionSnapshot(
            definition_id="unverified-legacy-snapshot",
            version="1",
            instructions="Reply.",
            required_capabilities=ModelCapabilities(),
            adapter_capabilities=adapter.capabilities,
            adapter_contract_fingerprint="",
        )
        store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        created = await store.create_run(
            RunRecord(
                run_id="unverified-legacy-snapshot-run",
                definition_id=snapshot.definition_id,
                definition_version=snapshot.version,
                input="hello",
                status=RunStatus.CREATED,
                snapshot=snapshot,
            )
        )
        runner = Runner(registry, store)

        terminal = await runner.start_run(created.run_id)

        self.assertEqual(adapter.call_count, 1)
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)

    async def test_recovery_does_not_replay_persisted_failed_model_step(
        self,
    ) -> None:
        from m_agent import DEFAULT_LEASE_TTL, FakeClock
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            FailureClassification,
            ModelFailure,
            ModelResponse,
            Runner,
            RunStatus,
            StepStatus,
        )

        class PermanentThenSuccessAdapter(DeterministicModelAdapter):
            async def generate(self, request):
                self.call_count += 1
                self._last_request = request
                if self.call_count == 1:
                    raise ModelFailure(
                        FailureClassification.PERMANENT,
                        "model_rejected",
                        "permanent model rejection",
                    )
                return ModelResponse(content="unexpected replay success")

        class InterruptedTerminalFailure:
            fail_terminal_transition = True

            async def transition_run(self, *args, **kwargs):
                if (
                    self.fail_terminal_transition
                    and kwargs.get("status") is RunStatus.FAILED
                ):
                    self.fail_terminal_transition = False
                    raise RuntimeError("simulated loss after failed model step")
                return await super().transition_run(*args, **kwargs)

        class InterruptedInMemoryStore(
            InterruptedTerminalFailure, InMemoryRunStore
        ):
            pass

        class InterruptedSQLiteStore(InterruptedTerminalFailure, SQLiteRunStore):
            pass

        def registry_and_adapter():
            adapter = PermanentThenSuccessAdapter(("unused",))
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="persisted-model-failure",
                    version="1",
                    instructions="Reply.",
                    model_adapter=adapter,
                )
            )
            return registry, adapter

        async def assert_no_replay(store, clock, registry, adapter) -> None:
            runner = Runner(registry, store)
            created = await runner.create_run(
                "persisted-model-failure", "1", "hello"
            )
            with self.assertRaisesRegex(RuntimeError, "failed model step"):
                await runner.start_run(created.run_id)
            before = await runner.inspect_run(created.run_id)
            self.assertIs(before.run.status, RunStatus.RUNNING)
            self.assertEqual(adapter.call_count, 1)
            self.assertEqual(
                [step.status for step in before.steps], [StepStatus.FAILED]
            )
            self.assertEqual(
                [attempt.status for attempt in before.attempts],
                [StepStatus.FAILED],
            )

            clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
            terminal = await Runner(registry, store).resume_run(created.run_id)
            after = await Runner(registry, store).inspect_run(created.run_id)
            self.assertIs(terminal.status, RunStatus.FAILED)
            self.assertEqual(adapter.call_count, 1)
            self.assertEqual(
                [step.status for step in after.steps], [StepStatus.FAILED]
            )
            self.assertEqual(
                [attempt.status for attempt in after.attempts],
                [StepStatus.FAILED],
            )

        memory_clock = FakeClock()
        memory_store = InterruptedInMemoryStore(
            payload_codec=PlaintextPayloadCodec(), clock=memory_clock
        )
        memory_registry, memory_adapter = registry_and_adapter()
        await assert_no_replay(
            memory_store, memory_clock, memory_registry, memory_adapter
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "persisted-model-failure.db")
            first_clock = FakeClock()
            first_store = InterruptedSQLiteStore(
                db_path,
                payload_codec=PlaintextPayloadCodec(),
                clock=first_clock,
            )
            registry, adapter = registry_and_adapter()
            try:
                runner = Runner(registry, first_store)
                created = await runner.create_run(
                    "persisted-model-failure", "1", "hello"
                )
                with self.assertRaisesRegex(RuntimeError, "failed model step"):
                    await runner.start_run(created.run_id)
                crashed = await first_store.get_run(created.run_id)
                assert crashed is not None
                assert crashed.lease_expires_at is not None
                restart_clock = FakeClock(
                    crashed.lease_expires_at + timedelta(seconds=1)
                )
            finally:
                first_store.close()

            reopened = SQLiteRunStore(
                db_path,
                payload_codec=PlaintextPayloadCodec(),
                clock=restart_clock,
            )
            try:
                terminal = await Runner(registry, reopened).resume_run(
                    created.run_id
                )
                inspection = await Runner(registry, reopened).inspect_run(
                    created.run_id
                )
            finally:
                reopened.close()

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(
            [step.status for step in inspection.steps], [StepStatus.FAILED]
        )
        self.assertEqual(
            [attempt.status for attempt in inspection.attempts],
            [StepStatus.FAILED],
        )

    async def test_recovery_preserves_pre_dispatch_budget_failure_code(
        self,
    ) -> None:
        from m_agent import DEFAULT_LEASE_TTL, FakeClock
        from m_agent.adapters import (
            DeterministicModelAdapter,
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
            StepStatus,
        )

        class InterruptedTerminalFailure:
            fail_terminal_transition = True

            async def transition_run(self, *args, **kwargs):
                if (
                    self.fail_terminal_transition
                    and kwargs.get("status") is RunStatus.FAILED
                ):
                    self.fail_terminal_transition = False
                    raise SystemExit("simulated loss after budget failure")
                return await super().transition_run(*args, **kwargs)

        class InterruptedInMemoryStore(
            InterruptedTerminalFailure, InMemoryRunStore
        ):
            pass

        class InterruptedSQLiteStore(InterruptedTerminalFailure, SQLiteRunStore):
            pass

        def registry_and_adapter():
            adapter = DeterministicModelAdapter(("must not dispatch",))
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="persisted-budget-failure",
                    version="1",
                    instructions="Never dispatch.",
                    model_execution_budget=ModelExecutionBudget(
                        run_max_attempts=0,
                        primary_max_attempts=0,
                        context_compression_max_attempts=0,
                        output_repair_max_attempts=0,
                    ),
                    model_adapter=adapter,
                )
            )
            return registry, adapter

        async def assert_memory_recovery() -> None:
            clock = FakeClock()
            store = InterruptedInMemoryStore(
                payload_codec=PlaintextPayloadCodec(), clock=clock
            )
            registry, adapter = registry_and_adapter()
            runner = Runner(registry, store)
            created = await runner.create_run(
                "persisted-budget-failure", "1", "hello"
            )
            with self.assertRaisesRegex(SystemExit, "budget failure"):
                await runner.start_run(created.run_id)
            before = await runner.inspect_run(created.run_id)
            self.assertIs(before.run.status, RunStatus.RUNNING)
            self.assertEqual(adapter.call_count, 0)
            self.assertEqual(
                before.steps[0].error_code,
                "MODEL_EXECUTION_BUDGET_EXCEEDED",
            )

            clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
            terminal = await Runner(registry, store).resume_run(created.run_id)
            self.assertIs(terminal.status, RunStatus.FAILED)
            self.assertEqual(
                terminal.error_code, "MODEL_EXECUTION_BUDGET_EXCEEDED"
            )
            self.assertEqual(adapter.call_count, 0)

        await assert_memory_recovery()

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "persisted-budget-failure.db")
            first_clock = FakeClock()
            first_store = InterruptedSQLiteStore(
                db_path,
                payload_codec=PlaintextPayloadCodec(),
                clock=first_clock,
            )
            registry, adapter = registry_and_adapter()
            try:
                runner = Runner(registry, first_store)
                created = await runner.create_run(
                    "persisted-budget-failure", "1", "hello"
                )
                with self.assertRaisesRegex(SystemExit, "budget failure"):
                    await runner.start_run(created.run_id)
                before = await runner.inspect_run(created.run_id)
                self.assertIs(before.run.status, RunStatus.RUNNING)
                self.assertEqual(
                    before.steps[0].error_code,
                    "MODEL_EXECUTION_BUDGET_EXCEEDED",
                )
                crashed = await first_store.get_run(created.run_id)
                assert crashed is not None
                assert crashed.lease_expires_at is not None
                restart_clock = FakeClock(
                    crashed.lease_expires_at + timedelta(seconds=1)
                )
            finally:
                first_store.close()

            reopened = SQLiteRunStore(
                db_path,
                payload_codec=PlaintextPayloadCodec(),
                clock=restart_clock,
            )
            try:
                terminal = await Runner(registry, reopened).resume_run(
                    created.run_id
                )
            finally:
                reopened.close()

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(
            terminal.error_code, "MODEL_EXECUTION_BUDGET_EXCEEDED"
        )
        self.assertEqual(adapter.call_count, 0)

    def test_live_contract_metadata_is_explicit_and_coherent(self) -> None:
        from pydantic import ValidationError

        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelAdapter,
            ModelCapabilities,
            ModelContract,
            ModelLimits,
            ModelRequirements,
            ModelUsageGuarantees,
            RevisionStability,
            UsageFieldGuarantee,
            UsageReportingMode,
        )

        contract_fields = dict(
            contract_id="contract",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="provider:model",
            limits=ModelLimits(context_window_tokens=8, max_output_tokens=4),
            input_sizer_id="provider-sizer-v1",
            serialization_id="provider-wire-v1",
        )
        with self.assertRaises(ValidationError):
            ModelContract(**contract_fields, fingerprint="")
        with self.assertRaises(ValidationError):
            ModelContract(
                **contract_fields,
                capabilities=ModelCapabilities(
                    usage_reporting=UsageReportingMode.NONE
                ),
                usage_guarantees=ModelUsageGuarantees(
                    input_tokens=UsageFieldGuarantee.REQUIRED
                ),
            )

        class UndeclaredLiveAdapter(ModelAdapter):
            capabilities = ModelCapabilities()

            async def generate(self, request):
                raise AssertionError("must not be dispatched")

        with self.assertRaisesRegex(ValueError, "instance ModelContract"):
            DefinitionRegistry().register(
                AgentDefinition.for_adapter(
                    definition_id="unknown-live-contract",
                    version="1",
                    instructions="Never dispatch.",
                    model_adapter=UndeclaredLiveAdapter(),
                )
            )

    def test_contract_fingerprint_and_structured_guarantees_are_semantic(self) -> None:
        """Contracts cannot share a fingerprint across different guarantees."""
        from pydantic import ValidationError

        from m_agent.runtime import (
            ModelCapabilities,
            ModelContract,
            ModelLimits,
            ModelRequirementReason,
            ModelRequirements,
            RevisionStability,
            StructuredOutputMode,
        )

        fields = dict(
            contract_id="structured-contract",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="provider:structured",
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="provider-sizer-v1",
            serialization_id="provider-wire-v1",
        )
        strict = ModelContract(
            **fields,
            capabilities=ModelCapabilities(
                structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
            ),
        )
        json_object = ModelContract(
            **fields,
            capabilities=ModelCapabilities(
                structured_output=StructuredOutputMode.JSON_OBJECT
            ),
        )

        self.assertNotEqual(strict.fingerprint, json_object.fingerprint)
        with self.assertRaises(ValidationError):
            ModelContract(
                **fields,
                capabilities=ModelCapabilities(
                    structured_output=StructuredOutputMode.JSON_OBJECT
                ),
                fingerprint=strict.fingerprint,
            )
        self.assertEqual(
            strict.model_copy(update={"fingerprint": "0" * 64}).fingerprint,
            strict.semantic_fingerprint(),
        )
        strict_required = ModelRequirements(
            capabilities=ModelCapabilities(
                structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
            )
        )
        self.assertFalse(strict_required.match(json_object).compatible)
        self.assertIs(
            strict_required.match(json_object).reason,
            ModelRequirementReason.STRUCTURED_OUTPUT_UNSUPPORTED,
        )
        self.assertTrue(
            ModelRequirements(
                capabilities=ModelCapabilities(
                    structured_output=StructuredOutputMode.JSON_OBJECT
                )
            ).match(strict).compatible
        )

    async def test_adapter_class_capability_ceiling_rejects_instance_contract(
        self,
    ) -> None:
        from m_agent.adapters import InMemoryRunStore, PlaintextPayloadCodec
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionNotFoundError,
            DefinitionRegistry,
            ModelAdapter,
            ModelCapabilities,
            ModelCapabilityError,
            ModelContract,
            ModelLimits,
            ModelRequirements,
            ModelResponse,
            RevisionStability,
            Runner,
            StreamingMode,
        )

        contract = ModelContract(
            contract_id="ceiling-mismatch",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="custom:ceiling-mismatch",
            capabilities=ModelCapabilities(streaming=StreamingMode.DELTA),
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="custom-sizer-v1",
            serialization_id="custom-wire-v1",
            configuration_fingerprint="custom-ceiling-mismatch-v1",
        )

        class CeilingMismatchAdapter(ModelAdapter):
            capabilities = ModelCapabilities()

            def __init__(self) -> None:
                self.dispatches = 0

            @property
            def model_contract(self) -> ModelContract:
                return contract

            def definition_contract_fingerprint(self) -> str:
                return "custom-ceiling-mismatch-v1"

            async def generate(self, request) -> ModelResponse:
                self.dispatches += 1
                return ModelResponse(content="must not dispatch")

        adapter = CeilingMismatchAdapter()
        registry = DefinitionRegistry()
        with self.assertRaisesRegex(
            ModelCapabilityError, "STREAMING_UNSUPPORTED"
        ):
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="ceiling-mismatch",
                    version="1",
                    instructions="Never dispatch.",
                    model_requirements=ModelRequirements(
                        capabilities=ModelCapabilities(
                            streaming=StreamingMode.DELTA
                        )
                    ),
                    model_adapter=adapter,
                )
            )
        self.assertFalse(registry.is_registered("ceiling-mismatch", "1"))
        self.assertEqual(adapter.dispatches, 0)

        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )
        with self.assertRaisesRegex(DefinitionNotFoundError, "ceiling-mismatch"):
            await runner.create_run("ceiling-mismatch", "1", "hello")
        self.assertEqual(adapter.dispatches, 0)

    def test_contract_identity_version_rejects_semantic_collision(self) -> None:
        """One contract id/version resolves to one frozen semantic Contract."""
        from m_agent.adapters import DeterministicModelAdapter
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionConflictError,
            DefinitionRegistry,
            ModelContract,
            ModelLimits,
            RevisionStability,
        )

        contract = ModelContract(
            contract_id="shared-contract",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:shared-contract",
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )
        changed_configuration = contract.model_copy(
            update={"configuration_fingerprint": "deployment-b"}
        )
        registry = DefinitionRegistry()

        def definition(definition_id: str, bound_contract: ModelContract):
            return AgentDefinition.for_adapter(
                definition_id=definition_id,
                version="1",
                instructions="Reply.",
                model_adapter=DeterministicModelAdapter(
                    ("answer",), model_contract=bound_contract
                ),
            )

        registry.register(definition("first-contract-user", contract))
        registry.register(definition("second-contract-user", contract))
        with self.assertRaisesRegex(DefinitionConflictError, "shared-contract@1"):
            registry.register(
                definition("conflicting-contract-user", changed_configuration)
            )

        self.assertFalse(registry.is_registered("conflicting-contract-user", "1"))

    def test_secondary_binding_requires_an_explicit_adapter_owner(self) -> None:
        """A direct secondary Contract cannot exist without its Adapter."""
        from m_agent.adapters import DeterministicModelAdapter
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelBinding,
            ModelBindingSet,
            ModelCapabilityError,
            ModelContract,
            ModelLimits,
            ModelPurpose,
            RevisionStability,
        )

        def contract(contract_id: str) -> ModelContract:
            return ModelContract(
                contract_id=contract_id,
                version="1",
                revision_stability=RevisionStability.PINNED,
                model_identity=f"deterministic:{contract_id}",
                limits=ModelLimits(
                    context_window_tokens=128, max_output_tokens=32
                ),
                input_sizer_id="deterministic-v1",
                serialization_id="deterministic-text-v1",
            )

        primary_adapter = DeterministicModelAdapter(
            ("primary",), model_contract=contract("primary")
        )
        secondary_adapter = DeterministicModelAdapter(
            ("secondary",), model_contract=contract("secondary")
        )
        primary = ModelBinding(
            purpose=ModelPurpose.PRIMARY,
            contract=primary_adapter.model_contract,
        )
        bindings = ModelBindingSet(
            bindings=(
                primary,
                ModelBinding(
                    purpose=ModelPurpose.CONTEXT_COMPRESSION,
                    contract=secondary_adapter.model_contract,
                ),
                primary.model_copy(
                    update={
                        "purpose": ModelPurpose.OUTPUT_REPAIR,
                        "source_purpose": ModelPurpose.PRIMARY,
                    }
                ),
            )
        )

        with self.assertRaisesRegex(ModelCapabilityError, "Adapter owner"):
            DefinitionRegistry().register(
                AgentDefinition.for_adapter(
                    definition_id="secondary-owner-missing",
                    version="1",
                    instructions="Reply.",
                    model_bindings=bindings,
                    model_adapter=primary_adapter,
                )
            )
        with self.assertRaisesRegex(ModelCapabilityError, "does not match"):
            DefinitionRegistry().register(
                AgentDefinition.for_adapter(
                    definition_id="secondary-owner-mismatch",
                    version="1",
                    instructions="Reply.",
                    model_bindings=bindings,
                    model_adapter=primary_adapter,
                    model_adapters={
                        ModelPurpose.CONTEXT_COMPRESSION: primary_adapter
                    },
                )
            )

    def test_secondary_binding_resolves_its_explicit_adapter_owner(self) -> None:
        """A complete Binding Set exposes every declared execution owner."""
        from m_agent.adapters import DeterministicModelAdapter
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelBinding,
            ModelBindingSet,
            ModelContract,
            ModelLimits,
            ModelPurpose,
            RevisionStability,
        )

        def contract(contract_id: str) -> ModelContract:
            return ModelContract(
                contract_id=contract_id,
                version="1",
                revision_stability=RevisionStability.PINNED,
                model_identity=f"deterministic:{contract_id}",
                limits=ModelLimits(
                    context_window_tokens=128, max_output_tokens=32
                ),
                input_sizer_id="deterministic-v1",
                serialization_id="deterministic-text-v1",
            )

        primary_adapter = DeterministicModelAdapter(
            ("primary",), model_contract=contract("primary")
        )
        secondary_adapter = DeterministicModelAdapter(
            ("secondary",), model_contract=contract("secondary")
        )
        primary = ModelBinding(
            purpose=ModelPurpose.PRIMARY,
            contract=primary_adapter.model_contract,
        )
        bindings = ModelBindingSet(
            bindings=(
                primary,
                ModelBinding(
                    purpose=ModelPurpose.CONTEXT_COMPRESSION,
                    contract=secondary_adapter.model_contract,
                ),
                primary.model_copy(
                    update={
                        "purpose": ModelPurpose.OUTPUT_REPAIR,
                        "source_purpose": ModelPurpose.PRIMARY,
                    }
                ),
            )
        )
        definition = AgentDefinition.for_adapter(
            definition_id="secondary-owner-present",
            version="1",
            instructions="Reply.",
            model_bindings=bindings,
            model_adapter=primary_adapter,
            model_adapters={
                ModelPurpose.CONTEXT_COMPRESSION: secondary_adapter
            },
        )

        DefinitionRegistry().register(definition)

        self.assertIs(
            definition.model_adapter_for(ModelPurpose.CONTEXT_COMPRESSION),
            secondary_adapter,
        )
        self.assertIs(
            definition.model_adapter_for(ModelPurpose.OUTPUT_REPAIR),
            primary_adapter,
        )

    def test_usage_value_cannot_claim_unavailable_provenance(self) -> None:
        from pydantic import ValidationError

        from m_agent.runtime import ModelUsage, UsageProvenance

        with self.assertRaises(ValidationError):
            ModelUsage(
                input_tokens=5,
                input_tokens_provenance=UsageProvenance.UNAVAILABLE,
            )
        with self.assertRaises(ValidationError):
            ModelUsage(
                input_tokens_provenance=UsageProvenance.PROVIDER_REPORTED,
            )
        for value in (-1, True, 1.0, "1"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                ModelUsage(input_tokens=value)
        empty = ModelUsage()
        self.assertIs(empty.provenance, UsageProvenance.UNAVAILABLE)
        self.assertTrue(
            all(
                getattr(empty, f"{field}_provenance")
                is UsageProvenance.UNAVAILABLE
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "reasoning_tokens",
                )
            )
        )

    async def test_contract_violation_failure_message_is_redacted(self) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelContractViolationError,
            Runner,
            RunStatus,
        )

        class InvalidatingAdapter(DeterministicModelAdapter):
            def validate_response(self, request, response):
                raise ModelContractViolationError("credential-canary-G27")

        adapter = InvalidatingAdapter(("answer",))
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="redacted-contract-error",
                version="1",
                instructions="Reply.",
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry, InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        )
        created = await runner.create_run(
            "redacted-contract-error", "1", input="hello"
        )
        terminal = await runner.start_run(created.run_id)
        self.assertIs(terminal.status, RunStatus.FAILED)
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(
            inspection.attempts[0].error,
            "adapter failure diagnostic redacted",
        )
        self.assertNotIn("credential-canary-G27", inspection.attempts[0].error)

    def test_quantitative_model_contract_values_reject_booleans(self) -> None:
        from pydantic import ValidationError

        from m_agent.runtime import (
            ModelExecutionBudget,
            ModelLimits,
            ModelRequirements,
        )

        invalid_constructors = (
            lambda: ModelLimits(
                context_window_tokens=True,
                max_output_tokens=True,
            ),
            lambda: ModelRequirements(
                min_context_window_tokens=True,
                min_output_tokens=True,
            ),
            lambda: ModelExecutionBudget(
                run_max_attempts=True,
                primary_max_attempts=False,
            ),
        )
        for constructor in invalid_constructors:
            with self.subTest(constructor=constructor):
                with self.assertRaises(ValidationError):
                    constructor()

    def test_typed_model_contract_values_reject_unknown_fields(self) -> None:
        """Future modes and typos cannot silently weaken a frozen binding."""
        from pydantic import ValidationError

        from m_agent.runtime import (
            ModelBinding,
            ModelBindingSet,
            ModelCapabilities,
            ModelCapabilityCombination,
            ModelContract,
            ModelDelta,
            ModelExecutionBudget,
            ModelLimits,
            ModelPurpose,
            ModelRequirementMatch,
            ModelRequirementReason,
            ModelRequirements,
            ModelResponse,
            ModelUsage,
            ModelUsageGuarantees,
            RevisionStability,
        )

        contract = ModelContract(
            contract_id="closed-model-values",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:closed-model-values",
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )
        bindings = ModelBindingSet.reuse_primary(contract)
        invalid_constructors = (
            lambda: ModelCapabilityCombination(future_mode="NATIVE"),
            lambda: ModelCapabilities(
                streaming="DELTA", future_mode="NATIVE"
            ),
            lambda: ModelLimits(
                context_window_tokens=128,
                max_output_tokens=32,
                future_limit=1,
            ),
            lambda: ModelUsageGuarantees(future_guarantee="REQUIRED"),
            lambda: ModelContract(
                contract_id="closed-model-values",
                version="1",
                revision_stability=RevisionStability.PINNED,
                model_identity="deterministic:closed-model-values",
                limits=ModelLimits(
                    context_window_tokens=128, max_output_tokens=32
                ),
                input_sizer_id="deterministic-v1",
                serialization_id="deterministic-text-v1",
                future_contract="unsupported",
            ),
            lambda: ModelRequirementMatch(
                compatible=True,
                reason=ModelRequirementReason.SATISFIED,
                future_reason="unsupported",
            ),
            lambda: ModelRequirements(future_requirement="unsupported"),
            lambda: ModelBinding(
                purpose=ModelPurpose.PRIMARY,
                contract=contract,
                future_binding="unsupported",
            ),
            lambda: ModelBindingSet(
                bindings=bindings.bindings,
                future_binding_set="unsupported",
            ),
            lambda: ModelExecutionBudget(future_budget=1),
            lambda: ModelUsage(future_usage=1),
            lambda: ModelDelta(content="delta", future_delta="unsupported"),
            lambda: ModelResponse(
                content="response", future_response="unsupported"
            ),
        )

        for constructor in invalid_constructors:
            with self.subTest(constructor=constructor):
                with self.assertRaises(ValidationError):
                    constructor()

    def test_binding_reuse_and_capability_combinations_are_explicit(self) -> None:
        """Binding reuse and multi-mode protocols need durable declarations."""
        from pydantic import ValidationError

        from m_agent.adapters import DeterministicModelAdapter
        from m_agent.runtime import (
            AgentDefinition,
            ModelBinding,
            ModelBindingSet,
            ModelCapabilities,
            ModelCapabilityCombination,
            ModelContract,
            ModelLimits,
            ModelPurpose,
            ModelRequirementReason,
            ModelRequirements,
            RevisionStability,
            StreamingMode,
            ToolCallingMode,
        )

        with self.assertRaises(ValidationError):
            ModelCapabilities(
                streaming=StreamingMode.DELTA,
                tool_calling=ToolCallingMode.NATIVE,
            )
        capabilities = ModelCapabilities(
            streaming=StreamingMode.DELTA,
            tool_calling=ToolCallingMode.NATIVE,
            supported_combinations=(
                ModelCapabilityCombination(
                    streaming=StreamingMode.DELTA,
                    tool_calling=ToolCallingMode.NATIVE,
                ),
            ),
        )
        contract = ModelContract(
            contract_id="explicit-reuse",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:explicit-reuse",
            capabilities=capabilities,
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )
        primary = ModelBinding(
            purpose=ModelPurpose.PRIMARY,
            contract=contract,
        )
        with self.assertRaises(ValidationError):
            ModelBindingSet(bindings=(primary,))
        legacy_definition = AgentDefinition(
            definition_id="implicit-primary-reuse",
            version="1",
            instructions="Use the temporary 0.2 primary reuse path.",
            model_adapter=DeterministicModelAdapter(),
        )
        self.assertIs(
            legacy_definition.model_bindings.for_purpose(
                ModelPurpose.CONTEXT_COMPRESSION
            ).source_purpose,
            ModelPurpose.PRIMARY,
        )
        bindings = ModelBindingSet(
            bindings=(
                primary,
                primary.model_copy(
                    update={
                        "purpose": ModelPurpose.CONTEXT_COMPRESSION,
                        "source_purpose": ModelPurpose.PRIMARY,
                    }
                ),
                primary.model_copy(
                    update={
                        "purpose": ModelPurpose.OUTPUT_REPAIR,
                        "source_purpose": ModelPurpose.PRIMARY,
                    }
                ),
            )
        )
        self.assertEqual(bindings.resolved(), bindings)
        self.assertIs(
            bindings.for_purpose(ModelPurpose.OUTPUT_REPAIR).source_purpose,
            ModelPurpose.PRIMARY,
        )
        split_capabilities = ModelCapabilities(
            streaming=StreamingMode.DELTA,
            tool_calling=ToolCallingMode.NATIVE,
            supported_combinations=(
                ModelCapabilityCombination(streaming=StreamingMode.DELTA),
                ModelCapabilityCombination(tool_calling=ToolCallingMode.NATIVE),
            ),
        )
        split_contract = contract.model_copy(
            update={"capabilities": split_capabilities}
        )
        with self.assertRaises(ValidationError):
            ModelRequirements(capabilities=split_capabilities)
        match = ModelRequirements(capabilities=capabilities).match(split_contract)
        self.assertFalse(match.compatible)
        self.assertIs(
            match.reason,
            ModelRequirementReason.CAPABILITY_COMBINATION_UNSUPPORTED,
        )

    async def test_structured_requirement_is_requested_without_streaming(
        self,
    ) -> None:
        """The frozen requirement selects the structured-only protocol."""
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelCapabilityCombination,
            ModelContract,
            ModelLimits,
            ModelRequirements,
            RevisionStability,
            Runner,
            RunStatus,
            StreamingMode,
            StructuredOutputMode,
        )

        contract = ModelContract(
            contract_id="separate-structured-protocol",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:structured",
            capabilities=ModelCapabilities(
                streaming=StreamingMode.DELTA,
                structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
                supported_combinations=(
                    ModelCapabilityCombination(streaming=StreamingMode.DELTA),
                    ModelCapabilityCombination(
                        structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
                    ),
                ),
            ),
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )
        adapter = DeterministicModelAdapter(
            ('{"answer":"structured"}',), model_contract=contract
        )
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="structured-only",
                version="1",
                instructions="Return the declared native structure.",
                model_requirements=ModelRequirements(
                    capabilities=ModelCapabilities(
                        structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
                    )
                ),
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run("structured-only", "1", "hello")
        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(adapter.call_count, 1)
        assert adapter.last_request is not None
        self.assertIs(
            adapter.last_request.structured_output,
            StructuredOutputMode.JSON_SCHEMA_STRICT,
        )

    async def test_json_object_requirement_is_not_upgraded_to_strict_schema(
        self,
    ) -> None:
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
            ModelLimits,
            ModelRequirements,
            RevisionStability,
            Runner,
            RunStatus,
            StructuredOutputMode,
        )

        contract = ModelContract(
            contract_id="strict-satisfies-json-object",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:json-object",
            capabilities=ModelCapabilities(
                structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
            ),
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )
        adapter = DeterministicModelAdapter(("{}",), model_contract=contract)
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="json-object-requirement",
                version="1",
                instructions="Return JSON.",
                model_requirements=ModelRequirements(
                    capabilities=ModelCapabilities(
                        structured_output=StructuredOutputMode.JSON_OBJECT
                    )
                ),
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run("json-object-requirement", "1", "hi")
        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        assert adapter.last_request is not None
        self.assertIs(
            adapter.last_request.structured_output,
            StructuredOutputMode.JSON_OBJECT,
        )

    async def test_tool_call_can_precede_a_strict_structured_final_response(
        self,
    ) -> None:
        """A tool-only intermediate response does not need final JSON content."""
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
            ModelCapabilityCombination,
            ModelContract,
            ModelLimits,
            ModelRequirements,
            ModelResponse,
            RevisionStability,
            Runner,
            RunStatus,
            StepStatus,
            StepType,
            StructuredOutputMode,
            ToolCall,
            ToolCallingMode,
            ToolEffect,
            ToolOutcome,
        )

        capabilities = ModelCapabilities(
            tool_calling=ToolCallingMode.NATIVE,
            structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
            supported_combinations=(
                ModelCapabilityCombination(
                    tool_calling=ToolCallingMode.NATIVE,
                    structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
                ),
            ),
        )
        contract = ModelContract(
            contract_id="tool-then-strict-json",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:tool-then-strict-json",
            capabilities=capabilities,
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )

        class ToolThenStrictResponseAdapter(DeterministicModelAdapter):
            async def generate(self, request):
                self.call_count += 1
                self._last_request = request
                if self.call_count == 1:
                    return ModelResponse(
                        tool_calls=(
                            ToolCall(
                                call_id="lookup-1",
                                tool_name="lookup",
                                arguments="{}",
                            ),
                        )
                    )
                return ModelResponse(content='{"answer":"confirmed"}')

        adapter = ToolThenStrictResponseAdapter(
            ("unused",), model_contract=contract
        )
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="tool-then-strict-json",
                version="1",
                instructions="Use lookup before returning JSON.",
                model_requirements=ModelRequirements(capabilities=capabilities),
                model_adapter=adapter,
                tools=(
                    DeterministicTool(
                        name="lookup",
                        effect=ToolEffect.READ_ONLY,
                        handler=lambda request: ToolOutcome.success(
                            request.call_id, request.tool_name, "confirmed"
                        ),
                    ),
                ),
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run("tool-then-strict-json", "1", "hi")
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(adapter.call_count, 2)
        self.assertEqual(
            [step.step_type for step in inspection.steps],
            [StepType.MODEL, StepType.TOOL, StepType.MODEL],
        )
        self.assertEqual(inspection.steps[1].status, StepStatus.SUCCEEDED)

    async def test_snapshotless_persisted_run_fails_closed_without_dispatch(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            RunRecord,
            Runner,
            RunStatus,
        )

        with tempfile.TemporaryDirectory() as tmp:
            stores = (
                InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
                SQLiteRunStore(
                    os.path.join(tmp, "snapshotless.db"),
                    payload_codec=PlaintextPayloadCodec(),
                ),
            )
            for index, store in enumerate(stores):
                with self.subTest(store=type(store).__name__):
                    adapter = DeterministicModelAdapter(("must not dispatch",))
                    registry = DefinitionRegistry()
                    registry.register(
                        AgentDefinition.for_adapter(
                            definition_id=f"snapshotless-{index}",
                            version="1",
                            instructions="Never dispatch.",
                            model_adapter=adapter,
                        )
                    )
                    persisted = await store.create_run(
                        RunRecord(
                            run_id=f"snapshotless-{index}",
                            definition_id=f"snapshotless-{index}",
                            definition_version="1",
                            input="hi",
                        )
                    )
                    runner = Runner(registry=registry, store=store)

                    with self.assertRaisesRegex(RuntimeError, "no frozen Model Binding"):
                        await runner.start_run(persisted.run_id)
                    restored = await store.get_run(persisted.run_id)

                    assert restored is not None
                    self.assertIs(restored.status, RunStatus.CREATED)
                    self.assertIsNone(restored.snapshot)
                    self.assertIsNone(restored.lease_owner)
                    self.assertEqual(adapter.call_count, 0)
            stores[1].close()

    def test_strict_provider_binding_requires_configured_schema(self) -> None:
        from m_agent.provider import ChatCompletionsModelAdapter
        from m_agent.runtime import (
            AgentDefinition,
            ModelCapabilities,
            ModelContract,
            ModelLimits,
            ModelRequirements,
            RevisionStability,
            StructuredOutputMode,
        )

        unbound = ChatCompletionsModelAdapter(
            base_url="https://offline-provider.invalid/v1"
        )
        contract = ModelContract(
            contract_id="offline-strict-provider",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity=unbound.model,
            capabilities=unbound.capabilities,
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="offline-provider-sizer-v1",
            serialization_id="offline-provider-wire-v1",
            configuration_fingerprint=unbound.definition_contract_fingerprint(),
        )
        adapter = ChatCompletionsModelAdapter(
            base_url="https://offline-provider.invalid/v1",
            model_contract=contract,
        )
        try:
            with self.assertRaisesRegex(ValueError, "strict JSON Schema"):
                AgentDefinition.for_adapter(
                    definition_id="schema-less-strict-provider",
                    version="1",
                    instructions="Never dispatch.",
                    model_requirements=ModelRequirements(
                        capabilities=ModelCapabilities(
                            structured_output=(
                                StructuredOutputMode.JSON_SCHEMA_STRICT
                            )
                        )
                    ),
                    model_adapter=adapter,
                )
            self.assertEqual(adapter.requests, [])
        finally:
            asyncio.run(unbound.aclose())
            asyncio.run(adapter.aclose())

    async def test_strict_provider_schema_violation_fails_public_runner(
        self,
    ) -> None:
        import httpx
        from unittest.mock import patch

        from m_agent.adapters import InMemoryRunStore, PlaintextPayloadCodec
        from m_agent.provider import ChatCompletionsModelAdapter
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelContract,
            ModelLimits,
            ModelRequirements,
            RevisionStability,
            Runner,
            RunStatus,
            StructuredOutputMode,
        )

        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        unbound = ChatCompletionsModelAdapter(
            base_url="https://offline-provider.invalid/v1",
            structured_output_schema=schema,
        )
        contract = ModelContract(
            contract_id="offline-strict-schema",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity=unbound.model,
            capabilities=unbound.capabilities,
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="offline-provider-sizer-v1",
            serialization_id="offline-provider-wire-v1",
            configuration_fingerprint=unbound.definition_contract_fingerprint(),
        )
        adapter = ChatCompletionsModelAdapter(
            base_url="https://offline-provider.invalid/v1",
            structured_output_schema=schema,
            model_contract=contract,
        )
        dispatches = 0

        def transport(request):
            nonlocal dispatches
            dispatches += 1
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": '{"wrong": 1}'}}
                    ]
                },
            )

        adapter._transport = httpx.MockTransport(transport)
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="offline-strict-schema",
                version="1",
                instructions="Return an answer.",
                model_requirements=ModelRequirements(
                    capabilities=ModelCapabilities(
                        structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
                    )
                ),
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )
        created = await runner.create_run("offline-strict-schema", "1", "hi")
        try:
            with patch.dict(
                os.environ,
                {"M_AGENT_OPENAI_API_KEY": "offline-test-key"},
                clear=False,
            ):
                terminal = await runner.start_run(created.run_id)
            inspection = await runner.inspect_run(created.run_id)
        finally:
            await unbound.aclose()
            await adapter.aclose()

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
        self.assertEqual(inspection.attempts[-1].error_code, "MODEL_CONTRACT_VIOLATION")
        self.assertEqual(dispatches, 1)

    async def test_strict_structured_response_rejects_non_json_output(self) -> None:
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
            ModelLimits,
            ModelRequirements,
            RevisionStability,
            Runner,
            RunStatus,
            StructuredOutputMode,
        )

        contract = ModelContract(
            contract_id="strict-output",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:strict-output",
            capabilities=ModelCapabilities(
                structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
            ),
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )
        adapter = DeterministicModelAdapter(("not-json",), model_contract=contract)
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="strict-output",
                version="1",
                instructions="Return strict JSON.",
                model_requirements=ModelRequirements(
                    capabilities=ModelCapabilities(
                        structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT
                    )
                ),
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run("strict-output", "1", "hello")
        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
        self.assertEqual(adapter.call_count, 1)

    async def test_json_object_response_rejects_invalid_json_output(self) -> None:
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
            ModelLimits,
            ModelRequirements,
            RevisionStability,
            Runner,
            RunStatus,
            StructuredOutputMode,
        )

        contract = ModelContract(
            contract_id="json-object-output",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:json-object-output",
            capabilities=ModelCapabilities(
                structured_output=StructuredOutputMode.JSON_OBJECT
            ),
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )
        for content in ("not-json", '{"value":NaN}'):
            with self.subTest(content=content):
                adapter = DeterministicModelAdapter(
                    (content,), model_contract=contract
                )
                registry = DefinitionRegistry()
                registry.register(
                    AgentDefinition.for_adapter(
                        definition_id="json-object-output",
                        version="1",
                        instructions="Return a JSON object.",
                        model_requirements=ModelRequirements(
                            capabilities=ModelCapabilities(
                                structured_output=StructuredOutputMode.JSON_OBJECT
                            )
                        ),
                        model_adapter=adapter,
                    )
                )
                runner = Runner(
                    registry=registry,
                    store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
                )

                created = await runner.create_run(
                    "json-object-output", "1", "hello"
                )
                terminal = await runner.start_run(created.run_id)

                self.assertIs(terminal.status, RunStatus.FAILED)
                self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
                self.assertEqual(adapter.call_count, 1)

    async def test_custom_deterministic_adapter_requires_binding_fingerprint(
        self,
    ) -> None:
        """A raw deterministic adapter cannot opt out of frozen behavior."""
        from m_agent.adapters import InMemoryRunStore, PlaintextPayloadCodec
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionNotFoundError,
            DefinitionRegistry,
            ModelAdapter,
            ModelCapabilities,
            ModelContract,
            ModelLimits,
            ModelResponse,
            RevisionStability,
            Runner,
        )

        contract = ModelContract(
            contract_id="unfingerprinted-deterministic",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:custom",
            capabilities=ModelCapabilities(),
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )

        class MutableDeterministicAdapter(ModelAdapter):
            deterministic = True
            capabilities = contract.capabilities

            def __init__(self) -> None:
                self.answer = "frozen"
                self.call_count = 0

            @property
            def model_contract(self) -> ModelContract:
                return contract

            async def generate(self, request) -> ModelResponse:
                self.call_count += 1
                return ModelResponse(content=self.answer)

        adapter = MutableDeterministicAdapter()
        registry = DefinitionRegistry()
        with self.assertRaisesRegex(
            ValueError, "deterministic adapter.*current configuration fingerprint"
        ):
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="unfingerprinted-deterministic",
                    version="1",
                    instructions="Never dispatch.",
                    model_adapter=adapter,
                )
            )
        adapter.answer = "drifted"
        self.assertFalse(registry.is_registered("unfingerprinted-deterministic", "1"))
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )
        with self.assertRaisesRegex(
            DefinitionNotFoundError, "unfingerprinted-deterministic"
        ):
            await runner.create_run("unfingerprinted-deterministic", "1", "hello")
        self.assertEqual(adapter.call_count, 0)

    async def test_deterministic_subclass_state_is_frozen_in_binding(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelResponse,
            Runner,
        )

        class ConfiguredAnswerAdapter(DeterministicModelAdapter):
            def __init__(self, answer: str) -> None:
                super().__init__(("unused",))
                self.answer = answer

            async def generate(self, request) -> ModelResponse:
                self.call_count += 1
                self._last_request = request
                return ModelResponse(content=self.answer)

        def registry(answer: str) -> tuple[DefinitionRegistry, ConfiguredAnswerAdapter]:
            adapter = ConfiguredAnswerAdapter(answer)
            result = DefinitionRegistry()
            result.register(
                AgentDefinition.for_adapter(
                    definition_id="configured-deterministic",
                    version="1",
                    instructions="Reply.",
                    model_adapter=adapter,
                )
            )
            return result, adapter

        async def assert_drift_rejected(store) -> None:
            original_registry, _ = registry("OLD")
            created = await Runner(original_registry, store).create_run(
                "configured-deterministic", "1", "hello"
            )
            changed_registry, changed_adapter = registry("NEW")

            with self.assertRaisesRegex(RuntimeError, "snapshot Model Contract"):
                await Runner(changed_registry, store).start_run(created.run_id)

            self.assertEqual(changed_adapter.call_count, 0)

        await assert_drift_rejected(
            InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteRunStore(
                os.path.join(tmp, "configured-deterministic.db"),
                payload_codec=PlaintextPayloadCodec(),
            )
            try:
                await assert_drift_rejected(store)
            finally:
                store.close()

    async def test_deterministic_container_state_is_frozen_in_binding(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelResponse,
            Runner,
        )

        class ListConfiguredAdapter(DeterministicModelAdapter):
            def __init__(self, answers: list[str]) -> None:
                super().__init__(("unused",))
                self.answers = answers

            async def generate(self, request) -> ModelResponse:
                self.call_count += 1
                self._last_request = request
                return ModelResponse(content=self.answers[0])

        def registry(
            answer: str,
        ) -> tuple[DefinitionRegistry, ListConfiguredAdapter]:
            adapter = ListConfiguredAdapter([answer])
            result = DefinitionRegistry()
            result.register(
                AgentDefinition.for_adapter(
                    definition_id="list-configured-deterministic",
                    version="1",
                    instructions="Reply.",
                    model_adapter=adapter,
                )
            )
            return result, adapter

        async def assert_drift_rejected(store) -> None:
            original_registry, _ = registry("ORIGINAL")
            created = await Runner(original_registry, store).create_run(
                "list-configured-deterministic", "1", "hello"
            )
            changed_registry, changed_adapter = registry("DRIFTED")

            with self.assertRaisesRegex(RuntimeError, "snapshot Model Contract"):
                await Runner(changed_registry, store).start_run(created.run_id)

            self.assertEqual(changed_adapter.call_count, 0)

        await assert_drift_rejected(
            InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteRunStore(
                os.path.join(tmp, "list-configured-deterministic.db"),
                payload_codec=PlaintextPayloadCodec(),
            )
            try:
                await assert_drift_rejected(store)
            finally:
                store.close()

    async def test_deterministic_object_state_is_frozen_or_rejected(
        self,
    ) -> None:
        """Custom behavior state must not disappear from a frozen binding."""
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelResponse,
            Runner,
        )

        class Strategy:
            def __init__(self, answer: str) -> None:
                self.answer = answer

        class StrategyAdapter(DeterministicModelAdapter):
            def __init__(self, answer: str) -> None:
                super().__init__(("unused",))
                self.strategy = Strategy(answer)

            async def generate(self, request) -> ModelResponse:
                self.call_count += 1
                self._last_request = request
                return ModelResponse(content=self.strategy.answer)

        original = StrategyAdapter("OLD")
        initial_registry = DefinitionRegistry()
        initial_registry.register(
            AgentDefinition.for_adapter(
                definition_id="object-configured-deterministic",
                version="1",
                instructions="Reply.",
                model_adapter=original,
            )
        )
        store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        created = await Runner(initial_registry, store).create_run(
            "object-configured-deterministic", "1", "hello"
        )

        changed = StrategyAdapter("NEW")
        changed_registry = DefinitionRegistry()
        changed_registry.register(
            AgentDefinition.for_adapter(
                definition_id="object-configured-deterministic",
                version="1",
                instructions="Reply.",
                model_adapter=changed,
            )
        )

        with self.assertRaisesRegex(RuntimeError, "snapshot Model Contract"):
            await Runner(changed_registry, store).start_run(created.run_id)
        self.assertEqual(changed.call_count, 0)

        class SensitiveStateAdapter(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(("unused",))
                self.api_key = "must-not-be-ignored"

        with self.assertRaisesRegex(ValueError, "api_key"):
            DefinitionRegistry().register(
                AgentDefinition.for_adapter(
                    definition_id="sensitive-deterministic",
                    version="1",
                    instructions="Never dispatch.",
                    model_adapter=SensitiveStateAdapter(),
                )
            )

    async def test_streaming_response_tool_call_requires_declared_combination(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelCapabilityCombination,
            ModelContract,
            ModelLimits,
            ModelRequirements,
            ModelResponse,
            RevisionStability,
            Runner,
            RunStatus,
            StreamingMode,
            ToolCall,
            ToolCallingMode,
        )

        capabilities = ModelCapabilities(
            streaming=StreamingMode.DELTA,
            tool_calling=ToolCallingMode.NATIVE,
            supported_combinations=(
                ModelCapabilityCombination(streaming=StreamingMode.DELTA),
                ModelCapabilityCombination(tool_calling=ToolCallingMode.NATIVE),
            ),
        )
        contract = ModelContract(
            contract_id="stream-tool-split",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:stream-tool-split",
            capabilities=capabilities,
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )

        class UnexpectedToolStream(DeterministicModelAdapter):
            async def stream(self, request):
                self.call_count += 1
                self._last_request = request
                yield ModelResponse(
                    tool_calls=(
                        ToolCall(
                            call_id="unexpected-tool",
                            tool_name="lookup",
                            arguments="{}",
                        ),
                    )
                )

        adapter = UnexpectedToolStream(("unused",), model_contract=contract)
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="stream-tool-split",
                version="1",
                instructions="Never accept the undeclared response mode.",
                model_requirements=ModelRequirements(
                    capabilities=ModelCapabilities(streaming=StreamingMode.DELTA)
                ),
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run("stream-tool-split", "1", "hello")
        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
        self.assertEqual(adapter.call_count, 1)

    async def test_streaming_provider_usage_requires_declared_combination(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelCapabilityCombination,
            ModelCapabilityError,
            ModelContract,
            ModelLimits,
            ModelRequirements,
            ModelResponse,
            ModelUsage,
            ModelUsageGuarantees,
            RevisionStability,
            Runner,
            RunStatus,
            StreamingMode,
            UsageFieldGuarantee,
            UsageProvenance,
            UsageReportingMode,
        )

        capabilities = ModelCapabilities(
            streaming=StreamingMode.DELTA,
            usage_reporting=UsageReportingMode.PROVIDER_REPORTED,
            supported_combinations=(
                ModelCapabilityCombination(streaming=StreamingMode.DELTA),
                ModelCapabilityCombination(
                    usage_reporting=UsageReportingMode.PROVIDER_REPORTED
                ),
            ),
        )
        contract = ModelContract(
            contract_id="stream-usage-split",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:stream-usage-split",
            capabilities=capabilities,
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
            usage_guarantees=ModelUsageGuarantees(
                input_tokens=UsageFieldGuarantee.REQUIRED
            ),
        )

        class UsageStream(DeterministicModelAdapter):
            async def stream(self, request):
                self.call_count += 1
                self._last_request = request
                yield ModelResponse(
                    content="accepted",
                    usage=ModelUsage(
                        input_tokens=1,
                        provenance=UsageProvenance.PROVIDER_REPORTED,
                        raw_unit="tokens",
                        normalization_source="deterministic-usage-v1",
                    ),
                )

        adapter = UsageStream(("unused",), model_contract=contract)
        registry = DefinitionRegistry()
        with self.assertRaisesRegex(
            ModelCapabilityError, "CAPABILITY_COMBINATION_UNSUPPORTED"
        ):
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="stream-usage-split",
                    version="1",
                    instructions="Stream without requesting provider usage.",
                    model_requirements=ModelRequirements(
                        capabilities=ModelCapabilities(
                            streaming=StreamingMode.DELTA
                        )
                    ),
                    model_adapter=adapter,
                )
            )
        self.assertFalse(registry.is_registered("stream-usage-split", "1"))
        self.assertEqual(adapter.call_count, 0)

    async def test_tool_call_identity_is_rejected_before_tool_effects(self) -> None:
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
            ModelResponse,
            Runner,
            RunStatus,
            ToolCall,
            ToolCallingMode,
            ToolEffect,
            ToolOutcome,
            StepType,
        )

        class InvalidToolCalls(DeterministicModelAdapter):
            def __init__(self, calls: tuple[ToolCall, ...]) -> None:
                super().__init__(
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    )
                )
                self._calls = calls

            async def generate(self, request):
                self.call_count += 1
                self._last_request = request
                return ModelResponse(tool_calls=self._calls)

        for label, calls in (
            (
                "duplicate",
                (
                    ToolCall(call_id="same", tool_name="lookup", arguments="{}"),
                    ToolCall(call_id="same", tool_name="lookup", arguments="{}"),
                ),
            ),
            (
                "empty",
                (ToolCall(call_id="", tool_name="lookup", arguments="{}"),),
            ),
            (
                "undeclared",
                (
                    ToolCall(
                        call_id="missing",
                        tool_name="not-offered",
                        arguments="{}",
                    ),
                ),
            ),
        ):
            with self.subTest(label=label):
                effects = 0

                def handler(request):
                    nonlocal effects
                    effects += 1
                    return ToolOutcome.success(
                        request.call_id, request.tool_name, "unused"
                    )

                adapter = InvalidToolCalls(calls)
                registry = DefinitionRegistry()
                registry.register(
                    AgentDefinition.for_adapter(
                        definition_id=f"invalid-tool-call-{label}",
                        version="1",
                        instructions="Never invoke malformed tool calls.",
                        model_adapter=adapter,
                        tools=(
                            DeterministicTool(
                                name="lookup",
                                effect=ToolEffect.IDEMPOTENT,
                                handler=handler,
                            ),
                        ),
                    )
                )
                runner = Runner(
                    registry=registry,
                    store=InMemoryRunStore(
                        payload_codec=PlaintextPayloadCodec()
                    ),
                )
                created = await runner.create_run(
                    f"invalid-tool-call-{label}", "1", "hello"
                )
                terminal = await runner.start_run(created.run_id)
                inspection = await runner.inspect_run(created.run_id)

                self.assertIs(terminal.status, RunStatus.FAILED)
                self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
                self.assertEqual(adapter.call_count, 1)
                self.assertEqual(effects, 0)
                self.assertEqual(
                    [step.step_type for step in inspection.steps], [StepType.MODEL]
                )

    async def test_unsupported_tool_definition_has_capability_error_code(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            DeterministicTool,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilityError,
            ToolEffect,
            ToolOutcome,
        )

        adapter = DeterministicModelAdapter(("must not dispatch",))
        registry = DefinitionRegistry()
        with self.assertRaisesRegex(
            ModelCapabilityError, "TOOL_CALLING_UNSUPPORTED"
        ):
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="predispatch-capability",
                    version="1",
                    instructions="Use the supplied tool.",
                    model_adapter=adapter,
                    tools=(
                        DeterministicTool(
                            name="lookup",
                            effect=ToolEffect.READ_ONLY,
                            handler=lambda request: ToolOutcome.success(
                                request.call_id,
                                request.tool_name,
                                "unused",
                            ),
                        ),
                    ),
                )
            )

        self.assertFalse(registry.is_registered("predispatch-capability", "1"))
        self.assertEqual(adapter.call_count, 0)

    async def test_malformed_model_response_is_contract_violation(self) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelResponse,
            Runner,
            RunStatus,
        )

        class MalformedAdapter(DeterministicModelAdapter):
            async def generate(self, request):
                self.call_count += 1
                return {"content": "not a ModelResponse"}

        adapter = MalformedAdapter(("unreachable",))
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="malformed-response",
                version="1",
                instructions="Reply.",
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run("malformed-response", "1", "hello")
        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
        self.assertEqual(adapter.call_count, 1)

    async def test_malformed_tool_arguments_fail_before_non_idempotent_effect(
        self,
    ) -> None:
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
            ModelRequest,
            ModelResponse,
            ModelRequirements,
            Runner,
            RunStatus,
            ToolCall,
            ToolCallingMode,
            ToolEffect,
            ToolOutcome,
        )

        class MalformedToolCallAdapter(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    )
                )

            async def generate(self, request: ModelRequest) -> ModelResponse:
                self.call_count += 1
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            call_id="write-1",
                            tool_name="write",
                            arguments="{",
                        ),
                    )
                )

        effects: list[str] = []
        tool = DeterministicTool(
            name="write",
            effect=ToolEffect.NON_IDEMPOTENT,
            handler=lambda request: (
                effects.append(request.arguments)
                or ToolOutcome.success(
                    request.call_id, request.tool_name, "written"
                )
            ),
        )
        adapter = MalformedToolCallAdapter()
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="malformed-tool-arguments",
                version="1",
                instructions="Never invoke malformed calls.",
                model_requirements=ModelRequirements(
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    )
                ),
                model_adapter=adapter,
                tools=(tool,),
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run(
            "malformed-tool-arguments", "1", "write"
        )
        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(effects, [])

    async def test_malformed_nested_model_response_is_contract_violation(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelResponse,
            Runner,
            RunStatus,
        )

        class MalformedAdapter(DeterministicModelAdapter):
            async def generate(self, request):
                self.call_count += 1
                return ModelResponse.model_construct(
                    content="ok", usage={"input_tokens": 1}
                )

        adapter = MalformedAdapter(("unreachable",))
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="malformed-nested-response",
                version="1",
                instructions="Reply.",
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run(
            "malformed-nested-response", "1", "hello"
        )
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
        self.assertEqual(inspection.attempts[-1].error_code, "MODEL_CONTRACT_VIOLATION")
        self.assertEqual(adapter.call_count, 1)

    async def test_provider_usage_requires_unit_and_normalization_provenance(
        self,
    ) -> None:
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
            ModelLimits,
            ModelUsage,
            ModelUsageGuarantees,
            ModelResponse,
            RevisionStability,
            Runner,
            RunStatus,
            UsageFieldGuarantee,
            UsageProvenance,
            UsageReportingMode,
        )

        contract = ModelContract(
            contract_id="usage-provenance",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:usage",
            capabilities=ModelCapabilities(
                usage_reporting=UsageReportingMode.PROVIDER_REPORTED
            ),
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
            usage_guarantees=ModelUsageGuarantees(
                input_tokens=UsageFieldGuarantee.REQUIRED
            ),
        )

        class UnprovenancedUsageAdapter(DeterministicModelAdapter):
            async def generate(self, request):
                response = await super().generate(request)
                return ModelResponse(
                    content=response.content,
                    usage=ModelUsage(
                        input_tokens=7,
                        provenance=UsageProvenance.RUNTIME_SIZED,
                    ),
                )

        adapter = UnprovenancedUsageAdapter(
            ("unreachable",), model_contract=contract
        )
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="usage-provenance",
                version="1",
                instructions="Reply.",
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run("usage-provenance", "1", "hello")
        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
        self.assertEqual(adapter.call_count, 1)

    async def test_provider_reported_usage_is_rejected_when_contract_disables_it(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelResponse,
            ModelUsage,
            Runner,
            RunStatus,
            StepStatus,
            UsageProvenance,
        )

        class UndeclaredUsageAdapter(DeterministicModelAdapter):
            async def generate(self, request):
                self.call_count += 1
                self._last_request = request
                return ModelResponse(
                    content="answer",
                    usage=ModelUsage(
                        input_tokens=3,
                        output_tokens=2,
                        provenance=UsageProvenance.PROVIDER_REPORTED,
                        raw_unit="tokens",
                        normalization_source="test-provider-usage-v1",
                    ),
                )

        adapter = UndeclaredUsageAdapter(("unused",))
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="undeclared-provider-usage",
                version="1",
                instructions="Reply.",
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run("undeclared-provider-usage", "1", "hi")
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(
            [attempt.status for attempt in inspection.attempts], [StepStatus.FAILED]
        )
        usage = inspection.attempts[0].usage
        assert usage is not None
        self.assertEqual(usage.input_tokens, 3)
        self.assertEqual(usage.output_tokens, 2)

    async def test_validation_failure_preserves_observed_provider_usage(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelContractViolationError,
            ModelResponse,
            ModelUsage,
            Runner,
            RunStatus,
            UsageProvenance,
            UsageReportingMode,
        )

        class UsageThenValidationFailureAdapter(DeterministicModelAdapter):
            async def generate(self, request):
                self.call_count += 1
                self._last_request = request
                return ModelResponse(
                    content="answer",
                    usage=ModelUsage(
                        input_tokens=7,
                        output_tokens=3,
                        provenance=UsageProvenance.PROVIDER_REPORTED,
                        raw_unit="tokens",
                        normalization_source="test-provider-usage-v1",
                    ),
                )

            def validate_response(self, request, response):
                raise ModelContractViolationError("provider response rejected")

        adapter = UsageThenValidationFailureAdapter(
            ("unused",),
            capabilities=ModelCapabilities(
                usage_reporting=UsageReportingMode.PROVIDER_REPORTED
            ),
        )
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="usage-before-validation-failure",
                version="1",
                instructions="Reply.",
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry,
            InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run(
            "usage-before-validation-failure", "1", "hello"
        )
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
        usage = inspection.attempts[0].usage
        assert usage is not None
        self.assertEqual((usage.input_tokens, usage.output_tokens), (7, 3))

    async def test_validation_policy_drift_fails_without_checkpoint(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
            RunStatus,
        )

        class PolicyChangingAdapter(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(("accepted",))
                self.validation_policy = "policy-a"

            def validate_response(self, request, response):
                self.validation_policy = "policy-b"
                return response

        adapter = PolicyChangingAdapter()
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="validation-policy-drift",
                version="1",
                instructions="Reply.",
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry,
            InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run(
            "validation-policy-drift", "1", "hi"
        )
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(inspection.checkpoints, [])

    async def test_actual_provider_revision_is_persisted_with_response(self) -> None:
        from m_agent import deserialize_model_response
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelResponse,
            Runner,
            RunStatus,
        )

        class RevisionAdapter(DeterministicModelAdapter):
            async def generate(self, request):
                response = await super().generate(request)
                return ModelResponse(
                    content=response.content,
                    actual_revision="provider-revision-123",
                )

        adapter = RevisionAdapter(("accepted",))
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="provider-revision",
                version="1",
                instructions="Reply.",
                model_adapter=adapter,
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run("provider-revision", "1", "hello")
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        response = deserialize_model_response(inspection.attempts[0].output)
        self.assertEqual(response.actual_revision, "provider-revision-123")

    async def test_create_run_freezes_explicit_primary_binding(self) -> None:
        """A CREATED Run preserves the caller's checked primary binding."""
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelBinding,
            ModelBindingSet,
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
        )
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="typed-contract",
                version="1",
                instructions="Reply deterministically.",
                model_requirements=ModelRequirements(
                    min_context_window_tokens=1,
                    min_output_tokens=1,
                ),
                model_bindings=ModelBindingSet(
                    bindings=(
                        ModelBinding(
                            purpose=ModelPurpose.PRIMARY,
                            contract=contract,
                            requirements=ModelRequirements(
                                min_context_window_tokens=64,
                                min_output_tokens=16,
                            ),
                        ),
                        ModelBinding(
                            purpose=ModelPurpose.CONTEXT_COMPRESSION,
                            contract=contract,
                            requirements=ModelRequirements(
                                min_context_window_tokens=64,
                                min_output_tokens=16,
                            ),
                            source_purpose=ModelPurpose.PRIMARY,
                        ),
                        ModelBinding(
                            purpose=ModelPurpose.OUTPUT_REPAIR,
                            contract=contract,
                            requirements=ModelRequirements(
                                min_context_window_tokens=64,
                                min_output_tokens=16,
                            ),
                            source_purpose=ModelPurpose.PRIMARY,
                        ),
                    )
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
        assert created.snapshot is not None
        primary = created.snapshot.model_bindings.for_purpose(
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
                AgentDefinition.for_adapter(
                    definition_id="requires-tools",
                    version="1",
                    instructions="Never dispatch.",
                    model_requirements=requirements,
                    model_adapter=adapter,
                )
            )
        self.assertEqual(adapter.call_count, 0)

    async def test_explicit_binding_still_enforces_definition_requirements(
        self,
    ) -> None:
        """An explicit PRIMARY binding cannot discard Definition minima."""
        from m_agent.adapters import DeterministicModelAdapter
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelBinding,
            ModelBindingSet,
            ModelCapabilities,
            ModelCapabilityError,
            ModelPurpose,
            ModelRequirements,
            ToolCallingMode,
        )

        adapter = DeterministicModelAdapter(("unreachable",))
        primary = ModelBinding(
            purpose=ModelPurpose.PRIMARY,
            contract=adapter.model_contract,
        )
        with self.assertRaisesRegex(ModelCapabilityError, "TOOL_CALLING_UNSUPPORTED"):
            DefinitionRegistry().register(
                AgentDefinition.for_adapter(
                    definition_id="explicit-binding-requirements",
                    version="1",
                    instructions="Never dispatch.",
                    model_requirements=ModelRequirements(
                        capabilities=ModelCapabilities(
                            tool_calling=ToolCallingMode.NATIVE
                        )
                    ),
                    model_bindings=ModelBindingSet(
                        bindings=(
                            primary,
                            primary.model_copy(
                                update={
                                    "purpose": ModelPurpose.CONTEXT_COMPRESSION,
                                    "source_purpose": ModelPurpose.PRIMARY,
                                }
                            ),
                            primary.model_copy(
                                update={
                                    "purpose": ModelPurpose.OUTPUT_REPAIR,
                                    "source_purpose": ModelPurpose.PRIMARY,
                                }
                            ),
                        )
                    ),
                    model_adapter=adapter,
                )
            )
        self.assertEqual(adapter.call_count, 0)

    async def test_unsupported_stream_tool_combination_is_rejected_at_registration(
        self,
    ) -> None:
        """Declared modes do not imply their undeclared combined protocol."""
        from m_agent.adapters import (
            DeterministicModelAdapter,
            DeterministicTool,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelCapabilityCombination,
            ModelContract,
            ModelCapabilityError,
            ModelLimits,
            ModelRequirements,
            RevisionStability,
            StreamingMode,
            ToolCallingMode,
            ToolEffect,
            ToolOutcome,
        )

        capabilities = ModelCapabilities(
            streaming=StreamingMode.DELTA,
            tool_calling=ToolCallingMode.NATIVE,
            supported_combinations=(
                ModelCapabilityCombination(streaming=StreamingMode.DELTA),
                ModelCapabilityCombination(tool_calling=ToolCallingMode.NATIVE),
            ),
        )
        contract = ModelContract(
            contract_id="split-protocols",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:split-protocols",
            capabilities=capabilities,
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )
        adapter = DeterministicModelAdapter(("unreachable",), model_contract=contract)
        tool = DeterministicTool(
            name="lookup",
            effect=ToolEffect.READ_ONLY,
            handler=lambda request: ToolOutcome.success(
                request.call_id, request.tool_name, "found"
            ),
        )
        registry = DefinitionRegistry()
        with self.assertRaisesRegex(
            ModelCapabilityError, "CAPABILITY_COMBINATION_UNSUPPORTED"
        ):
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="split-protocols",
                    version="1",
                    instructions="Never dispatch an undeclared combination.",
                    model_requirements=ModelRequirements(
                        capabilities=ModelCapabilities(
                            streaming=StreamingMode.DELTA
                        )
                    ),
                    model_adapter=adapter,
                    tools=(tool,),
                )
            )

        self.assertFalse(registry.is_registered("split-protocols", "1"))
        self.assertEqual(adapter.call_count, 0)


    async def test_start_rejects_contract_drift_after_run_creation(self) -> None:
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
            RunStatus,
        )

        def contract(revision: str) -> ModelContract:
            return ModelContract(
                contract_id="frozen-at-create",
                version="1",
                revision_stability=RevisionStability.PINNED,
                model_identity=f"deterministic:frozen:{revision}",
                limits=ModelLimits(
                    context_window_tokens=128, max_output_tokens=32
                ),
                input_sizer_id="deterministic-v1",
                serialization_id="deterministic-text-v1",
            )

        original = DeterministicModelAdapter(
            ("first",), model_contract=contract("first")
        )
        initial_registry = DefinitionRegistry()
        initial_registry.register(
            AgentDefinition.for_adapter(
                definition_id="create-freeze",
                version="1",
                instructions="Reply.",
                model_adapter=original,
            )
        )
        store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        created = await Runner(initial_registry, store).create_run(
            "create-freeze", "1", "hello"
        )

        changed = DeterministicModelAdapter(
            ("must not dispatch",), model_contract=contract("changed")
        )
        changed_registry = DefinitionRegistry()
        changed_registry.register(
            AgentDefinition.for_adapter(
                definition_id="create-freeze",
                version="1",
                instructions="Reply.",
                model_adapter=changed,
            )
        )
        with self.assertRaisesRegex(RuntimeError, "snapshot Model Contract"):
            await Runner(changed_registry, store).start_run(created.run_id)
        self.assertEqual(changed.call_count, 0)
        self.assertIsNone(await store.get_lease(created.run_id))

        terminal = await Runner(initial_registry, store).start_run(created.run_id)
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(original.call_count, 1)

    async def test_default_deterministic_adapter_drift_is_rejected(self) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import AgentDefinition, DefinitionRegistry, Runner

        original = DeterministicModelAdapter(("old",))
        initial_registry = DefinitionRegistry()
        initial_registry.register(
            AgentDefinition.for_adapter(
                definition_id="deterministic-drift",
                version="1",
                instructions="Reply.",
                model_adapter=original,
            )
        )
        store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        created = await Runner(initial_registry, store).create_run(
            "deterministic-drift", "1", "hi"
        )

        changed = DeterministicModelAdapter(("new",))
        changed_registry = DefinitionRegistry()
        changed_registry.register(
            AgentDefinition.for_adapter(
                definition_id="deterministic-drift",
                version="1",
                instructions="Reply.",
                model_adapter=changed,
            )
        )

        with self.assertRaisesRegex(RuntimeError, "snapshot Model Contract"):
            await Runner(changed_registry, store).start_run(created.run_id)

        self.assertEqual(original.call_count, 0)
        self.assertEqual(changed.call_count, 0)

    async def test_reordered_binding_set_preserves_recovery_semantics(
        self,
    ) -> None:
        """Bindings are a purpose mapping, not a construction-order contract."""
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelBindingSet,
            Runner,
            RunStatus,
        )

        adapter = DeterministicModelAdapter(("answer",))
        bindings = ModelBindingSet.reuse_primary(adapter.model_contract)

        def definition(model_bindings: ModelBindingSet) -> AgentDefinition:
            return AgentDefinition.for_adapter(
                definition_id="binding-order",
                version="1",
                instructions="Reply.",
                model_bindings=model_bindings,
                model_adapter=adapter,
            )

        initial_registry = DefinitionRegistry()
        initial_registry.register(definition(bindings))
        store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        created = await Runner(initial_registry, store).create_run(
            "binding-order", "1", "hello"
        )

        reordered_registry = DefinitionRegistry()
        reordered_registry.register(
            definition(ModelBindingSet(bindings=tuple(reversed(bindings.bindings))))
        )
        result = await Runner(reordered_registry, store).start_run(created.run_id)

        self.assertIs(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(adapter.call_count, 1)

    async def test_deterministic_adapter_type_is_frozen_before_dispatch(
        self,
    ) -> None:
        """Identical contracts cannot exchange distinct deterministic code."""
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
            ModelResponse,
            RevisionStability,
            Runner,
        )

        contract = ModelContract(
            contract_id="frozen-deterministic-type",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:frozen-type",
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
            configuration_fingerprint="frozen-deterministic-type-v1",
        )

        class FirstAdapter(DeterministicModelAdapter):
            async def generate(self, request) -> ModelResponse:
                self.call_count += 1
                return ModelResponse(content="first")

        class SecondAdapter(DeterministicModelAdapter):
            async def generate(self, request) -> ModelResponse:
                self.call_count += 1
                return ModelResponse(content="second")

        first = FirstAdapter(model_contract=contract)
        first_registry = DefinitionRegistry()
        first_registry.register(
            AgentDefinition.for_adapter(
                definition_id="frozen-deterministic-type",
                version="1",
                instructions="Reply.",
                model_adapter=first,
            )
        )
        store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        created = await Runner(first_registry, store).create_run(
            "frozen-deterministic-type", "1", "hello"
        )

        second = SecondAdapter(model_contract=contract)
        self.assertEqual(first.model_contract, second.model_contract)
        self.assertNotEqual(
            first.definition_contract_fingerprint(),
            second.definition_contract_fingerprint(),
        )
        second_registry = DefinitionRegistry()
        second_registry.register(
            AgentDefinition.for_adapter(
                definition_id="frozen-deterministic-type",
                version="1",
                instructions="Reply.",
                model_adapter=second,
            )
        )

        with self.assertRaisesRegex(RuntimeError, "snapshot Model Contract"):
            await Runner(second_registry, store).start_run(created.run_id)
        self.assertEqual(first.call_count, 0)
        self.assertEqual(second.call_count, 0)

    async def test_explicit_deterministic_contract_binds_behavior_configuration(
        self,
    ) -> None:
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

        explicit_contract = ModelContract(
            contract_id="explicit-deterministic",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:explicit",
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )
        original = DeterministicModelAdapter(
            ("old",), model_contract=explicit_contract
        )
        initial_registry = DefinitionRegistry()
        initial_registry.register(
            AgentDefinition.for_adapter(
                definition_id="explicit-deterministic-drift",
                version="1",
                instructions="Reply.",
                model_adapter=original,
            )
        )
        store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        created = await Runner(initial_registry, store).create_run(
            "explicit-deterministic-drift", "1", "hi"
        )

        changed = DeterministicModelAdapter(
            ("new",), model_contract=explicit_contract
        )
        changed_registry = DefinitionRegistry()
        changed_registry.register(
            AgentDefinition.for_adapter(
                definition_id="explicit-deterministic-drift",
                version="1",
                instructions="Reply.",
                model_adapter=changed,
            )
        )

        with self.assertRaisesRegex(
            RuntimeError, "snapshot Model Contract Binding set"
        ):
            await Runner(changed_registry, store).start_run(created.run_id)
        self.assertEqual(changed.call_count, 0)

    async def test_start_rejects_secondary_binding_set_drift(self) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelBinding,
            ModelBindingSet,
            ModelContract,
            ModelLimits,
            ModelPurpose,
            RevisionStability,
            Runner,
        )

        def contract(name: str) -> ModelContract:
            return ModelContract(
                contract_id=name,
                version="1",
                revision_stability=RevisionStability.PINNED,
                model_identity=f"deterministic:{name}",
                limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
                input_sizer_id="deterministic-v1",
                serialization_id="deterministic-text-v1",
            )

        primary_contract = contract("binding-primary")

        def definition(
            primary: DeterministicModelAdapter,
            secondary: DeterministicModelAdapter,
        ) -> AgentDefinition:
            primary_binding = ModelBinding(
                purpose=ModelPurpose.PRIMARY,
                contract=primary.model_contract,
            )
            return AgentDefinition.for_adapter(
                definition_id="binding-set-drift",
                version="1",
                instructions="Reply.",
                model_bindings=ModelBindingSet(
                    bindings=(
                        primary_binding,
                        ModelBinding(
                            purpose=ModelPurpose.CONTEXT_COMPRESSION,
                            contract=secondary.model_contract,
                        ),
                        primary_binding.model_copy(
                            update={
                                "purpose": ModelPurpose.OUTPUT_REPAIR,
                                "source_purpose": ModelPurpose.PRIMARY,
                            }
                        ),
                    )
                ),
                model_adapter=primary,
                model_adapters={
                    ModelPurpose.CONTEXT_COMPRESSION: secondary,
                },
            )

        async def assert_rejected(store) -> None:
            original_primary = DeterministicModelAdapter(
                ("primary",), model_contract=primary_contract
            )
            original_secondary = DeterministicModelAdapter(
                ("secondary",), model_contract=contract("secondary-original")
            )
            initial_registry = DefinitionRegistry()
            initial_registry.register(
                definition(original_primary, original_secondary)
            )
            created = await Runner(initial_registry, store).create_run(
                "binding-set-drift", "1", "hi"
            )

            changed_primary = DeterministicModelAdapter(
                ("primary",), model_contract=primary_contract
            )
            changed_secondary = DeterministicModelAdapter(
                ("changed",), model_contract=contract("secondary-changed")
            )
            changed_registry = DefinitionRegistry()
            changed_registry.register(
                definition(changed_primary, changed_secondary)
            )

            with self.assertRaisesRegex(
                RuntimeError, "snapshot Model Contract Binding set"
            ):
                await Runner(changed_registry, store).start_run(created.run_id)
            self.assertEqual(changed_primary.call_count, 0)
            self.assertEqual(changed_secondary.call_count, 0)

        with tempfile.TemporaryDirectory() as tmp:
            stores = (
                InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
                SQLiteRunStore(
                    os.path.join(tmp, "binding-set-drift.db"),
                    payload_codec=PlaintextPayloadCodec(),
                ),
            )
            try:
                for store in stores:
                    with self.subTest(store=type(store).__name__):
                        await assert_rejected(store)
            finally:
                stores[1].close()

    async def test_model_contract_is_rechecked_after_reservation(self) -> None:
        from m_agent.adapters import InMemoryRunStore, PlaintextPayloadCodec
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelAdapter,
            ModelContract,
            ModelLimits,
            ModelResponse,
            RevisionStability,
            Runner,
            RunStatus,
        )

        def contract(revision: str) -> ModelContract:
            return ModelContract(
                contract_id="mutable-live-contract",
                version="1",
                revision_stability=RevisionStability.PINNED,
                model_identity=f"live:{revision}",
                limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
                input_sizer_id="live-sizer-v1",
                serialization_id="live-wire-v1",
                configuration_fingerprint=f"deployment-{revision}",
            )

        class MutableLiveAdapter(ModelAdapter):
            capabilities = contract("a").capabilities

            def __init__(self) -> None:
                self.current_contract = contract("a")
                self.dispatched_contracts: list[str] = []

            @property
            def model_contract(self) -> ModelContract:
                return self.current_contract

            def definition_contract_fingerprint(self) -> str:
                return self.current_contract.configuration_fingerprint or ""

            async def generate(self, request) -> ModelResponse:
                self.dispatched_contracts.append(
                    self.current_contract.model_identity
                )
                return ModelResponse(
                    content=f"used:{self.current_contract.model_identity}"
                )

        class ContractSwitchingStore(InMemoryRunStore):
            def __init__(self, adapter: MutableLiveAdapter) -> None:
                super().__init__(payload_codec=PlaintextPayloadCodec())
                self._adapter = adapter

            async def reserve_model_attempt(self, *args, **kwargs) -> bool:
                reserved = await super().reserve_model_attempt(*args, **kwargs)
                if reserved:
                    self._adapter.current_contract = contract("b")
                return reserved

        adapter = MutableLiveAdapter()
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="reservation-contract",
                version="1",
                instructions="Do not dispatch after contract drift.",
                model_adapter=adapter,
            )
        )
        runner = Runner(registry, ContractSwitchingStore(adapter))
        created = await runner.create_run("reservation-contract", "1", "hello")
        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(adapter.dispatched_contracts, [])

    async def test_live_configuration_drift_is_rejected_before_run_creation(
        self,
    ) -> None:
        from m_agent.adapters import (
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelAdapter,
            ModelContract,
            ModelLimits,
            ModelResponse,
            RevisionStability,
            Runner,
        )

        contract = ModelContract(
            contract_id="mutable-live-configuration",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="live:mutable-configuration",
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="live-sizer-v1",
            serialization_id="live-wire-v1",
            configuration_fingerprint="configuration-a",
        )

        class MutableConfigurationAdapter(ModelAdapter):
            capabilities = contract.capabilities

            def __init__(self) -> None:
                self.configuration = "configuration-a"
                self.dispatches = 0

            @property
            def model_contract(self) -> ModelContract:
                return contract

            def definition_contract_fingerprint(self) -> str:
                return self.configuration

            async def generate(self, request) -> ModelResponse:
                self.dispatches += 1
                return ModelResponse(content="must not dispatch")

        async def assert_rejected(store) -> None:
            adapter = MutableConfigurationAdapter()
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="mutable-live-configuration",
                    version="1",
                    instructions="Reject configuration drift before creating a run.",
                    model_adapter=adapter,
                )
            )
            adapter.configuration = "configuration-b"

            with self.assertRaisesRegex(
                ValueError, "configuration fingerprint does not match"
            ):
                await Runner(registry, store).create_run(
                    "mutable-live-configuration", "1", "hello"
                )
            self.assertEqual(adapter.dispatches, 0)

        with tempfile.TemporaryDirectory() as tmp:
            stores = (
                InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
                SQLiteRunStore(
                    os.path.join(tmp, "mutable-live-configuration.db"),
                    payload_codec=PlaintextPayloadCodec(),
                ),
            )
            try:
                for store in stores:
                    with self.subTest(store=type(store).__name__):
                        await assert_rejected(store)
            finally:
                stores[1].close()

    async def test_final_lease_check_precedes_adapter_dispatch(self) -> None:
        """A mutable adapter hook cannot invalidate the lease after its guard."""
        from m_agent import DEFAULT_LEASE_TTL, FakeClock
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            LeaseNotHeldError,
            Runner,
        )

        clock = FakeClock()

        class LeaseExpiringAdapter(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(("must not dispatch",))
                self._expire_after_reservation = False

            @property
            def model_contract(self):
                if self._expire_after_reservation:
                    self._expire_after_reservation = False
                    clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
                return super().model_contract

        class ReservationClockAdvancingStore(InMemoryRunStore):
            def __init__(self, adapter: LeaseExpiringAdapter) -> None:
                super().__init__(
                    payload_codec=PlaintextPayloadCodec(), clock=clock
                )
                self._adapter = adapter

            async def reserve_model_attempt(self, *args, **kwargs) -> bool:
                reserved = await super().reserve_model_attempt(*args, **kwargs)
                if reserved:
                    self._adapter._expire_after_reservation = True
                return reserved

        adapter = LeaseExpiringAdapter()
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="lease-adjacent-dispatch",
                version="1",
                instructions="Never dispatch after lease expiry.",
                model_adapter=adapter,
            )
        )
        runner = Runner(registry, ReservationClockAdvancingStore(adapter))
        created = await runner.create_run("lease-adjacent-dispatch", "1", "hi")

        with self.assertRaises(LeaseNotHeldError):
            await runner.start_run(created.run_id)

        self.assertEqual(adapter.call_count, 0)

    async def test_model_dispatch_rechecks_lease_after_contract_hooks(
        self,
    ) -> None:
        from m_agent import DEFAULT_LEASE_TTL, FakeClock
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            LeaseNotHeldError,
            Runner,
        )

        clock = FakeClock()

        class LeaseExpiringAdapter(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(("must not dispatch",))
                self.expire_on_contract_read = False

            @property
            def model_contract(self):
                if self.expire_on_contract_read:
                    self.expire_on_contract_read = False
                    clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
                return super().model_contract

        class PostGuardExpiringStore(InMemoryRunStore):
            def __init__(self, adapter: LeaseExpiringAdapter) -> None:
                super().__init__(
                    payload_codec=PlaintextPayloadCodec(), clock=clock
                )
                self._adapter = adapter
                self._armed = False

            async def reserve_model_attempt(self, *args, **kwargs) -> bool:
                reserved = await super().reserve_model_attempt(*args, **kwargs)
                self._armed = reserved
                return reserved

            async def prepare_model_dispatch(self, *args, **kwargs) -> None:
                if self._armed:
                    self._armed = False
                    self._adapter.expire_on_contract_read = True
                await super().prepare_model_dispatch(*args, **kwargs)

        adapter = LeaseExpiringAdapter()
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="post-contract-lease-guard",
                version="1",
                instructions="Never dispatch after lease expiry.",
                model_adapter=adapter,
            )
        )
        runner = Runner(registry, PostGuardExpiringStore(adapter))
        created = await runner.create_run(
            "post-contract-lease-guard", "1", "hi"
        )

        with self.assertRaises(LeaseNotHeldError):
            await runner.start_run(created.run_id)

        self.assertEqual(adapter.call_count, 0)

    def test_live_adapter_requires_verifiable_current_configuration(self) -> None:
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelAdapter,
            ModelContract,
            ModelLimits,
            ModelResponse,
            RevisionStability,
        )

        class UnverifiableLiveAdapter(ModelAdapter):
            capabilities = ModelContract(
                contract_id="unverifiable",
                version="1",
                revision_stability=RevisionStability.PINNED,
                model_identity="live:unverifiable",
                limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
                input_sizer_id="live-sizer-v1",
                serialization_id="live-wire-v1",
                configuration_fingerprint="claimed-deployment",
            ).capabilities

            @property
            def model_contract(self) -> ModelContract:
                return ModelContract(
                    contract_id="unverifiable",
                    version="1",
                    revision_stability=RevisionStability.PINNED,
                    model_identity="live:unverifiable",
                    limits=ModelLimits(
                        context_window_tokens=128, max_output_tokens=32
                    ),
                    input_sizer_id="live-sizer-v1",
                    serialization_id="live-wire-v1",
                    configuration_fingerprint="claimed-deployment",
                )

            async def generate(self, request) -> ModelResponse:
                raise AssertionError("must not dispatch")

        with self.assertRaisesRegex(ValueError, "current configuration"):
            DefinitionRegistry().register(
                AgentDefinition.for_adapter(
                    definition_id="unverifiable-live",
                    version="1",
                    instructions="Never dispatch.",
                    model_adapter=UnverifiableLiveAdapter(),
                )
            )

    async def test_tool_bearing_definition_is_rejected_at_registration(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            DeterministicTool,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilityError,
            ToolEffect,
            ToolOutcome,
        )

        adapter = DeterministicModelAdapter(("unreachable",))
        tool = DeterministicTool(
            name="lookup",
            effect=ToolEffect.READ_ONLY,
            handler=lambda request: ToolOutcome.success(
                request.call_id, request.tool_name, "found"
            ),
        )
        registry = DefinitionRegistry()
        with self.assertRaisesRegex(
            ModelCapabilityError, "TOOL_CALLING_UNSUPPORTED"
        ):
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="undeclared-tools",
                    version="1",
                    instructions="Never dispatch unsupported tools.",
                    model_adapter=adapter,
                    tools=(tool,),
                )
            )

        self.assertFalse(registry.is_registered("undeclared-tools", "1"))
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
            AgentDefinition.for_adapter(
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

    async def test_legacy_definition_does_not_gain_an_implicit_model_cap(
        self,
    ) -> None:
        """The documented 0.2 construction path remains unbounded in 0.2.x."""
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
            ModelResponse,
            Runner,
            RunStatus,
            ToolCall,
            ToolCallingMode,
            ToolEffect,
            ToolOutcome,
        )

        class EightToolsThenAnswer(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(
                    ("unused",),
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    ),
                )

            async def generate(self, request) -> ModelResponse:
                self.call_count += 1
                self._last_request = request
                if self.call_count <= 8:
                    return ModelResponse(
                        tool_calls=(
                            ToolCall(
                                call_id=f"lookup-{self.call_count}",
                                tool_name="lookup",
                                arguments="{}",
                            ),
                        )
                    )
                return ModelResponse(content="ninth model response")

        adapter = EightToolsThenAnswer()
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
                definition_id="legacy-unbounded-model-loop",
                version="1",
                instructions="Use the tool until the answer is ready.",
                model_adapter=adapter,
                tools=(tool,),
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )
        created = await runner.create_run(
            "legacy-unbounded-model-loop", "1", "hello"
        )
        assert created.snapshot is not None
        self.assertIsNone(created.snapshot.model_execution_budget)

        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(terminal.output, "ninth model response")
        self.assertEqual(adapter.call_count, 9)

    async def test_explicit_typed_definition_freezes_default_model_budget(
        self,
    ) -> None:
        """New explicit bindings cannot enter the 0.2 unbounded path."""
        from m_agent.adapters import (
            DeterministicModelAdapter,
            DeterministicTool,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelBindingSet,
            ModelCapabilities,
            ModelExecutionBudget,
            ModelResponse,
            Runner,
            RunStatus,
            ToolCall,
            ToolCallingMode,
            ToolEffect,
            ToolOutcome,
        )

        class EightToolsThenAnswer(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(
                    ("unused",),
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    ),
                )

            async def generate(self, request) -> ModelResponse:
                self.call_count += 1
                self._last_request = request
                if self.call_count <= 8:
                    return ModelResponse(
                        tool_calls=(
                            ToolCall(
                                call_id=f"lookup-{self.call_count}",
                                tool_name="lookup",
                                arguments="{}",
                            ),
                        )
                    )
                return ModelResponse(content="ninth model response")

        adapter = EightToolsThenAnswer()
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
                definition_id="typed-default-model-budget",
                version="1",
                instructions="Use the tool until the answer is ready.",
                model_bindings=ModelBindingSet.reuse_primary(
                    adapter.model_contract
                ),
                model_adapter=adapter,
                tools=(tool,),
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )
        created = await runner.create_run(
            "typed-default-model-budget", "1", "hello"
        )
        assert created.snapshot is not None
        self.assertEqual(
            created.snapshot.model_execution_budget, ModelExecutionBudget()
        )

        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_EXECUTION_BUDGET_EXCEEDED")
        self.assertEqual(adapter.call_count, 8)

    async def test_legacy_run_store_without_typed_dispatch_methods_runs(
        self,
    ) -> None:
        """The 0.2 RunStore protocol remains usable by legacy Definitions."""
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
            RunStatus,
        )

        class LegacyRunStore(InMemoryRunStore):
            def __getattribute__(self, name):
                if name in {"prepare_model_dispatch", "reserve_model_attempt"}:
                    raise AttributeError(name)
                return super().__getattribute__(name)

        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition(
                definition_id="legacy-store",
                version="1",
                instructions="Reply.",
                model_adapter=DeterministicModelAdapter(("answer",)),
            )
        )
        runner = Runner(
            registry,
            LegacyRunStore(payload_codec=PlaintextPayloadCodec()),
        )
        created = await runner.create_run("legacy-store", "1", "hello")

        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(terminal.output, "answer")

    async def test_usage_keeps_unavailable_and_rejects_missing_required_fields(
        self,
    ) -> None:
        from m_agent import deserialize_model_response
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelContract,
            ModelLimits,
            ModelUsageGuarantees,
            RevisionStability,
            Runner,
            RunStatus,
            StepStatus,
            UsageFieldGuarantee,
            UsageProvenance,
            UsageReportingMode,
        )

        class UsageAdapter(DeterministicModelAdapter):
            def __init__(
                self,
                provenance: UsageProvenance,
                model_contract: ModelContract,
            ) -> None:
                super().__init__(("accepted",), model_contract=model_contract)
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
                        raw_unit=(
                            "tokens"
                            if self._provenance
                            is UsageProvenance.PROVIDER_REPORTED
                            else None
                        ),
                        normalization_source=(
                            "deterministic-usage-v1"
                            if self._provenance
                            is UsageProvenance.PROVIDER_REPORTED
                            else None
                        ),
                    ),
                )

        class PartialUsageAdapter(DeterministicModelAdapter):
            async def generate(self, request):
                response = await super().generate(request)
                from m_agent.runtime import ModelUsage, ModelResponse

                return ModelResponse(
                    content=response.content,
                    usage=ModelUsage(
                        input_tokens=5,
                        provenance=UsageProvenance.PROVIDER_REPORTED,
                        raw_unit="tokens",
                        normalization_source="deterministic-usage-v1",
                    ),
                )

        def contract(guarantees: ModelUsageGuarantees) -> ModelContract:
            return ModelContract(
                contract_id="usage-contract",
                version="1",
                revision_stability=RevisionStability.PINNED,
                model_identity="deterministic:usage",
                capabilities=ModelCapabilities(
                    usage_reporting=UsageReportingMode.PROVIDER_REPORTED
                ),
                limits=ModelLimits(
                    context_window_tokens=128, max_output_tokens=32
                ),
                input_sizer_id="deterministic-v1",
                serialization_id="deterministic-text-v1",
                usage_guarantees=guarantees,
            )

        async def run(
            guarantees: ModelUsageGuarantees,
            adapter: DeterministicModelAdapter | None = None,
            store=None,
        ):
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
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
                store=(
                    store
                    if store is not None
                    else InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
                ),
            )
            created = await runner.create_run(
                "usage", guarantees.input_tokens.value, "hello"
            )
            terminal = await runner.start_run(created.run_id)
            return created, terminal, await runner.inspect_run(created.run_id)

        _, optional_terminal, optional_inspection = await run(
            ModelUsageGuarantees()
        )
        self.assertIs(optional_terminal.status, RunStatus.SUCCEEDED)
        response = deserialize_model_response(optional_inspection.attempts[0].output)
        assert response.usage is not None
        self.assertIs(response.usage.provenance, UsageProvenance.UNAVAILABLE)
        self.assertIsNone(response.usage.input_tokens)
        self.assertIs(
            response.usage.output_tokens_provenance,
            UsageProvenance.UNAVAILABLE,
        )

        _, provider_terminal, provider_inspection = await run(
            ModelUsageGuarantees(),
            UsageAdapter(
                UsageProvenance.PROVIDER_REPORTED,
                contract(ModelUsageGuarantees()),
            ),
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
        self.assertIs(
            provider_response.usage.input_tokens_provenance,
            UsageProvenance.PROVIDER_REPORTED,
        )

        _, sized_terminal, sized_inspection = await run(
            ModelUsageGuarantees(),
            UsageAdapter(
                UsageProvenance.RUNTIME_SIZED,
                contract(ModelUsageGuarantees()),
            ),
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

        _, partial_terminal, partial_inspection = await run(
            ModelUsageGuarantees(),
            PartialUsageAdapter(
                ("accepted",), model_contract=contract(ModelUsageGuarantees())
            ),
        )
        self.assertIs(partial_terminal.status, RunStatus.SUCCEEDED)
        partial_response = deserialize_model_response(
            partial_inspection.attempts[0].output
        )
        assert partial_response.usage is not None
        self.assertIs(
            partial_response.usage.input_tokens_provenance,
            UsageProvenance.PROVIDER_REPORTED,
        )
        self.assertIs(
            partial_response.usage.output_tokens_provenance,
            UsageProvenance.UNAVAILABLE,
        )

        _, required_terminal, required_inspection = await run(
            ModelUsageGuarantees(input_tokens=UsageFieldGuarantee.REQUIRED)
        )
        self.assertIs(required_terminal.status, RunStatus.FAILED)
        self.assertEqual(
            required_terminal.error_code, "MODEL_CONTRACT_VIOLATION"
        )
        self.assertEqual(len(required_inspection.attempts), 1)

        required_usage = ModelUsageGuarantees(
            input_tokens=UsageFieldGuarantee.REQUIRED,
            output_tokens=UsageFieldGuarantee.REQUIRED,
        )

        async def assert_failed_attempt_keeps_usage(store):
            created, terminal, inspection = await run(
                required_usage,
                PartialUsageAdapter(
                    ("accepted",), model_contract=contract(required_usage)
                ),
                store,
            )
            self.assertIs(terminal.status, RunStatus.FAILED)
            self.assertEqual(
                terminal.error_code, "MODEL_CONTRACT_VIOLATION"
            )
            self.assertEqual(inspection.attempts[0].status, StepStatus.FAILED)
            usage = inspection.attempts[0].usage
            assert usage is not None
            self.assertEqual(usage.input_tokens, 5)
            self.assertIsNone(usage.output_tokens)
            self.assertIs(
                usage.input_tokens_provenance,
                UsageProvenance.PROVIDER_REPORTED,
            )
            return created.run_id

        await assert_failed_attempt_keeps_usage(
            InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "failed-usage.db")
            store = SQLiteRunStore(db_path, payload_codec=PlaintextPayloadCodec())
            try:
                run_id = await assert_failed_attempt_keeps_usage(store)
            finally:
                store.close()
            reopened = SQLiteRunStore(
                db_path, payload_codec=PlaintextPayloadCodec()
            )
            try:
                inspection = await Runner(
                    DefinitionRegistry(), reopened
                ).inspect_run(run_id)
            finally:
                reopened.close()
        usage = inspection.attempts[0].usage
        assert usage is not None
        self.assertEqual(usage.input_tokens, 5)

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

        def contract(revision: str) -> ModelContract:
            return ModelContract(
                contract_id="pinned-model",
                version="1",
                revision_stability=RevisionStability.PINNED,
                model_identity=f"deterministic:pinned:{revision}",
                limits=ModelLimits(
                    context_window_tokens=128, max_output_tokens=32
                ),
                input_sizer_id="deterministic-v1",
                serialization_id="deterministic-text-v1",
            )

        first_adapter = DeterministicModelAdapter(
            ("first",), model_contract=contract("first-revision")
        )
        first_registry = DefinitionRegistry()
        first_registry.register(
            AgentDefinition.for_adapter(
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
            AgentDefinition.for_adapter(
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

    async def test_public_runner_contract_matrix_for_inmemory_and_sqlite(
        self,
    ) -> None:
        """Both public Store paths preserve the typed Contract boundaries."""
        from m_agent.adapters import (
            DeterministicModelAdapter,
            DeterministicTool,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelCapabilityError,
            ModelContract,
            ModelExecutionBudget,
            ModelLimits,
            ModelRequirements,
            ModelResponse,
            RevisionStability,
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
                return ModelResponse(content="must not dispatch twice")

        with tempfile.TemporaryDirectory() as tmp:
            stores = (
                (
                    "memory",
                    InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
                ),
                (
                    "sqlite",
                    SQLiteRunStore(
                        os.path.join(tmp, "runs.db"),
                        payload_codec=PlaintextPayloadCodec(),
                    ),
                ),
            )
            for name, store in stores:
                with self.subTest(store=name):
                    try:
                        success_adapter = DeterministicModelAdapter(("accepted",))
                        success_registry = DefinitionRegistry()
                        success_registry.register(
                            AgentDefinition.for_adapter(
                                definition_id="matrix-success",
                                version="1",
                                instructions="Reply.",
                                model_adapter=success_adapter,
                            )
                        )
                        success_runner = Runner(success_registry, store)
                        created = await success_runner.create_run(
                            "matrix-success", "1", "hello"
                        )
                        terminal = await success_runner.start_run(created.run_id)
                        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
                        self.assertEqual(success_adapter.call_count, 1)

                        mismatch_adapter = DeterministicModelAdapter(("unused",))
                        with self.assertRaises(ModelCapabilityError):
                            DefinitionRegistry().register(
                                AgentDefinition.for_adapter(
                                    definition_id="matrix-mismatch",
                                    version="1",
                                    instructions="Never dispatch.",
                                    model_requirements=ModelRequirements(
                                        capabilities=ModelCapabilities(
                                            tool_calling=ToolCallingMode.NATIVE
                                        )
                                    ),
                                    model_adapter=mismatch_adapter,
                                )
                            )
                        self.assertEqual(mismatch_adapter.call_count, 0)

                        def contract(revision: str) -> ModelContract:
                            return ModelContract(
                                contract_id="matrix-contract",
                                version="1",
                                revision_stability=RevisionStability.PINNED,
                                model_identity=(
                                    f"deterministic:matrix:{revision}"
                                ),
                                limits=ModelLimits(
                                    context_window_tokens=128,
                                    max_output_tokens=32,
                                ),
                                input_sizer_id="deterministic-v1",
                                serialization_id="deterministic-text-v1",
                            )

                        original = DeterministicModelAdapter(
                            ("first",), model_contract=contract("original")
                        )
                        original_registry = DefinitionRegistry()
                        original_registry.register(
                            AgentDefinition.for_adapter(
                                definition_id="matrix-drift",
                                version="1",
                                instructions="Reply.",
                                model_adapter=original,
                            )
                        )
                        drift_runner = Runner(original_registry, store)
                        drift_created = await drift_runner.create_run(
                            "matrix-drift", "1", "hello"
                        )
                        changed = DeterministicModelAdapter(
                            ("must not dispatch",),
                            model_contract=contract("changed"),
                        )
                        changed_registry = DefinitionRegistry()
                        changed_registry.register(
                            AgentDefinition.for_adapter(
                                definition_id="matrix-drift",
                                version="1",
                                instructions="Reply.",
                                model_adapter=changed,
                            )
                        )
                        with self.assertRaisesRegex(
                            RuntimeError, "snapshot Model Contract"
                        ):
                            await Runner(changed_registry, store).start_run(
                                drift_created.run_id
                            )
                        self.assertEqual(changed.call_count, 0)

                        budget_adapter = ToolThenAnswer()
                        tool = DeterministicTool(
                            name="lookup",
                            effect=ToolEffect.READ_ONLY,
                            handler=lambda request: ToolOutcome.success(
                                request.call_id,
                                request.tool_name,
                                "found",
                            ),
                        )
                        budget_registry = DefinitionRegistry()
                        budget_registry.register(
                            AgentDefinition.for_adapter(
                                definition_id="matrix-budget",
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
                                model_adapter=budget_adapter,
                                tools=(tool,),
                            )
                        )
                        budget_runner = Runner(budget_registry, store)
                        budget_created = await budget_runner.create_run(
                            "matrix-budget", "1", "lookup"
                        )
                        budget_terminal = await budget_runner.start_run(
                            budget_created.run_id
                        )
                        self.assertIs(budget_terminal.status, RunStatus.FAILED)
                        self.assertEqual(
                            budget_terminal.error_code,
                            "MODEL_EXECUTION_BUDGET_EXCEEDED",
                        )
                        self.assertEqual(budget_adapter.call_count, 1)
                    finally:
                        close = getattr(store, "close", None)
                        if close is not None:
                            close()

    async def test_sqlite_recovery_replays_uncheckpointed_model_with_budget(
        self,
    ) -> None:
        from m_agent import FakeClock
        from m_agent.adapters import (
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelExecutionBudget,
            RetryPolicy,
            Runner,
            RunStatus,
            StepStatus,
        )
        from fixtures.crash_worker import LoggingModelAdapter

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "run.db")
            model_log = os.path.join(tmp, "model-calls.log")
            environment = {
                **os.environ,
                "M_AGENT_TEST_MODEL_BUDGET": "2",
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

            adapter = LoggingModelAdapter(model_log, ("crash-safe answer",))
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="assistant",
                    version="1.0",
                    instructions="Answer deterministically.",
                    model_execution_budget=ModelExecutionBudget(
                        run_max_attempts=2,
                        primary_max_attempts=2,
                        context_compression_max_attempts=0,
                        output_repair_max_attempts=0,
                    ),
                    model_adapter=adapter,
                    retry_policy=RetryPolicy(max_attempts=2),
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

            self.assertIs(terminal.status, RunStatus.SUCCEEDED)
            self.assertEqual(adapter.call_count, 1)
            with open(model_log, encoding="utf-8") as fh:
                self.assertEqual(fh.read().splitlines(), ["hi"])
            self.assertEqual(
                [attempt.status for attempt in inspection.attempts],
                [StepStatus.FAILED, StepStatus.SUCCEEDED],
            )
            self.assertEqual(
                len(
                    [
                        attempt
                        for attempt in inspection.attempts
                        if attempt.model_purpose is not None
                    ]
                ),
                2,
            )

    async def test_recovery_replays_after_success_attempt_persistence_fails(
        self,
    ) -> None:
        """An uncheckpointed Model Attempt consumes budget then replays."""
        from m_agent import DEFAULT_LEASE_TTL, FakeClock
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            RetryPolicy,
            Runner,
            RunStatus,
            StepStatus,
        )

        class FailingInMemoryStore(InMemoryRunStore):
            fail_success_attempt = True

            async def record_attempt(self, attempt, **kwargs):
                if (
                    self.fail_success_attempt
                    and attempt.status is StepStatus.SUCCEEDED
                ):
                    self.fail_success_attempt = False
                    raise RuntimeError("injected attempt persistence failure")
                return await super().record_attempt(attempt, **kwargs)

        class FailingSQLiteStore(SQLiteRunStore):
            fail_success_attempt = True

            async def record_attempt(self, attempt, **kwargs):
                if (
                    self.fail_success_attempt
                    and attempt.status is StepStatus.SUCCEEDED
                ):
                    self.fail_success_attempt = False
                    raise RuntimeError("injected attempt persistence failure")
                return await super().record_attempt(attempt, **kwargs)

        def registry_and_adapter():
            adapter = DeterministicModelAdapter(("answer",))
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="attempt-persistence",
                    version="1",
                    instructions="Reply.",
                    model_adapter=adapter,
                    retry_policy=RetryPolicy(max_attempts=2),
                )
            )
            return registry, adapter

        clock = FakeClock()
        memory_store = FailingInMemoryStore(
            payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        registry, adapter = registry_and_adapter()
        created = await Runner(registry, memory_store).create_run(
            "attempt-persistence", "1", "hello"
        )
        with self.assertRaisesRegex(RuntimeError, "attempt persistence"):
            await Runner(registry, memory_store).start_run(created.run_id)
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        terminal = await Runner(registry, memory_store).resume_run(created.run_id)
        inspection = await Runner(registry, memory_store).inspect_run(
            created.run_id
        )
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(adapter.call_count, 2)
        self.assertEqual(
            [step.status for step in inspection.steps], [StepStatus.SUCCEEDED]
        )
        self.assertEqual(
            [attempt.status for attempt in inspection.attempts],
            [StepStatus.FAILED, StepStatus.SUCCEEDED],
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "run.db")
            first_clock = FakeClock()
            first_store = FailingSQLiteStore(
                db_path,
                payload_codec=PlaintextPayloadCodec(),
                clock=first_clock,
            )
            registry, adapter = registry_and_adapter()
            try:
                created = await Runner(registry, first_store).create_run(
                    "attempt-persistence", "1", "hello"
                )
                with self.assertRaisesRegex(
                    RuntimeError, "attempt persistence"
                ):
                    await Runner(registry, first_store).start_run(created.run_id)
                crashed = await first_store.get_run(created.run_id)
                assert crashed is not None
                assert crashed.lease_expires_at is not None
                restart_clock = FakeClock(
                    crashed.lease_expires_at + timedelta(seconds=1)
                )
            finally:
                first_store.close()
            reopened = SQLiteRunStore(
                db_path,
                payload_codec=PlaintextPayloadCodec(),
                clock=restart_clock,
            )
            try:
                terminal = await Runner(registry, reopened).resume_run(
                    created.run_id
                )
                inspection = await Runner(registry, reopened).inspect_run(
                    created.run_id
                )
            finally:
                reopened.close()
            self.assertIs(terminal.status, RunStatus.SUCCEEDED)
            self.assertEqual(adapter.call_count, 2)
            self.assertEqual(
                [step.status for step in inspection.steps], [StepStatus.SUCCEEDED]
            )
            self.assertEqual(
                [attempt.status for attempt in inspection.attempts],
                [StepStatus.FAILED, StepStatus.SUCCEEDED],
            )

    async def test_recovery_replays_after_success_checkpoint_persistence_fails(
        self,
    ) -> None:
        """A failed checkpoint retains usage before bounded replay."""
        from m_agent import DEFAULT_LEASE_TTL, FakeClock
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelResponse,
            ModelUsage,
            RetryPolicy,
            Runner,
            RunStatus,
            StepStatus,
            StepType,
            UsageProvenance,
        )

        class FailingCheckpointStore:
            fail_model_checkpoint = True

            async def record_checkpoint(self, checkpoint, **kwargs):
                if (
                    self.fail_model_checkpoint
                    and checkpoint.step_type is StepType.MODEL
                ):
                    self.fail_model_checkpoint = False
                    raise RuntimeError("injected checkpoint persistence failure")
                return await super().record_checkpoint(checkpoint, **kwargs)

        class FailingInMemoryStore(FailingCheckpointStore, InMemoryRunStore):
            pass

        class FailingSQLiteStore(FailingCheckpointStore, SQLiteRunStore):
            pass

        usage = ModelUsage(
            input_tokens=11,
            output_tokens=7,
            provenance=UsageProvenance.RUNTIME_SIZED,
            raw_unit="tokens",
            normalization_source="runtime-sizer-v1",
        )

        class UsageAdapter(DeterministicModelAdapter):
            async def generate(self, request):
                self.call_count += 1
                self._last_request = request
                return ModelResponse(content="answer", usage=usage)

        def registry_and_adapter():
            adapter = UsageAdapter(("unused",))
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="checkpoint-persistence",
                    version="1",
                    instructions="Reply.",
                    model_adapter=adapter,
                    retry_policy=RetryPolicy(max_attempts=2),
                )
            )
            return registry, adapter

        clock = FakeClock()
        memory_store = FailingInMemoryStore(
            payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        registry, adapter = registry_and_adapter()
        created = await Runner(registry, memory_store).create_run(
            "checkpoint-persistence", "1", "hello"
        )
        with self.assertRaisesRegex(RuntimeError, "checkpoint persistence"):
            await Runner(registry, memory_store).start_run(created.run_id)
        before_recovery = await Runner(registry, memory_store).inspect_run(
            created.run_id
        )
        self.assertEqual(before_recovery.attempts[0].usage, usage)
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        terminal = await Runner(registry, memory_store).resume_run(created.run_id)
        inspection = await Runner(registry, memory_store).inspect_run(
            created.run_id
        )
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(adapter.call_count, 2)
        self.assertEqual(
            [step.status for step in inspection.steps], [StepStatus.SUCCEEDED]
        )
        self.assertEqual(
            [attempt.status for attempt in inspection.attempts],
            [StepStatus.FAILED, StepStatus.SUCCEEDED],
        )
        self.assertEqual(inspection.attempts[0].usage, usage)
        self.assertEqual(inspection.attempts[1].usage, usage)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "run.db")
            first_clock = FakeClock()
            first_store = FailingSQLiteStore(
                db_path,
                payload_codec=PlaintextPayloadCodec(),
                clock=first_clock,
            )
            registry, adapter = registry_and_adapter()
            try:
                created = await Runner(registry, first_store).create_run(
                    "checkpoint-persistence", "1", "hello"
                )
                with self.assertRaisesRegex(
                    RuntimeError, "checkpoint persistence"
                ):
                    await Runner(registry, first_store).start_run(created.run_id)
                before_recovery = await Runner(
                    registry, first_store
                ).inspect_run(created.run_id)
                self.assertEqual(before_recovery.attempts[0].usage, usage)
                crashed = await first_store.get_run(created.run_id)
                assert crashed is not None
                assert crashed.lease_expires_at is not None
                restart_clock = FakeClock(
                    crashed.lease_expires_at + timedelta(seconds=1)
                )
            finally:
                first_store.close()
            reopened = SQLiteRunStore(
                db_path,
                payload_codec=PlaintextPayloadCodec(),
                clock=restart_clock,
            )
            try:
                terminal = await Runner(registry, reopened).resume_run(
                    created.run_id
                )
                inspection = await Runner(registry, reopened).inspect_run(
                    created.run_id
                )
            finally:
                reopened.close()
            self.assertIs(terminal.status, RunStatus.SUCCEEDED)
            self.assertEqual(adapter.call_count, 2)
            self.assertEqual(
                [step.status for step in inspection.steps], [StepStatus.SUCCEEDED]
            )
            self.assertEqual(
                [attempt.status for attempt in inspection.attempts],
                [StepStatus.FAILED, StepStatus.SUCCEEDED],
            )
            self.assertEqual(inspection.attempts[0].usage, usage)
            self.assertEqual(inspection.attempts[1].usage, usage)

    async def test_recovery_replays_checkpoint_unconfirmed_attempt_with_budget(
        self,
    ) -> None:
        from m_agent import DEFAULT_LEASE_TTL, CrashPoint, FakeClock
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            RetryPolicy,
            Runner,
            RunStatus,
            StepStatus,
            StepType,
        )

        class FailingCheckpointStore:
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.fail_model_checkpoint = True

            async def record_checkpoint(self, checkpoint, **kwargs):
                if (
                    self.fail_model_checkpoint
                    and checkpoint.step_type is StepType.MODEL
                ):
                    self.fail_model_checkpoint = False
                    raise RuntimeError("injected checkpoint persistence failure")
                return await super().record_checkpoint(checkpoint, **kwargs)

        class FailingInMemoryStore(FailingCheckpointStore, InMemoryRunStore):
            pass

        class FailingSQLiteStore(FailingCheckpointStore, SQLiteRunStore):
            pass

        async def assert_replay(store, clock) -> None:
            adapter = DeterministicModelAdapter(("first", "must not dispatch"))
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="checkpoint-unconfirmed-recovery",
                    version="1",
                    instructions="Reply.",
                    model_adapter=adapter,
                    retry_policy=RetryPolicy(max_attempts=2),
                )
            )
            created = await Runner(registry, store).create_run(
                "checkpoint-unconfirmed-recovery", "1", "hello"
            )
            with self.assertRaisesRegex(RuntimeError, "checkpoint persistence"):
                await Runner(registry, store).start_run(created.run_id)

            clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))

            def crash_after_failure(point: CrashPoint, run_id: str) -> None:
                if point is CrashPoint.AFTER_ATTEMPT_FAILED:
                    raise RuntimeError("interrupt after checkpoint failure")

            with self.assertRaisesRegex(RuntimeError, "checkpoint failure"):
                await Runner(
                    registry, store, crash_hook=crash_after_failure
                ).resume_run(created.run_id)

            intermediate = await store.get_run(created.run_id)
            self.assertIsNotNone(intermediate)
            assert intermediate is not None
            self.assertIs(intermediate.status, RunStatus.RUNNING)
            self.assertEqual(
                [attempt.status for attempt in await store.get_attempts(created.run_id)],
                [StepStatus.FAILED],
            )

            clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
            terminal = await Runner(registry, store).resume_run(created.run_id)

            self.assertIs(terminal.status, RunStatus.SUCCEEDED)
            self.assertEqual(adapter.call_count, 2)

        memory_clock = FakeClock()
        memory_store = FailingInMemoryStore(
            payload_codec=PlaintextPayloadCodec(), clock=memory_clock
        )
        await assert_replay(memory_store, memory_clock)

        with tempfile.TemporaryDirectory() as tmp:
            sqlite_clock = FakeClock()
            sqlite_store = FailingSQLiteStore(
                os.path.join(tmp, "checkpoint-unconfirmed-recovery.db"),
                payload_codec=PlaintextPayloadCodec(),
                clock=sqlite_clock,
            )
            try:
                await assert_replay(sqlite_store, sqlite_clock)
            finally:
                sqlite_store.close()

    async def test_recovery_prefers_later_model_checkpoint_over_old_uncertainty(
        self,
    ) -> None:
        """A later checkpoint closes an older failed reservation for that Step."""
        from m_agent import DEFAULT_LEASE_TTL, CrashPoint, FakeClock
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            RetryPolicy,
            Runner,
            RunStatus,
            StepType,
        )

        class FailingFirstCheckpoint:
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self._fail_first_model_checkpoint = True

            async def record_checkpoint(self, checkpoint, **kwargs):
                if (
                    self._fail_first_model_checkpoint
                    and checkpoint.step_type is StepType.MODEL
                ):
                    self._fail_first_model_checkpoint = False
                    raise RuntimeError("first checkpoint persistence failure")
                return await super().record_checkpoint(checkpoint, **kwargs)

        class FailingInMemoryStore(FailingFirstCheckpoint, InMemoryRunStore):
            pass

        class FailingSQLiteStore(FailingFirstCheckpoint, SQLiteRunStore):
            pass

        async def assert_later_checkpoint_wins(store, clock) -> None:
            adapter = DeterministicModelAdapter(("first", "second", "third"))
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="latest-checkpoint-wins",
                    version="1",
                    instructions="Reply.",
                    model_adapter=adapter,
                    retry_policy=RetryPolicy(max_attempts=2),
                )
            )
            created = await Runner(registry, store).create_run(
                "latest-checkpoint-wins", "1", "hello"
            )
            with self.assertRaisesRegex(RuntimeError, "first checkpoint"):
                await Runner(registry, store).start_run(created.run_id)

            clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))

            def crash_after_later_checkpoint(point: CrashPoint, run_id: str) -> None:
                if point is CrashPoint.AFTER_MODEL_CHECKPOINT:
                    raise RuntimeError("interrupt after later checkpoint")

            with self.assertRaisesRegex(RuntimeError, "later checkpoint"):
                await Runner(
                    registry, store, crash_hook=crash_after_later_checkpoint
                ).resume_run(created.run_id)

            clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
            terminal = await Runner(registry, store).resume_run(created.run_id)

            self.assertIs(terminal.status, RunStatus.SUCCEEDED)
            self.assertEqual(terminal.output, "second")
            self.assertEqual(adapter.call_count, 2)

        memory_clock = FakeClock()
        memory_store = FailingInMemoryStore(
            payload_codec=PlaintextPayloadCodec(), clock=memory_clock
        )
        await assert_later_checkpoint_wins(memory_store, memory_clock)

        with tempfile.TemporaryDirectory() as tmp:
            sqlite_clock = FakeClock()
            sqlite_store = FailingSQLiteStore(
                os.path.join(tmp, "latest-checkpoint-wins.db"),
                payload_codec=PlaintextPayloadCodec(),
                clock=sqlite_clock,
            )
            try:
                await assert_later_checkpoint_wins(sqlite_store, sqlite_clock)
            finally:
                sqlite_store.close()

    async def test_recovery_does_not_replay_uncheckpointed_model_without_policy(
        self,
    ) -> None:
        """A budget limit cannot authorize a second uncertain provider call."""
        from m_agent import DEFAULT_LEASE_TTL, CrashPoint, FakeClock
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
            RunStatus,
            StepStatus,
        )

        adapter = DeterministicModelAdapter(("first", "must not dispatch"))
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="unconfirmed-no-retry",
                version="1",
                instructions="Reply.",
                model_adapter=adapter,
            )
        )
        clock = FakeClock()
        store = InMemoryRunStore(
            payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        created = await Runner(registry, store).create_run(
            "unconfirmed-no-retry", "1", "hello"
        )

        def crash_before_checkpoint(point: CrashPoint, run_id: str) -> None:
            if point is CrashPoint.BEFORE_MODEL_CHECKPOINT:
                raise RuntimeError("interrupt before checkpoint")

        with self.assertRaisesRegex(RuntimeError, "before checkpoint"):
            await Runner(
                registry, store, crash_hook=crash_before_checkpoint
            ).start_run(created.run_id)

        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        terminal = await Runner(registry, store).resume_run(created.run_id)
        inspection = await Runner(registry, store).inspect_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "model_checkpoint_unconfirmed")
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(
            [attempt.status for attempt in inspection.attempts],
            [StepStatus.FAILED],
        )
        self.assertEqual(
            [step.status for step in inspection.steps], [StepStatus.FAILED]
        )

    async def test_final_lease_guard_rechecks_binding_before_model_dispatch(
        self,
    ) -> None:
        from m_agent.adapters import InMemoryRunStore, PlaintextPayloadCodec
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelAdapter,
            ModelContract,
            ModelLimits,
            ModelResponse,
            RevisionStability,
            Runner,
            RunStatus,
        )

        contract = ModelContract(
            contract_id="final-lease-binding",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:final-lease-binding",
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-sizer-v1",
            serialization_id="deterministic-wire-v1",
            configuration_fingerprint="configuration-a",
        )

        class MutableFingerprintAdapter(ModelAdapter):
            deterministic = True
            capabilities = contract.capabilities

            def __init__(self) -> None:
                self.configuration = "configuration-a"
                self.dispatched_configurations: list[str] = []

            @property
            def model_contract(self) -> ModelContract:
                return contract

            def definition_contract_fingerprint(self) -> str:
                return self.configuration

            async def generate(self, request) -> ModelResponse:
                self.dispatched_configurations.append(self.configuration)
                return ModelResponse(content="must not dispatch")

        class YieldingFinalLeaseStore(InMemoryRunStore):
            def __init__(self) -> None:
                super().__init__(payload_codec=PlaintextPayloadCodec())
                self.final_lease_guard_entered = asyncio.Event()
                self.allow_final_lease_guard = asyncio.Event()

            async def prepare_model_dispatch(self, *args, **kwargs) -> None:
                self.final_lease_guard_entered.set()
                await self.allow_final_lease_guard.wait()
                await super().prepare_model_dispatch(*args, **kwargs)

        adapter = MutableFingerprintAdapter()
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="final-lease-binding",
                version="1",
                instructions="Never dispatch a drifted binding.",
                model_adapter=adapter,
            )
        )
        store = YieldingFinalLeaseStore()
        runner = Runner(registry, store)
        created = await runner.create_run("final-lease-binding", "1", "hello")

        advancing = asyncio.create_task(runner.start_run(created.run_id))
        await asyncio.wait_for(store.final_lease_guard_entered.wait(), timeout=1)
        adapter.configuration = "configuration-b"
        store.allow_final_lease_guard.set()
        terminal = await advancing

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(adapter.dispatched_configurations, [])

    async def test_model_usage_is_authoritative_attempt_metadata(self) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelContract,
            ModelLimits,
            ModelRequirements,
            ModelResponse,
            ModelUsage,
            RevisionStability,
            Runner,
            RunStatus,
            UsageReportingMode,
        )

        usage = ModelUsage(
            input_tokens=11,
            output_tokens=7,
            raw_unit="tokens",
            normalization_source="test-usage-v1",
        )
        contract = ModelContract(
            contract_id="attempt-usage",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic:attempt-usage",
            capabilities=ModelCapabilities(
                usage_reporting=UsageReportingMode.PROVIDER_REPORTED
            ),
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )

        class UsageAdapter(DeterministicModelAdapter):
            async def generate(self, request):
                self.call_count += 1
                self._last_request = request
                return ModelResponse(content="answer", usage=usage)

        def registry() -> DefinitionRegistry:
            result = DefinitionRegistry()
            result.register(
                AgentDefinition.for_adapter(
                    definition_id="attempt-usage",
                    version="1",
                    instructions="Reply.",
                    model_requirements=ModelRequirements(
                        capabilities=ModelCapabilities(
                            usage_reporting=UsageReportingMode.PROVIDER_REPORTED
                        )
                    ),
                    model_adapter=UsageAdapter(("unused",), model_contract=contract),
                )
            )
            return result

        memory_registry = registry()
        memory_runner = Runner(
            memory_registry,
            InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )
        created = await memory_runner.create_run("attempt-usage", "1", "hello")
        terminal = await memory_runner.start_run(created.run_id)
        inspection = await memory_runner.inspect_run(created.run_id)
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(inspection.attempts[0].usage, usage)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "run.db")
            store = SQLiteRunStore(db_path, payload_codec=PlaintextPayloadCodec())
            sqlite_registry = registry()
            try:
                runner = Runner(sqlite_registry, store)
                created = await runner.create_run("attempt-usage", "1", "hello")
                terminal = await runner.start_run(created.run_id)
                self.assertIs(terminal.status, RunStatus.SUCCEEDED)
            finally:
                store.close()
            reopened = SQLiteRunStore(
                db_path, payload_codec=PlaintextPayloadCodec()
            )
            try:
                inspection = await Runner(sqlite_registry, reopened).inspect_run(
                    created.run_id
                )
            finally:
                reopened.close()
            self.assertEqual(inspection.attempts[0].usage, usage)

    async def test_sqlite_model_reservation_is_atomic_across_connections(
        self,
    ) -> None:
        from m_agent.adapters import (
            DeterministicModelAdapter,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelExecutionBudget,
            ModelPurpose,
            Runner,
            RunStatus,
            StepAttempt,
            StepRecord,
            StepStatus,
            StepType,
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "run.db")
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="atomic-budget",
                    version="1",
                    instructions="Reply.",
                    model_execution_budget=ModelExecutionBudget(
                        run_max_attempts=1,
                        primary_max_attempts=1,
                        context_compression_max_attempts=0,
                        output_repair_max_attempts=0,
                    ),
                    model_adapter=DeterministicModelAdapter(("unused",)),
                )
            )
            seed = SQLiteRunStore(db_path, payload_codec=PlaintextPayloadCodec())
            try:
                created = await Runner(registry, seed).create_run(
                    "atomic-budget", "1", "hello"
                )
                running = await seed.transition_run(
                    created.run_id,
                    created.version,
                    status=RunStatus.RUNNING,
                )
                await seed.acquire_lease(
                    created.run_id,
                    "shared-owner",
                    timedelta(minutes=1),
                    expected_version=running.version,
                )
            finally:
                seed.close()

            barrier = threading.Barrier(2)

            def reserve(index: int) -> bool:
                async def invoke() -> bool:
                    store = SQLiteRunStore(
                        db_path, payload_codec=PlaintextPayloadCodec()
                    )
                    try:
                        barrier.wait(timeout=5)
                        return await store.reserve_model_attempt(
                            StepRecord(
                                step_id=f"model-{index}",
                                run_id=created.run_id,
                                step_type=StepType.MODEL,
                                status=StepStatus.RUNNING,
                            ),
                            StepAttempt(
                                attempt_id=f"attempt-{index}",
                                step_id=f"model-{index}",
                                run_id=created.run_id,
                                status=StepStatus.RUNNING,
                                model_purpose=ModelPurpose.PRIMARY,
                            ),
                            run_max_attempts=1,
                            purpose_max_attempts=1,
                            expected_version=running.version,
                            lease_owner="shared-owner",
                        )
                    finally:
                        store.close()

                return asyncio.run(invoke())

            results = await asyncio.gather(
                asyncio.to_thread(reserve, 1),
                asyncio.to_thread(reserve, 2),
            )
            probe = SQLiteRunStore(db_path, payload_codec=PlaintextPayloadCodec())
            try:
                attempts = await probe.get_attempts(created.run_id)
            finally:
                probe.close()

            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(len(attempts), 1)
