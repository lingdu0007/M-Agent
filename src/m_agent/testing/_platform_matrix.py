"""0.5 Runtime Foundation 的冻结跨平台矩阵与诚实证据聚合（Ticket 22）。

冻结声明（``FOUNDATION_PLATFORM_MATRIX_0_5``）：

- Linux（CPython 官方支持范围内最低到最高版本 3.11–3.14）的
  ``CONTRACT`` 证据是 required 门槛；
- Linux 3.11 是 primary HOST 平台（发布 profile 的完整 HOST 证据默认
  在这里执行）；
- macOS（darwin）最低（3.11）与最高（3.14）支持 Python 以 secondary
  HOST 角色覆盖；
- Windows 不在声明范围内——绝不伪装成支持平台。

``PlatformMatrixObservation`` 记录单一 (platform, python, level, role)
组合的证据状态：PASS 必须携带真实 sha256 证据 digest 与同一 RC 的
artifact digest；无法覆盖的平台以诚实 ``NOT_RUN`` 记录并说明证据来源，
且 NOT_RUN 观察绝不携带伪造的证据 digest。

``PlatformMatrixEvidence`` 聚合一次发布尝试的全部观察：每个冻结矩阵
条目必须恰好有一条观察、全部观察绑定同一 artifact digest（不同 RC 的
观察不能拼接救场）、任何非 PASS 条目都成为 gap。整体状态只有在全部
条目 PASS 时才是 ``PASS``；存在 NOT_RUN 时是 ``INCOMPLETE``；观察到的
FAIL/ERROR 是 ``FAILED``。本模块不发起任何网络请求，也不把 CONTRACT /
HOST 结果表述为 live 或 FIELD 结论。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, model_validator

from ._pack import AcceptanceCheckStatus, EvidenceLevel

__all__ = [
    "FOUNDATION_PLATFORM_MATRIX_0_5",
    "PlatformMatrixEntry",
    "PlatformMatrixEvidence",
    "PlatformMatrixObservation",
]

_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PYTHON_VERSION = re.compile(r"3\.[0-9]{1,2}\Z")
_ROLES = frozenset({"required", "primary", "secondary"})


class PlatformMatrixEntry(BaseModel, frozen=True):
    """One frozen requirement cell of the 0.5 cross-platform matrix."""

    model_config = ConfigDict(extra="forbid")

    platform: str
    python: str
    level: EvidenceLevel
    role: str

    @model_validator(mode="after")
    def _validate_entry(self) -> "PlatformMatrixEntry":
        if self.platform not in {"linux", "darwin"}:
            raise ValueError("platform matrix only declares linux and darwin")
        if not _PYTHON_VERSION.fullmatch(self.python):
            raise ValueError("python must be a supported minor version like 3.11")
        if self.role not in _ROLES:
            raise ValueError("role must be required, primary, or secondary")
        if self.level is EvidenceLevel.CONTRACT and self.role != "required":
            raise ValueError("CONTRACT evidence is a required gate, not a role")
        if self.level is EvidenceLevel.HOST and self.role == "required":
            raise ValueError("HOST evidence is primary or secondary, not required")
        return self

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.platform, self.python, self.level.value, self.role)


#: 0.5 发布的冻结跨平台矩阵（ADR 0042 / Ticket 22）。
FOUNDATION_PLATFORM_MATRIX_0_5: tuple[PlatformMatrixEntry, ...] = (
    PlatformMatrixEntry(platform="linux", python="3.11", level=EvidenceLevel.CONTRACT, role="required"),
    PlatformMatrixEntry(platform="linux", python="3.12", level=EvidenceLevel.CONTRACT, role="required"),
    PlatformMatrixEntry(platform="linux", python="3.13", level=EvidenceLevel.CONTRACT, role="required"),
    PlatformMatrixEntry(platform="linux", python="3.14", level=EvidenceLevel.CONTRACT, role="required"),
    PlatformMatrixEntry(platform="linux", python="3.11", level=EvidenceLevel.HOST, role="primary"),
    PlatformMatrixEntry(platform="darwin", python="3.11", level=EvidenceLevel.HOST, role="secondary"),
    PlatformMatrixEntry(platform="darwin", python="3.14", level=EvidenceLevel.HOST, role="secondary"),
)


class PlatformMatrixObservation(BaseModel, frozen=True):
    """One honest evidence record for exactly one matrix cell."""

    model_config = ConfigDict(extra="forbid")

    platform: str
    python: str
    level: EvidenceLevel
    role: str
    status: AcceptanceCheckStatus
    evidence_source: str
    evidence_digest: str
    artifact_digest: str

    @model_validator(mode="after")
    def _validate_observation(self) -> "PlatformMatrixObservation":
        if not self.evidence_source.strip():
            raise ValueError("evidence_source must explain where the evidence came from")
        if self.status is AcceptanceCheckStatus.NOT_RUN:
            if self.evidence_digest:
                raise ValueError(
                    "a NOT_RUN observation must not claim a fabricated evidence digest"
                )
        elif not _SHA256_DIGEST.fullmatch(self.evidence_digest):
            raise ValueError("an executed observation must carry a sha256 evidence digest")
        if not _SHA256_DIGEST.fullmatch(self.artifact_digest):
            raise ValueError("artifact_digest must be the release candidate wheel digest")
        return self

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.platform, self.python, self.level.value, self.role)


class PlatformMatrixEvidence(BaseModel, frozen=True):
    """Aggregated honest verdict for one release candidate's platform matrix."""

    model_config = ConfigDict(extra="forbid")

    observations: tuple[PlatformMatrixObservation, ...]
    gaps: tuple[PlatformMatrixEntry, ...]
    overall_status: str

    @classmethod
    def create(
        cls, *, observations: tuple[PlatformMatrixObservation, ...]
    ) -> "PlatformMatrixEvidence":
        """Aggregate observations against the frozen matrix, honestly.

        - 每个观察必须对应冻结矩阵的一个条目，且每个条目恰好一条观察；
        - 所有观察必须绑定同一 RC artifact digest（异质 RC 证据被拒绝）；
        - gap 是所有非 PASS 条目；全部 PASS 才是 ``PASS``，存在
          ``NOT_RUN`` 是 ``INCOMPLETE``，观察到 FAIL/ERROR 是 ``FAILED``。
        """
        observations = tuple(observations)
        if not observations:
            raise ValueError("platform matrix evidence requires observations")
        by_key: dict[tuple[str, str, str, str], PlatformMatrixObservation] = {}
        for observation in observations:
            if observation.key in by_key:
                raise ValueError(
                    f"duplicate observation for matrix cell {observation.key}"
                )
            by_key[observation.key] = observation
        unknown = set(by_key) - {entry.key for entry in FOUNDATION_PLATFORM_MATRIX_0_5}
        if unknown:
            raise ValueError(f"observations outside the frozen matrix: {sorted(unknown)}")
        missing = [
            entry.key
            for entry in FOUNDATION_PLATFORM_MATRIX_0_5
            if entry.key not in by_key
        ]
        if missing:
            raise ValueError(f"missing required matrix observations: {missing}")
        artifact_digests = {
            observation.artifact_digest for observation in observations
        }
        if len(artifact_digests) != 1:
            raise ValueError(
                "platform matrix observations must attest one release candidate"
            )
        gaps = tuple(
            entry
            for entry in FOUNDATION_PLATFORM_MATRIX_0_5
            if by_key[entry.key].status is not AcceptanceCheckStatus.PASS
        )
        statuses = {observation.status for observation in observations}
        if not gaps:
            overall_status = "PASS"
        elif statuses & {
            AcceptanceCheckStatus.FAIL,
            AcceptanceCheckStatus.ERROR,
        }:
            overall_status = "FAILED"
        else:
            overall_status = "INCOMPLETE"
        return cls(
            observations=observations,
            gaps=gaps,
            overall_status=overall_status,
        )

    @model_validator(mode="after")
    def _validate_evidence(self) -> "PlatformMatrixEvidence":
        if self.overall_status not in {"PASS", "INCOMPLETE", "FAILED"}:
            raise ValueError("overall_status must be PASS, INCOMPLETE, or FAILED")
        return self
