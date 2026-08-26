"""Ticket 19: routing integration with real Stores and adapter sentinels.

选择发生在 Session Claim 与 Run 创建之前：所有失败结果用公开 Store
查询（SessionStore.get_claim / RunStore.get_run）与 adapter
sentinel（DeterministicModelAdapter.call_count）证明零 Claim、零 Run、
零 provider dispatch；selected Variant 的 Decision 经
``bind_decision_to_run`` 绑定到新 Run，Run 启动后不因可用性、价格或
Eval 更新自动换模，跨模型继续只能由应用显式创建 Replacement Run。
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from m_agent.adapters import (
    DeterministicModelAdapter,
    InMemoryRunStore,
    PlaintextPayloadCodec,
)
from m_agent.companion import InMemorySessionStore
from m_agent.companion.routing import (
    HardRoutingGates,
    ModelRouter,
    RoutingBindingError,
    RoutingEvidence,
    RoutingOutcome,
    bind_decision_to_run,
)
from m_agent.companion.routing import (
    DeploymentConstraints,
)
from m_agent.runtime import (
    AgentDefinition,
    DefinitionRegistry,
    ModelExecutionBudget,
    ModelPurpose,
    ModelResponse,
    RunStatus,
    Runner,
)

from routing_fixtures import (
    AS_OF,
    make_availability,
    make_catalog,
    make_contract,
    make_entry,
    make_evidence,
    make_model_evidence,
    make_policy,
    make_pricing,
    make_variant,
    tool_and_structured_capabilities,
)
from m_agent.companion import SessionScope

SCOPE = SessionScope(token="routing-scope")


class RoutingIntegrationRig:
    """两个离线 Variant + 真实 Runner/SessionStore 的组合环境。"""

    def __init__(self) -> None:
        self.adapter_economy = DeterministicModelAdapter(
            ("economy answer",),
            model_contract=make_contract("contract-economy"),
        )
        self.adapter_premium = DeterministicModelAdapter(
            ("premium answer",),
            model_contract=make_contract(
                "contract-premium",
                capabilities=tool_and_structured_capabilities(),
            ),
        )
        self.economy = make_variant(
            "variant-economy",
            make_contract("contract-economy"),
            definition_id="definition-economy",
        )
        self.premium = make_variant(
            "variant-premium",
            make_contract(
                "contract-premium",
                capabilities=tool_and_structured_capabilities(),
            ),
            definition_id="definition-premium",
        )
        self.registry = DefinitionRegistry()
        self.registry.register(
            AgentDefinition.for_adapter(
                definition_id="definition-economy",
                version="1",
                instructions="Economy agent.",
                model_adapter=self.adapter_economy,
                model_bindings=self.economy.model_bindings,
                model_execution_budget=ModelExecutionBudget(),
            )
        )
        self.registry.register(
            AgentDefinition.for_adapter(
                definition_id="definition-premium",
                version="1",
                instructions="Premium agent.",
                model_adapter=self.adapter_premium,
                model_bindings=self.premium.model_bindings,
                model_execution_budget=ModelExecutionBudget(),
            )
        )
        self.run_store = InMemoryRunStore(PlaintextPayloadCodec())
        self.runner = Runner(registry=self.registry, store=self.run_store)
        self.session_store = InMemorySessionStore()
        self.catalog = make_catalog(
            make_entry(self.economy, provider="offline-provider"),
            make_entry(self.premium, provider="offline-provider"),
        )
        self.router = ModelRouter()

    def evidence(self):  # noqa: ANN202 - test helper
        return make_evidence(
            (self.economy, self.premium),
            pricing=(
                make_pricing(self.economy, input_price="0.80"),
                make_pricing(self.premium, input_price="6.00"),
            ),
            model_evidence=(
                make_model_evidence(self.economy, quality=0.86),
                make_model_evidence(self.premium, quality=0.96),
            ),
        )

    def preallocated_run_id(self) -> str:
        return "preallocated-run-identity"


class ZeroSideEffectFailureTests(unittest.IsolatedAsyncioTestCase):
    """六种结果中的每条失败路径：零 Claim、零 Run、零 dispatch。"""

    async def asyncSetUp(self) -> None:
        self.rig = RoutingIntegrationRig()
        await self.rig.session_store.create_session(SCOPE, "session-1")

    async def _assert_zero_claim_run_dispatch(self, run_id: str) -> None:
        claim = await self.rig.session_store.get_claim(SCOPE, "session-1")
        self.assertIsNone(claim)
        stored = await self.rig.run_store.get_run(run_id)
        self.assertIsNone(stored)
        self.assertEqual(self.rig.adapter_economy.call_count, 0)
        self.assertEqual(self.rig.adapter_premium.call_count, 0)

    async def test_no_compatible_variant_leaves_no_trace(self) -> None:
        from m_agent.runtime import ModelRequirements

        policy = make_policy(
            requirements=ModelRequirements(min_context_window_tokens=500_000)
        )
        result = self.rig.router.select(
            catalog=self.rig.catalog,
            policy=policy,
            evidence=self.rig.evidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.NO_COMPATIBLE_VARIANT)
        await self._assert_zero_claim_run_dispatch(
            self.rig.preallocated_run_id()
        )

    async def test_policy_unsatisfied_leaves_no_trace(self) -> None:
        policy = make_policy(
            hard_gates=HardRoutingGates(min_quality_score=0.99)
        )
        result = self.rig.router.select(
            catalog=self.rig.catalog,
            policy=policy,
            evidence=self.rig.evidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.POLICY_UNSATISFIED)
        await self._assert_zero_claim_run_dispatch(
            self.rig.preallocated_run_id()
        )

    async def test_evidence_unavailable_leaves_no_trace(self) -> None:
        policy = make_policy(
            hard_gates=HardRoutingGates(require_availability=True)
        )
        evidence = self.rig.evidence()
        result = self.rig.router.select(
            catalog=self.rig.catalog,
            policy=policy,
            evidence=RoutingEvidence(
                pricing=evidence.pricing,
                model_evidence=evidence.model_evidence,
                retention=evidence.retention,
                availability=(),
            ),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        await self._assert_zero_claim_run_dispatch(
            self.rig.preallocated_run_id()
        )

    async def test_catalog_conflict_leaves_no_trace(self) -> None:
        conflicting = make_variant(
            "variant-economy",
            make_contract("contract-economy-twin"),
            definition_id="definition-economy",
        )
        catalog = make_catalog(
            make_entry(self.rig.economy),
            make_entry(conflicting),
        )
        result = self.rig.router.select(
            catalog=catalog,
            policy=make_policy(),
            evidence=self.rig.evidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.CATALOG_CONFLICT)
        await self._assert_zero_claim_run_dispatch(
            self.rig.preallocated_run_id()
        )

    async def test_invalid_policy_leaves_no_trace(self) -> None:
        policy = make_policy(allowed_variants=())
        result = self.rig.router.select(
            catalog=self.rig.catalog,
            policy=policy,
            evidence=self.rig.evidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.INVALID_POLICY)
        await self._assert_zero_claim_run_dispatch(
            self.rig.preallocated_run_id()
        )

    async def test_stale_deployment_evidence_leaves_no_trace(self) -> None:
        policy = make_policy(
            deployment=DeploymentConstraints(
                allowed_retention_evidence_versions=frozenset({"retention-2026-01"})
            )
        )
        evidence = self.rig.evidence()
        stale_retention = RoutingEvidence(
            pricing=evidence.pricing,
            model_evidence=evidence.model_evidence,
            availability=evidence.availability,
            retention=(
                evidence.retention[0].model_copy(
                    update={"valid_until": AS_OF - timedelta(days=1)}
                ),
                evidence.retention[1],
            ),
        )
        result = self.rig.router.select(
            catalog=self.rig.catalog,
            policy=policy,
            evidence=stale_retention,
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE)
        await self._assert_zero_claim_run_dispatch(
            self.rig.preallocated_run_id()
        )


class SelectedBeforeClaimAndRunTests(unittest.IsolatedAsyncioTestCase):
    """SELECTED 发生在 Claim 与 Run 创建之前，且路由本身零 dispatch。"""

    async def asyncSetUp(self) -> None:
        self.rig = RoutingIntegrationRig()
        await self.rig.session_store.create_session(SCOPE, "session-1")

    async def test_routing_selects_without_any_dispatch_or_claim(self) -> None:
        policy = make_policy(
            hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("5.00"))
        )
        result = self.rig.router.select(
            catalog=self.rig.catalog,
            policy=policy,
            evidence=self.rig.evidence(),
            as_of=AS_OF,
        )
        self.assertIs(result.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(
            result.decision.selected_variant.variant_id, "variant-economy"
        )
        claim = await self.rig.session_store.get_claim(SCOPE, "session-1")
        self.assertIsNone(claim)
        self.assertEqual(self.rig.adapter_economy.call_count, 0)
        self.assertEqual(self.rig.adapter_premium.call_count, 0)

    async def test_full_flow_claim_create_start_uses_selected_variant(self) -> None:
        policy = make_policy(
            hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("5.00"))
        )
        result = self.rig.router.select(
            catalog=self.rig.catalog,
            policy=policy,
            evidence=self.rig.evidence(),
            as_of=AS_OF,
        )
        decision = result.decision
        self.assertEqual(decision.selected_variant.variant_id, "variant-economy")

        run_id = "claimed-run-1"
        snapshot = await self.rig.session_store.read_snapshot(SCOPE, "session-1")
        claim = await self.rig.session_store.claim_run(
            SCOPE, "session-1", run_id, expected_version=snapshot.version
        )
        self.assertEqual(claim.run_id, run_id)
        run = await self.rig.runner.create_run(
            decision.selected_variant.definition_id,
            decision.selected_variant.definition_version,
            "route me",
            run_id=run_id,
        )
        self.assertEqual(run.status, RunStatus.CREATED)

        binding = bind_decision_to_run(decision, run)
        self.assertEqual(binding.decision_id, decision.decision_id)
        self.assertEqual(binding.run_id, run_id)
        self.assertEqual(binding.variant_id, "variant-economy")

        finished = await self.rig.runner.start_run(run_id)
        self.assertIs(finished.status, RunStatus.SUCCEEDED)
        self.assertEqual(finished.output, "economy answer")
        self.assertEqual(self.rig.adapter_economy.call_count, 1)
        self.assertEqual(self.rig.adapter_premium.call_count, 0)


class DecisionRunBindingTests(unittest.IsolatedAsyncioTestCase):
    """Decision snapshot 绑定与不可变、不可换模语义。"""

    async def asyncSetUp(self) -> None:
        self.rig = RoutingIntegrationRig()
        await self.rig.session_store.create_session(SCOPE, "session-1")

    def _select(self):  # noqa: ANN202 - test helper
        policy = make_policy(
            hard_gates=HardRoutingGates(max_input_price_per_mtok=Decimal("5.00"))
        )
        return self.rig.router.select(
            catalog=self.rig.catalog,
            policy=policy,
            evidence=self.rig.evidence(),
            as_of=AS_OF,
        )

    async def test_binding_rejects_definition_identity_mismatch(self) -> None:
        decision = self._select().decision
        other_run = await self.rig.runner.create_run(
            "definition-premium",
            "1",
            "different definition",
            run_id="premium-run-1",
        )
        with self.assertRaises(RoutingBindingError):
            bind_decision_to_run(decision, other_run)

    async def test_binding_rejects_contract_fingerprint_mismatch(self) -> None:
        decision = self._select().decision
        self.assertEqual(
            decision.selected_variant.variant_id, "variant-economy"
        )
        # 同 definition identity 但换绑了另一个 contract 的 definition。
        swapped = AgentDefinition.for_adapter(
            definition_id="definition-economy",
            version="1",
            instructions="Economy agent.",
            model_adapter=self.rig.adapter_premium,
            model_bindings=self.rig.premium.model_bindings,
            model_execution_budget=ModelExecutionBudget(),
        )
        swapped_registry = DefinitionRegistry()
        swapped_registry.register(swapped)
        swapped_store = InMemoryRunStore(PlaintextPayloadCodec())
        swapped_runner = Runner(registry=swapped_registry, store=swapped_store)
        run = await swapped_runner.create_run(
            "definition-economy", "1", "swapped contract"
        )
        with self.assertRaises(RoutingBindingError):
            bind_decision_to_run(decision, run)

    async def test_binding_rejects_run_without_frozen_snapshot(self) -> None:
        """无冻结 Definition Snapshot 的 Run 无法验证指纹：fail closed。"""
        decision = self._select().decision
        run = await self.rig.runner.create_run(
            decision.selected_variant.definition_id,
            decision.selected_variant.definition_version,
            "no snapshot",
        )
        run.snapshot = None
        with self.assertRaises(RoutingBindingError):
            bind_decision_to_run(decision, run)

    async def test_binding_rejects_legacy_snapshot_without_bindings(self) -> None:
        """未冻结 Model Binding Set 的 legacy 快照同样 fail closed。"""
        decision = self._select().decision
        run = await self.rig.runner.create_run(
            decision.selected_variant.definition_id,
            decision.selected_variant.definition_version,
            "legacy snapshot",
        )
        assert run.snapshot is not None
        run.snapshot = run.snapshot.model_copy(update={"model_bindings": None})
        with self.assertRaises(RoutingBindingError):
            bind_decision_to_run(decision, run)

    async def test_started_run_does_not_switch_model_after_snapshot_updates(
        self,
    ) -> None:
        """Run 创建后可用性/价格/Eval 更新只影响之后的路由。"""
        result = self._select()
        decision = result.decision
        self.assertEqual(decision.selected_variant.variant_id, "variant-economy")

        run = await self.rig.runner.create_run(
            decision.selected_variant.definition_id,
            decision.selected_variant.definition_version,
            "stable model please",
            run_id="frozen-run-1",
        )
        binding = bind_decision_to_run(decision, run)
        self.assertEqual(binding.variant_id, "variant-economy")
        frozen_snapshot_before = run.snapshot

        # availability / pricing / eval evidence 全部反转：economy 变得
        # 不可用、更贵、质量更低。
        flipped = RoutingEvidence(
            retention=self.rig.evidence().retention,
            pricing=(
                make_pricing(self.rig.economy, input_price="50.00"),
                make_pricing(self.rig.premium, input_price="1.00"),
            ),
            model_evidence=(
                make_model_evidence(self.rig.economy, quality=0.10),
                make_model_evidence(self.rig.premium, quality=0.99),
            ),
            availability=(
                make_availability(self.rig.economy, available=False),
                make_availability(self.rig.premium, available=True),
            ),
        )
        later_policy = make_policy(
            hard_gates=HardRoutingGates(
                max_input_price_per_mtok=Decimal("5.00"),
                min_quality_score=0.5,
                require_availability=True,
            )
        )
        later = self.rig.router.select(
            catalog=self.rig.catalog,
            policy=later_policy,
            evidence=flipped,
            as_of=AS_OF + timedelta(hours=1),
        )
        self.assertIs(later.outcome, RoutingOutcome.SELECTED)
        self.assertEqual(
            later.decision.selected_variant.variant_id, "variant-premium"
        )

        # 已创建的 Run 不换模：仍以 economy 的冻结 Definition Snapshot 启动。
        finished = await self.rig.runner.start_run("frozen-run-1")
        self.assertIs(finished.status, RunStatus.SUCCEEDED)
        self.assertEqual(finished.output, "economy answer")
        self.assertEqual(finished.snapshot, frozen_snapshot_before)
        self.assertEqual(self.rig.adapter_economy.call_count, 1)
        self.assertEqual(self.rig.adapter_premium.call_count, 0)
        inspection = await self.rig.runner.inspect_run("frozen-run-1")
        self.assertEqual(
            inspection.run.snapshot.model_bindings.for_purpose(
                ModelPurpose.PRIMARY
            ).contract.fingerprint,
            decision.selected_contract_fingerprint,
        )

    async def test_retry_after_transient_failure_stays_on_same_contract(
        self,
    ) -> None:
        """dispatch 后的失败按冻结 Retry Policy 重试同一 Model Contract。"""
        from m_agent.runtime import ModelFailure, RetryPolicy
        from m_agent.runtime import FailureClassification

        class TransientOnce(DeterministicModelAdapter):
            def __init__(self) -> None:
                super().__init__(
                    ("recovered answer",),
                    model_contract=make_contract("contract-economy"),
                )

            async def generate(self, request):  # noqa: ANN001
                self.call_count += 1
                self._last_request = request
                if self.call_count == 1:
                    raise ModelFailure(
                        FailureClassification.TRANSIENT,
                        "rate_limited",
                        "transient failure",
                    )
                return ModelResponse(content="recovered answer")

        flaky = TransientOnce()
        flaky_registry = DefinitionRegistry()
        flaky_registry.register(
            AgentDefinition.for_adapter(
                definition_id="definition-economy",
                version="1",
                instructions="Economy agent.",
                model_adapter=flaky,
                model_bindings=self.rig.economy.model_bindings,
                model_execution_budget=ModelExecutionBudget(),
                retry_policy=RetryPolicy(max_attempts=2),
            )
        )
        store = InMemoryRunStore(PlaintextPayloadCodec())
        runner = Runner(registry=flaky_registry, store=store)

        decision = self._select().decision
        run = await runner.create_run(
            decision.selected_variant.definition_id,
            decision.selected_variant.definition_version,
            "retry same contract",
            run_id="retry-run-1",
        )
        binding = bind_decision_to_run(decision, run)
        self.assertEqual(binding.variant_id, "variant-economy")
        finished = await runner.start_run("retry-run-1")
        self.assertIs(finished.status, RunStatus.SUCCEEDED)
        self.assertEqual(flaky.call_count, 2)
        self.assertEqual(self.rig.adapter_premium.call_count, 0)

    async def test_no_public_api_reroutes_a_started_run(self) -> None:
        """Router 无任何接受 run identity 的公开入口。"""
        import inspect

        router = ModelRouter()
        for name, member in inspect.getmembers(router, callable):
            if name.startswith("_"):
                continue
            parameters = inspect.signature(member).parameters
            self.assertNotIn(
                "run_id",
                parameters,
                f"ModelRouter.{name} 不应接受 run identity",
            )


class RoutingOutcomeCoverageTests(unittest.IsolatedAsyncioTestCase):
    """验收目标 4：六种 Routing Result 均可从公共 seam 观察且携带稳定 reason code。"""

    async def asyncSetUp(self) -> None:
        self.rig = RoutingIntegrationRig()
        await self.rig.session_store.create_session(SCOPE, "session-1")

    async def test_all_six_outcomes_are_reachable_with_stable_reason_codes(
        self,
    ) -> None:
        from m_agent.runtime import ModelRequirements

        router = self.rig.router
        catalog = self.rig.catalog
        evidence = self.rig.evidence()

        outcomes: dict[RoutingOutcome, str] = {}

        # 1. SELECTED
        selected = router.select(
            catalog=catalog,
            policy=make_policy(
                hard_gates=HardRoutingGates(
                    max_input_price_per_mtok=Decimal("5.00")
                )
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        outcomes[selected.outcome] = selected.reason_code

        # 2. NO_COMPATIBLE_VARIANT（能力/容量过滤后无候选）
        no_compatible = router.select(
            catalog=catalog,
            policy=make_policy(
                requirements=ModelRequirements(
                    min_context_window_tokens=500_000
                )
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(
            no_compatible.outcome, RoutingOutcome.NO_COMPATIBLE_VARIANT
        )
        outcomes[no_compatible.outcome] = no_compatible.reason_code

        # 3. POLICY_UNSATISFIED（兼容候选全部不满足硬门槛）
        policy_unsatisfied = router.select(
            catalog=catalog,
            policy=make_policy(
                hard_gates=HardRoutingGates(min_quality_score=0.99)
            ),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(
            policy_unsatisfied.outcome, RoutingOutcome.POLICY_UNSATISFIED
        )
        outcomes[policy_unsatisfied.outcome] = policy_unsatisfied.reason_code

        # 4. EVIDENCE_UNAVAILABLE（硬策略证据缺失）
        evidence_unavailable = router.select(
            catalog=catalog,
            policy=make_policy(
                hard_gates=HardRoutingGates(require_availability=True)
            ),
            evidence=RoutingEvidence(),
            as_of=AS_OF,
        )
        self.assertIs(
            evidence_unavailable.outcome, RoutingOutcome.EVIDENCE_UNAVAILABLE
        )
        outcomes[evidence_unavailable.outcome] = (
            evidence_unavailable.reason_code
        )

        # 5. CATALOG_CONFLICT（不可变身份冲突）
        conflicting = make_variant(
            "variant-economy",
            make_contract("contract-economy-twin"),
            definition_id="definition-economy",
        )
        catalog_conflict = router.select(
            catalog=make_catalog(
                make_entry(self.rig.economy), make_entry(conflicting)
            ),
            policy=make_policy(),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(
            catalog_conflict.outcome, RoutingOutcome.CATALOG_CONFLICT
        )
        outcomes[catalog_conflict.outcome] = catalog_conflict.reason_code

        # 6. INVALID_POLICY（空候选范围的结构性错误）
        invalid_policy = router.select(
            catalog=catalog,
            policy=make_policy(allowed_variants=()),
            evidence=evidence,
            as_of=AS_OF,
        )
        self.assertIs(invalid_policy.outcome, RoutingOutcome.INVALID_POLICY)
        outcomes[invalid_policy.outcome] = invalid_policy.reason_code

        self.assertEqual(
            set(outcomes),
            {
                RoutingOutcome.SELECTED,
                RoutingOutcome.NO_COMPATIBLE_VARIANT,
                RoutingOutcome.POLICY_UNSATISFIED,
                RoutingOutcome.EVIDENCE_UNAVAILABLE,
                RoutingOutcome.CATALOG_CONFLICT,
                RoutingOutcome.INVALID_POLICY,
            },
        )
        # 每种结果都携带稳定 reason code。
        for outcome, reason_code in outcomes.items():
            with self.subTest(outcome=outcome):
                self.assertRegex(reason_code, r"^[A-Z][A-Z0-9_]*$")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
