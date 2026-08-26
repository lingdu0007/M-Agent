"""纯函数确定性 Model Router（ADR 0041，Runtime Companion）。

Router 在 ``create_run`` 之前依据冻结的 Model Catalog、版本化
Routing Policy 与只读 evidence snapshot 选择完整 Agent Variant：

1. 结构校验：规则不完整或不能形成确定顺序 → ``INVALID_POLICY``；
2. Catalog 校验：不可变身份或 Contract 指纹冲突 → ``CATALOG_CONFLICT``；
3. 确定性过滤（按稳定 ``variant_id + version`` 顺序迭代，首个失败
   原因进入 reason trace）：策略注册与候选范围 → typed capabilities
   与 numeric Limits（含 Variant 自身 Binding Set 一致性）→
   Deployment Constraints（属性未知 fail closed，合规证据缺失/过期
   整体 ``EVIDENCE_UNAVAILABLE``）→ hard 价格/质量/稳定性/可用性
   门槛（硬策略证据缺失或过期 fail closed，绝不隐式收窄候选）；
4. 幸存候选按声明了方向与缺失值策略的字典序目标排序，最终以稳定
   ``variant_id + version`` tie-break 破除并列——不使用隐式加权总分。

失败发生在任何 Session Claim 与 Agent Run 创建之前，产生零模型
dispatch；Router 不读取原始 prompt、Conversation History 或敏感
Context Item，也不向 Runtime Core 引入 catalog、pricing、availability
或 selection algorithm。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import enum
import functools
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..._errors import MAgentError
from ..._model import ModelPurpose
from ..._run import RunRecord
from ._catalog import AgentVariant, ModelCatalog, ModelCatalogEntry
from ._evidence import RoutingEvidence
from ._policy import (
    DeploymentConstraints,
    MissingValuePolicy,
    ObjectiveDirection,
    ObjectiveDimension,
    RoutingObjective,
    RoutingPolicy,
    RoutingPolicyIdentity,
    canonical_digest,
)

_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class RoutingOutcome(str, enum.Enum):
    """六种已决议的 Routing Result。"""

    SELECTED = "SELECTED"
    NO_COMPATIBLE_VARIANT = "NO_COMPATIBLE_VARIANT"
    POLICY_UNSATISFIED = "POLICY_UNSATISFIED"
    EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
    CATALOG_CONFLICT = "CATALOG_CONFLICT"
    INVALID_POLICY = "INVALID_POLICY"


# -- 稳定 reason code ------------------------------------------------------

REASON_SELECTED = "SELECTED"
REASON_NO_COMPATIBLE_VARIANT = "NO_COMPATIBLE_VARIANT"
REASON_POLICY_UNSATISFIED = "POLICY_UNSATISFIED"

REASON_MODEL_EVIDENCE_MISSING = "MODEL_EVIDENCE_MISSING"
REASON_MODEL_EVIDENCE_STALE = "MODEL_EVIDENCE_STALE"
REASON_PRICING_SNAPSHOT_MISSING = "PRICING_SNAPSHOT_MISSING"
REASON_PRICING_SNAPSHOT_STALE = "PRICING_SNAPSHOT_STALE"
REASON_PRICING_SNAPSHOT_NOT_EFFECTIVE = "PRICING_SNAPSHOT_NOT_EFFECTIVE"
REASON_AVAILABILITY_SNAPSHOT_MISSING = "AVAILABILITY_SNAPSHOT_MISSING"
REASON_AVAILABILITY_SNAPSHOT_STALE = "AVAILABILITY_SNAPSHOT_STALE"
REASON_RETENTION_EVIDENCE_MISSING = "RETENTION_EVIDENCE_MISSING"
REASON_RETENTION_EVIDENCE_STALE = "RETENTION_EVIDENCE_STALE"

REASON_DUPLICATE_VARIANT_IDENTITY = "DUPLICATE_VARIANT_IDENTITY"
REASON_CONTRACT_FINGERPRINT_CONFLICT = "CONTRACT_FINGERPRINT_CONFLICT"

REASON_AMBIGUOUS_OBJECTIVES = "AMBIGUOUS_OBJECTIVES"
REASON_EMPTY_CANDIDATE_SCOPE = "EMPTY_CANDIDATE_SCOPE"

REASON_NOT_REGISTERED_FOR_POLICY = "NOT_REGISTERED_FOR_POLICY"
REASON_NOT_IN_CANDIDATE_SCOPE = "NOT_IN_CANDIDATE_SCOPE"
REASON_PROVIDER_UNKNOWN = "PROVIDER_UNKNOWN"
REASON_PROVIDER_NOT_ALLOWED = "PROVIDER_NOT_ALLOWED"
REASON_REGION_UNKNOWN = "REGION_UNKNOWN"
REASON_REGION_NOT_ALLOWED = "REGION_NOT_ALLOWED"
REASON_ENDPOINT_CLASS_UNKNOWN = "ENDPOINT_CLASS_UNKNOWN"
REASON_ENDPOINT_CLASS_NOT_ALLOWED = "ENDPOINT_CLASS_NOT_ALLOWED"
REASON_RETENTION_EVIDENCE_UNKNOWN = "RETENTION_EVIDENCE_UNKNOWN"
REASON_RETENTION_EVIDENCE_VERSION_NOT_ALLOWED = (
    "RETENTION_EVIDENCE_VERSION_NOT_ALLOWED"
)
REASON_RETENTION_EVIDENCE_VERSION_MISMATCH = "RETENTION_EVIDENCE_VERSION_MISMATCH"
REASON_HARD_PRICE_GATE_FAILED = "HARD_PRICE_GATE_FAILED"
REASON_HARD_QUALITY_GATE_FAILED = "HARD_QUALITY_GATE_FAILED"
REASON_HARD_STABILITY_GATE_FAILED = "HARD_STABILITY_GATE_FAILED"
REASON_HARD_AVAILABILITY_GATE_FAILED = "HARD_AVAILABILITY_GATE_FAILED"
REASON_OUTRANKED = "OUTRANKED"

WARNING_SOFT_EVIDENCE_MISSING = "SOFT_EVIDENCE_MISSING"
WARNING_SOFT_EVIDENCE_STALE = "SOFT_EVIDENCE_STALE"

_EVIDENCE_MODEL_EVIDENCE = "MODEL_EVIDENCE"
_EVIDENCE_PRICING = "PRICING"
_EVIDENCE_AVAILABILITY = "AVAILABILITY"
_EVIDENCE_RETENTION = "RETENTION"


class RoutingError(MAgentError):
    """Routing Companion 的公共错误基类。"""


class RoutingBindingError(RoutingError):
    """Routing Decision 与 Run 身份或冻结 Contract 指纹不一致。"""


class _FrozenRouterValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _validate_reason_code(value: str) -> str:
    if not _REASON_CODE.fullmatch(value):
        raise ValueError("reason code must be a stable uppercase code")
    return value


class CandidateEvaluationStage(str, enum.Enum):
    """候选被决议（通过或过滤）的确定性阶段。"""

    POLICY_SCOPE = "POLICY_SCOPE"
    MODEL_REQUIREMENTS = "MODEL_REQUIREMENTS"
    DEPLOYMENT_CONSTRAINTS = "DEPLOYMENT_CONSTRAINTS"
    HARD_GATES = "HARD_GATES"
    ORDERING = "ORDERING"


class ObjectiveValue(_FrozenRouterValue):
    """一个候选在一个排序维度上的读数；缺失保持缺失。"""

    dimension: ObjectiveDimension
    value: Decimal | None
    missing: bool


class CandidateEvaluation(_FrozenRouterValue):
    """reason trace 中一个候选的结构化结论。"""

    variant_id: str = Field(min_length=1)
    variant_version: str = Field(min_length=1)
    stage: CandidateEvaluationStage
    passed: bool
    reason_code: str
    rank: int | None = None
    objective_values: tuple[ObjectiveValue, ...] = Field(default_factory=tuple)

    @field_validator("reason_code")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        return _validate_reason_code(value)


class RoutingWarning(_FrozenRouterValue):
    """soft evidence 缺口的显式 warning（不放宽、不静默）。"""

    code: str
    variant_id: str = Field(min_length=1)
    variant_version: str = Field(min_length=1)
    dimension: ObjectiveDimension | None = None

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        return _validate_reason_code(value)


class EvidenceSnapshotReference(_FrozenRouterValue):
    """Decision 引用的一个快照版本与有效期（不携带数据正文）。"""

    kind: str = Field(min_length=1)
    variant_id: str = Field(min_length=1)
    variant_version: str = Field(min_length=1)
    snapshot_version: str = Field(min_length=1)
    valid_until: datetime


class RoutingDecision(_FrozenRouterValue):
    """一次成功选择的不可变证据。

    记录最终 Variant、PRIMARY Contract 指纹、候选过滤 reason trace、
    排序读数、确定性 tie-break，以及使用的 policy、catalog 与全部
    evidence snapshot 输入的摘要与版本/有效期引用。``decision_id`` 由
    输入内容派生：相同输入 snapshot 复跑必然得到相同 Decision。
    """

    decision_id: str = Field(min_length=1)
    outcome: RoutingOutcome
    selected_variant: AgentVariant
    selected_contract_fingerprint: str = Field(min_length=1)
    policy_identity: RoutingPolicyIdentity
    policy_digest: str = Field(min_length=1)
    catalog_id: str = Field(min_length=1)
    catalog_version: str = Field(min_length=1)
    catalog_digest: str = Field(min_length=1)
    evidence_digest: str = Field(min_length=1)
    evidence_references: tuple[EvidenceSnapshotReference, ...] = Field(
        default_factory=tuple
    )
    candidate_evaluations: tuple[CandidateEvaluation, ...] = Field(
        default_factory=tuple
    )
    tie_break_applied: bool = False


class RoutingResult(_FrozenRouterValue):
    """Run 前选择的结构化结果（在任何 Claim/Run 创建之前返回）。"""

    outcome: RoutingOutcome
    reason_code: str
    decision: RoutingDecision | None = None
    candidate_evaluations: tuple[CandidateEvaluation, ...] = Field(
        default_factory=tuple
    )
    warnings: tuple[RoutingWarning, ...] = Field(default_factory=tuple)

    @field_validator("reason_code")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        return _validate_reason_code(value)


class RoutingDecisionBinding(_FrozenRouterValue):
    """把 Routing Decision 显式绑定到一个已创建 Run 的不可变记录。

    Core 只关联 Decision 标识与摘要（ADR 0041）；本绑定由应用在
    ``create_run`` 之后持有，证明新 Run 的冻结 Definition Snapshot
    与所选 Variant 完全一致。跨模型继续只能由应用显式创建并关联
    Replacement Run，绝不伪装成原 Run 的重试或恢复。
    """

    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    variant_id: str = Field(min_length=1)
    variant_version: str = Field(min_length=1)
    selected_contract_fingerprint: str = Field(min_length=1)


def bind_decision_to_run(
    decision: RoutingDecision, run: RunRecord
) -> RoutingDecisionBinding:
    """校验并冻结 Decision 与新 Run 的绑定关系。

    Run 的 ``definition_id + version`` 必须精确等于所选 Variant 冻结的
    Definition 身份，且 Run 冻结 Definition Snapshot 中 PRIMARY binding
    的 Contract 指纹必须等于 Decision 记录的指纹；无法验证指纹（Run
    没有冻结 Definition Snapshot，或快照是未冻结 Model Binding Set 的
    legacy 快照）同样 fail closed，绝不静默换绑或放宽。
    """
    variant = decision.selected_variant
    if (
        run.definition_id != variant.definition_id
        or run.definition_version != variant.definition_version
    ):
        raise RoutingBindingError(
            "run definition identity does not match the selected variant: "
            f"{run.definition_id}@{run.definition_version} != "
            f"{variant.definition_id}@{variant.definition_version}"
        )
    snapshot = run.snapshot
    if snapshot is None:
        raise RoutingBindingError(
            "run has no frozen definition snapshot; cannot verify the "
            "routing decision contract fingerprint"
        )
    if snapshot.model_bindings is None:
        raise RoutingBindingError(
            "run definition snapshot has no frozen model bindings; cannot "
            "verify the routing decision contract fingerprint"
        )
    frozen_fingerprint = snapshot.model_bindings.for_purpose(
        ModelPurpose.PRIMARY
    ).contract.fingerprint
    if frozen_fingerprint != decision.selected_contract_fingerprint:
        raise RoutingBindingError(
            "run frozen PRIMARY contract fingerprint does not match the "
            "routing decision fingerprint"
        )
    return RoutingDecisionBinding(
        decision_id=decision.decision_id,
        run_id=run.run_id,
        definition_id=run.definition_id,
        definition_version=run.definition_version,
        variant_id=variant.variant_id,
        variant_version=variant.version,
        selected_contract_fingerprint=decision.selected_contract_fingerprint,
    )


# -- Router 实现 ------------------------------------------------------------


class ModelRouter:
    """纯函数确定性 Router：无状态、无 IO、无隐藏模型探测。"""

    def select(
        self,
        *,
        catalog: ModelCatalog,
        policy: RoutingPolicy,
        evidence: RoutingEvidence | None = None,
        as_of: datetime,
    ) -> RoutingResult:
        """依据相同输入 snapshot 确定性地选择一个完整 Agent Variant。"""
        if evidence is None:
            evidence = RoutingEvidence()
        invalid = _policy_structure_error(policy)
        if invalid is not None:
            return RoutingResult(
                outcome=RoutingOutcome.INVALID_POLICY, reason_code=invalid
            )
        conflict = _catalog_conflict(catalog)
        if conflict is not None:
            return RoutingResult(
                outcome=RoutingOutcome.CATALOG_CONFLICT, reason_code=conflict
            )

        references: dict[tuple[str, str], list[EvidenceSnapshotReference]] = {}
        evaluations: list[CandidateEvaluation] = []

        compatible: list[ModelCatalogEntry] = []
        for entry in catalog.canonical_entries():
            stage_code = _compatibility_failure(
                entry, policy, evidence, as_of, references, evaluations
            )
            if stage_code is not None:
                if stage_code in _EVIDENCE_ABORT_CODES:
                    return RoutingResult(
                        outcome=RoutingOutcome.EVIDENCE_UNAVAILABLE,
                        reason_code=stage_code,
                        candidate_evaluations=tuple(evaluations),
                    )
                continue
            compatible.append(entry)

        if not compatible:
            return RoutingResult(
                outcome=RoutingOutcome.NO_COMPATIBLE_VARIANT,
                reason_code=REASON_NO_COMPATIBLE_VARIANT,
                candidate_evaluations=tuple(evaluations),
            )

        gate_survivors: list[ModelCatalogEntry] = []
        for entry in compatible:
            failure, abort = _hard_gate_failure(
                entry.variant, policy, evidence, as_of, references
            )
            if abort is not None:
                evaluations.append(
                    CandidateEvaluation(
                        variant_id=entry.variant.variant_id,
                        variant_version=entry.variant.version,
                        stage=CandidateEvaluationStage.HARD_GATES,
                        passed=False,
                        reason_code=abort,
                    )
                )
                return RoutingResult(
                    outcome=RoutingOutcome.EVIDENCE_UNAVAILABLE,
                    reason_code=abort,
                    candidate_evaluations=tuple(evaluations),
                )
            if failure is not None:
                evaluations.append(
                    CandidateEvaluation(
                        variant_id=entry.variant.variant_id,
                        variant_version=entry.variant.version,
                        stage=CandidateEvaluationStage.HARD_GATES,
                        passed=False,
                        reason_code=failure,
                    )
                )
                continue
            gate_survivors.append(entry)

        if not gate_survivors:
            return RoutingResult(
                outcome=RoutingOutcome.POLICY_UNSATISFIED,
                reason_code=REASON_POLICY_UNSATISFIED,
                candidate_evaluations=tuple(evaluations),
            )

        warnings = _WarningCollector()
        readings: dict[tuple[str, str], dict[ObjectiveDimension, Decimal | None]] = {}
        for entry in gate_survivors:
            readings[entry.variant.identity] = {
                objective.dimension: _objective_value(
                    entry.variant,
                    objective.dimension,
                    evidence,
                    as_of,
                    references,
                    warnings,
                )
                for objective in policy.objectives
            }

        ranked = sorted(
            gate_survivors,
            key=functools.cmp_to_key(
                _ordering_comparator(policy.objectives, readings)
            ),
        )
        selected = ranked[0]
        ordered_evaluations = []
        for rank, entry in enumerate(ranked, start=1):
            ordered_evaluations.append(
                CandidateEvaluation(
                    variant_id=entry.variant.variant_id,
                    variant_version=entry.variant.version,
                    stage=CandidateEvaluationStage.ORDERING,
                    passed=True,
                    reason_code=(
                        REASON_SELECTED if rank == 1 else REASON_OUTRANKED
                    ),
                    rank=rank,
                    objective_values=tuple(
                        ObjectiveValue(
                            dimension=dimension,
                            value=value,
                            missing=value is None,
                        )
                        for dimension, value in readings[
                            entry.variant.identity
                        ].items()
                    ),
                )
            )
        evaluations.extend(ordered_evaluations)

        decision_id = canonical_digest(
            {
                "outcome": RoutingOutcome.SELECTED.value,
                "reason_code": REASON_SELECTED,
                "selected_variant_digest": selected.variant.variant_digest(),
                "policy_digest": policy.content_digest(),
                "catalog_digest": catalog.catalog_digest(),
                "evidence_digest": evidence.evidence_digest(),
            }
        )
        selected_fingerprint = selected.variant.primary_contract().fingerprint
        if selected_fingerprint is None:
            # ModelContract 构造时总会填充指纹；此分支防御性 fail closed。
            raise RoutingError(
                "selected variant PRIMARY contract is missing its fingerprint"
            )
        decision = RoutingDecision(
            decision_id=decision_id,
            outcome=RoutingOutcome.SELECTED,
            selected_variant=selected.variant,
            selected_contract_fingerprint=selected_fingerprint,
            policy_identity=policy.identity,
            policy_digest=policy.content_digest(),
            catalog_id=catalog.catalog_id,
            catalog_version=catalog.version,
            catalog_digest=catalog.catalog_digest(),
            evidence_digest=evidence.evidence_digest(),
            evidence_references=_dedupe_references(
                references.get(selected.variant.identity, ())
            ),
            candidate_evaluations=tuple(evaluations),
            tie_break_applied=_tie_break_decided(ranked, readings),
        )
        return RoutingResult(
            outcome=RoutingOutcome.SELECTED,
            reason_code=REASON_SELECTED,
            decision=decision,
            candidate_evaluations=tuple(evaluations),
            warnings=tuple(warnings.collected),
        )


_EVIDENCE_ABORT_CODES = frozenset(
    {
        REASON_MODEL_EVIDENCE_MISSING,
        REASON_MODEL_EVIDENCE_STALE,
        REASON_PRICING_SNAPSHOT_MISSING,
        REASON_PRICING_SNAPSHOT_STALE,
        REASON_PRICING_SNAPSHOT_NOT_EFFECTIVE,
        REASON_AVAILABILITY_SNAPSHOT_MISSING,
        REASON_AVAILABILITY_SNAPSHOT_STALE,
        REASON_RETENTION_EVIDENCE_MISSING,
        REASON_RETENTION_EVIDENCE_STALE,
    }
)


def _policy_structure_error(policy: RoutingPolicy) -> str | None:
    """结构不完整的规则在过滤开始前确定性失败。"""
    dimensions = [objective.dimension for objective in policy.objectives]
    if len(dimensions) != len(set(dimensions)):
        return REASON_AMBIGUOUS_OBJECTIVES
    if policy.allowed_variants is not None and not policy.allowed_variants:
        return REASON_EMPTY_CANDIDATE_SCOPE
    return None


def _catalog_conflict(catalog: ModelCatalog) -> str | None:
    """不可变 Catalog 身份或 Contract 指纹冲突。"""
    digests: dict[tuple[str, str], str] = {}
    for entry in catalog.entries:
        identity = entry.variant.identity
        digest = entry.variant.variant_digest()
        previous = digests.get(identity)
        if previous is not None and previous != digest:
            return REASON_DUPLICATE_VARIANT_IDENTITY
        digests[identity] = digest
    fingerprints: dict[tuple[str, str], str] = {}
    for entry in catalog.entries:
        for binding in entry.variant.model_bindings.bindings:
            contract = binding.contract
            key = (contract.contract_id, contract.version)
            fingerprint = contract.fingerprint
            if fingerprint is None:
                # Contract 缺失语义指纹即身份不完整：fail closed。
                return REASON_CONTRACT_FINGERPRINT_CONFLICT
            previous = fingerprints.get(key)
            if previous is not None and previous != fingerprint:
                return REASON_CONTRACT_FINGERPRINT_CONFLICT
            fingerprints[key] = fingerprint
    return None


def _compatibility_failure(
    entry: ModelCatalogEntry,
    policy: RoutingPolicy,
    evidence: RoutingEvidence,
    as_of: datetime,
    references: dict[tuple[str, str], list[EvidenceSnapshotReference]],
    evaluations: list[CandidateEvaluation],
) -> str | None:
    """逐阶段确定性过滤；返回 reason code（evidence abort code 除外）。

    返回 ``None`` 表示候选通过全部兼容性阶段。过滤性失败会向
    ``evaluations`` 追加一条记录；evidence abort（属于
    ``_EVIDENCE_ABORT_CODES``）同样追加记录后由调用方整体 fail closed。
    """
    variant = entry.variant

    # 阶段 1：策略注册与候选范围。
    if policy.identity not in variant.policy_identities:
        evaluations.append(
            _filtered(
                variant,
                CandidateEvaluationStage.POLICY_SCOPE,
                REASON_NOT_REGISTERED_FOR_POLICY,
            )
        )
        return REASON_NOT_REGISTERED_FOR_POLICY
    if policy.allowed_variants is not None and (
        variant.identity not in set(policy.allowed_variants)
    ):
        evaluations.append(
            _filtered(
                variant,
                CandidateEvaluationStage.POLICY_SCOPE,
                REASON_NOT_IN_CANDIDATE_SCOPE,
            )
        )
        return REASON_NOT_IN_CANDIDATE_SCOPE

    # 阶段 2：Variant 自身完整 Binding Set 的一致性。
    for purpose in ModelPurpose:
        binding = variant.model_bindings.for_purpose(purpose)
        match = binding.requirements.match(binding.contract)
        if not match.compatible:
            evaluations.append(
                _filtered(
                    variant,
                    CandidateEvaluationStage.MODEL_REQUIREMENTS,
                    match.reason.value,
                )
            )
            return match.reason.value

    # 阶段 3：Policy 的 Model Requirements 对 PRIMARY Contract。
    policy_match = policy.model_requirements.match(variant.primary_contract())
    if not policy_match.compatible:
        evaluations.append(
            _filtered(
                variant,
                CandidateEvaluationStage.MODEL_REQUIREMENTS,
                policy_match.reason.value,
            )
        )
        return policy_match.reason.value

    # 阶段 4：Deployment Constraints 硬匹配。
    deployment = policy.deployment
    attributes = entry.deployment
    for value, allowed, unknown_code, not_allowed_code in (
        (
            attributes.provider,
            deployment.allowed_providers,
            REASON_PROVIDER_UNKNOWN,
            REASON_PROVIDER_NOT_ALLOWED,
        ),
        (
            attributes.region,
            deployment.allowed_regions,
            REASON_REGION_UNKNOWN,
            REASON_REGION_NOT_ALLOWED,
        ),
        (
            attributes.endpoint_class,
            deployment.allowed_endpoint_classes,
            REASON_ENDPOINT_CLASS_UNKNOWN,
            REASON_ENDPOINT_CLASS_NOT_ALLOWED,
        ),
    ):
        if allowed is None:
            continue
        if value is None:
            evaluations.append(
                _filtered(
                    variant,
                    CandidateEvaluationStage.DEPLOYMENT_CONSTRAINTS,
                    unknown_code,
                )
            )
            return unknown_code
        if value not in allowed:
            evaluations.append(
                _filtered(
                    variant,
                    CandidateEvaluationStage.DEPLOYMENT_CONSTRAINTS,
                    not_allowed_code,
                )
            )
            return not_allowed_code

    if deployment.allowed_retention_evidence_versions is not None:
        if attributes.retention_evidence_version is None:
            evaluations.append(
                _filtered(
                    variant,
                    CandidateEvaluationStage.DEPLOYMENT_CONSTRAINTS,
                    REASON_RETENTION_EVIDENCE_UNKNOWN,
                )
            )
            return REASON_RETENTION_EVIDENCE_UNKNOWN
        retention = evidence.latest_retention(
            variant.variant_id, variant.version
        )
        if retention is None:
            evaluations.append(
                _filtered(
                    variant,
                    CandidateEvaluationStage.DEPLOYMENT_CONSTRAINTS,
                    REASON_RETENTION_EVIDENCE_MISSING,
                )
            )
            return REASON_RETENTION_EVIDENCE_MISSING
        if retention.valid_until < as_of:
            evaluations.append(
                _filtered(
                    variant,
                    CandidateEvaluationStage.DEPLOYMENT_CONSTRAINTS,
                    REASON_RETENTION_EVIDENCE_STALE,
                )
            )
            return REASON_RETENTION_EVIDENCE_STALE
        _record_reference(
            references,
            variant,
            _EVIDENCE_RETENTION,
            retention.version,
            retention.valid_until,
        )
        if (
            attributes.retention_evidence_version
            not in deployment.allowed_retention_evidence_versions
        ):
            evaluations.append(
                _filtered(
                    variant,
                    CandidateEvaluationStage.DEPLOYMENT_CONSTRAINTS,
                    REASON_RETENTION_EVIDENCE_VERSION_NOT_ALLOWED,
                )
            )
            return REASON_RETENTION_EVIDENCE_VERSION_NOT_ALLOWED
        if (
            retention.verified_retention_evidence_version
            != attributes.retention_evidence_version
        ):
            evaluations.append(
                _filtered(
                    variant,
                    CandidateEvaluationStage.DEPLOYMENT_CONSTRAINTS,
                    REASON_RETENTION_EVIDENCE_VERSION_MISMATCH,
                )
            )
            return REASON_RETENTION_EVIDENCE_VERSION_MISMATCH

    return None


def _filtered(
    variant: AgentVariant,
    stage: CandidateEvaluationStage,
    reason_code: str,
) -> CandidateEvaluation:
    return CandidateEvaluation(
        variant_id=variant.variant_id,
        variant_version=variant.version,
        stage=stage,
        passed=False,
        reason_code=reason_code,
    )


def _hard_gate_failure(
    variant: AgentVariant,
    policy: RoutingPolicy,
    evidence: RoutingEvidence,
    as_of: datetime,
    references: dict[tuple[str, str], list[EvidenceSnapshotReference]],
) -> tuple[str | None, str | None]:
    """返回 ``(门槛失败 code, evidence abort code)``；两者同时最多一个。"""
    gates = policy.hard_gates
    if gates.max_input_price_per_mtok is not None:
        pricing = evidence.latest_pricing(variant.variant_id, variant.version)
        if pricing is None:
            return None, REASON_PRICING_SNAPSHOT_MISSING
        if pricing.effective_at > as_of:
            return None, REASON_PRICING_SNAPSHOT_NOT_EFFECTIVE
        if pricing.valid_until < as_of:
            return None, REASON_PRICING_SNAPSHOT_STALE
        _record_reference(
            references, variant, _EVIDENCE_PRICING, pricing.version, pricing.valid_until
        )
        if pricing.input_price_per_mtok > gates.max_input_price_per_mtok:
            return REASON_HARD_PRICE_GATE_FAILED, None

    if (
        gates.min_quality_score is not None
        or gates.min_stability_score is not None
    ):
        model = evidence.latest_model_evidence(
            variant.variant_id, variant.version
        )
        if model is None:
            return None, REASON_MODEL_EVIDENCE_MISSING
        if model.valid_until < as_of:
            return None, REASON_MODEL_EVIDENCE_STALE
        _record_reference(
            references, variant, _EVIDENCE_MODEL_EVIDENCE, model.version, model.valid_until
        )
        if (
            gates.min_quality_score is not None
            and model.quality_score < gates.min_quality_score
        ):
            return REASON_HARD_QUALITY_GATE_FAILED, None
        if (
            gates.min_stability_score is not None
            and model.stability_score < gates.min_stability_score
        ):
            return REASON_HARD_STABILITY_GATE_FAILED, None

    if gates.require_availability:
        availability = evidence.latest_availability(
            variant.variant_id, variant.version
        )
        if availability is None:
            return None, REASON_AVAILABILITY_SNAPSHOT_MISSING
        if availability.valid_until < as_of:
            return None, REASON_AVAILABILITY_SNAPSHOT_STALE
        _record_reference(
            references,
            variant,
            _EVIDENCE_AVAILABILITY,
            availability.version,
            availability.valid_until,
        )
        if not availability.available:
            return REASON_HARD_AVAILABILITY_GATE_FAILED, None
    return None, None


class _WarningCollector:
    """按首次出现顺序去重收集 soft evidence warning。"""

    def __init__(self) -> None:
        self.collected: list[RoutingWarning] = []
        self._seen: set[tuple[str, str, str, ObjectiveDimension | None]] = set()

    def add(
        self,
        variant: AgentVariant,
        code: str,
        dimension: ObjectiveDimension,
    ) -> None:
        key = (
            code,
            variant.variant_id,
            variant.version,
            dimension,
        )
        if key in self._seen:
            return
        self._seen.add(key)
        self.collected.append(
            RoutingWarning(
                code=code,
                variant_id=variant.variant_id,
                variant_version=variant.version,
                dimension=dimension,
            )
        )


def _objective_value(
    variant: AgentVariant,
    dimension: ObjectiveDimension,
    evidence: RoutingEvidence,
    as_of: datetime,
    references: dict[tuple[str, str], list[EvidenceSnapshotReference]],
    warnings: _WarningCollector,
) -> Decimal | None:
    """读取一个排序维度值；缺失/过期产生显式 warning 并保持缺失。"""
    if dimension is ObjectiveDimension.COST:
        pricing = evidence.latest_pricing(variant.variant_id, variant.version)
        if pricing is None:
            warnings.add(variant, WARNING_SOFT_EVIDENCE_MISSING, dimension)
            return None
        if pricing.effective_at > as_of:
            warnings.add(variant, WARNING_SOFT_EVIDENCE_MISSING, dimension)
            return None
        if pricing.valid_until < as_of:
            warnings.add(variant, WARNING_SOFT_EVIDENCE_STALE, dimension)
            return None
        _record_reference(
            references, variant, _EVIDENCE_PRICING, pricing.version, pricing.valid_until
        )
        return pricing.input_price_per_mtok

    model = evidence.latest_model_evidence(variant.variant_id, variant.version)
    if model is None:
        warnings.add(variant, WARNING_SOFT_EVIDENCE_MISSING, dimension)
        return None
    if model.valid_until < as_of:
        warnings.add(variant, WARNING_SOFT_EVIDENCE_STALE, dimension)
        return None
    _record_reference(
        references, variant, _EVIDENCE_MODEL_EVIDENCE, model.version, model.valid_until
    )
    if dimension is ObjectiveDimension.QUALITY:
        return Decimal(str(model.quality_score))
    if dimension is ObjectiveDimension.STABILITY:
        return Decimal(str(model.stability_score))
    if model.latency_ms_p50 is None:
        warnings.add(variant, WARNING_SOFT_EVIDENCE_MISSING, dimension)
        return None
    return Decimal(str(model.latency_ms_p50))


def _ordering_comparator(
    objectives: tuple[RoutingObjective, ...],
    readings: dict[tuple[str, str], dict[ObjectiveDimension, Decimal | None]],
):
    def compare(
        left: ModelCatalogEntry, right: ModelCatalogEntry
    ) -> int:
        left_values = readings[left.variant.identity]
        right_values = readings[right.variant.identity]
        for objective in objectives:
            result = _compare_objective(
                left_values.get(objective.dimension),
                right_values.get(objective.dimension),
                objective,
            )
            if result != 0:
                return result
        if left.variant.identity < right.variant.identity:
            return -1
        if left.variant.identity > right.variant.identity:
            return 1
        return 0

    return compare


def _compare_objective(
    left: Decimal | None,
    right: Decimal | None,
    objective: RoutingObjective,
) -> int:
    if left is None and right is None:
        return 0
    if left is None:
        return (
            1
            if objective.missing_value is MissingValuePolicy.ORDER_LAST
            else -1
        )
    if right is None:
        return (
            -1
            if objective.missing_value is MissingValuePolicy.ORDER_LAST
            else 1
        )
    if left == right:
        return 0
    ascending = left < right
    if objective.direction is ObjectiveDirection.MINIMIZE:
        return -1 if ascending else 1
    return 1 if ascending else -1


def _tie_break_decided(
    ranked: list[ModelCatalogEntry],
    readings: dict[tuple[str, str], dict[ObjectiveDimension, Decimal | None]],
) -> bool:
    """前两名在全部排序维度上并列时，由稳定身份 tie-break 决出。"""
    if len(ranked) < 2:
        return False
    first = readings[ranked[0].variant.identity]
    second = readings[ranked[1].variant.identity]
    for dimension, value in first.items():
        if second.get(dimension) != value:
            return False
    return True


def _record_reference(
    references: dict[tuple[str, str], list[EvidenceSnapshotReference]],
    variant: AgentVariant,
    kind: str,
    snapshot_version: str,
    valid_until: datetime,
) -> None:
    references.setdefault(variant.identity, []).append(
        EvidenceSnapshotReference(
            kind=kind,
            variant_id=variant.variant_id,
            variant_version=variant.version,
            snapshot_version=snapshot_version,
            valid_until=valid_until,
        )
    )


def _dedupe_references(
    references,
) -> tuple[EvidenceSnapshotReference, ...]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[EvidenceSnapshotReference] = []
    for reference in references:
        key = (reference.kind, reference.variant_id, reference.snapshot_version)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(reference)
    return tuple(deduped)
