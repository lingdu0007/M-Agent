"""最小授权 Observation Projection（ADR 0029 / Ticket 17 AC 5）。

每个 Evaluator 显式声明 Evidence Requirements；版本化授权策略只放行
所需且获授权的字段。Projection 只携带已交付字段的值——Evaluator 无法
绕过 Projection 读取私有 Store schema 或未授权 payload；证据缺失或
未授权一律表现为 INCONCLUSIVE，而非「问题不存在」。
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._case import EvaluatorRef
from ._evidence import EvidenceArtifact
from ._observation import EvalObservation, EvidenceCompleteness

__all__ = [
    "EvidenceField",
    "EvidenceRequirements",
    "ObservationProjection",
    "ObservationProjectionPolicy",
    "project_observation",
]


class EvidenceField(str, enum.Enum):
    """Projection 可交付的稳定证据字段。"""

    RUN_STATUS = "RUN_STATUS"
    RUN_ERROR_CODE = "RUN_ERROR_CODE"
    RUN_INPUT = "RUN_INPUT"
    RUN_OUTPUT = "RUN_OUTPUT"
    CONVERSATION_HISTORY = "CONVERSATION_HISTORY"
    STEP_TYPES = "STEP_TYPES"
    MODEL_USAGE = "MODEL_USAGE"
    EXTERNAL_EVIDENCE = "EXTERNAL_EVIDENCE"

    def __str__(self) -> str:  # pragma: no cover - 便捷展示
        return self.value


class EvidenceRequirements(BaseModel):
    """Evaluator 声明的最小证据需求（显式、可审计）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluator: EvaluatorRef
    fields: frozenset[EvidenceField] = Field(min_length=1)
    #: 需要的外部 Evidence Artifact id（配合 EXTERNAL_EVIDENCE 字段）。
    external_evidence_ids: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _external_ids_need_field(self) -> "EvidenceRequirements":
        if self.external_evidence_ids and (
            EvidenceField.EXTERNAL_EVIDENCE not in self.fields
        ):
            raise ValueError(
                "external_evidence_ids require the EXTERNAL_EVIDENCE field"
            )
        return self


class ObservationProjectionPolicy(BaseModel):
    """版本化授权策略：哪些字段与外部证据 id 允许交付给 Evaluator。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    allowed_fields: frozenset[EvidenceField] = Field(min_length=1)
    authorized_evidence_ids: frozenset[str] = frozenset()


class ObservationProjection(BaseModel):
    """Evaluator 可见的只读最小视图。

    ``values`` 只包含 delivered 字段；``denied``（未授权）与
    ``unavailable``（授权但证据缺失）显式记录，供 harness 归一为
    INCONCLUSIVE。本对象不携带 Observation、Store 或 payload 引用。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str
    completeness: EvidenceCompleteness
    reason_code: str
    delivered: frozenset[EvidenceField]
    denied: frozenset[EvidenceField]
    unavailable: frozenset[EvidenceField]
    values: dict[EvidenceField, Any] = Field(default_factory=dict)
    denied_evidence_ids: frozenset[str] = frozenset()
    missing_evidence_ids: frozenset[str] = frozenset()

    def get(self, field: EvidenceField) -> Any | None:
        """读取已交付字段的值；未交付字段一律 None（绝不泄漏）。"""
        return self.values.get(field)


def project_observation(
    observation: EvalObservation,
    requirements: EvidenceRequirements,
    policy: ObservationProjectionPolicy,
) -> ObservationProjection:
    """依据 Requirements ∧ Policy 构造最小授权投影。

    只交付「所需、获授权且证据可用」的字段；其余进入 denied（未授权）
    或 unavailable（授权但缺失），由 Evaluator harness 归一为
    INCONCLUSIVE，绝不降级为 subject FAIL。
    """
    delivered: dict[EvidenceField, Any] = {}
    denied: set[EvidenceField] = set()
    unavailable: set[EvidenceField] = set()
    for field in sorted(requirements.fields, key=lambda item: item.value):
        if field not in policy.allowed_fields:
            denied.add(field)
            continue
        value = _field_value(observation, field)
        if value is None:
            unavailable.add(field)
            continue
        delivered[field] = value
    denied_ids: set[str] = set()
    missing_ids: set[str] = set()
    if EvidenceField.EXTERNAL_EVIDENCE in requirements.fields:
        artifacts = {
            artifact.artifact_id: artifact
            for artifact in observation.external_evidence
            if isinstance(artifact, EvidenceArtifact)
        }
        for artifact_id in sorted(requirements.external_evidence_ids):
            if artifact_id not in policy.authorized_evidence_ids:
                denied_ids.add(artifact_id)
                continue
            if artifact_id not in artifacts:
                missing_ids.add(artifact_id)
        if (
            EvidenceField.EXTERNAL_EVIDENCE in delivered
            and (denied_ids or missing_ids)
        ):
            # 部分外部证据缺失/未授权：整体字段不可用，明确归因。
            delivered.pop(EvidenceField.EXTERNAL_EVIDENCE)
            unavailable.add(EvidenceField.EXTERNAL_EVIDENCE)
    return ObservationProjection(
        observation_id=observation.observation_id,
        completeness=observation.completeness,
        reason_code=observation.reason_code,
        delivered=frozenset(delivered),
        denied=frozenset(denied),
        unavailable=frozenset(unavailable),
        values=delivered,
        denied_evidence_ids=frozenset(denied_ids),
        missing_evidence_ids=frozenset(missing_ids),
    )


def _field_value(
    observation: EvalObservation, field: EvidenceField
) -> Any | None:
    if field is EvidenceField.RUN_STATUS:
        return observation.run_status
    if field is EvidenceField.RUN_ERROR_CODE:
        return observation.error_code
    if field is EvidenceField.RUN_INPUT:
        return observation.run_input
    if field is EvidenceField.RUN_OUTPUT:
        return observation.run_output
    if field is EvidenceField.CONVERSATION_HISTORY:
        return observation.conversation_history
    if field is EvidenceField.STEP_TYPES:
        return observation.step_types
    if field is EvidenceField.MODEL_USAGE:
        return observation.usage
    if field is EvidenceField.EXTERNAL_EVIDENCE:
        return tuple(observation.external_evidence) or None
    return None  # pragma: no cover - 未知字段按缺失处理
