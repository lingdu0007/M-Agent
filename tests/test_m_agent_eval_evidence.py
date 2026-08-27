"""Ticket 17 AC 6：三类离线 Evidence Adapter 的统一只读 contract。

append-only journal、content-addressed snapshot 与 sentinel 三类 Adapter
共享同一份行为契约套件（m_agent.testing.EvidenceAdapterContractMixin），
完整保留 schema、provenance、collection time、subject 与 integrity；
证据缺失返回 None（缺失绝不解释为外部效果不存在），读取不改写来源。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from m_agent.companion.eval import (
    AppendOnlyJournalEvidenceAdapter,
    evidence_digest,
    ContentAddressedSnapshotEvidenceAdapter,
    EvidenceIntegrityError,
    SentinelEvidenceAdapter,
)
from m_agent.testing import EvidenceAdapterContractMixin

_SUBJECT = "run-1"


class AppendOnlyJournalAdapterContractTests(
    EvidenceAdapterContractMixin, unittest.IsolatedAsyncioTestCase
):
    """append-only journal Adapter 的共享契约 + 专属负例。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp)
        self.journal_path = self._tmp / "ledger.jsonl"

    def make_adapter(self):  # noqa: ANN201
        return AppendOnlyJournalEvidenceAdapter(
            self.journal_path,
            artifact_id="ledger",
            schema_name="ledger-entries",
            schema_version="1.0",
        )

    def source_paths(self):  # noqa: ANN201
        return (self.journal_path,)

    def seed_subject_evidence(self, subject_ref: str) -> None:
        lines = [
            {
                "subject_ref": subject_ref,
                "seq": 1,
                "entry": "ticket updated",
            },
            {
                "subject_ref": "run-other",
                "seq": 2,
                "entry": "unrelated subject line",
            },
        ]
        self.journal_path.write_text(
            "".join(json.dumps(line) + "\n" for line in lines),
            encoding="utf-8",
        )

    async def test_payload_contains_only_requested_subject_lines(self) -> None:
        self.seed_subject_evidence(_SUBJECT)
        artifact = await self.make_adapter().collect(_SUBJECT)
        self.assertIsNotNone(artifact)
        entries = json.loads(artifact.payload)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["subject_ref"], _SUBJECT)
        self.assertEqual(entries[0]["entry"], "ticket updated")

    async def test_malformed_journal_line_fails_closed(self) -> None:
        self.journal_path.write_text("not-json\n", encoding="utf-8")
        with self.assertRaises(EvidenceIntegrityError):
            await self.make_adapter().collect(_SUBJECT)

    async def test_line_without_subject_field_fails_closed(self) -> None:
        self.journal_path.write_text(
            json.dumps({"seq": 1, "entry": "no subject"}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(EvidenceIntegrityError):
            await self.make_adapter().collect(_SUBJECT)


class ContentAddressedSnapshotAdapterContractTests(
    EvidenceAdapterContractMixin, unittest.IsolatedAsyncioTestCase
):
    """content-addressed snapshot Adapter 的共享契约 + 专属负例。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp)
        self.index_path = self._tmp / "index.json"
        self._content = '{"orders": [{"id": 7, "status": "shipped"}]}'

    def make_adapter(self):  # noqa: ANN201
        return ContentAddressedSnapshotEvidenceAdapter(
            self.index_path,
            artifact_id="order-snapshot",
            schema_name="order-snapshot",
            schema_version="1.0",
        )

    def source_paths(self):  # noqa: ANN201
        return (
            self.index_path,
            *sorted(p for p in self._tmp.iterdir() if p.suffix == ".json"),
        )

    def seed_subject_evidence(self, subject_ref: str) -> None:
        digest = evidence_digest(
            "order-snapshot", subject_ref, self._content
        )
        (self._tmp / f"{digest}.json").write_text(
            self._content, encoding="utf-8"
        )
        self.index_path.write_text(
            json.dumps(
                {
                    "artifacts": {
                        "order-snapshot": {
                            "subject_ref": subject_ref,
                            "digest": digest,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    async def test_unknown_artifact_is_missing_evidence(self) -> None:
        self.index_path.write_text(
            json.dumps({"artifacts": {}}), encoding="utf-8"
        )
        self.assertIsNone(await self.make_adapter().collect(_SUBJECT))

    async def test_indexed_content_file_missing_fails_closed(self) -> None:
        self.seed_subject_evidence(_SUBJECT)
        for path in self._tmp.glob("*.json"):
            if path != self.index_path:
                path.unlink()
        with self.assertRaises(EvidenceIntegrityError):
            await self.make_adapter().collect(_SUBJECT)

    async def test_tampered_snapshot_content_fails_closed(self) -> None:
        self.seed_subject_evidence(_SUBJECT)
        for path in self._tmp.glob("*.json"):
            if path != self.index_path:
                path.write_text(
                    '{"orders": [{"id": 7, "status": "refunded"}]}',
                    encoding="utf-8",
                )
        with self.assertRaises(EvidenceIntegrityError):
            await self.make_adapter().collect(_SUBJECT)


class SentinelEvidenceAdapterContractTests(
    EvidenceAdapterContractMixin, unittest.IsolatedAsyncioTestCase
):
    """sentinel Adapter 的共享契约 + 专属负例。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self._tmp)
        self.sentinel_path = self._tmp / "notify.sentinel"

    def make_adapter(self):  # noqa: ANN201
        return SentinelEvidenceAdapter(
            self.sentinel_path,
            artifact_id="notify-sentinel",
            schema_name="effect-sentinel",
            schema_version="1.0",
        )

    def source_paths(self):  # noqa: ANN201
        return (self.sentinel_path,)

    def seed_subject_evidence(self, subject_ref: str) -> None:
        self.sentinel_path.write_text(
            json.dumps(
                {
                    "subject_ref": subject_ref,
                    "effect": "notification dispatched",
                    "written_at": "2026-08-25T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

    async def test_sentinel_for_other_subject_is_missing(self) -> None:
        self.seed_subject_evidence("run-other")
        self.assertIsNone(await self.make_adapter().collect(_SUBJECT))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
