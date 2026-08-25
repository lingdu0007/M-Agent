"""Evidence Adapter 共享行为契约套件（Ticket 17 AC 6）。

append-only journal、content-addressed snapshot 与 sentinel 三类离线
Evidence Adapter（以及垂直团队的 FIELD Adapter）共享同一份实现无关
行为契约。绑定方式：实现 ``unittest.IsolatedAsyncioTestCase`` 子类并
继承本 Mixin，提供 ``make_adapter()``、``source_paths()`` 与
``seed_subject_evidence(subject_ref)``。

契约只通过 EvidenceAdapter 公开接口（collect）与 EvidenceArtifact 公开
字段驱动，不触碰实现细节。
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from typing import TYPE_CHECKING

from ..companion.eval import EvidenceArtifact, EvidenceIntegrityError

__all__ = ["EvidenceAdapterContractMixin"]


if TYPE_CHECKING:
    # 静态检查视角：Mixin 的 assert* 断言来自 TestCase 绑定基类；
    # 运行时保持纯 Mixin（object 基类），pytest 不单独收集本类，
    # 只有绑定 IsolatedAsyncioTestCase 的子类才会运行契约用例。
    _ContractBase = unittest.TestCase
else:
    _ContractBase = object


class EvidenceAdapterContractMixin(_ContractBase):
    """三类离线 Evidence Adapter 的统一只读行为契约。

    覆盖：schema/provenance/collection time/subject/integrity 的完整
    保留；证据缺失返回 None（缺失绝不等于「外部效果不存在」）；subject
    作用域隔离；读取不改写来源且幂等；冻结 Artifact 的篡改检测。
    """

    def make_adapter(self):  # pragma: no cover - 由绑定类提供
        raise NotImplementedError

    def source_paths(self) -> tuple[Path, ...]:  # pragma: no cover
        raise NotImplementedError

    def seed_subject_evidence(self, subject_ref: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _source_digest(self) -> str:
        digest = hashlib.sha256()
        for path in self.source_paths():
            if path.exists():
                digest.update(path.read_bytes())
        return digest.hexdigest()

    async def test_collect_preserves_contract_fields(self) -> None:
        adapter = self.make_adapter()
        self.seed_subject_evidence("subject-a")
        artifact = await adapter.collect("subject-a")
        self.assertIsNotNone(artifact)
        self.assertIsInstance(artifact, EvidenceArtifact)
        self.assertEqual(artifact.subject_ref, "subject-a")
        self.assertTrue(artifact.schema_name)
        self.assertTrue(artifact.schema_version)
        self.assertTrue(artifact.adapter_kind)
        self.assertTrue(artifact.source)
        self.assertIsNotNone(artifact.collected_at)
        self.assertTrue(artifact.payload)
        self.assertRegex(artifact.digest, r"^[0-9a-f]{64}$")
        artifact.verify()

    async def test_missing_evidence_returns_none_not_absence(self) -> None:
        adapter = self.make_adapter()
        # 来源没有任何该 subject 的证据：None = 证据缺失，
        # 绝不能被解释为「外部效果不存在」。
        self.assertIsNone(await adapter.collect("subject-a"))

    async def test_collect_is_subject_scoped(self) -> None:
        adapter = self.make_adapter()
        self.seed_subject_evidence("subject-b")
        self.assertIsNone(await adapter.collect("subject-a"))

    async def test_collect_is_read_only_and_repeatable(self) -> None:
        adapter = self.make_adapter()
        self.seed_subject_evidence("subject-a")
        before = self._source_digest()
        first = await adapter.collect("subject-a")
        second = await adapter.collect("subject-a")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        # 同一来源状态 => 同一证据内容与完整性摘要（collected_at 是
        # 每次读取的元数据，不参与幂等比较）。
        self.assertEqual(first.payload, second.payload)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(first.subject_ref, second.subject_ref)
        first.verify()
        second.verify()
        # 读取不改写来源（只读 contract 的可复现证据）。
        self.assertEqual(self._source_digest(), before)

    def test_frozen_artifact_detects_payload_tampering(self) -> None:
        import asyncio

        adapter = self.make_adapter()
        self.seed_subject_evidence("subject-a")
        artifact = asyncio.run(adapter.collect("subject-a"))
        self.assertIsNotNone(artifact)
        tampered = artifact.model_copy(update={"payload": "tampered"})
        with self.assertRaises(EvidenceIntegrityError):
            tampered.verify()

