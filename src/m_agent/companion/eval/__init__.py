"""Eval Companion 公共契约（ADR 0029；Ticket 17 最小证据 + Ticket 18 durable 回归）。

Eval 作为 Runtime Companion：以隔离 ``EXECUTE`` 启动受控 subject Run，
或以严格只读 ``OBSERVE`` 评估显式选择的既有 Run；两种模式都归一化
为 immutable Eval Observation，经最小授权 Observation Projection、
版本化 Evidence Adapter 与结构化 Evaluator result/reason code 消费。

Ticket 18 追加 durable 回归语义：SQLiteEvalStore 持久化六类事实、
可恢复的 EvalExecutionEngine、不可变 Report revision（pass^k 与统计
披露）、独立 Judge Run、Baseline 五态比较与只读 Recommendation。

本命名空间只组合公开端口，不给 Runner 注入 hook；Runtime Core 不
导入这里。未包含：Model Catalog/Router/Policy（后续 ticket）、
Recommendation 发布、live provider。
"""

from ._case import (
    AgentVariant,
    EvalCase,
    EvalSuite,
    EvalSuiteItem,
    EvaluatorRef,
    ExecutionProtocol,
    FixtureBundle,
    FixtureFact,
)
from ._errors import (
    EvalError,
    EvalFixtureBoundaryError,
    EvalRecordConflictError,
    EvalSuiteError,
    EvidenceIntegrityError,
)
from ._evidence import (
    AppendOnlyJournalEvidenceAdapter,
    ContentAddressedSnapshotEvidenceAdapter,
    EvidenceAdapter,
    EvidenceArtifact,
    SentinelEvidenceAdapter,
    evidence_digest,
)
from ._evaluator import (
    DeterministicEvaluator,
    EvalFailureKind,
    EvaluatorAggregate,
    EvaluatorOutcome,
    EvaluatorResult,
    EvaluatorResultRecord,
    OutputMatchesEvaluator,
    REASON_EVALUATOR_RAISED,
    REASON_EVIDENCE_MISSING,
    REASON_EVIDENCE_UNAUTHORIZED,
    REASON_SUBJECT_EVIDENCE_INCONCLUSIVE,
    REASON_SUBJECT_EVIDENCE_UNAVAILABLE,
    REASON_SUBJECT_OUTPUT_MATCHED,
    REASON_SUBJECT_OUTPUT_MISMATCH,
    REASON_SUBJECT_STATUS_MISMATCH,
    RunStatusEvaluator,
    aggregate_evaluator_results,
    run_evaluator,
)
from ._execute import (
    EvalExecutor,
    EvalSuiteExecutionResult,
)
from ._observation import (
    REASON_MODEL_CAPABILITIES_MISSING,
    REASON_SAMPLING_APPLIED,
    REASON_SUBJECT_NOT_TERMINAL,
    REASON_SUBJECT_RUN_MISSING,
    REASON_SUBJECT_TERMINAL,
    REASON_VARIANT_UNRESOLVED,
    EvalMode,
    EvalObservation,
    EvidenceCompleteness,
    SamplingDisclosure,
)
from ._observe import EvalObserver, ObservationSelection
from ._projection import (
    EvidenceField,
    EvidenceRequirements,
    ObservationProjection,
    ObservationProjectionPolicy,
    project_observation,
)
from ._store import (
    EvalCaseView,
    EvalExecutionRecord,
    EvalExecutionView,
    EvalObservationView,
    EvalReportView,
    EvalStore,
    InMemoryEvalStore,
)
from ._sqlite_store import SQLiteEvalStore
from ._report import (
    DEFAULT_MIN_SAMPLES_FOR_PERCENTILE,
    CaseVariantReport,
    NumericSummary,
    RepetitionOutcome,
    ReportRevisionRecord,
    build_report_revision,
    summarize_samples,
)
from ._baseline import (
    BaselineComparison,
    CaseComparison,
    ComparisonOverall,
    ComparisonPolicy,
    ComparisonVerdict,
    EvalBaselineRecord,
    compare_report_revisions,
)
from ._recommendation import (
    ModelRecommendationRecord,
    RECOMMENDATION_TARGET_AGENT_VARIANT,
    RECOMMENDATION_TARGET_ROUTING_POLICY,
    RecommendationTarget,
)
from ._judge import (
    JudgeBinding,
    JudgeRunExecutor,
    REASON_JUDGE_RUN_FAILED,
    REASON_JUDGE_VERDICT_INVALID,
    REASON_JUDGE_VERDICT_RENDERED,
    parse_judge_verdict,
)
from ._engine import EvalExecutionEngine, EvalSuiteRunResult

__all__ = [
    "AgentVariant",
    "AppendOnlyJournalEvidenceAdapter",
    "BaselineComparison",
    "CaseComparison",
    "CaseVariantReport",
    "ComparisonOverall",
    "ComparisonPolicy",
    "ComparisonVerdict",
    "ContentAddressedSnapshotEvidenceAdapter",
    "DEFAULT_MIN_SAMPLES_FOR_PERCENTILE",
    "DeterministicEvaluator",
    "EvalBaselineRecord",
    "EvalCaseView",
    "EvalError",
    "EvalExecutionEngine",
    "EvalExecutionRecord",
    "EvalExecutionView",
    "EvalExecutor",
    "EvalFailureKind",
    "EvalCase",
    "EvalFixtureBoundaryError",
    "EvalMode",
    "EvalObservation",
    "EvalObservationView",
    "EvalRecordConflictError",
    "EvalReportView",
    "EvalSuite",
    "EvalSuiteError",
    "EvalSuiteExecutionResult",
    "EvalSuiteItem",
    "EvalSuiteRunResult",
    "EvalStore",
    "EvalObserver",
    "EvaluatorAggregate",
    "EvaluatorOutcome",
    "EvaluatorRef",
    "EvaluatorResult",
    "EvaluatorResultRecord",
    "EvidenceAdapter",
    "EvidenceArtifact",
    "EvidenceCompleteness",
    "EvidenceField",
    "EvidenceIntegrityError",
    "EvidenceRequirements",
    "ExecutionProtocol",
    "FixtureBundle",
    "FixtureFact",
    "InMemoryEvalStore",
    "JudgeBinding",
    "JudgeRunExecutor",
    "ModelRecommendationRecord",
    "NumericSummary",
    "ObservationProjection",
    "ObservationProjectionPolicy",
    "ObservationSelection",
    "OutputMatchesEvaluator",
    "RecommendationTarget",
    "REASON_EVALUATOR_RAISED",
    "REASON_EVIDENCE_MISSING",
    "REASON_EVIDENCE_UNAUTHORIZED",
    "REASON_JUDGE_RUN_FAILED",
    "REASON_JUDGE_VERDICT_INVALID",
    "REASON_JUDGE_VERDICT_RENDERED",
    "REASON_MODEL_CAPABILITIES_MISSING",
    "REASON_SAMPLING_APPLIED",
    "REASON_SUBJECT_EVIDENCE_INCONCLUSIVE",
    "REASON_SUBJECT_EVIDENCE_UNAVAILABLE",
    "REASON_SUBJECT_NOT_TERMINAL",
    "REASON_SUBJECT_OUTPUT_MATCHED",
    "REASON_SUBJECT_OUTPUT_MISMATCH",
    "REASON_SUBJECT_RUN_MISSING",
    "REASON_SUBJECT_STATUS_MISMATCH",
    "REASON_SUBJECT_TERMINAL",
    "REASON_VARIANT_UNRESOLVED",
    "RECOMMENDATION_TARGET_AGENT_VARIANT",
    "RECOMMENDATION_TARGET_ROUTING_POLICY",
    "RepetitionOutcome",
    "ReportRevisionRecord",
    "RunStatusEvaluator",
    "SamplingDisclosure",
    "SentinelEvidenceAdapter",
    "SQLiteEvalStore",
    "aggregate_evaluator_results",
    "build_report_revision",
    "compare_report_revisions",
    "evidence_digest",
    "parse_judge_verdict",
    "project_observation",
    "run_evaluator",
    "summarize_samples",
]
