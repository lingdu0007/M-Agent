"""Public contract tests for Ticket 09 deterministic Run Policy gates."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path


class RunPolicyContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_rejection_prevents_model_dispatch_and_is_inspectable(self) -> None:
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            PolicyAction,
            PolicyDecision,
            PolicyGate,
            Runner,
            StaticRunPolicy,
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

    async def test_policy_fault_fails_closed_without_model_dispatch(self) -> None:
        from m_agent import (
            AgentDefinition, DefinitionRegistry, DeterministicModelAdapter,
            InMemoryRunStore, PlaintextPayloadCodec, PolicyIdentity, Runner,
            RunPolicy,
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

    async def test_invalid_final_output_is_preserved_then_repaired_in_a_new_model_step(self) -> None:
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            DeterministicModelAdapter,
            InMemoryRunStore,
            OutputContract,
            OutputFallback,
            OutputRepairPolicy,
            PlaintextPayloadCodec,
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
        from m_agent import (
            AgentDefinition, CrashPoint, DefinitionRegistry, DeterministicModelAdapter,
            InMemoryRunStore, OutputContract, OutputFallback, OutputRepairPolicy,
            PlaintextPayloadCodec, Runner,
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
        from m_agent import (
            AgentDefinition,
            CrashPoint,
            DefinitionRegistry,
            DeterministicModelAdapter,
            OutputContract,
            OutputFallback,
            OutputRepairPolicy,
            PlaintextPayloadCodec,
            Runner,
            SQLiteRunStore,
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
