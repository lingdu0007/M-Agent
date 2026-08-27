"""M-Agent 0.5 确定性模型路由与 Eval 回归示例。

展示 Runtime Integrator 如何组合 0.5 的两个新能力：

1. **确定性模型路由**：公开 :class:`ModelRouter` 依据 typed capability
   与 Contract Limits 过滤候选并给出可检视的 reason code；
   :func:`estimate_run_cost` 依据版本化价格证据声明性地估算成本；
   ``SQLiteRoutingStore`` 以 append-only 方式保存不可变路由决策并与
   新 Run 绑定——重放幂等，绝不重算；
2. **durable Eval 回归**：:class:`EvalExecutionEngine` 在
   :class:`SQLiteEvalStore` 上执行冻结 Suite，崩溃后凭 durable 事实
   续跑（已完成单元零重复执行）；:func:`build_report_revision` 聚合出
   保留 repetition 的 pass-at-k 报告与有依据的统计。

全程确定性、离线（无网络、无凭据），存储使用本仓库临时目录。

运行（仓库根目录）：

    python examples/m_agent_routing_eval.py
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from m_agent.adapters import (
    DeterministicModelAdapter,
    InMemoryRunStore,
    PlaintextPayloadCodec,
)
from m_agent.companion.eval import (
    AgentVariant as EvalAgentVariant,
    EvalCase,
    EvalExecutionEngine,
    EvalSuite,
    EvaluatorRef,
    EvidenceField,
    ExecutionProtocol,
    FixtureBundle,
    ObservationProjectionPolicy,
    OutputMatchesEvaluator,
    SQLiteEvalStore,
    build_report_revision,
)
from m_agent.companion.routing import (
    AgentVariant,
    AvailabilitySnapshot,
    CostFormula,
    DeploymentAttributes,
    DeploymentConstraints,
    HardRoutingGates,
    ModelCatalog,
    ModelCatalogEntry,
    ModelEvidenceSnapshot,
    ModelRouter,
    ObjectiveDimension,
    ObjectiveDirection,
    OperationalLimitsSnapshot,
    PricingSnapshot,
    RetentionEvidenceSnapshot,
    RoutingEvidence,
    RoutingObjective,
    RoutingPolicy,
    RoutingPolicyIdentity,
    RunCostPolicy,
    SoftRoutingPreferences,
    SQLiteRoutingStore,
    UsageObservation,
    bind_decision_to_run,
    estimate_run_cost,
    with_integrity,
)
from m_agent.runtime import (
    AgentDefinition,
    DefinitionRegistry,
    DefinitionSnapshot,
    ModelBinding,
    ModelBindingSet,
    ModelCapabilities,
    ModelCapabilityCombination,
    ModelContract,
    ModelExecutionBudget,
    ModelLimits,
    ModelPurpose,
    ModelRequirements,
    RevisionStability,
    RunRecord,
    RunStatus,
    StructuredOutputMode,
    ToolCallingMode,
    UsageProvenance,
)

AS_OF = datetime(2026, 8, 27, 12, 0, 0)
POLICY_IDENTITY = RoutingPolicyIdentity(policy_id="example-router", version="1")

_TOOLING = ModelCapabilities(
    tool_calling=ToolCallingMode.NATIVE,
    structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
    supported_combinations=(
        ModelCapabilityCombination(
            tool_calling=ToolCallingMode.NATIVE,
            structured_output=StructuredOutputMode.JSON_SCHEMA_STRICT,
        ),
    ),
)


def _contract(contract_id: str, *, capabilities: ModelCapabilities | None = None):
    return ModelContract(
        contract_id=contract_id,
        version="1",
        revision_stability=RevisionStability.PINNED,
        model_identity=f"offline:{contract_id}",
        capabilities=(capabilities or ModelCapabilities()).as_typed(),
        limits=ModelLimits(context_window_tokens=128_000, max_output_tokens=8_000),
        input_sizer_id=f"sizer-{contract_id}-v1",
        serialization_id=f"serial-{contract_id}-v1",
    )


def _variant(variant_id: str, contract: ModelContract):
    primary = ModelBinding(
        purpose=ModelPurpose.PRIMARY,
        contract=contract,
        requirements=ModelRequirements(),
    )
    return AgentVariant(
        variant_id=variant_id,
        version="1",
        definition_id=f"definition-{variant_id}",
        definition_version="1",
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
        model_execution_budget=ModelExecutionBudget(
            run_max_attempts=4,
            primary_max_attempts=2,
            context_compression_max_attempts=1,
            output_repair_max_attempts=1,
        ),
        policy_identities=(POLICY_IDENTITY,),
    )


def _evidence(variants) -> RoutingEvidence:
    def pricing(variant):
        return with_integrity(
            PricingSnapshot(
                variant_id=variant.variant_id,
                variant_version=variant.version,
                currency="USD",
                input_price_per_mtok=Decimal("3.50"),
                output_price_per_mtok=Decimal("12.00"),
                version="price-1",
                source="offline-price-sheet",
                effective_at=AS_OF - timedelta(days=1),
                valid_until=AS_OF + timedelta(days=7),
                contract_fingerprint=variant.primary_contract().fingerprint,
            )
        )

    def availability(variant):
        return with_integrity(
            AvailabilitySnapshot(
                variant_id=variant.variant_id,
                variant_version=variant.version,
                available=True,
                version="avail-1",
                source="offline-probe",
                sampled_at=AS_OF - timedelta(hours=1),
                valid_until=AS_OF + timedelta(hours=6),
                contract_fingerprint=variant.primary_contract().fingerprint,
            )
        )

    def operational(variant):
        return with_integrity(
            OperationalLimitsSnapshot(
                variant_id=variant.variant_id,
                variant_version=variant.version,
                available_rpm=600,
                available_tpm=120_000,
                available_concurrency=8,
                remaining_period_quota=Decimal("0.75"),
                version="ops-1",
                source="offline-quota-probe",
                collected_at=AS_OF - timedelta(minutes=30),
                valid_until=AS_OF + timedelta(hours=2),
                contract_fingerprint=variant.primary_contract().fingerprint,
            )
        )

    def retention(variant):
        return RetentionEvidenceSnapshot(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            verified_retention_evidence_version="retention-2026-01",
            version="ret-ev-1",
            source="offline-compliance-office",
            collected_at=AS_OF - timedelta(days=2),
            valid_until=AS_OF + timedelta(days=30),
        )

    def model_evidence(variant):
        return ModelEvidenceSnapshot(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            quality_score=0.9,
            stability_score=0.97,
            latency_ms_p50=800.0,
            version="ev-1",
            source="offline-eval-report",
            collected_at=AS_OF - timedelta(days=1),
            valid_until=AS_OF + timedelta(days=7),
        )

    return RoutingEvidence(
        model_evidence=tuple(model_evidence(v) for v in variants),
        pricing=tuple(pricing(v) for v in variants),
        availability=tuple(availability(v) for v in variants),
        retention=tuple(retention(v) for v in variants),
        operational_limits=tuple(operational(v) for v in variants),
    )


def _run_record(variant: AgentVariant, run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        definition_id=variant.definition_id,
        definition_version=variant.definition_version,
        input="route me",
        status=RunStatus.CREATED,
        snapshot=DefinitionSnapshot(
            definition_id=variant.definition_id,
            version=variant.definition_version,
            instructions="offline routing example agent",
            model_bindings=variant.model_bindings,
        ),
    )


def routing_demo() -> None:
    capable = _variant("variant-capable", _contract("contract-capable", capabilities=_TOOLING))
    basic = _variant("variant-basic", _contract("contract-basic"))
    catalog = ModelCatalog(
        catalog_id="example-catalog",
        version="1",
        entries=(
            ModelCatalogEntry(
                variant=capable,
                deployment=DeploymentAttributes(
                    provider="offline-provider",
                    region="cn-north",
                    endpoint_class="standard",
                    retention_evidence_version="retention-2026-01",
                ),
            ),
            ModelCatalogEntry(
                variant=basic,
                deployment=DeploymentAttributes(
                    provider="offline-provider",
                    region="cn-north",
                    endpoint_class="standard",
                    retention_evidence_version="retention-2026-01",
                ),
            ),
        ),
    )
    policy = RoutingPolicy(
        identity=POLICY_IDENTITY,
        model_requirements=ModelRequirements(capabilities=_TOOLING),
        deployment=DeploymentConstraints(),
        hard_gates=HardRoutingGates(),
        objectives=(
            RoutingObjective(
                dimension=ObjectiveDimension.QUALITY,
                direction=ObjectiveDirection.MAXIMIZE,
            ),
        ),
        allowed_variants=None,
        soft_preferences=SoftRoutingPreferences(),
    )
    evidence = _evidence((capable, basic))

    result = ModelRouter().select(
        catalog=catalog, policy=policy, evidence=evidence, as_of=AS_OF
    )
    print("== Deterministic model routing ==")
    print(f"outcome: {result.outcome.value}")
    for evaluation in result.candidate_evaluations:
        print(
            f"- {evaluation.variant_id}: passed={evaluation.passed} "
            f"stage={evaluation.stage.value} reason={evaluation.reason_code}"
        )
    decision = result.decision
    assert decision is not None
    print(f"selected variant: {decision.selected_variant.variant_id}")

    estimate = estimate_run_cost(
        policy=RunCostPolicy(
            policy_id="example-cost",
            version="1",
            currency="USD",
            formula=CostFormula.REPORTED_USAGE,
            usage_provenance=UsageProvenance.PROVIDER_REPORTED,
        ),
        variant=decision.selected_variant,
        evidence=evidence,
        as_of=AS_OF,
        usage=UsageObservation(
            input_tokens=1_200,
            output_tokens=300,
            provenance=UsageProvenance.PROVIDER_REPORTED,
        ),
    )
    print(
        f"declared run cost estimate: [{estimate.lower_bound}, "
        f"{estimate.upper_bound}] {estimate.currency} "
        f"(settlement_guaranteed={estimate.settlement_guaranteed}, "
        f"gaps={list(estimate.evidence_gaps)})"
    )

    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "routing-store.sqlite3"
        store = SQLiteRoutingStore(path)
        try:
            store.save_decision(decision, evidence=evidence)
            store.save_decision(decision, evidence=evidence)  # idempotent replay
            binding = bind_decision_to_run(
                decision, _run_record(decision.selected_variant, "example-run-1")
            )
            store.attach_run_binding(decision.decision_id, binding)
        finally:
            store.close()
        reopened = SQLiteRoutingStore(path)
        try:
            stored = reopened.load_decision(decision.decision_id)
        finally:
            reopened.close()
        assert (
            stored is not None
            and stored.decision.decision_id == decision.decision_id
        )
        print(
            "immutable decision survived close/reopen "
            f"(decision_id={decision.decision_id}, bound_run=example-run-1)"
        )


def _case(case_id: str) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        input=f"example-input-{case_id}",
        variant=EvalAgentVariant(
            variant_id="example-assistant",
            definition_id="example-assistant",
            definition_version="1.0",
        ),
        fixture_bundle=FixtureBundle.build(
            bundle_id=f"example-bundle-{case_id}",
            facts=(),
            declared_external_effects=(),
            expected_evidence_ids=(),
        ),
        execution_protocol=ExecutionProtocol(deterministic=True),
        evaluators=(EvaluatorRef(evaluator_id="example-match", version="1.0"),),
    )


def eval_demo() -> None:
    print("== Durable eval regression ==")
    suite = EvalSuite(
        suite_id="example-suite",
        version="1.0",
        cases=(
            _case("case-1"),
            _case("case-2"),
        ),
        variants=(
            EvalAgentVariant(
                variant_id="example-assistant",
                definition_id="example-assistant",
                definition_version="1.0",
            ),
        ),
    )

    def engine(store):
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="example-assistant",
                version="1.0",
                instructions="offline eval example subject",
                model_adapter=DeterministicModelAdapter(responses=("ok",)),
                tools=[],
            )
        )
        return EvalExecutionEngine(
            registry=registry,
            run_store=InMemoryRunStore(PlaintextPayloadCodec()),
            eval_store=store,
            evaluators={
                "example-match": OutputMatchesEvaluator(
                    evaluator_id="example-match",
                    version="1.0",
                    expected="ok",
                    hard=True,
                )
            },
            projection_policy=ObservationProjectionPolicy(
                policy_id="example-projection",
                version="1.0",
                allowed_fields=frozenset({EvidenceField.RUN_OUTPUT}),
            ),
        )

    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary) / "eval-store.sqlite3"
        store = SQLiteEvalStore(database)
        try:
            run = asyncio.run(engine(store).run_suite(suite))
        finally:
            store.close()
        reopened = SQLiteEvalStore(database)
        try:
            replay = asyncio.run(engine(reopened).run_suite(suite))
        finally:
            reopened.close()
        print(f"suite items executed: {len(run.results)}")
        print(
            "outcomes: "
            + ", ".join(result.outcome.value for result in run.results)
        )
        print(
            "idempotent rerun reused durable facts "
            f"(execution_id preserved: "
            f"{replay.execution.execution_id == run.execution.execution_id})"
        )

    report = build_report_revision(
        report_id="example-report",
        revision=1,
        execution_id=run.execution.execution_id,
        suite=suite,
        results=run.results,
    )
    for case_report in report.case_results:
        print(
            f"- {case_report.case_id}/{case_report.variant_id}: "
            f"pass_at_k={case_report.pass_at_k} "
            f"overall={case_report.overall_outcome.value}"
        )
    print("non-claim: offline acceptance evidence only, not a live provider claim")


def main() -> None:
    routing_demo()
    print()
    eval_demo()


if __name__ == "__main__":
    main()
