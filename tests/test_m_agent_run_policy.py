"""Public contract tests for deterministic Run Policy gates."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path


class RunPolicyContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_rejection_prevents_model_dispatch_and_is_inspectable(self) -> None:
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            PolicyAction,
            PolicyDecision,
            PolicyGate,
            Runner,
            StaticRunPolicy,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
        )

        adapter = DeterministicModelAdapter(("never dispatched",))
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="input-policy",
                version="1",
                instructions="test",
                model_adapter=adapter,
                run_policy=StaticRunPolicy(
                    policy_id="input-deny",
                    version="1",
                    decisions={
                        PolicyGate.INPUT: PolicyDecision(
                            action=PolicyAction.REJECT,
                            reason_code="INPUT_DENIED",
                        )
                    },
                ),
            )
        )
        runner = Runner(registry, InMemoryRunStore(PlaintextPayloadCodec()))

        created = await runner.create_run("input-policy", "1", "secret")
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)

        self.assertEqual(terminal.status.value, "REJECTED")
        self.assertEqual(terminal.error_code, "INPUT_DENIED")
        self.assertEqual(adapter.call_count, 0)
        self.assertEqual(len(inspection.policy_decisions), 1)
        evidence = inspection.policy_decisions[0]
        self.assertEqual(evidence.gate, PolicyGate.INPUT)
        self.assertEqual(evidence.action, PolicyAction.REJECT)
        self.assertEqual(evidence.policy_id, "input-deny")
        self.assertNotIn("secret", evidence.input_summary)

    async def test_each_policy_gate_has_allow_and_reject_evidence(self) -> None:
        """Each public gate executes before the effect it protects."""
        from m_agent.runtime import (
            AgentDefinition,
            ContextItem,
            DefinitionRegistry,
            ModelCapabilities,
            ModelRequest,
            ModelResponse,
            PolicyAction,
            PolicyDecision,
            PolicyGate,
            PolicyIdentity,
            RunPolicy,
            Runner,
            ToolCall,
            ToolEffect,
            ToolOutcome,
        )
        from m_agent.adapters import (
            DeterministicContextProvider,
            DeterministicModelAdapter,
            DeterministicTool,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
        )
        from m_agent.runtime import ModelRequirements, ToolCallingMode

        class ToolThenFinal(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(capabilities=ModelCapabilities(
                    tool_calling=ToolCallingMode.NATIVE
                ))

            async def generate(self, request: ModelRequest) -> ModelResponse:
                self.call_count += 1
                if not request.tool_outcomes:
                    return ModelResponse(tool_calls=(ToolCall(
                        call_id="gate-call", tool_name="probe", arguments="{}"
                    ),))
                return ModelResponse(content="approved")

        class GatePolicy(RunPolicy):
            def __init__(self, rejected: PolicyGate | None = None) -> None:
                self.rejected = rejected
                self.gates: list[PolicyGate] = []

            @property
            def identity(self) -> PolicyIdentity:
                return PolicyIdentity(
                    policy_id="gate-policy", version="1", fingerprint="gate-v1"
                )

            def evaluate(self, request):
                self.gates.append(request.gate)
                rejected = request.gate is self.rejected
                return PolicyDecision(
                    action=PolicyAction.REJECT if rejected else PolicyAction.ALLOW,
                    reason_code=(f"{request.gate.value}_DENIED" if rejected else "ALLOWED"),
                )

        async def execute(policy: GatePolicy):
            model = ToolThenFinal()
            tool_calls = 0

            def probe(request):
                nonlocal tool_calls
                tool_calls += 1
                return ToolOutcome.success(request.call_id, request.tool_name, "ok")

            registry = DefinitionRegistry()
            registry.register(AgentDefinition.for_adapter(
                definition_id=f"gates-{policy.rejected}", version="1",
                instructions="test", model_adapter=model,
                model_requirements=ModelRequirements(capabilities=ModelCapabilities(
                    tool_calling=ToolCallingMode.NATIVE
                )),
                context_provider=DeterministicContextProvider((ContextItem(
                    item_id="context", content="data", source="test"
                ),)),
                tools=(DeterministicTool(
                    name="probe", effect=ToolEffect.READ_ONLY, handler=probe
                ),),
                run_policy=policy,
            ))
            runner = Runner(registry, InMemoryRunStore(PlaintextPayloadCodec()))
            created = await runner.create_run(
                f"gates-{policy.rejected}", "1", "input"
            )
            return await runner.start_run(created.run_id), model.call_count, tool_calls

        allowed = GatePolicy()
        terminal, model_calls, tool_calls = await execute(allowed)
        self.assertEqual(terminal.status.value, "SUCCEEDED")
        self.assertEqual((model_calls, tool_calls), (2, 1))
        self.assertTrue(set(PolicyGate).issubset(allowed.gates))

        expected_dispatches = {
            PolicyGate.INPUT: (0, 0),
            PolicyGate.CONTEXT: (0, 0),
            PolicyGate.TOOL_REQUEST: (1, 0),
            PolicyGate.TOOL_OUTCOME: (1, 1),
            PolicyGate.FINAL_OUTPUT: (2, 1),
        }
        for gate, expected in expected_dispatches.items():
            with self.subTest(rejected_gate=gate.value):
                terminal, model_calls, tool_calls = await execute(GatePolicy(gate))
                self.assertEqual(terminal.status.value, "REJECTED")
                self.assertEqual(terminal.error_code, f"{gate.value}_DENIED")
                self.assertEqual((model_calls, tool_calls), expected)

    async def test_final_tool_authorization_wins_over_earlier_allow(self) -> None:
        """A changed policy at the final Tool request gate prevents the effect."""
        from m_agent.runtime import (
            AgentDefinition,
            CrashPoint,
            DefinitionRegistry,
            ModelCapabilities,
            ModelRequest,
            ModelResponse,
            PolicyAction,
            PolicyDecision,
            PolicyGate,
            PolicyIdentity,
            RunPolicy,
            Runner,
            ToolCall,
            ToolEffect,
            ToolOutcome,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter,
            DeterministicTool,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
        )
        from m_agent.runtime import ModelRequirements, ToolCallingMode

        class OneTool(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(capabilities=ModelCapabilities(
                    tool_calling=ToolCallingMode.NATIVE
                ))

            async def generate(self, request: ModelRequest) -> ModelResponse:
                self.call_count += 1
                return ModelResponse(tool_calls=(ToolCall(
                    call_id="race-call", tool_name="effect", arguments="{}"
                ),))

        class FinalAuthorizationPolicy(RunPolicy):
            def __init__(self) -> None:
                self.request_payloads: list[dict[str, object]] = []

            @property
            def identity(self) -> PolicyIdentity:
                return PolicyIdentity(
                    policy_id="final-tool-gate", version="1", fingerprint="tool-gate-v1"
                )

            def evaluate(self, request):
                if request.gate is PolicyGate.TOOL_REQUEST:
                    payload = dict(request.payload)
                    self.request_payloads.append(payload)
                    if payload.get("final_authorization") is True:
                        return PolicyDecision(
                            action=PolicyAction.REJECT,
                            reason_code="FINAL_TOOL_AUTHORIZATION_DENIED",
                        )
                return PolicyDecision(action=PolicyAction.ALLOW, reason_code="ALLOWED")

        model = OneTool()
        policy = FinalAuthorizationPolicy()
        tool_calls = 0

        def effect(request):
            nonlocal tool_calls
            tool_calls += 1
            return ToolOutcome.success(request.call_id, request.tool_name, "unexpected")

        registry = DefinitionRegistry()
        registry.register(AgentDefinition.for_adapter(
            definition_id="final-tool-authorization", version="1", instructions="test",
            model_adapter=model,
            model_requirements=ModelRequirements(capabilities=ModelCapabilities(
                tool_calling=ToolCallingMode.NATIVE
            )),
            tools=(DeterministicTool(
                name="effect", effect=ToolEffect.IDEMPOTENT, handler=effect
            ),),
            run_policy=policy,
        ))
        runner = Runner(registry, InMemoryRunStore(PlaintextPayloadCodec()))
        created = await runner.create_run("final-tool-authorization", "1", "input")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status.value, "REJECTED")
        self.assertEqual(terminal.error_code, "FINAL_TOOL_AUTHORIZATION_DENIED")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(tool_calls, 0)
        self.assertEqual(len(policy.request_payloads), 2)
        self.assertNotIn("final_authorization", policy.request_payloads[0])
        self.assertTrue(policy.request_payloads[1]["final_authorization"])

    async def test_policy_resolution_continues_a_pending_tool_outcome_without_replaying_effect(self) -> None:
        """A held outcome is re-authorized, never recreated by redispatch."""
        from m_agent.runtime import (
            AgentDefinition,
            CrashPoint,
            DefinitionRegistry,
            ModelCapabilities,
            ModelRequest,
            ModelResponse,
            PolicyAction,
            PolicyDecision,
            PolicyGate,
            PolicyIdentity,
            RunPolicy,
            RunResolution,
            Runner,
            ToolCall,
            ToolEffect,
            ToolOutcome,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter,
            DeterministicTool,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
        )
        from m_agent.runtime import ModelRequirements, ToolCallingMode

        class ToolThenFinal(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(capabilities=ModelCapabilities(
                    tool_calling=ToolCallingMode.NATIVE
                ))

            async def generate(self, request: ModelRequest) -> ModelResponse:
                self.call_count += 1
                if not request.tool_outcomes:
                    return ModelResponse(tool_calls=(ToolCall(
                        call_id="held-outcome", tool_name="effect", arguments="{}"
                    ),))
                return ModelResponse(content="approved")

        class HoldThenAllowPolicy(RunPolicy):
            def __init__(self, continued_action: PolicyAction) -> None:
                self.outcome_checks = 0
                self.continued_action = continued_action

            @property
            def identity(self) -> PolicyIdentity:
                return PolicyIdentity(
                    policy_id="hold-then-allow", version="1", fingerprint="hold-v1"
                )

            def evaluate(self, request):
                if request.gate is PolicyGate.TOOL_OUTCOME:
                    self.outcome_checks += 1
                    if self.outcome_checks == 1:
                        return PolicyDecision(
                            action=PolicyAction.REQUIRE_RESOLUTION,
                            reason_code="OUTCOME_APPROVAL_REQUIRED",
                        )
                    if self.continued_action is PolicyAction.REJECT:
                        return PolicyDecision(
                            action=PolicyAction.REJECT,
                            reason_code="OUTCOME_CONTINUATION_DENIED",
                        )
                return PolicyDecision(action=PolicyAction.ALLOW, reason_code="ALLOWED")

        for effect in (ToolEffect.READ_ONLY, ToolEffect.NON_IDEMPOTENT):
            for continued_action in (PolicyAction.ALLOW, PolicyAction.REJECT):
                model = ToolThenFinal()
                policy = HoldThenAllowPolicy(continued_action)
                effect_calls = 0

                def invoke(request):
                    nonlocal effect_calls
                    effect_calls += 1
                    return ToolOutcome.success(request.call_id, request.tool_name, "done")

                registry = DefinitionRegistry()
                registry.register(AgentDefinition.for_adapter(
                    definition_id=f"pending-outcome-{effect.value}", version="1",
                    instructions="test", model_adapter=model,
                    model_requirements=ModelRequirements(capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    )),
                    tools=(DeterministicTool(
                        name="effect", effect=effect, handler=invoke
                    ),),
                    run_policy=policy,
                ))
                store = InMemoryRunStore(PlaintextPayloadCodec())
                runner = Runner(registry, store)
                created = await runner.create_run(
                    f"pending-outcome-{effect.value}", "1", "input"
                )

                def fail_if_checkpointed(point, run_id):
                    if point is CrashPoint.AFTER_TOOL_CHECKPOINT:
                        raise AssertionError("held outcome became a checkpoint")

                waiting = await Runner(
                    registry, store, crash_hook=fail_if_checkpointed
                ).start_run(created.run_id)
                self.assertEqual(waiting.status.value, "WAITING")
                self.assertEqual(effect_calls, 1)
                inspection = await runner.inspect_run(created.run_id)
                self.assertEqual(
                    [checkpoint for checkpoint in inspection.checkpoints if checkpoint.step_type.value == "TOOL"],
                    [],
                )

                terminal = await runner.resolve_run(
                    created.run_id,
                    RunResolution(action="CONTINUE_RUN"),
                    expected_version=waiting.version,
                )
                self.assertEqual(
                    terminal.status.value,
                    "SUCCEEDED" if continued_action is PolicyAction.ALLOW else "REJECTED",
                )
                self.assertEqual(effect_calls, 1)
                self.assertEqual(
                    model.call_count,
                    2 if continued_action is PolicyAction.ALLOW else 1,
                )
                self.assertEqual(policy.outcome_checks, 2)

    async def test_policy_fault_fails_closed_without_model_dispatch(self) -> None:
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            PolicyIdentity,
            Runner,
            RunPolicy,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
        )

        class FaultyPolicy(RunPolicy):
            @property
            def identity(self):
                return PolicyIdentity(policy_id="faulty", version="1", fingerprint="faulty-v1")
            def evaluate(self, request):
                raise RuntimeError("policy implementation failure")

        adapter = DeterministicModelAdapter(("never",))
        registry = DefinitionRegistry()
        registry.register(AgentDefinition.for_adapter(
            definition_id="faulty-policy", version="1", instructions="test",
            model_adapter=adapter, run_policy=FaultyPolicy(),
        ))
        runner = Runner(registry, InMemoryRunStore(PlaintextPayloadCodec()))
        created = await runner.create_run("faulty-policy", "1", "hello")
        terminal = await runner.start_run(created.run_id)

        self.assertEqual(terminal.status.value, "FAILED")
        self.assertEqual(terminal.error_code, "POLICY_ERROR")
        self.assertEqual(adapter.call_count, 0)

    async def test_confirmed_uncertain_outcome_cannot_bypass_tool_outcome_policy(self) -> None:
        """Application confirmation still crosses the final outcome gate."""
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            FailureClassification,
            ModelCapabilities,
            ModelRequest,
            ModelResponse,
            PolicyAction,
            PolicyDecision,
            PolicyGate,
            RunResolution,
            Runner,
            StaticRunPolicy,
            StepType,
            ToolCall,
            ToolEffect,
            ToolFailure,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter,
            DeterministicTool,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
        )
        from m_agent.runtime import ModelRequirements, ToolCallingMode

        class RequestToolModel(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    )
                )

            async def generate(self, request: ModelRequest) -> ModelResponse:
                self.call_count += 1
                self._last_request = request
                if not request.tool_outcomes:
                    return ModelResponse(
                        tool_calls=(
                            ToolCall(
                                call_id="uncertain-call",
                                tool_name="notify",
                                arguments="{}",
                            ),
                        )
                    )
                return ModelResponse(content="must not be dispatched")

        tool_calls = 0

        def uncertain_effect(_request):
            nonlocal tool_calls
            tool_calls += 1
            raise ToolFailure(
                FailureClassification.UNCERTAIN,
                "EFFECT_UNCONFIRMED",
                "application must resolve this effect",
            )

        model = RequestToolModel()
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="confirm-outcome-policy",
                version="1",
                instructions="test",
                model_adapter=model,
                model_requirements=ModelRequirements(
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    )
                ),
                tools=(
                    DeterministicTool(
                        name="notify",
                        effect=ToolEffect.NON_IDEMPOTENT,
                        handler=uncertain_effect,
                    ),
                ),
                run_policy=StaticRunPolicy(
                    policy_id="deny-confirmed-outcome",
                    version="1",
                    decisions={
                        PolicyGate.TOOL_OUTCOME: PolicyDecision(
                            action=PolicyAction.REJECT,
                            reason_code="CONFIRMED_OUTCOME_DENIED",
                        )
                    },
                ),
            )
        )
        runner = Runner(registry, InMemoryRunStore(PlaintextPayloadCodec()))
        created = await runner.create_run(
            "confirm-outcome-policy", "1", "input"
        )

        waiting = await runner.start_run(created.run_id)
        self.assertEqual(waiting.status.value, "WAITING")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(tool_calls, 1)

        current = await runner.get_run(created.run_id)
        terminal = await runner.resolve_run(
            created.run_id,
            RunResolution.confirm_step("application-confirmed"),
            expected_version=current.version,
        )
        inspection = await runner.inspect_run(created.run_id)

        self.assertEqual(terminal.status.value, "REJECTED")
        self.assertEqual(terminal.error_code, "CONFIRMED_OUTCOME_DENIED")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(tool_calls, 1)
        self.assertEqual(
            [
                checkpoint
                for checkpoint in inspection.checkpoints
                if checkpoint.step_type is StepType.TOOL
            ],
            [],
        )
        outcome_decisions = [
            decision
            for decision in inspection.policy_decisions
            if decision.gate is PolicyGate.TOOL_OUTCOME
        ]
        self.assertEqual(len(outcome_decisions), 1)
        self.assertEqual(outcome_decisions[0].action, PolicyAction.REJECT)
        self.assertEqual(
            outcome_decisions[0].reason_code, "CONFIRMED_OUTCOME_DENIED"
        )

    async def test_tool_outcome_policy_fault_closes_active_tool_evidence(self) -> None:
        """A policy fault cannot leave an inspected Tool attempt in progress."""
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            ModelCapabilities,
            ModelRequest,
            ModelResponse,
            PolicyAction,
            PolicyDecision,
            PolicyGate,
            PolicyIdentity,
            RunPolicy,
            Runner,
            StepStatus,
            StepType,
            ToolCall,
            ToolEffect,
            ToolOutcome,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter,
            DeterministicTool,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
        )
        from m_agent.runtime import ModelRequirements, ToolCallingMode

        class RequestToolModel(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    )
                )

            async def generate(self, request: ModelRequest) -> ModelResponse:
                self.call_count += 1
                self._last_request = request
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            call_id="faulty-outcome-call",
                            tool_name="notify",
                            arguments="{}",
                        ),
                    )
                )

        class FaultyOutcomePolicy(RunPolicy):
            @property
            def identity(self) -> PolicyIdentity:
                return PolicyIdentity(
                    policy_id="faulty-outcome-policy",
                    version="1",
                    fingerprint="faulty-outcome-policy-v1",
                )

            def evaluate(self, request):
                if request.gate is PolicyGate.TOOL_OUTCOME:
                    raise RuntimeError("outcome policy implementation fault")
                return PolicyDecision(
                    action=PolicyAction.ALLOW, reason_code="ALLOWED"
                )

        tool_calls = 0

        def successful_effect(request):
            nonlocal tool_calls
            tool_calls += 1
            return ToolOutcome.success(
                request.call_id, request.tool_name, "effect complete"
            )

        model = RequestToolModel()
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="faulty-tool-outcome-policy",
                version="1",
                instructions="test",
                model_adapter=model,
                model_requirements=ModelRequirements(
                    capabilities=ModelCapabilities(
                        tool_calling=ToolCallingMode.NATIVE
                    )
                ),
                tools=(
                    DeterministicTool(
                        name="notify",
                        effect=ToolEffect.IDEMPOTENT,
                        handler=successful_effect,
                    ),
                ),
                run_policy=FaultyOutcomePolicy(),
            )
        )
        runner = Runner(registry, InMemoryRunStore(PlaintextPayloadCodec()))
        created = await runner.create_run(
            "faulty-tool-outcome-policy", "1", "input"
        )

        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)

        self.assertEqual(terminal.status.value, "FAILED")
        self.assertEqual(terminal.error_code, "POLICY_ERROR")
        self.assertEqual(model.call_count, 1)
        self.assertEqual(tool_calls, 1)
        tool_steps = [
            step for step in inspection.steps if step.step_type is StepType.TOOL
        ]
        tool_attempts = [
            attempt
            for attempt in inspection.attempts
            if attempt.step_id == tool_steps[0].step_id
        ]
        self.assertEqual(len(tool_steps), 1)
        self.assertEqual(tool_steps[0].status, StepStatus.FAILED)
        self.assertEqual(tool_steps[0].error_code, "POLICY_ERROR")
        self.assertEqual(len(tool_attempts), 1)
        self.assertEqual(tool_attempts[0].status, StepStatus.FAILED)
        self.assertEqual(tool_attempts[0].error_code, "POLICY_ERROR")
        self.assertEqual(
            [
                checkpoint
                for checkpoint in inspection.checkpoints
                if checkpoint.step_type is StepType.TOOL
            ],
            [],
        )
        self.assertEqual(
            [
                decision.reason_code
                for decision in inspection.policy_decisions
                if decision.gate is PolicyGate.TOOL_OUTCOME
            ],
            ["POLICY_ERROR"],
        )

    async def test_invalid_final_output_is_preserved_then_repaired_in_a_new_model_step(self) -> None:
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionRegistry,
            OutputContract,
            OutputFallback,
            OutputRepairPolicy,
            Runner,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
        )

        adapter = DeterministicModelAdapter(("not-json", '{"answer":"fixed"}'))
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="output-repair",
                version="1",
                instructions="test",
                model_adapter=adapter,
                output_contract=OutputContract(
                    contract_id="answer", version="1",
                    schema={"type": "object", "required": ["answer"]},
                    fallback=OutputFallback.REPAIR,
                    repair=OutputRepairPolicy(max_attempts=1),
                ),
            )
        )
        runner = Runner(registry, InMemoryRunStore(PlaintextPayloadCodec()))
        created = await runner.create_run("output-repair", "1", "hello")

        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)

        self.assertEqual(terminal.status.value, "SUCCEEDED")
        self.assertEqual(terminal.output, '{"answer":"fixed"}')
        self.assertEqual(adapter.call_count, 2)
        self.assertEqual([a.model_purpose.value for a in inspection.attempts], ["PRIMARY", "OUTPUT_REPAIR"])
        self.assertIn("not-json", inspection.checkpoints[0].output)
        self.assertIn("OUTPUT_NOT_VALID_JSON", adapter.last_request.input)

    async def test_repair_limit_stays_exhausted_after_a_repair_checkpoint_crash(self) -> None:
        from m_agent.runtime import (
            AgentDefinition,
            CrashPoint,
            DefinitionRegistry,
            OutputContract,
            OutputFallback,
            OutputRepairPolicy,
            Runner,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
        )
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
        )

        adapter = DeterministicModelAdapter(("not-json", "still-not-json", '{"answer":"must-not-run"}'))
        registry = DefinitionRegistry()
        registry.register(AgentDefinition.for_adapter(
            definition_id="repair-limit", version="1", instructions="test",
            model_adapter=adapter,
            output_contract=OutputContract(
                contract_id="answer", version="1",
                schema={"type": "object", "required": ["answer"]},
                fallback=OutputFallback.REPAIR,
                repair=OutputRepairPolicy(max_attempts=1),
            ),
        ))
        store = InMemoryRunStore(PlaintextPayloadCodec())
        created = await Runner(registry, store).create_run("repair-limit", "1", "hello")
        def crash(point, run_id):
            if point is CrashPoint.AFTER_MODEL_CHECKPOINT and adapter.call_count == 2:
                raise RuntimeError("repair checkpoint crash")
        with self.assertRaisesRegex(RuntimeError, "repair checkpoint crash"):
            await Runner(registry, store, crash_hook=crash, owner="worker").start_run(created.run_id)

        terminal = await Runner(registry, store, owner="worker").resume_run(created.run_id)
        self.assertEqual(terminal.status.value, "FAILED")
        self.assertEqual(terminal.error_code, "OUTPUT_VALIDATION_FAILED")
        self.assertEqual(adapter.call_count, 2)

    async def test_sqlite_recovery_repairs_checkpointed_invalid_output_without_replaying_primary(self) -> None:
        from m_agent.runtime import (
            AgentDefinition,
            CrashPoint,
            DefinitionRegistry,
            OutputContract,
            OutputFallback,
            OutputRepairPolicy,
            Runner,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
        )
        from m_agent.runtime import ModelExecutionBudget

        adapter = DeterministicModelAdapter(("not-json", '{"answer":"fixed"}'))
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="sqlite-output-repair", version="1", instructions="test",
                model_adapter=adapter,
                model_execution_budget=ModelExecutionBudget(
                    run_max_attempts=2, primary_max_attempts=1,
                    output_repair_max_attempts=1,
                ),
                output_contract=OutputContract(
                    contract_id="answer", version="1",
                    schema={"type": "object", "required": ["answer"]},
                    fallback=OutputFallback.REPAIR,
                    repair=OutputRepairPolicy(max_attempts=1),
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.sqlite"
            store = SQLiteRunStore(path, PlaintextPayloadCodec())
            created = await Runner(registry, store).create_run(
                "sqlite-output-repair", "1", "hello"
            )
            def crash(point, run_id):
                if point is CrashPoint.AFTER_MODEL_CHECKPOINT:
                    raise RuntimeError("controlled crash")
            with self.assertRaisesRegex(RuntimeError, "controlled crash"):
                await Runner(registry, store, crash_hook=crash, owner="worker").start_run(created.run_id)
            store.close()

            reopened = SQLiteRunStore(path, PlaintextPayloadCodec())
            terminal = await Runner(registry, reopened, owner="worker").resume_run(created.run_id)
            inspection = await Runner(registry, reopened, owner="worker").inspect_run(created.run_id)
            self.assertEqual(terminal.status.value, "SUCCEEDED")
            self.assertEqual(adapter.call_count, 2)
            self.assertEqual(
                [attempt.model_purpose.value for attempt in inspection.attempts],
                ["PRIMARY", "OUTPUT_REPAIR"],
            )
            reopened.close()

    async def test_sqlite_recovery_replays_uncheckpointed_repair_with_frozen_purpose_and_input(self) -> None:
        """Repair recovery keeps its purpose, input, tool ban, and budget."""
        from m_agent.runtime import (
            AgentDefinition,
            CrashPoint,
            DefinitionRegistry,
            ModelRequest,
            OutputContract,
            OutputFallback,
            OutputRepairPolicy,
            RetryPolicy,
            Runner,
            StepStatus,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
        )
        from m_agent.runtime import ModelExecutionBudget, ModelPurpose

        class RecordingAdapter(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(
                    (
                        "not-json",
                        '{"answer":"reservation-recovered"}',
                        '{"answer":"checkpoint-recovered"}',
                    )
                )
                self.requests: list[ModelRequest] = []

            async def generate(self, request: ModelRequest):
                self.requests.append(request)
                return await super().generate(request)

        for crash_point, expected_output, expected_calls in (
            (
                CrashPoint.AFTER_MODEL_ATTEMPT_RESERVATION,
                '{"answer":"reservation-recovered"}',
                2,
            ),
            (
                CrashPoint.BEFORE_MODEL_CHECKPOINT,
                '{"answer":"checkpoint-recovered"}',
                3,
            ),
        ):
            with self.subTest(crash_point=crash_point.value):
                adapter = RecordingAdapter()
                registry = DefinitionRegistry()
                registry.register(
                    AgentDefinition.for_adapter(
                        definition_id=f"repair-recovery-{crash_point.value}",
                        version="1",
                        instructions="test",
                        model_adapter=adapter,
                        model_execution_budget=ModelExecutionBudget(
                            run_max_attempts=3,
                            primary_max_attempts=1,
                            output_repair_max_attempts=2,
                        ),
                        retry_policy=RetryPolicy(max_attempts=2),
                        output_contract=OutputContract(
                            contract_id="answer",
                            version="1",
                            schema={"type": "object", "required": ["answer"]},
                            fallback=OutputFallback.REPAIR,
                            repair=OutputRepairPolicy(max_attempts=1),
                        ),
                    )
                )
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "runs.sqlite"
                    store = SQLiteRunStore(path, PlaintextPayloadCodec())
                    created = await Runner(registry, store).create_run(
                        f"repair-recovery-{crash_point.value}", "1", "hello"
                    )

                    def crash(point, run_id):
                        required_call_count = (
                            1
                            if crash_point
                            == CrashPoint.AFTER_MODEL_ATTEMPT_RESERVATION
                            else 2
                        )
                        if point == crash_point and adapter.call_count == required_call_count:
                            raise SystemExit("controlled repair interruption")

                    with self.assertRaisesRegex(
                        SystemExit, "controlled repair interruption"
                    ):
                        await Runner(
                            registry, store, crash_hook=crash, owner="repair-worker"
                        ).start_run(created.run_id)
                    store.close()

                    reopened = SQLiteRunStore(path, PlaintextPayloadCodec())
                    try:
                        terminal = await Runner(
                            registry, reopened, owner="repair-worker"
                        ).resume_run(created.run_id)
                        inspection = await Runner(
                            registry, reopened, owner="repair-worker"
                        ).inspect_run(created.run_id)
                    finally:
                        reopened.close()

                self.assertEqual(terminal.status.value, "SUCCEEDED")
                self.assertEqual(terminal.output, expected_output)
                self.assertEqual(adapter.call_count, expected_calls)
                self.assertEqual(len(adapter.requests), expected_calls)
                self.assertEqual(adapter.requests[0].input, "hello")
                for request in adapter.requests[1:]:
                    self.assertIn("not-json", request.input)
                    self.assertIn('"contract_id":"answer"', request.input)
                    self.assertEqual(request.tools, ())
                    self.assertEqual(request.tool_outcomes, ())
                self.assertEqual(
                    [attempt.model_purpose for attempt in inspection.attempts],
                    [
                        ModelPurpose.PRIMARY,
                        ModelPurpose.OUTPUT_REPAIR,
                        ModelPurpose.OUTPUT_REPAIR,
                    ],
                )
                self.assertEqual(
                    [attempt.status for attempt in inspection.attempts],
                    [
                        StepStatus.SUCCEEDED,
                        StepStatus.FAILED,
                        StepStatus.SUCCEEDED,
                    ],
                )

    async def test_output_contract_schema_is_deeply_frozen_across_definition_snapshots(self) -> None:
        """External schema mutation cannot rewrite an existing Run contract."""
        from m_agent.runtime import (
            AgentDefinition,
            DefinitionSnapshot,
            OutputContract,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter,
        )
        from m_agent import (
            AgentDefinition,
        )

        source_schema = {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "enum": ["approved"],
                }
            },
            "required": ["answer"],
            "additionalProperties": False,
        }
        contract = OutputContract(
            contract_id="answer",
            version="1",
            schema=source_schema,
        )
        definition = AgentDefinition.for_adapter(
            definition_id="frozen-output-contract",
            version="1",
            instructions="test",
            model_adapter=DeterministicModelAdapter(),
            output_contract=contract,
        )
        snapshot = definition.frozen_snapshot()

        source_schema["properties"]["answer"]["type"] = "integer"
        source_schema["properties"]["answer"]["enum"].append("rewritten")
        source_schema["required"].append("unexpected")

        self.assertEqual(
            snapshot.output_contract.schema_definition["properties"]["answer"]["type"],
            "string",
        )
        self.assertEqual(
            snapshot.output_contract.schema_definition["properties"]["answer"]["enum"],
            ["approved"],
        )
        self.assertEqual(
            snapshot.output_contract.schema_definition["required"], ["answer"]
        )
        with self.assertRaises(TypeError):
            contract.schema_definition["properties"]["answer"]["type"] = "integer"
        with self.assertRaises(TypeError):
            contract.schema_definition["required"].append("unexpected")
        restored = DefinitionSnapshot.model_validate_json(snapshot.model_dump_json())
        self.assertEqual(
            restored.output_contract.schema_definition["properties"]["answer"]["type"],
            "string",
        )
        self.assertEqual(
            restored.output_contract.schema_definition["required"], ["answer"]
        )

        replacement = AgentDefinition.for_adapter(
            definition_id="frozen-output-contract",
            version="2",
            instructions="test",
            model_adapter=DeterministicModelAdapter(),
            output_contract=OutputContract(
                contract_id="answer",
                version="2",
                schema={"type": "object", "required": ["replacement"]},
            ),
        )
        self.assertEqual(
            replacement.frozen_snapshot().output_contract.version, "2"
        )
        self.assertEqual(snapshot.output_contract.version, "1")

        copied_schema = {"type": "object", "required": ["copied"]}
        copied = contract.model_copy(
            update={"schema_definition": copied_schema}
        )
        copied_schema["required"].append("rewritten")
        with self.assertRaises(TypeError):
            copied.schema_definition["required"].append("rewritten")
        self.assertEqual(copied.schema_definition["required"], ["copied"])

    async def test_sqlite_recovery_keeps_repair_final_output_policy_provenance(self) -> None:
        """A recovered repair response crosses the same repair-specific gate."""
        from m_agent.runtime import (
            AgentDefinition,
            CrashPoint,
            DefinitionRegistry,
            OutputContract,
            OutputFallback,
            OutputRepairPolicy,
            PolicyAction,
            PolicyDecision,
            PolicyGate,
            PolicyIdentity,
            RetryPolicy,
            RunPolicy,
            Runner,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter,
            PlaintextPayloadCodec,
            SQLiteRunStore,
        )
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            Runner,
        )
        from m_agent.runtime import ModelExecutionBudget

        class RepairOnlyRejectPolicy(RunPolicy):
            @property
            def identity(self) -> PolicyIdentity:
                return PolicyIdentity(
                    policy_id="repair-only-reject",
                    version="1",
                    fingerprint="repair-only-reject-v1",
                )

            def evaluate(self, request):
                if (
                    request.gate is PolicyGate.FINAL_OUTPUT
                    and request.payload.get("repair") is True
                ):
                    return PolicyDecision(
                        action=PolicyAction.REJECT,
                        reason_code="RECOVERED_REPAIR_DENIED",
                    )
                return PolicyDecision(
                    action=PolicyAction.ALLOW, reason_code="ALLOWED"
                )

        adapter = DeterministicModelAdapter(("not-json", '{"answer":"fixed"}'))
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="recovered-repair-policy",
                version="1",
                instructions="test",
                model_adapter=adapter,
                model_execution_budget=ModelExecutionBudget(
                    run_max_attempts=3,
                    primary_max_attempts=1,
                    output_repair_max_attempts=2,
                ),
                retry_policy=RetryPolicy(max_attempts=2),
                output_contract=OutputContract(
                    contract_id="answer",
                    version="1",
                    schema={"type": "object", "required": ["answer"]},
                    fallback=OutputFallback.REPAIR,
                    repair=OutputRepairPolicy(max_attempts=1),
                ),
                run_policy=RepairOnlyRejectPolicy(),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.sqlite"
            store = SQLiteRunStore(path, PlaintextPayloadCodec())
            created = await Runner(registry, store).create_run(
                "recovered-repair-policy", "1", "hello"
            )

            def crash(point, run_id):
                if (
                    point is CrashPoint.AFTER_MODEL_ATTEMPT_RESERVATION
                    and adapter.call_count == 1
                ):
                    raise SystemExit("interrupt repair reservation")

            with self.assertRaisesRegex(SystemExit, "interrupt repair reservation"):
                await Runner(registry, store, crash_hook=crash, owner="worker").start_run(
                    created.run_id
                )
            store.close()
            reopened = SQLiteRunStore(path, PlaintextPayloadCodec())
            try:
                terminal = await Runner(
                    registry, reopened, owner="worker"
                ).resume_run(created.run_id)
            finally:
                reopened.close()

        self.assertEqual(terminal.status.value, "REJECTED")
        self.assertEqual(terminal.error_code, "RECOVERED_REPAIR_DENIED")
