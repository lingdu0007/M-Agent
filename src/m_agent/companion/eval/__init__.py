"""Eval Companion 公共契约（ADR 0029，Ticket 17 最小证据范围）。

Eval 作为 Runtime Companion：以隔离 ``EXECUTE`` 启动受控 subject Run，
或以严格只读 ``OBSERVE`` 评估显式选择的既有 Run；两种模式都归一化
为 immutable Eval Observation，经最小授权 Observation Projection、
版本化 Evidence Adapter 与结构化 Evaluator result/reason code 消费。

本命名空间只组合公开端口，不给 Runner 注入 hook；Runtime Core 不
导入这里。Ticket 17 明确不包含：SQLite 持久化、中断恢复、LLM-as-judge、
不可变 Report/Baseline、Recommendation、Routing 与 live provider。
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
    EvalExecutionRecord,
    EvalExecutionView,
    EvalObservationView,
    EvalStore,
    InMemoryEvalStore,
)

__all__ = [
    "AgentVariant",
    "AppendOnlyJournalEvidenceAdapter",
    "ContentAddressedSnapshotEvidenceAdapter",
    "DeterministicEvaluator",
    "EvalError",
    "EvalExecutionRecord",
    "EvalExecutionView",
    "EvalExecutor",
    "EvalFailureKind",
    "EvalCase",
    "EvalMode",
    "EvalObservation",
    "EvalObservationView",
    "EvalRecordConflictError",
    "EvalSuite",
    "EvalSuiteError",
    "EvalSuiteExecutionResult",
    "EvalSuiteItem",
    "EvalStore",
    "EvalObserver",
    "EvaluatorAggregate",
    "EvaluatorOutcome",
    "EvaluatorRef",
    "EvaluatorResult",
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
    "ObservationProjection",
    "ObservationProjectionPolicy",
    "ObservationSelection",
    "OutputMatchesEvaluator",
    "REASON_EVALUATOR_RAISED",
    "REASON_EVIDENCE_MISSING",
    "REASON_EVIDENCE_UNAUTHORIZED",
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
    "RunStatusEvaluator",
    "SamplingDisclosure",
    "SentinelEvidenceAdapter",
    "aggregate_evaluator_results",
    "evidence_digest",
    "project_observation",
    "run_evaluator",
]
