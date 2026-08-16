"""Ticket 08 public Runner contracts for typed model bindings."""

from __future__ import annotations

import asyncio
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
    def test_legacy_boolean_capabilities_and_definition_field_are_rejected(
        self,
    ) -> None:
        from pydantic import ValidationError

        from m_agent.adapters import DeterministicModelAdapter
        from m_agent.runtime import AgentDefinition, ModelCapabilities

        with self.assertRaises(ValidationError):
            ModelCapabilities(tool_calling=True)
        with self.assertRaises(ValidationError):
            AgentDefinition.for_adapter(
                definition_id="legacy-capabilities",
                version="1",
                instructions="Reply.",
                required_capabilities=ModelCapabilities(),
                model_adapter=DeterministicModelAdapter(),
            )

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
        with self.assertRaises(ValidationError):
            AgentDefinition(
                definition_id="implicit-primary-reuse",
                version="1",
                instructions="Never select a binding implicitly.",
                model_adapter=DeterministicModelAdapter(),
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
            ("structured response",), model_contract=contract
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

    async def test_malformed_model_response_is_contract_violation(self) -> None:
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
                    usage=ModelUsage(input_tokens=7),
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

    async def test_unsupported_stream_tool_combination_fails_before_dispatch(
        self,
    ) -> None:
        """Declared modes do not imply their undeclared combined protocol."""
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
            RevisionStability,
            Runner,
            RunStatus,
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
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )
        created = await runner.create_run("split-protocols", "1", "lookup")
        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
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

    async def test_tool_bearing_request_fails_before_dispatch_when_unsupported(
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
            Runner,
            RunStatus,
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
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="undeclared-tools",
                version="1",
                instructions="Never dispatch unsupported tools.",
                model_adapter=adapter,
                tools=(tool,),
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )

        created = await runner.create_run("undeclared-tools", "1", "lookup")
        terminal = await runner.start_run(created.run_id)

        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, "MODEL_CONTRACT_VIOLATION")
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
            ModelCapabilities,
            ModelContract,
            ModelLimits,
            ModelUsageGuarantees,
            RevisionStability,
            Runner,
            RunStatus,
            UsageFieldGuarantee,
            UsageProvenance,
            UsageReportingMode,
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
        self.assertIs(
            response.usage.output_tokens_provenance,
            UsageProvenance.UNAVAILABLE,
        )

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
        self.assertIs(
            provider_response.usage.input_tokens_provenance,
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

        partial_terminal, partial_inspection = await run(
            ModelUsageGuarantees(), PartialUsageAdapter(("accepted",))
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
                AgentDefinition.for_adapter(
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
