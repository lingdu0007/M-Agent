"""Offline eval-regression scenario (Ticket 18, 0.5 Eval milestone).

八个 required CONTRACT 检查的完整离线证明：

- durable recovery：engine 完成首个 item 后丢弃全部内存状态（模拟
  崩溃），仅凭 SQLiteEvalStore 中的 durable 事实续跑——已完成单元
  绝不重复执行（零多余 model dispatch），execution 身份幂等复用；
- judge isolation：Judge 强制专用 RunStore（共享即构造失败），Judge
  结果与 deterministic 结果同样 append-only 且重跑复用；
- baseline comparison：五态分类（UNCHANGED / CHANGED / NEW /
  MISSING / INCONCLUSIVE）逐态验证，证据不足绝不冒充「无回归」；
- regression detection：hard gate 恒判定（policy 不可关闭）、quality
  gate 按策略、pass^k 恶化——全部经由真实 engine 执行路径证明；
- report metrics：每次 repetition 原样保留、pass^k 要求全部通过、
  统计披露 sample count / min / max / median，样本不足时绝不报告
  无依据 P95（充足时才按 nearest-rank 报告）；
- observe + projection：OBSERVE 对显式选择的既有 Run 严格只读
  （零新增 dispatch），Projection 按「需求 ∧ 策略」最小授权，未
  授权字段（denied）绝不泄漏；
- recommendation readonly：Recommendation 只冻结引用（Report
  revision digest、Baseline、目标 Variant 身份），落盘后全部既有
  durable 事实保持原样，篡改确定性冲突；
- mutation detection：篡改报告（digest 不匹配）、Baseline 身份错配
  （fail-closed ValueError）、pass^k 劣化变异（比较判 REGRESSION）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Mapping

from ..adapters import (
    DeterministicModelAdapter,
    InMemoryRunStore,
    PlaintextPayloadCodec,
)
from ..companion.eval import (
    RECOMMENDATION_TARGET_AGENT_VARIANT,
    REASON_EVIDENCE_MISSING,
    REASON_SUBJECT_OUTPUT_MATCHED,
    AgentVariant,
    CaseVariantReport,
    ComparisonOverall,
    ComparisonPolicy,
    ComparisonVerdict,
    EvalBaselineRecord,
    EvalCase,
    EvalError,
    EvalExecutionEngine,
    EvalExecutionRecord,
    EvalFailureKind,
    EvalMode,
    EvalObserver,
    EvalRecordConflictError,
    EvalSuite,
    EvaluatorOutcome,
    EvaluatorRef,
    EvaluatorResult,
    EvidenceCompleteness,
    EvidenceField,
    EvidenceRequirements,
    ExecutionProtocol,
    FixtureBundle,
    JudgeBinding,
    JudgeRunExecutor,
    ModelRecommendationRecord,
    ObservationProjection,
    ObservationProjectionPolicy,
    ObservationSelection,
    OutputMatchesEvaluator,
    RecommendationTarget,
    RepetitionOutcome,
    ReportRevisionRecord,
    SQLiteEvalStore,
    build_report_revision,
    compare_report_revisions,
    project_observation,
)
from ..runtime import AgentDefinition, DefinitionRegistry, Runner
from ._pack import AcceptanceCheckResult, AcceptanceCheckStatus, EvidenceLevel

__all__ = ["reconcile_eval_regression", "run_eval_regression"]

_OK_OUTPUT = "ok"
_BROKEN_OUTPUT = "regressed output"
_POLICY = ObservationProjectionPolicy(
    policy_id="eval-regression-policy",
    version="1.0",
    allowed_fields=frozenset({EvidenceField.RUN_OUTPUT}),
)


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle(bundle_id: str) -> FixtureBundle:
    return FixtureBundle.build(
        bundle_id=bundle_id,
        facts=(),
        declared_external_effects=(),
        expected_evidence_ids=(),
    )


def _variant(definition_id: str, version: str) -> AgentVariant:
    return AgentVariant(
        variant_id="primary",
        definition_id=definition_id,
        definition_version=version,
    )


def _register(
    registry: DefinitionRegistry,
    adapter: DeterministicModelAdapter,
    *,
    definition_id: str,
    version: str,
) -> None:
    registry.register(
        AgentDefinition.for_adapter(
            definition_id=definition_id,
            version=version,
            instructions="eval-regression subject",
            model_adapter=adapter,
            tools=[],
        )
    )


def _case(
    case_id: str,
    *,
    input: str,  # noqa: A002 - 与 EvalCase 字段同名
    variant: AgentVariant,
    evaluator_id: str,
    bundle_id: str,
    repetitions: int = 1,
) -> EvalCase:
    protocol = (
        ExecutionProtocol(deterministic=True)
        if repetitions == 1
        else ExecutionProtocol(
            deterministic=False, repetitions=repetitions, seed=f"seed-{case_id}"
        )
    )
    return EvalCase(
        case_id=case_id,
        input=input,
        variant=variant,
        fixture_bundle=_bundle(bundle_id),
        execution_protocol=protocol,
        evaluators=(EvaluatorRef(evaluator_id=evaluator_id, version="1.0"),),
    )


def _evaluator(evaluator_id: str, *, hard: bool) -> OutputMatchesEvaluator:
    return OutputMatchesEvaluator(
        evaluator_id=evaluator_id,
        version="1.0",
        expected=_OK_OUTPUT,
        hard=hard,
    )


class _LengthScoreEvaluator:
    """确定性打分 Evaluator：score = len(RUN_OUTPUT)（统计披露探针用）。

    实现 DeterministicEvaluator 契约（identity / hard /
    evidence_requirements / evaluate）：非 hard（质量测量）、只声明
    RUN_OUTPUT 需求、缺失证据归一为 INCONCLUSIVE 而非伪造分数。
    """

    def __init__(self, *, evaluator_id: str) -> None:
        self._identity = EvaluatorRef(evaluator_id=evaluator_id, version="1.0")

    @property
    def identity(self) -> EvaluatorRef:
        return self._identity

    @property
    def hard(self) -> bool:
        return False

    @property
    def evidence_requirements(self) -> EvidenceRequirements:
        return EvidenceRequirements(
            evaluator=self._identity,
            fields=frozenset({EvidenceField.RUN_OUTPUT}),
        )

    def evaluate(self, projection: ObservationProjection) -> EvaluatorResult:
        output = projection.get(EvidenceField.RUN_OUTPUT)
        if output is None:
            return EvaluatorResult(
                evaluator=self._identity,
                outcome=EvaluatorOutcome.INCONCLUSIVE,
                failure_kind=EvalFailureKind.EVIDENCE,
                reason_code=REASON_EVIDENCE_MISSING,
                detail="run output not delivered by the projection",
                hard=False,
                evidence_refs=(projection.observation_id,),
            )
        return EvaluatorResult(
            evaluator=self._identity,
            outcome=EvaluatorOutcome.PASS,
            failure_kind=EvalFailureKind.NONE,
            reason_code=REASON_SUBJECT_OUTPUT_MATCHED,
            detail="",
            score=float(len(str(output))),
            hard=False,
            evidence_refs=(projection.observation_id,),
        )


# ---------------------------------------------------------------------------
# Probe 1：durable recovery（崩溃后续跑，已完成单元零重复执行）
# ---------------------------------------------------------------------------


async def _durable_recovery_probe(directory: Path) -> dict[str, object]:
    database = directory / "eval-recovery.sqlite3"
    suite = EvalSuite(
        suite_id="eval-recovery-suite",
        version="1.0",
        cases=tuple(
            _case(
                f"case-{index}",
                input=f"recovery-input-{index}",
                variant=_variant("recovery-assistant", "1.0"),
                evaluator_id="hard-output-match",
                bundle_id=f"recovery-bundle-{index}",
            )
            for index in range(3)
        ),
        variants=(_variant("recovery-assistant", "1.0"),),
    )
    evaluators = {"hard-output-match": _evaluator("hard-output-match", hard=True)}
    execution_id = EvalExecutionEngine.execution_identity(suite)
    items = suite.expand()

    # 「崩溃」前：engine 1 只完成第一个 item，随后关闭（内存状态全部丢弃）。
    adapter_before = DeterministicModelAdapter(responses=(_OK_OUTPUT,))
    registry_before = DefinitionRegistry()
    _register(
        registry_before,
        adapter_before,
        definition_id="recovery-assistant",
        version="1.0",
    )
    store_before = SQLiteEvalStore(database)
    try:
        # 真实 run_suite 在执行任何 item 之前先落盘 execution 记录；
        # 「崩溃」模拟必须同样包含这一 durable 事实（resume 的公共
        # seam 对不存在的 execution fail-closed）。
        await store_before.record_execution(
            EvalExecutionRecord(
                execution_id=execution_id,
                suite_id=suite.suite_id,
                suite_version=suite.version,
                suite_digest=suite.content_digest(),
                mode=EvalMode.EXECUTE.value,
                item_ids=tuple(item.item_id for item in items),
            )
        )
        engine_before = EvalExecutionEngine(
            registry=registry_before,
            run_store=InMemoryRunStore(PlaintextPayloadCodec()),
            eval_store=store_before,
            evaluators=evaluators,
            projection_policy=_POLICY,
        )
        first_observation = await engine_before._ensure_observation(
            suite=suite, item=items[0], execution_id=execution_id
        )
        first_result = await engine_before._ensure_result(
            item=items[0],
            execution_id=execution_id,
            observation=first_observation,
            ref=suite.cases[0].evaluators[0],
        )
    finally:
        store_before.close()

    # 恢复：engine 2 只有 SQLite 文件这一共享事实，全新 adapter/registry/
    # RunStore——续跑必须只执行未完成的两个 item。
    adapter_after = DeterministicModelAdapter(responses=(_OK_OUTPUT,))
    registry_after = DefinitionRegistry()
    _register(
        registry_after,
        adapter_after,
        definition_id="recovery-assistant",
        version="1.0",
    )
    store_after = SQLiteEvalStore(database)
    try:
        engine_after = EvalExecutionEngine(
            registry=registry_after,
            run_store=InMemoryRunStore(PlaintextPayloadCodec()),
            eval_store=store_after,
            evaluators=evaluators,
            projection_policy=_POLICY,
        )
        # 恢复走冻结声明的公共 seam：resume_execution 显式指定 execution 身份。
        resumed = await engine_after.resume_execution(suite, execution_id)
        resume_dispatches = adapter_after.call_count
        rerun = await engine_after.run_suite(suite)
        rerun_dispatches = adapter_after.call_count
    finally:
        store_after.close()

    problems: list[str] = []
    if resumed.execution.execution_id != execution_id:
        problems.append("execution_identity_not_preserved")
    if len(resumed.observations) != 3 or len(resumed.results) != 3:
        problems.append("resume_incomplete")
    if resume_dispatches != 2:
        problems.append("completed_unit_rerun_after_recovery")
    if rerun_dispatches != resume_dispatches:
        problems.append("idempotent_rerun_dispatched_again")
    if resumed.observations[0].observation_id != first_observation.observation_id:
        problems.append("persisted_observation_not_reused")
    if resumed.results[0].result_id != first_result.result_id:
        problems.append("persisted_result_not_reused")
    if any(result.outcome is not EvaluatorOutcome.PASS for result in resumed.results):
        problems.append("recovery_results_not_pass")
    if rerun.execution.execution_id != execution_id:
        problems.append("rerun_created_new_execution")

    return {
        "suite_items": len(items),
        "completed_before_crash": 1,
        "resume_dispatches": resume_dispatches,
        "rerun_dispatches": rerun_dispatches,
        "execution_id_preserved": resumed.execution.execution_id == execution_id,
        "first_observation_reused": resumed.observations[0].observation_id
        == first_observation.observation_id,
        "first_result_reused": resumed.results[0].result_id == first_result.result_id,
        "problems": problems,
        "sqlite_digest": _file_digest(database),
    }


# ---------------------------------------------------------------------------
# Probe 2：judge isolation（专用 RunStore + append-only 复用 + 构造 fail-closed）
# ---------------------------------------------------------------------------


async def _judge_isolation_probe(directory: Path) -> dict[str, object]:
    database = directory / "eval-judge.sqlite3"
    subject_adapter = DeterministicModelAdapter(responses=(_OK_OUTPUT,))
    judge_verdict = json.dumps({"verdict": "PASS", "score": 0.9, "rationale": "ok"})
    judge_adapter = DeterministicModelAdapter(responses=(judge_verdict,))
    registry = DefinitionRegistry()
    _register(
        registry,
        subject_adapter,
        definition_id="subject-assistant",
        version="1.0",
    )
    _register(
        registry,
        judge_adapter,
        definition_id="judge-agent",
        version="1.0",
    )
    subject_store = InMemoryRunStore(PlaintextPayloadCodec())
    judge_store = InMemoryRunStore(PlaintextPayloadCodec())
    judge = JudgeRunExecutor(registry=registry, run_store=judge_store)
    judge_ref = EvaluatorRef(evaluator_id="judge-eval", version="1.0")
    binding = JudgeBinding(
        judge=judge_ref,
        definition_id="judge-agent",
        definition_version="1.0",
        requirements=EvidenceRequirements(
            evaluator=judge_ref,
            fields=frozenset({EvidenceField.RUN_OUTPUT}),
        ),
    )
    suite = EvalSuite(
        suite_id="eval-judge-suite",
        version="1.0",
        cases=(
            _case(
                "case-judged",
                input="judge-input",
                variant=_variant("subject-assistant", "1.0"),
                evaluator_id="judge-eval",
                bundle_id="judge-bundle",
            ),
        ),
        variants=(_variant("subject-assistant", "1.0"),),
    )
    store = SQLiteEvalStore(database)
    try:
        engine = EvalExecutionEngine(
            registry=registry,
            run_store=subject_store,
            eval_store=store,
            evaluators={},
            projection_policy=_POLICY,
            judge=judge,
            judge_bindings={"judge-eval": binding},
        )
        first = await engine.run_suite(suite)
        subject_dispatches = subject_adapter.call_count
        judge_dispatches = judge_adapter.call_count
        second = await engine.run_suite(suite)
    finally:
        store.close()

    # 构造 fail-closed：Judge 共享 subject RunStore 必须被拒绝。
    shared_judge = JudgeRunExecutor(registry=registry, run_store=subject_store)
    shared_store = SQLiteEvalStore(directory / "eval-judge-shared.sqlite3")
    construction_rejected = False
    try:
        EvalExecutionEngine(
            registry=registry,
            run_store=subject_store,
            eval_store=shared_store,
            evaluators={},
            projection_policy=_POLICY,
            judge=shared_judge,
            judge_bindings={"judge-eval": binding},
        )
    except EvalError:
        construction_rejected = True
    finally:
        shared_store.close()

    problems: list[str] = []
    if not construction_rejected:
        problems.append("shared_store_construction_accepted")
    if subject_dispatches != 1 or judge_dispatches != 1:
        problems.append("unexpected_first_round_dispatches")
    if len(first.results) != 1 or first.results[0].outcome is not EvaluatorOutcome.PASS:
        problems.append("judge_run_not_pass")
    if not first.results[0].judge_run_id:
        problems.append("judge_run_not_recorded")
    if second.results[0].result_id != first.results[0].result_id:
        problems.append("judge_result_not_reused")
    if subject_adapter.call_count != subject_dispatches:
        problems.append("subject_rerun_after_judge_reuse")
    if judge_adapter.call_count != judge_dispatches:
        problems.append("judge_rerun_not_append_only")

    return {
        "construction_fail_closed": construction_rejected,
        "judge_result_pass": first.results[0].outcome is EvaluatorOutcome.PASS,
        "judge_run_recorded": bool(first.results[0].judge_run_id),
        "result_reused": second.results[0].result_id == first.results[0].result_id,
        "subject_dispatches": subject_dispatches,
        "judge_dispatches": judge_dispatches,
        "problems": problems,
        "sqlite_digest": _file_digest(database),
    }


# ---------------------------------------------------------------------------
# Probe 3：baseline comparison 五态分类（纯契约路径，逐态验证）
# ---------------------------------------------------------------------------


def _passing_case(case_id: str) -> CaseVariantReport:
    return CaseVariantReport(
        case_id=case_id,
        variant_id="primary",
        repetitions=(
            RepetitionOutcome(
                repetition_index=0,
                outcome=EvaluatorOutcome.PASS,
                hard_outcome=EvaluatorOutcome.PASS,
                quality_outcome=EvaluatorOutcome.PASS,
            ),
        ),
        hard_outcome=EvaluatorOutcome.PASS,
        quality_outcome=EvaluatorOutcome.PASS,
        overall_outcome=EvaluatorOutcome.PASS,
        pass_at_k=True,
    )


def _inconclusive_case(case_id: str) -> CaseVariantReport:
    return CaseVariantReport(
        case_id=case_id,
        variant_id="primary",
        repetitions=(
            RepetitionOutcome(
                repetition_index=0,
                outcome=EvaluatorOutcome.INCONCLUSIVE,
                hard_outcome=EvaluatorOutcome.INCONCLUSIVE,
                quality_outcome=EvaluatorOutcome.INCONCLUSIVE,
            ),
        ),
        hard_outcome=EvaluatorOutcome.INCONCLUSIVE,
        quality_outcome=EvaluatorOutcome.INCONCLUSIVE,
        overall_outcome=EvaluatorOutcome.INCONCLUSIVE,
        pass_at_k=False,
    )


def _failing_case(case_id: str) -> CaseVariantReport:
    return CaseVariantReport(
        case_id=case_id,
        variant_id="primary",
        repetitions=(
            RepetitionOutcome(
                repetition_index=0,
                outcome=EvaluatorOutcome.FAIL,
                hard_outcome=EvaluatorOutcome.FAIL,
                quality_outcome=EvaluatorOutcome.PASS,
            ),
        ),
        hard_outcome=EvaluatorOutcome.FAIL,
        quality_outcome=EvaluatorOutcome.PASS,
        overall_outcome=EvaluatorOutcome.FAIL,
        pass_at_k=False,
    )


async def _baseline_comparison_probe(directory: Path) -> dict[str, object]:
    database = directory / "eval-five-state.sqlite3"
    baseline_cases = (
        _passing_case("case-unchanged"),
        _failing_case("case-changed"),
        _passing_case("case-missing"),
        _inconclusive_case("case-inconclusive"),
    )
    current_cases = (
        _passing_case("case-unchanged"),
        _passing_case("case-changed"),
        _passing_case("case-new"),
        _passing_case("case-inconclusive"),
    )

    def _report(
        *, revision: int, cases: tuple[CaseVariantReport, ...], suite_id: str
    ) -> ReportRevisionRecord:
        return ReportRevisionRecord.build(
            report_id="five-state-report",
            revision=revision,
            execution_id=None,
            suite_id=suite_id,
            suite_version="1.0",
            suite_digest="sha256:" + "0" * 64,
            case_results=cases,
            hard_outcome=_worst_of(cases, "hard_outcome"),
            quality_outcome=_worst_of(cases, "quality_outcome"),
            overall_outcome=_worst_of(cases, "overall_outcome"),
        )

    baseline_report = _report(revision=1, cases=baseline_cases, suite_id="five-state-suite")
    current_report = _report(revision=2, cases=current_cases, suite_id="five-state-suite")
    baseline = EvalBaselineRecord(
        baseline_id="baseline-five-state-1",
        report_id="five-state-report",
        report_revision=1,
        suite_id="five-state-suite",
        suite_version="1.0",
        comparison_policy=ComparisonPolicy(
            policy_id="default-policy", version="1.0"
        ),
    )
    comparison = compare_report_revisions(
        current=current_report, baseline=baseline, baseline_report=baseline_report
    )
    verdicts = {
        (item.case_id, item.variant_id): item.verdict
        for item in comparison.case_comparisons
    }

    # suite 不匹配：整体 INCONCLUSIVE，绝不做跨 suite 比较。
    other_suite_report = _report(
        revision=3, cases=current_cases, suite_id="other-suite"
    )
    suite_mismatch = compare_report_revisions(
        current=other_suite_report, baseline=baseline, baseline_report=baseline_report
    )

    store = SQLiteEvalStore(database)
    try:
        await store.record_report(baseline_report)
        await store.record_report(current_report)
        await store.record_baseline(baseline)
        # 读回与幂等重放：durable 事实与内存记录精确一致，同内容重放无冲突。
        read_back = await store.get_report("five-state-report", 1)
        replayed = await store.record_report(baseline_report)
        if read_back != baseline_report or replayed != baseline_report:
            problems_prepend = ["report_roundtrip_mismatch"]
        else:
            problems_prepend = []
    finally:
        store.close()

    problems: list[str] = []
    problems.extend(problems_prepend)
    if verdicts.get(("case-unchanged", "primary")) is not ComparisonVerdict.UNCHANGED:
        problems.append("unchanged_misclassified")
    if verdicts.get(("case-changed", "primary")) is not ComparisonVerdict.CHANGED:
        problems.append("changed_misclassified")
    if verdicts.get(("case-new", "primary")) is not ComparisonVerdict.NEW:
        problems.append("new_misclassified")
    if verdicts.get(("case-missing", "primary")) is not ComparisonVerdict.MISSING:
        problems.append("missing_misclassified")
    if verdicts.get(("case-inconclusive", "primary")) is not ComparisonVerdict.INCONCLUSIVE:
        problems.append("inconclusive_misclassified")
    if comparison.overall is not ComparisonOverall.INCONCLUSIVE:
        problems.append("overall_not_inconclusive")
    if (
        suite_mismatch.overall is not ComparisonOverall.INCONCLUSIVE
        or suite_mismatch.case_comparisons
    ):
        problems.append("suite_mismatch_compared")

    return {
        "states_verified": 5 - len(
            [problem for problem in problems if problem.endswith("misclassified")]
        ),
        "overall_inconclusive": comparison.overall is ComparisonOverall.INCONCLUSIVE,
        "suite_mismatch_inconclusive": (
            suite_mismatch.overall is ComparisonOverall.INCONCLUSIVE
            and not suite_mismatch.case_comparisons
        ),
        "problems": problems,
        "sqlite_digest": _file_digest(database),
    }


def _worst_of(
    cases: tuple[CaseVariantReport, ...], field: str
) -> EvaluatorOutcome:
    outcomes = [getattr(case, field) for case in cases]
    for candidate in (
        EvaluatorOutcome.FAIL,
        EvaluatorOutcome.ERROR,
        EvaluatorOutcome.INCONCLUSIVE,
        EvaluatorOutcome.UNSUPPORTED,
    ):
        if candidate in outcomes:
            return candidate
    return EvaluatorOutcome.PASS


# ---------------------------------------------------------------------------
# Probe 4：regression detection（真实 engine 执行路径）
# ---------------------------------------------------------------------------


async def _regression_detection_probe(directory: Path) -> dict[str, object]:
    database = directory / "eval-regression.sqlite3"
    store = SQLiteEvalStore(database)
    try:
        observation = await _run_regression_detection(store)
    finally:
        store.close()
    return {**observation, "sqlite_digest": _file_digest(database)}


async def _run_regression_detection(store: SQLiteEvalStore) -> dict[str, object]:
    # -- hard + quality 回归：同一 suite 两个 Variant 版本 ----------------
    suite_v1 = EvalSuite(
        suite_id="eval-regression-suite",
        version="1.0",
        cases=(
            _case(
                "case-hard",
                input="hard-input",
                variant=_variant("assistant", "1.0"),
                evaluator_id="hard-output-match",
                bundle_id="hard-bundle",
            ),
            _case(
                "case-soft",
                input="soft-input",
                variant=_variant("assistant", "1.0"),
                evaluator_id="soft-output-match",
                bundle_id="soft-bundle",
            ),
        ),
        variants=(_variant("assistant", "1.0"),),
    )
    suite_v2 = EvalSuite(
        suite_id="eval-regression-suite",
        version="2.0",
        cases=(
            _case(
                "case-hard",
                input="hard-input",
                variant=_variant("assistant", "2.0"),
                evaluator_id="hard-output-match",
                bundle_id="hard-bundle",
            ),
            _case(
                "case-soft",
                input="soft-input",
                variant=_variant("assistant", "2.0"),
                evaluator_id="soft-output-match",
                bundle_id="soft-bundle",
            ),
        ),
        variants=(_variant("assistant", "2.0"),),
    )
    evaluators = {
        "hard-output-match": _evaluator("hard-output-match", hard=True),
        "soft-output-match": _evaluator("soft-output-match", hard=False),
    }
    registry = DefinitionRegistry()
    _register(
        registry,
        DeterministicModelAdapter(responses=(_OK_OUTPUT,)),
        definition_id="assistant",
        version="1.0",
    )
    _register(
        registry,
        DeterministicModelAdapter(responses=(_BROKEN_OUTPUT,)),
        definition_id="assistant",
        version="2.0",
    )

    def _engine() -> EvalExecutionEngine:
        return EvalExecutionEngine(
            registry=registry,
            run_store=InMemoryRunStore(PlaintextPayloadCodec()),
            eval_store=store,
            evaluators=evaluators,
            projection_policy=_POLICY,
        )

    baseline_run = await _engine().run_suite(suite_v1)
    current_run = await _engine().run_suite(suite_v2)
    baseline_report = build_report_revision(
        report_id="eval-regression-report",
        revision=1,
        execution_id=baseline_run.execution.execution_id,
        suite=suite_v1,
        results=baseline_run.results,
    )
    current_report = build_report_revision(
        report_id="eval-regression-report",
        revision=2,
        execution_id=current_run.execution.execution_id,
        suite=suite_v2,
        results=current_run.results,
    )
    # 干净对照：同一 execution 的结果重新聚合，必须是 UNCHANGED/COMPARABLE。
    rerun_report = build_report_revision(
        report_id="eval-regression-report",
        revision=3,
        execution_id=baseline_run.execution.execution_id,
        suite=suite_v1,
        results=baseline_run.results,
    )
    default_policy = ComparisonPolicy(policy_id="default-policy", version="1.0")
    strict_policy = ComparisonPolicy(
        policy_id="strict-policy",
        version="1.0",
        quality_regression=False,
        pass_at_k_regression=False,
    )
    baseline_record = EvalBaselineRecord(
        baseline_id="baseline-regression-1",
        report_id="eval-regression-report",
        report_revision=1,
        suite_id="eval-regression-suite",
        suite_version="1.0",
        comparison_policy=default_policy,
    )
    strict_baseline_record = EvalBaselineRecord(
        baseline_id="baseline-regression-strict",
        report_id="eval-regression-report",
        report_revision=1,
        suite_id="eval-regression-suite",
        suite_version="1.0",
        comparison_policy=strict_policy,
    )
    regression_comparison = compare_report_revisions(
        current=current_report,
        baseline=baseline_record,
        baseline_report=baseline_report,
    )
    strict_comparison = compare_report_revisions(
        current=current_report,
        baseline=strict_baseline_record,
        baseline_report=baseline_report,
    )
    clean_comparison = compare_report_revisions(
        current=rerun_report,
        baseline=baseline_record,
        baseline_report=baseline_report,
    )
    regression_verdicts = {
        (item.case_id, item.variant_id): item.verdict
        for item in regression_comparison.case_comparisons
    }
    clean_verdicts = {
        (item.case_id, item.variant_id): item.verdict
        for item in clean_comparison.case_comparisons
    }

    # -- pass^k 回归：soft evaluator 一次 repetition 劣化 ------------------
    passk_suite_v1 = EvalSuite(
        suite_id="eval-passk-suite",
        version="1.0",
        cases=(
            _case(
                "case-flaky",
                input="flaky-input",
                variant=_variant("flaky-assistant", "1.0"),
                evaluator_id="soft-output-match",
                bundle_id="flaky-bundle",
                repetitions=2,
            ),
        ),
        variants=(_variant("flaky-assistant", "1.0"),),
    )
    passk_suite_v2 = EvalSuite(
        suite_id="eval-passk-suite",
        version="2.0",
        cases=(
            _case(
                "case-flaky",
                input="flaky-input",
                variant=_variant("flaky-assistant", "2.0"),
                evaluator_id="soft-output-match",
                bundle_id="flaky-bundle",
                repetitions=2,
            ),
        ),
        variants=(_variant("flaky-assistant", "2.0"),),
    )
    passk_registry = DefinitionRegistry()
    _register(
        passk_registry,
        DeterministicModelAdapter(responses=(_OK_OUTPUT,)),
        definition_id="flaky-assistant",
        version="1.0",
    )
    # responses 按 call 顺序消费：rep0 -> "ok"（PASS）、rep1 -> 劣化输出（FAIL）。
    _register(
        passk_registry,
        DeterministicModelAdapter(responses=(_OK_OUTPUT, _BROKEN_OUTPUT)),
        definition_id="flaky-assistant",
        version="2.0",
    )

    def _passk_engine() -> EvalExecutionEngine:
        return EvalExecutionEngine(
            registry=passk_registry,
            run_store=InMemoryRunStore(PlaintextPayloadCodec()),
            eval_store=store,
            evaluators=evaluators,
            projection_policy=_POLICY,
        )

    passk_baseline_run = await _passk_engine().run_suite(passk_suite_v1)
    passk_current_run = await _passk_engine().run_suite(passk_suite_v2)
    passk_baseline_report = build_report_revision(
        report_id="eval-passk-report",
        revision=1,
        execution_id=passk_baseline_run.execution.execution_id,
        suite=passk_suite_v1,
        results=passk_baseline_run.results,
    )
    passk_current_report = build_report_revision(
        report_id="eval-passk-report",
        revision=2,
        execution_id=passk_current_run.execution.execution_id,
        suite=passk_suite_v2,
        results=passk_current_run.results,
    )
    passk_policy = ComparisonPolicy(
        policy_id="passk-policy",
        version="1.0",
        quality_regression=False,
        pass_at_k_regression=True,
    )
    passk_disabled_policy = ComparisonPolicy(
        policy_id="passk-disabled-policy",
        version="1.0",
        quality_regression=False,
        pass_at_k_regression=False,
    )
    passk_baseline_record = EvalBaselineRecord(
        baseline_id="baseline-passk-1",
        report_id="eval-passk-report",
        report_revision=1,
        suite_id="eval-passk-suite",
        suite_version="1.0",
        comparison_policy=passk_policy,
    )
    passk_disabled_record = EvalBaselineRecord(
        baseline_id="baseline-passk-disabled",
        report_id="eval-passk-report",
        report_revision=1,
        suite_id="eval-passk-suite",
        suite_version="1.0",
        comparison_policy=passk_disabled_policy,
    )
    passk_comparison = compare_report_revisions(
        current=passk_current_report,
        baseline=passk_baseline_record,
        baseline_report=passk_baseline_report,
    )
    passk_disabled_comparison = compare_report_revisions(
        current=passk_current_report,
        baseline=passk_disabled_record,
        baseline_report=passk_baseline_report,
    )

    await store.record_report(baseline_report)
    await store.record_report(current_report)
    await store.record_report(passk_baseline_report)
    await store.record_report(passk_current_report)
    await store.record_baseline(baseline_record)
    await store.record_baseline(strict_baseline_record)
    await store.record_baseline(passk_baseline_record)
    await store.record_baseline(passk_disabled_record)
    # 读回与幂等重放：durable 事实与内存记录精确一致，同内容重放无冲突。
    read_back = await store.get_report("eval-regression-report", 1)
    replayed = await store.record_report(baseline_report)
    baseline_intact = read_back == baseline_report and replayed == baseline_report

    passk_baseline_case = passk_baseline_report.case_results[0]
    passk_current_case = passk_current_report.case_results[0]
    # 五条语义路径（与冻结验收断言一一对应，每条都是原子布尔
    # 的合取：全部子断言成立才计为该路径已验证）。
    semantic_paths = {
        # 1. hard gate 恒判定：默认策略与「关闭 quality/pass^k」的
        #    strict 策略都必须判 REGRESSION（policy 不可关闭 hard gate）。
        "hard_gate_fail_closed": (
            regression_comparison.overall is ComparisonOverall.REGRESSION
            and regression_verdicts.get(("case-hard", "primary"))
            is ComparisonVerdict.REGRESSION
            and strict_comparison.overall is ComparisonOverall.REGRESSION
        ),
        # 2. quality gate 按策略：默认策略（quality_regression=True）
        #    下 soft evaluator 劣化判 REGRESSION。
        "quality_gate_by_policy": (
            regression_verdicts.get(("case-soft", "primary"))
            is ComparisonVerdict.REGRESSION
        ),
        # 3. pass^k 恶化：状态翻转且比较判 REGRESSION。
        "pass_at_k_degradation": (
            bool(passk_baseline_case.pass_at_k)
            and not passk_current_case.pass_at_k
            and passk_comparison.overall is ComparisonOverall.REGRESSION
        ),
        # 4. 关闭策略被尊重：同一劣化在 pass^k 关闭的策略下不判回归。
        "disabled_policy_respected": (
            passk_disabled_comparison.overall is ComparisonOverall.COMPARABLE
        ),
        # 5. 干净重跑不误判：同 execution 结果重聚合全部 UNCHANGED。
        "clean_rerun_not_misjudged": (
            clean_comparison.overall is ComparisonOverall.COMPARABLE
            and all(
                verdict is ComparisonVerdict.UNCHANGED
                for verdict in clean_verdicts.values()
            )
        ),
    }

    problems: list[str] = []
    if not baseline_intact:
        problems.append("report_roundtrip_mismatch")
    if regression_comparison.overall is not ComparisonOverall.REGRESSION:
        problems.append("regression_missed")
    if regression_verdicts.get(("case-hard", "primary")) is not ComparisonVerdict.REGRESSION:
        problems.append("hard_gate_regression_missed")
    if regression_verdicts.get(("case-soft", "primary")) is not ComparisonVerdict.REGRESSION:
        problems.append("quality_gate_regression_missed")
    if strict_comparison.overall is not ComparisonOverall.REGRESSION:
        problems.append("hard_gate_not_fail_closed")
    if clean_comparison.overall is not ComparisonOverall.COMPARABLE:
        problems.append("clean_rerun_misclassified")
    if any(verdict is not ComparisonVerdict.UNCHANGED for verdict in clean_verdicts.values()):
        problems.append("clean_rerun_case_not_unchanged")
    if not passk_baseline_case.pass_at_k or passk_current_case.pass_at_k:
        problems.append("pass_at_k_states_unexpected")
    if passk_comparison.overall is not ComparisonOverall.REGRESSION:
        problems.append("pass_at_k_regression_missed")
    if passk_disabled_comparison.overall is not ComparisonOverall.COMPARABLE:
        problems.append("disabled_policy_not_respected")

    return {
        "regression_paths": sum(semantic_paths.values()),
        "hard_regression_overall": regression_comparison.overall.value,
        "hard_regression_strict_overall": strict_comparison.overall.value,
        "clean_rerun_overall": clean_comparison.overall.value,
        "pass_at_k_regression_overall": passk_comparison.overall.value,
        "pass_at_k_disabled_overall": passk_disabled_comparison.overall.value,
        "pass_at_k_baseline": passk_baseline_case.pass_at_k,
        "pass_at_k_current": passk_current_case.pass_at_k,
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# Probe 5：mutation detection（受控变异必须全部检出）
# ---------------------------------------------------------------------------


def _stable_two_rep_case(case_id: str) -> CaseVariantReport:
    repetitions = tuple(
        RepetitionOutcome(
            repetition_index=index,
            outcome=EvaluatorOutcome.PASS,
            hard_outcome=EvaluatorOutcome.PASS,
            quality_outcome=EvaluatorOutcome.PASS,
        )
        for index in range(2)
    )
    return CaseVariantReport(
        case_id=case_id,
        variant_id="primary",
        repetitions=repetitions,
        hard_outcome=EvaluatorOutcome.PASS,
        quality_outcome=EvaluatorOutcome.PASS,
        overall_outcome=EvaluatorOutcome.PASS,
        pass_at_k=True,
    )


def _degraded_two_rep_case(case_id: str) -> CaseVariantReport:
    """第二个 repetition 的 soft gate 劣化：hard 仍 PASS、pass^k 恶化。"""
    return CaseVariantReport(
        case_id=case_id,
        variant_id="primary",
        repetitions=(
            RepetitionOutcome(
                repetition_index=0,
                outcome=EvaluatorOutcome.PASS,
                hard_outcome=EvaluatorOutcome.PASS,
                quality_outcome=EvaluatorOutcome.PASS,
            ),
            RepetitionOutcome(
                repetition_index=1,
                outcome=EvaluatorOutcome.FAIL,
                hard_outcome=EvaluatorOutcome.PASS,
                quality_outcome=EvaluatorOutcome.FAIL,
            ),
        ),
        hard_outcome=EvaluatorOutcome.PASS,
        quality_outcome=EvaluatorOutcome.FAIL,
        overall_outcome=EvaluatorOutcome.FAIL,
        pass_at_k=False,
    )


async def _mutation_probe(directory: Path) -> dict[str, object]:
    database = directory / "eval-mutation.sqlite3"
    baseline_report = ReportRevisionRecord.build(
        report_id="mutation-report",
        revision=1,
        execution_id=None,
        suite_id="mutation-suite",
        suite_version="1.0",
        suite_digest="sha256:" + "0" * 64,
        case_results=(_stable_two_rep_case("case-stable"),),
        hard_outcome=EvaluatorOutcome.PASS,
        quality_outcome=EvaluatorOutcome.PASS,
        overall_outcome=EvaluatorOutcome.PASS,
    )
    current_report = ReportRevisionRecord.build(
        report_id="mutation-report",
        revision=2,
        execution_id=None,
        suite_id="mutation-suite",
        suite_version="1.0",
        suite_digest="sha256:" + "0" * 64,
        case_results=(_stable_two_rep_case("case-stable"),),
        hard_outcome=EvaluatorOutcome.PASS,
        quality_outcome=EvaluatorOutcome.PASS,
        overall_outcome=EvaluatorOutcome.PASS,
    )
    baseline = EvalBaselineRecord(
        baseline_id="baseline-mutation-1",
        report_id="mutation-report",
        report_revision=1,
        suite_id="mutation-suite",
        suite_version="1.0",
        comparison_policy=ComparisonPolicy(
            policy_id="passk-policy",
            version="1.0",
            quality_regression=False,
            pass_at_k_regression=True,
        ),
    )

    # 干净对照：内容一致 -> UNCHANGED -> COMPARABLE。
    control = compare_report_revisions(
        current=current_report, baseline=baseline, baseline_report=baseline_report
    )

    # 变异 1：篡改报告内容（digest 不再匹配内容）。检测走真实 durable
    # 路径：与已落盘 (report_id, revision) 同身份、异内容的记录重放进
    # append-only store 必须确定性冲突；且读回的 durable 事实保持原样，
    # 未被篡改记录覆盖。
    tampered = current_report.model_copy(
        update={"case_results": (_degraded_two_rep_case("case-stable"),)}
    )
    tamper_rejected = False
    store = SQLiteEvalStore(database)
    try:
        await store.record_report(baseline_report)
        await store.record_report(current_report)
        await store.record_baseline(baseline)
        try:
            await store.record_report(tampered)
        except EvalRecordConflictError:
            tamper_rejected = True
        read_back = await store.get_report("mutation-report", 2)
    finally:
        store.close()
    tamper_detected = (
        tamper_rejected
        and read_back == current_report
        and tampered.content_digest != tampered.content_digest_payload()
    )

    # 变异 2：Baseline 身份错配（引用 revision 1 却传入 revision 99 的
    # 报告）-> fail-closed ValueError，绝不比较错误 revision。
    wrong_revision_report = ReportRevisionRecord.build(
        report_id="mutation-report",
        revision=99,
        execution_id=None,
        suite_id="mutation-suite",
        suite_version="1.0",
        suite_digest="sha256:" + "0" * 64,
        case_results=(_stable_two_rep_case("case-stable"),),
        hard_outcome=EvaluatorOutcome.PASS,
        quality_outcome=EvaluatorOutcome.PASS,
        overall_outcome=EvaluatorOutcome.PASS,
    )
    mismatch_detected = False
    try:
        compare_report_revisions(
            current=current_report,
            baseline=baseline,
            baseline_report=wrong_revision_report,
        )
    except ValueError as error:
        # 只把「冻结 Baseline 引用不匹配」这一确定性 fail-closed 错误
        # 计为检出，避免无关的 ValueError 造成假阳性。
        mismatch_detected = "does not match the frozen baseline" in str(error)

    # 变异 3：pass^k 劣化变异（digest 重算、记录本身「合法」）——
    # 比较必须判 REGRESSION，绝不静默接受劣化。
    degraded_report = ReportRevisionRecord.build(
        report_id="mutation-report",
        revision=3,
        execution_id=None,
        suite_id="mutation-suite",
        suite_version="1.0",
        suite_digest="sha256:" + "0" * 64,
        case_results=(_degraded_two_rep_case("case-stable"),),
        hard_outcome=EvaluatorOutcome.PASS,
        quality_outcome=EvaluatorOutcome.FAIL,
        overall_outcome=EvaluatorOutcome.FAIL,
    )
    degradation = compare_report_revisions(
        current=degraded_report, baseline=baseline, baseline_report=baseline_report
    )
    degradation_detected = (
        degradation.overall is ComparisonOverall.REGRESSION
        and degradation.case_comparisons[0].verdict
        is ComparisonVerdict.REGRESSION
    )

    observation = {
        "clean_control_comparable": control.overall is ComparisonOverall.COMPARABLE,
        "tampered_report_detected": tamper_detected,
        "baseline_identity_mismatch_detected": mismatch_detected,
        "pass_at_k_degradation_detected": degradation_detected,
    }
    problems = reconcile_eval_regression(observation)
    return {
        **observation,
        "mutation_probes": sum(
            1
            for key in (
                "tampered_report_detected",
                "baseline_identity_mismatch_detected",
                "pass_at_k_degradation_detected",
            )
            if observation[key]
        ),
        "mutations_detected": not problems,
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# Probe 6：report metrics（repetition 保留、pass^k 与有依据的统计披露）
# ---------------------------------------------------------------------------


async def _report_metrics_probe(directory: Path) -> dict[str, object]:
    """metrics 披露经真实 engine + 聚合路径证明。

    20 个不同长度的输出 -> 20 个不同 score（样本充足，P95 有依据、
    nearest-rank）；3 个样本的对照组样本不足，p95=None 且
    p95_justified=False，绝不报告无依据 P95。
    """
    database = directory / "eval-metrics.sqlite3"
    sufficient_responses = tuple("x" * (index + 1) for index in range(20))
    registry = DefinitionRegistry()
    _register(
        registry,
        DeterministicModelAdapter(responses=sufficient_responses),
        definition_id="metrics-assistant",
        version="1.0",
    )
    _register(
        registry,
        DeterministicModelAdapter(responses=("x", "xx", "xxx")),
        definition_id="small-assistant",
        version="1.0",
    )
    sufficient_suite = EvalSuite(
        suite_id="eval-metrics-suite",
        version="1.0",
        cases=(
            _case(
                "case-metrics",
                input="metrics-input",
                variant=_variant("metrics-assistant", "1.0"),
                evaluator_id="length-score",
                bundle_id="metrics-bundle",
                repetitions=20,
            ),
        ),
        variants=(_variant("metrics-assistant", "1.0"),),
    )
    insufficient_suite = EvalSuite(
        suite_id="eval-metrics-small-suite",
        version="1.0",
        cases=(
            _case(
                "case-metrics-small",
                input="small-input",
                variant=_variant("small-assistant", "1.0"),
                evaluator_id="length-score",
                bundle_id="small-bundle",
                repetitions=3,
            ),
        ),
        variants=(_variant("small-assistant", "1.0"),),
    )
    store = SQLiteEvalStore(database)
    try:
        evaluators = {"length-score": _LengthScoreEvaluator(
            evaluator_id="length-score"
        )}

        def _engine() -> EvalExecutionEngine:
            return EvalExecutionEngine(
                registry=registry,
                run_store=InMemoryRunStore(PlaintextPayloadCodec()),
                eval_store=store,
                evaluators=evaluators,
                projection_policy=_POLICY,
            )

        sufficient_run = await _engine().run_suite(sufficient_suite)
        insufficient_run = await _engine().run_suite(insufficient_suite)
        sufficient_report = build_report_revision(
            report_id="eval-metrics-report",
            revision=1,
            execution_id=sufficient_run.execution.execution_id,
            suite=sufficient_suite,
            results=sufficient_run.results,
        )
        insufficient_report = build_report_revision(
            report_id="eval-metrics-report",
            revision=2,
            execution_id=insufficient_run.execution.execution_id,
            suite=insufficient_suite,
            results=insufficient_run.results,
        )
        await store.record_report(sufficient_report)
        await store.record_report(insufficient_report)
        read_back = await store.get_report("eval-metrics-report", 1)
    finally:
        store.close()

    sufficient_case = sufficient_report.case_results[0]
    insufficient_case = insufficient_report.case_results[0]
    summary = sufficient_case.score_summary
    insufficient_summary = insufficient_case.score_summary
    problems: list[str] = []
    if len(sufficient_case.repetitions) != 20:
        problems.append("repetitions_not_retained")
    if not sufficient_case.pass_at_k:
        problems.append("pass_at_k_requires_all_repetitions_pass")
    if summary is None or summary.sample_count != 20:
        problems.append("sample_count_mismatch")
    if (
        summary is None
        or summary.minimum != 1.0
        or summary.maximum != 20.0
        or summary.median != 10.5
    ):
        problems.append("basic_statistics_mismatch")
    # nearest-rank P95：ceil(0.95 * 20) = 19 -> 第 19 个次序统计量 = 19.0。
    if summary is None or not summary.p95_justified or summary.p95 != 19.0:
        problems.append("justified_p95_missing")
    if (
        insufficient_summary is None
        or insufficient_summary.sample_count != 3
        or insufficient_summary.p95 is not None
        or insufficient_summary.p95_justified
    ):
        problems.append("insufficient_samples_reported_p95")
    if read_back != sufficient_report:
        problems.append("report_roundtrip_mismatch")

    return {
        "repetitions_retained": len(sufficient_case.repetitions) == 20,
        "pass_at_k": sufficient_case.pass_at_k,
        "sample_count": (
            sufficient_case.score_summary.sample_count
            if sufficient_case.score_summary
            else 0
        ),
        "p95_justified": bool(
            sufficient_case.score_summary
            and sufficient_case.score_summary.p95_justified
        ),
        "insufficient_p95_suppressed": bool(
            insufficient_case.score_summary
            and insufficient_case.score_summary.p95 is None
            and not insufficient_case.score_summary.p95_justified
        ),
        "problems": problems,
        "sqlite_digest": _file_digest(database),
    }


# ---------------------------------------------------------------------------
# Probe 7：OBSERVE 严格只读 + Projection 最小授权（零泄漏）
# ---------------------------------------------------------------------------


async def _observe_projection_probe(directory: Path) -> dict[str, object]:
    """OBSERVE 只读观察与 Projection 证据完整性经真实路径证明。

    - OBSERVE 对显式 selection 中的既有 subject Run 只读归一化
      （零新增 model dispatch）；不存在的 Run 归一为 UNAVAILABLE，
      绝不伪造结论；
    - Projection 按「需求 ∧ 策略」交付：获授权字段精确交付，未授权
      字段进入 denied 且值绝不泄漏；
    - OBSERVE observation（execution_id=None）同样 append-only 落盘
      且幂等重放复用。
    """
    database = directory / "eval-observe.sqlite3"
    adapter = DeterministicModelAdapter(responses=(_OK_OUTPUT,))
    registry = DefinitionRegistry()
    _register(
        registry,
        adapter,
        definition_id="observe-assistant",
        version="1.0",
    )
    subject_store = InMemoryRunStore(PlaintextPayloadCodec())
    suite = EvalSuite(
        suite_id="eval-observe-suite",
        version="1.0",
        cases=(
            _case(
                "case-observed",
                input="observe-input",
                variant=_variant("observe-assistant", "1.0"),
                evaluator_id="hard-output-match",
                bundle_id="observe-bundle",
            ),
        ),
        variants=(_variant("observe-assistant", "1.0"),),
    )
    store = SQLiteEvalStore(database)
    try:
        engine = EvalExecutionEngine(
            registry=registry,
            run_store=subject_store,
            eval_store=store,
            evaluators={
                "hard-output-match": _evaluator("hard-output-match", hard=True)
            },
            projection_policy=_POLICY,
        )
        executed = await engine.run_suite(suite)
        dispatches_before_observe = adapter.call_count

        observer = EvalObserver(
            runner=Runner(registry=registry, store=subject_store)
        )
        run_id = executed.observations[0].subject_run_id
        selection = ObservationSelection(
            selection_id="eval-regression-observe",
            version="1.0",
            run_ids=(run_id,),
        )
        observed = await observer.observe_selection(selection)
        missing = await observer.observe_run("eval-run-does-not-exist")
        read_only = adapter.call_count == dispatches_before_observe

        stored = await store.record_observation(observed[0])
        replayed = await store.record_observation(observed[0])

        output_requirements = EvidenceRequirements(
            evaluator=EvaluatorRef(
                evaluator_id="projection-eval", version="1.0"
            ),
            fields=frozenset({EvidenceField.RUN_OUTPUT}),
        )
        input_requirements = EvidenceRequirements(
            evaluator=EvaluatorRef(
                evaluator_id="projection-eval", version="1.0"
            ),
            fields=frozenset({EvidenceField.RUN_INPUT}),
        )
        output_projection = project_observation(
            observed[0], output_requirements, _POLICY
        )
        input_projection = project_observation(
            observed[0], input_requirements, _POLICY
        )
    finally:
        store.close()

    problems: list[str] = []
    if observed[0].mode is not EvalMode.OBSERVE:
        problems.append("observe_mode_not_observe")
    if observed[0].completeness is not EvidenceCompleteness.COMPLETE:
        problems.append("observed_run_not_complete")
    if observed[0].run_output != _OK_OUTPUT:
        problems.append("observed_output_mismatch")
    if missing.completeness is not EvidenceCompleteness.UNAVAILABLE:
        problems.append("missing_run_forged")
    if not read_only:
        problems.append("observe_dispatched_model")
    if stored != observed[0] or replayed != observed[0]:
        problems.append("observe_observation_roundtrip_mismatch")
    if (
        EvidenceField.RUN_OUTPUT not in output_projection.delivered
        or output_projection.get(EvidenceField.RUN_OUTPUT) != _OK_OUTPUT
        or output_projection.denied
    ):
        problems.append("authorized_field_not_delivered")
    if (
        EvidenceField.RUN_INPUT not in input_projection.denied
        or input_projection.get(EvidenceField.RUN_INPUT) is not None
        or input_projection.values
    ):
        problems.append("unauthorized_field_leaked")

    return {
        "observe_read_only": read_only,
        "missing_run_unavailable": (
            missing.completeness is EvidenceCompleteness.UNAVAILABLE
        ),
        "projection_minimally_authorized": not any(
            problem in problems
            for problem in (
                "authorized_field_not_delivered",
                "unauthorized_field_leaked",
            )
        ),
        "problems": problems,
        "sqlite_digest": _file_digest(database),
    }


# ---------------------------------------------------------------------------
# Probe 8：Recommendation 只读引用（证据冻结、落盘幂等、绝不改写事实）
# ---------------------------------------------------------------------------


async def _recommendation_probe(directory: Path) -> dict[str, object]:
    """Recommendation 只读引用语义经真实 engine + store 路径证明。

    Recommendation 冻结引用（Report revision digest、Baseline、目标
    Variant 身份）与 gate 结论；落盘后全部既有 durable 事实
    （execution / report / baseline）保持原样；同 id 异内容篡改确定
    性冲突——它绝不自动修改 Baseline、Definition、Catalog 或未来
    routing 行为。
    """
    database = directory / "eval-recommendation.sqlite3"
    adapter = DeterministicModelAdapter(responses=(_OK_OUTPUT,))
    registry = DefinitionRegistry()
    _register(
        registry,
        adapter,
        definition_id="recommend-assistant",
        version="1.0",
    )
    suite = EvalSuite(
        suite_id="eval-recommendation-suite",
        version="1.0",
        cases=(
            _case(
                "case-recommended",
                input="recommendation-input",
                variant=_variant("recommend-assistant", "1.0"),
                evaluator_id="hard-output-match",
                bundle_id="recommendation-bundle",
            ),
        ),
        variants=(_variant("recommend-assistant", "1.0"),),
    )
    store = SQLiteEvalStore(database)
    try:
        engine = EvalExecutionEngine(
            registry=registry,
            run_store=InMemoryRunStore(PlaintextPayloadCodec()),
            eval_store=store,
            evaluators={
                "hard-output-match": _evaluator("hard-output-match", hard=True)
            },
            projection_policy=_POLICY,
        )
        run = await engine.run_suite(suite)
        report = build_report_revision(
            report_id="eval-recommendation-report",
            revision=1,
            execution_id=run.execution.execution_id,
            suite=suite,
            results=run.results,
        )
        baseline = EvalBaselineRecord(
            baseline_id="baseline-recommendation-1",
            report_id="eval-recommendation-report",
            report_revision=1,
            suite_id=suite.suite_id,
            suite_version=suite.version,
            comparison_policy=ComparisonPolicy(
                policy_id="default-policy", version="1.0"
            ),
        )
        await store.record_report(report)
        await store.record_baseline(baseline)

        recommendation = ModelRecommendationRecord(
            recommendation_id="recommendation-eval-regression-1",
            version="1.0",
            report_id=report.report_id,
            report_revision=report.revision,
            baseline_id=baseline.baseline_id,
            target=RecommendationTarget(
                kind=RECOMMENDATION_TARGET_AGENT_VARIANT,
                target_id="recommend-assistant",
                target_version="1.0",
            ),
            hard_gate=report.hard_outcome,
            quality_gate=report.quality_outcome,
            overall_outcome=report.overall_outcome,
            confidence=0.9,
            evidence_digest=report.content_digest,
        )
        recorded = await store.record_recommendation(recommendation)
        replayed = await store.record_recommendation(recommendation)
        # 只读性：recommendation 落盘后，全部既有 durable 事实原样可读。
        report_after = await store.get_report(report.report_id, 1)
        baseline_after = await store.get_baseline(baseline.baseline_id)
        execution_after = await store.get_execution(
            run.execution.execution_id
        )
        readback = await store.get_recommendation(
            "recommendation-eval-regression-1"
        )
        # 变异：同 id 异内容必须确定性冲突（append-only，绝不改写）。
        tampered = recommendation.model_copy(update={"confidence": 0.1})
        tamper_rejected = False
        try:
            await store.record_recommendation(tampered)
        except EvalRecordConflictError:
            tamper_rejected = True
    finally:
        store.close()

    problems: list[str] = []
    if recorded != recommendation or replayed != recommendation:
        problems.append("recommendation_replay_not_idempotent")
    if readback != recommendation:
        problems.append("recommendation_roundtrip_mismatch")
    if (
        report_after != report
        or baseline_after != baseline
        or execution_after != run.execution
    ):
        problems.append("stored_facts_mutated")
    if not tamper_rejected:
        problems.append("recommendation_tamper_accepted")
    if recommendation.evidence_digest != report.content_digest:
        problems.append("recommendation_evidence_not_frozen")

    return {
        "recommendation_readonly": not problems,
        "recommendation_replay_idempotent": (
            recorded == recommendation and replayed == recommendation
        ),
        "stored_facts_unchanged": (
            report_after == report
            and baseline_after == baseline
            and execution_after == run.execution
        ),
        "recommendation_tamper_rejected": tamper_rejected,
        "problems": problems,
        "sqlite_digest": _file_digest(database),
    }


# ---------------------------------------------------------------------------
# 公共 reconciliation 与 Scenario 入口
# ---------------------------------------------------------------------------


def reconcile_eval_regression(
    observation: Mapping[str, object],
) -> list[str]:
    """验证 eval-regression 变异证据：干净对照可比 + 三类变异全检出。

    ``observation`` 需要包含四个布尔键：

    - ``clean_control_comparable``：内容一致的干净对照必须判 COMPARABLE
      （未变化被误判为回归即 harness 错误）；
    - ``tampered_report_detected``：篡改内容但未更新 digest 必须被
      content-addressed 校验检出；
    - ``baseline_identity_mismatch_detected``：Baseline 引用与实际报告
      revision 错配必须 fail-closed（ValueError），绝不比较错误 revision；
    - ``pass_at_k_degradation_detected``：合法构造的 pass^k 劣化报告
      必须被判 REGRESSION，绝不静默接受。
    """
    problems: list[str] = []
    if observation.get("clean_control_comparable") is not True:
        problems.append("clean_comparison_not_comparable")
    if observation.get("tampered_report_detected") is not True:
        problems.append("undetected_report_tamper")
    if observation.get("baseline_identity_mismatch_detected") is not True:
        problems.append("undetected_baseline_identity_mismatch")
    if observation.get("pass_at_k_degradation_detected") is not True:
        problems.append("undetected_pass_at_k_degradation")
    return problems


def run_eval_regression() -> tuple[
    tuple[AcceptanceCheckResult, ...],
    dict[str, str | int | bool],
    dict[str, str],
]:
    """运行 eval-regression Scenario 的完整离线证明。

    返回 ``(checks, evidence_view, independent_evidence)``，与
    :func:`m_agent.testing.run_durable_effects_recovery` 相同的形态。
    """

    async def execute() -> tuple[dict, ...]:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            recovery = await _durable_recovery_probe(directory)
            judge = await _judge_isolation_probe(directory)
            comparison = await _baseline_comparison_probe(directory)
            regression = await _regression_detection_probe(directory)
            metrics = await _report_metrics_probe(directory)
            observe = await _observe_projection_probe(directory)
            recommendation = await _recommendation_probe(directory)
            mutation = await _mutation_probe(directory)
            return (
                recovery,
                judge,
                comparison,
                regression,
                metrics,
                observe,
                recommendation,
                mutation,
            )

    (
        recovery,
        judge,
        comparison,
        regression,
        metrics,
        observe,
        recommendation,
        mutation,
    ) = asyncio.run(execute())

    recovery_clean = not recovery["problems"]
    judge_clean = not judge["problems"]
    comparison_clean = not comparison["problems"]
    regression_clean = not regression["problems"]
    metrics_clean = not metrics["problems"]
    observe_clean = not observe["problems"]
    recommendation_clean = not recommendation["problems"]
    mutation_clean = not mutation["problems"]

    recovery_digest = _digest(
        {key: value for key, value in recovery.items() if key != "sqlite_digest"}
    )
    judge_digest = _digest(
        {key: value for key, value in judge.items() if key != "sqlite_digest"}
    )
    comparison_digest = _digest(
        {key: value for key, value in comparison.items() if key != "sqlite_digest"}
    )
    regression_digest = _digest(
        {key: value for key, value in regression.items() if key != "sqlite_digest"}
    )
    metrics_digest = _digest(
        {key: value for key, value in metrics.items() if key != "sqlite_digest"}
    )
    observe_digest = _digest(
        {key: value for key, value in observe.items() if key != "sqlite_digest"}
    )
    recommendation_digest = _digest(
        {
            key: value
            for key, value in recommendation.items()
            if key != "sqlite_digest"
        }
    )
    mutation_digest = _digest(mutation)

    evidence_view: dict[str, str | int | bool] = {
        "durable_recovery_items": int(recovery["suite_items"]),  # type: ignore[arg-type]
        "durable_recovery_resume_dispatches": int(
            recovery["resume_dispatches"]  # type: ignore[arg-type]
        ),
        "durable_recovery_idempotent": bool(recovery_clean),
        "durable_recovery_observed": bool(recovery_clean),
        "judge_isolation_enforced": bool(judge["construction_fail_closed"]),
        "judge_isolation_observed": bool(judge_clean),
        "baseline_comparison_states": int(comparison["states_verified"]),
        "baseline_comparison_observed": bool(comparison_clean),
        "regression_detection_paths": int(regression["regression_paths"]),  # type: ignore[arg-type]
        "regression_detection_observed": bool(regression_clean),
        "report_metrics_repetitions": int(metrics["sample_count"]),  # type: ignore[arg-type]
        "report_metrics_p95_justified": bool(metrics["p95_justified"]),
        "report_metrics_insufficient_p95_suppressed": bool(
            metrics["insufficient_p95_suppressed"]
        ),
        "report_metrics_observed": bool(metrics_clean),
        "observe_projection_read_only": bool(observe["observe_read_only"]),
        "observe_projection_observed": bool(observe_clean),
        "recommendation_readonly": bool(recommendation["recommendation_readonly"]),
        "recommendation_observed": bool(recommendation_clean),
        "mutation_probes": int(mutation["mutation_probes"]),
        "mutation_detected": bool(mutation_clean),
        "durable_recovery_authoritative_digest": recovery_digest,
        "judge_isolation_authoritative_digest": judge_digest,
        "baseline_comparison_authoritative_digest": comparison_digest,
        "regression_detection_authoritative_digest": regression_digest,
        "report_metrics_authoritative_digest": metrics_digest,
        "observe_projection_authoritative_digest": observe_digest,
        "recommendation_authoritative_digest": recommendation_digest,
        "mutation_authoritative_digest": mutation_digest,
    }
    mutation_independent_digest = _digest(
        {
            "probes": [
                "tampered_report",
                "baseline_identity",
                "pass_at_k",
            ],
            "problems": sorted(  # type: ignore[type-var]
                mutation["problems"]  # type: ignore[arg-type]
            ),
        }
    )
    independent_evidence = {
        "durable_recovery_sqlite_digest": str(recovery["sqlite_digest"]),
        "judge_isolation_sqlite_digest": str(judge["sqlite_digest"]),
        "baseline_comparison_sqlite_digest": str(comparison["sqlite_digest"]),
        "regression_detection_sqlite_digest": str(regression["sqlite_digest"]),
        "report_metrics_sqlite_digest": str(metrics["sqlite_digest"]),
        "observe_projection_sqlite_digest": str(observe["sqlite_digest"]),
        "recommendation_sqlite_digest": str(recommendation["sqlite_digest"]),
        "mutation_independent_digest": mutation_independent_digest,
    }

    def result(
        check_id: str, passed: bool, digest: str, reason: str
    ) -> AcceptanceCheckResult:
        return AcceptanceCheckResult(
            check_id=check_id,
            status=(
                AcceptanceCheckStatus.PASS if passed else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
            reason_code=reason,
            evidence_digest=digest,
        )

    return (
        (
            result(
                "eval.regression.durable-recovery",
                recovery_clean,
                recovery_digest,
                "durable_recovery_observed",
            ),
            result(
                "eval.regression.judge-isolation",
                judge_clean,
                judge_digest,
                "judge_isolation_observed",
            ),
            result(
                "eval.regression.baseline-comparison",
                comparison_clean,
                comparison_digest,
                "baseline_comparison_observed",
            ),
            result(
                "eval.regression.regression-detection",
                regression_clean,
                regression_digest,
                "regression_detection_observed",
            ),
            result(
                "eval.regression.report-metrics",
                metrics_clean,
                metrics_digest,
                "report_metrics_observed",
            ),
            result(
                "eval.regression.observe-projection",
                observe_clean,
                observe_digest,
                "observe_projection_observed",
            ),
            result(
                "eval.regression.recommendation-readonly",
                recommendation_clean,
                recommendation_digest,
                "recommendation_observed",
            ),
            result(
                "eval.regression.mutation",
                mutation_clean,
                mutation_digest,
                "mutation_detection_observed",
            ),
        ),
        evidence_view,
        independent_evidence,
    )
