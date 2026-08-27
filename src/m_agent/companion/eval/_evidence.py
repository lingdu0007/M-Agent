"""离线 Evidence Adapter：统一只读 contract 与三类参考实现（ADR 0029）。

Evidence Adapter 把 Run Store 外部的只读证据归一化为 immutable
Evidence Artifact，完整保留 schema、provenance、collection time、
subject 与 integrity digest。证据缺失返回 None——缺失绝不能被解释为
「外部效果不存在」；来源被篡改/自相矛盾抛
:class:`EvidenceIntegrityError`（fail-closed，不静默降级）。

Ticket 17 提供三类离线参考 Adapter：append-only journal、
content-addressed snapshot 与 sentinel。它们与共享行为契约套件
（:mod:`m_agent.testing` 的 EvidenceAdapterContractMixin）一起，
供垂直团队构建 FIELD Adapter 时复用。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..._steps import utc_now
from ._errors import EvidenceIntegrityError
from ._identity import canonical_json, evidence_digest

__all__ = [
    "ADAPTER_KIND_APPEND_ONLY_JOURNAL",
    "ADAPTER_KIND_CONTENT_ADDRESSED_SNAPSHOT",
    "ADAPTER_KIND_SENTINEL",
    "AppendOnlyJournalEvidenceAdapter",
    "ContentAddressedSnapshotEvidenceAdapter",
    "EvidenceAdapter",
    "EvidenceArtifact",
    "EvidenceIntegrityError",
    "SentinelEvidenceAdapter",
    "evidence_digest",
]

ADAPTER_KIND_APPEND_ONLY_JOURNAL = "APPEND_ONLY_JOURNAL"
ADAPTER_KIND_CONTENT_ADDRESSED_SNAPSHOT = "CONTENT_ADDRESSED_SNAPSHOT"
ADAPTER_KIND_SENTINEL = "SENTINEL"


class EvidenceArtifact(BaseModel):
    """从外部来源只读收集并冻结的版本化评估证据。

    ``digest`` 绑定 artifact 身份与 payload 内容；``verify`` 在读取后
    任意时点复核完整性（检测冻结后篡改）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: str = Field(min_length=1)
    subject_ref: str
    schema_name: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    adapter_kind: str = Field(min_length=1)
    source: str = Field(min_length=1)
    collected_at: datetime = Field(default_factory=utc_now)
    payload: str
    digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def _verify_digest(self) -> "EvidenceArtifact":
        expected = evidence_digest(
            self.artifact_id, self.subject_ref, self.payload
        )
        if self.digest != expected:
            raise EvidenceIntegrityError(
                f"evidence artifact {self.artifact_id!r} digest mismatch: "
                "content was tampered after freezing"
            )
        return self

    def verify(self) -> None:
        """复核冻结证据的完整性；被篡改抛 :class:`EvidenceIntegrityError`。"""
        expected = evidence_digest(
            self.artifact_id, self.subject_ref, self.payload
        )
        if self.digest != expected:
            raise EvidenceIntegrityError(
                f"evidence artifact {self.artifact_id!r} digest mismatch"
            )


@runtime_checkable
class EvidenceAdapter(Protocol):
    """只读 Evidence Adapter 契约。

    实现不得写入、改名或删除任何来源文件；``collect`` 幂等且对同一
    来源状态返回相同 Artifact。返回 None 表示「证据缺失」——绝不
    表示「外部效果不存在」。
    """

    async def collect(self, subject_ref: str) -> EvidenceArtifact | None: ...


class AppendOnlyJournalEvidenceAdapter:
    """append-only journal Adapter（JSONL 外部账本/日志的只读投影）。

    每行必须是带 subject 字段的 JSON 对象；只返回请求 subject 的行。
    行损坏或缺少 subject 字段视为来源完整性破坏，fail-closed 抛错。
    """

    def __init__(
        self,
        journal_path: str | Path,
        *,
        artifact_id: str,
        schema_name: str,
        schema_version: str,
        subject_field: str = "subject_ref",
    ) -> None:
        self._path = Path(journal_path)
        self._artifact_id = artifact_id
        self._schema_name = schema_name
        self._schema_version = schema_version
        self._subject_field = subject_field

    async def collect(self, subject_ref: str) -> EvidenceArtifact | None:
        if not self._path.exists():
            return None
        subject_lines: list[dict] = []
        with self._path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EvidenceIntegrityError(
                        f"journal {str(self._path)!r} has a malformed line"
                    ) from exc
                if not isinstance(entry, dict) or (
                    self._subject_field not in entry
                ):
                    raise EvidenceIntegrityError(
                        f"journal {str(self._path)!r} line lacks the "
                        f"{self._subject_field!r} subject field"
                    )
                if entry[self._subject_field] == subject_ref:
                    subject_lines.append(entry)
        if not subject_lines:
            # 该 subject 无任何记录：证据缺失（None），绝不产出一个
            # 会误导为「无事件发生」的空 payload Artifact。
            return None
        payload = canonical_json(subject_lines)
        return EvidenceArtifact(
            artifact_id=self._artifact_id,
            subject_ref=subject_ref,
            schema_name=self._schema_name,
            schema_version=self._schema_version,
            adapter_kind=ADAPTER_KIND_APPEND_ONLY_JOURNAL,
            source=str(self._path),
            payload=payload,
            digest=evidence_digest(self._artifact_id, subject_ref, payload),
        )


class ContentAddressedSnapshotEvidenceAdapter:
    """content-addressed snapshot Adapter（内容寻址快照的只读读取）。

    index.json 把 artifact_id 映射到 {subject_ref, digest}；内容文件
    以 digest 命名（``<digest>.json``）。读取时按同一公式复核：
    index 有记录但文件缺失、或内容摘要与 index 不一致，都是完整性
    破坏，fail-closed 抛错；index 无记录则返回 None（证据缺失）。
    """

    def __init__(
        self,
        index_path: str | Path,
        *,
        artifact_id: str,
        schema_name: str,
        schema_version: str,
    ) -> None:
        self._index_path = Path(index_path)
        self._artifact_id = artifact_id
        self._schema_name = schema_name
        self._schema_version = schema_version

    async def collect(self, subject_ref: str) -> EvidenceArtifact | None:
        if not self._index_path.exists():
            return None
        try:
            index = json.loads(self._index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise EvidenceIntegrityError(
                f"snapshot index {str(self._index_path)!r} is malformed"
            ) from exc
        entry = index.get("artifacts", {}).get(self._artifact_id)
        if entry is None:
            return None
        if entry.get("subject_ref") != subject_ref:
            return None
        digest = entry.get("digest")
        content_path = self._index_path.parent / f"{digest}.json"
        if not content_path.exists():
            raise EvidenceIntegrityError(
                f"snapshot index references missing content file "
                f"{str(content_path)!r}"
            )
        payload = content_path.read_text(encoding="utf-8")
        if evidence_digest(self._artifact_id, subject_ref, payload) != digest:
            raise EvidenceIntegrityError(
                f"snapshot content for {self._artifact_id!r} does not "
                "match its content-addressed digest"
            )
        return EvidenceArtifact(
            artifact_id=self._artifact_id,
            subject_ref=subject_ref,
            schema_name=self._schema_name,
            schema_version=self._schema_version,
            adapter_kind=ADAPTER_KIND_CONTENT_ADDRESSED_SNAPSHOT,
            source=str(content_path),
            payload=payload,
            digest=digest,
        )


class SentinelEvidenceAdapter:
    """sentinel Adapter（一次性外部效果标记的只读读取）。

    sentinel 文件是外部效果发生后写入的标记；文件缺失 => None
    （证据缺失，绝不等于「效果未发生」）；subject 不匹配 => None；
    内容损坏 => fail-closed 抛错。
    """

    def __init__(
        self,
        sentinel_path: str | Path,
        *,
        artifact_id: str,
        schema_name: str,
        schema_version: str,
    ) -> None:
        self._path = Path(sentinel_path)
        self._artifact_id = artifact_id
        self._schema_name = schema_name
        self._schema_version = schema_version

    async def collect(self, subject_ref: str) -> EvidenceArtifact | None:
        if not self._path.exists():
            return None
        payload = self._path.read_text(encoding="utf-8")
        try:
            marker = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise EvidenceIntegrityError(
                f"sentinel {str(self._path)!r} is malformed"
            ) from exc
        if not isinstance(marker, dict) or (
            marker.get("subject_ref") != subject_ref
        ):
            return None
        return EvidenceArtifact(
            artifact_id=self._artifact_id,
            subject_ref=subject_ref,
            schema_name=self._schema_name,
            schema_version=self._schema_version,
            adapter_kind=ADAPTER_KIND_SENTINEL,
            source=str(self._path),
            payload=payload,
            digest=evidence_digest(self._artifact_id, subject_ref, payload),
        )
