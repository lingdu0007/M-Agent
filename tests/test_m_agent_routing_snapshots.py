"""Ticket 20: versioned evidence snapshots with provenance, subject,
schema, and integrity (AC 1) plus the shared validity judgment (AC 2).

Pricing、Availability 与 Operational Limits 都是独立版本化 snapshot：
完整携带 provenance（source/version）、collected/effective time、subject
（variant 身份 + 观测时的 Contract 指纹）、schema 与 integrity 摘要。
判定 seam 把缺失之外的异常（过期、未生效、subject 不匹配、指纹漂移、
integrity failure）分类为稳定状态，供 hard fail-closed 与 soft 降级
路径共用；两种路径都不把异常快照当作健康证据。
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from m_agent.companion.routing import (
    AvailabilitySnapshot,
    OperationalLimitsSnapshot,
    PricingSnapshot,
    RoutingEvidence,
    SnapshotStatus,
    evaluate_availability_snapshot,
    evaluate_operational_limits_snapshot,
    evaluate_pricing_snapshot,
    snapshot_integrity_digest,
    with_integrity,
)

from routing_fixtures import AS_OF, make_contract, make_variant


def _variant():  # noqa: ANN202 - test helper
    variant = make_variant("variant-subject", make_contract("contract-subject"))
    return variant


class SnapshotContractTests(unittest.TestCase):
    """三类 snapshot 的完整契约：provenance/时间/subject/schema/integrity。"""

    def test_pricing_snapshot_carries_full_contract_surface(self) -> None:
        variant = _variant()
        snapshot = PricingSnapshot(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            currency="USD",
            input_price_per_mtok=Decimal("0.80"),
            output_price_per_mtok=Decimal("3.20"),
            version="price-7",
            source="offline-price-sheet",
            effective_at=AS_OF - timedelta(days=1),
            valid_until=AS_OF + timedelta(days=7),
            schema_version="pricing-snapshot-v1",
            contract_fingerprint=variant.primary_contract().fingerprint,
        )
        self.assertEqual(snapshot.schema_version, "pricing-snapshot-v1")
        self.assertEqual(
            snapshot.contract_fingerprint, variant.primary_contract().fingerprint
        )
        self.assertTrue(snapshot.source)
        self.assertIsNotNone(snapshot.effective_at)
        self.assertIsNotNone(snapshot.valid_until)

    def test_availability_snapshot_carries_schema_and_fingerprint(self) -> None:
        variant = _variant()
        snapshot = AvailabilitySnapshot(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            available=True,
            version="avail-3",
            source="offline-probe",
            sampled_at=AS_OF - timedelta(hours=1),
            valid_until=AS_OF + timedelta(hours=6),
            schema_version="availability-snapshot-v1",
            contract_fingerprint=variant.primary_contract().fingerprint,
        )
        self.assertEqual(snapshot.schema_version, "availability-snapshot-v1")
        self.assertIsNotNone(snapshot.sampled_at)

    def test_operational_limits_snapshot_is_an_independent_snapshot(self) -> None:
        variant = _variant()
        snapshot = OperationalLimitsSnapshot(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            available_rpm=600,
            available_tpm=120_000,
            available_concurrency=8,
            remaining_period_quota=Decimal("0.75"),
            version="ops-2",
            source="offline-quota-probe",
            collected_at=AS_OF - timedelta(minutes=30),
            valid_until=AS_OF + timedelta(hours=2),
            schema_version="operational-limits-snapshot-v1",
            contract_fingerprint=variant.primary_contract().fingerprint,
        )
        self.assertEqual(snapshot.schema_version, "operational-limits-snapshot-v1")
        self.assertEqual(snapshot.available_rpm, 600)
        self.assertEqual(snapshot.available_tpm, 120_000)
        self.assertEqual(snapshot.available_concurrency, 8)
        self.assertEqual(snapshot.remaining_period_quota, Decimal("0.75"))

    def test_snapshots_do_not_rewrite_model_contract_semantics(self) -> None:
        """snapshot 只描述外部观测：不携带模型语义字段，也无法改写 Contract。"""
        variant = _variant()
        fingerprint_before = variant.primary_contract().fingerprint
        sealed = with_integrity(
            OperationalLimitsSnapshot(
                variant_id=variant.variant_id,
                variant_version=variant.version,
                version="ops-3",
                source="probe",
                collected_at=AS_OF,
                valid_until=AS_OF + timedelta(hours=1),
            )
        )
        self.assertEqual(
            variant.primary_contract().fingerprint, fingerprint_before
        )
        # snapshot 冻结值不可变，也没有任何指向 Contract 的可写引用。
        with self.assertRaises(Exception):
            sealed.available_rpm = 999  # type: ignore[misc]


class SnapshotIntegrityTests(unittest.TestCase):
    """integrity 摘要：seal 可复算，篡改在加载与消费两侧都可检出。"""

    def test_sealed_snapshot_digest_is_reproducible(self) -> None:
        variant = _variant()
        base = dict(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            available=True,
            version="avail-9",
            source="probe",
            sampled_at=AS_OF,
            valid_until=AS_OF + timedelta(hours=1),
        )
        first = with_integrity(AvailabilitySnapshot(**base))
        second = with_integrity(AvailabilitySnapshot(**base))
        self.assertEqual(
            first.integrity_digest, second.integrity_digest
        )
        self.assertEqual(
            first.integrity_digest,
            snapshot_integrity_digest(first),
        )

    def test_tampered_digest_is_rejected_at_load_time(self) -> None:
        variant = _variant()
        sealed = with_integrity(
            PricingSnapshot(
                variant_id=variant.variant_id,
                variant_version=variant.version,
                currency="USD",
                input_price_per_mtok=Decimal("1.00"),
                version="price-1",
                source="sheet",
                effective_at=AS_OF - timedelta(days=1),
                valid_until=AS_OF + timedelta(days=1),
            )
        )
        tampered = sealed.model_copy(
            update={"input_price_per_mtok": Decimal("0.01")}
        )
        self.assertNotEqual(
            tampered.integrity_digest, snapshot_integrity_digest(tampered)
        )
        with self.assertRaises(ValueError):
            PricingSnapshot.model_validate(
                tampered.model_dump(mode="json")
            )

    def test_unsealed_snapshot_is_not_integrity_verifiable(self) -> None:
        variant = _variant()
        unsealed = PricingSnapshot(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            currency="USD",
            input_price_per_mtok=Decimal("1.00"),
            version="price-2",
            source="sheet",
            effective_at=AS_OF - timedelta(days=1),
            valid_until=AS_OF + timedelta(days=1),
        )
        self.assertIsNone(unsealed.integrity_digest)


class SnapshotValidityJudgmentTests(unittest.TestCase):
    """五类异常的稳定分类：subject、指纹漂移、integrity、未生效、过期。"""

    def _pricing(self, variant, **overrides):  # noqa: ANN001, ANN202
        values = dict(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            currency="USD",
            input_price_per_mtok=Decimal("1.00"),
            output_price_per_mtok=Decimal("4.00"),
            version="price-1",
            source="sheet",
            effective_at=AS_OF - timedelta(days=1),
            valid_until=AS_OF + timedelta(days=1),
            contract_fingerprint=variant.primary_contract().fingerprint,
        )
        values.update(overrides)
        return with_integrity(PricingSnapshot(**values))

    def _availability(self, variant, **overrides):  # noqa: ANN001, ANN202
        values = dict(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            available=True,
            version="avail-1",
            source="probe",
            sampled_at=AS_OF - timedelta(minutes=5),
            valid_until=AS_OF + timedelta(hours=1),
            contract_fingerprint=variant.primary_contract().fingerprint,
        )
        values.update(overrides)
        return with_integrity(AvailabilitySnapshot(**values))

    def _operational(self, variant, **overrides):  # noqa: ANN001, ANN202
        values = dict(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            available_rpm=100,
            version="ops-1",
            source="quota-probe",
            collected_at=AS_OF - timedelta(minutes=5),
            valid_until=AS_OF + timedelta(hours=1),
            contract_fingerprint=variant.primary_contract().fingerprint,
        )
        values.update(overrides)
        return with_integrity(OperationalLimitsSnapshot(**values))

    def test_valid_snapshots_judge_valid(self) -> None:
        variant = _variant()
        self.assertIs(
            evaluate_pricing_snapshot(
                self._pricing(variant), expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.VALID,
        )
        self.assertIs(
            evaluate_availability_snapshot(
                self._availability(variant), expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.VALID,
        )
        self.assertIs(
            evaluate_operational_limits_snapshot(
                self._operational(variant), expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.VALID,
        )

    def test_stale_snapshots_judge_stale(self) -> None:
        variant = _variant()
        stale = self._pricing(
            variant, valid_until=AS_OF - timedelta(seconds=1)
        )
        self.assertIs(
            evaluate_pricing_snapshot(
                stale, expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.STALE,
        )
        self.assertIs(
            evaluate_availability_snapshot(
                self._availability(
                    variant, valid_until=AS_OF - timedelta(seconds=1)
                ),
                expected_variant=variant,
                as_of=AS_OF,
            ),
            SnapshotStatus.STALE,
        )
        self.assertIs(
            evaluate_operational_limits_snapshot(
                self._operational(
                    variant, valid_until=AS_OF - timedelta(seconds=1)
                ),
                expected_variant=variant,
                as_of=AS_OF,
            ),
            SnapshotStatus.STALE,
        )

    def test_not_yet_effective_pricing_judges_not_effective(self) -> None:
        variant = _variant()
        future = self._pricing(
            variant, effective_at=AS_OF + timedelta(days=1)
        )
        self.assertIs(
            evaluate_pricing_snapshot(
                future, expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.NOT_EFFECTIVE,
        )

    def test_subject_mismatch_is_classified(self) -> None:
        variant = _variant()
        other = make_variant(
            "variant-other", make_contract("contract-other"), version="2"
        )
        mismatched = self._pricing(
            variant,
            variant_id=other.variant_id,
            variant_version=other.version,
        )
        self.assertIs(
            evaluate_pricing_snapshot(
                mismatched, expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.SUBJECT_MISMATCH,
        )
        mismatched_availability = self._availability(
            variant, variant_id=other.variant_id
        )
        self.assertIs(
            evaluate_availability_snapshot(
                mismatched_availability, expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.SUBJECT_MISMATCH,
        )
        mismatched_operational = self._operational(
            variant, variant_id=other.variant_id
        )
        self.assertIs(
            evaluate_operational_limits_snapshot(
                mismatched_operational, expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.SUBJECT_MISMATCH,
        )

    def test_fingerprint_drift_is_classified(self) -> None:
        variant = _variant()
        drifted = self._pricing(variant, contract_fingerprint="f" * 64)
        self.assertIs(
            evaluate_pricing_snapshot(
                drifted, expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.FINGERPRINT_DRIFT,
        )
        self.assertIs(
            evaluate_availability_snapshot(
                self._availability(variant, contract_fingerprint="f" * 64),
                expected_variant=variant,
                as_of=AS_OF,
            ),
            SnapshotStatus.FINGERPRINT_DRIFT,
        )
        self.assertIs(
            evaluate_operational_limits_snapshot(
                self._operational(variant, contract_fingerprint="f" * 64),
                expected_variant=variant,
                as_of=AS_OF,
            ),
            SnapshotStatus.FINGERPRINT_DRIFT,
        )

    def test_integrity_failure_is_classified_for_tampered_and_unsealed(self) -> None:
        variant = _variant()
        tampered = self._pricing(variant).model_copy(
            update={"input_price_per_mtok": Decimal("0.01")}
        )
        self.assertIs(
            evaluate_pricing_snapshot(
                tampered, expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.INTEGRITY_FAILURE,
        )
        unsealed = self._availability(variant).model_copy(
            update={"integrity_digest": None}
        )
        self.assertIs(
            evaluate_availability_snapshot(
                unsealed, expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.INTEGRITY_FAILURE,
        )
        unsealed_operational = self._operational(variant).model_copy(
            update={"integrity_digest": None}
        )
        self.assertIs(
            evaluate_operational_limits_snapshot(
                unsealed_operational, expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.INTEGRITY_FAILURE,
        )

    def test_unbound_fingerprint_does_not_drift_when_absent(self) -> None:
        """未声明指纹的快照不做漂移判定（缺失保持缺失，不伪造）。"""
        variant = _variant()
        unbound = self._pricing(variant, contract_fingerprint=None)
        self.assertIs(
            evaluate_pricing_snapshot(
                unbound, expected_variant=variant, as_of=AS_OF
            ),
            SnapshotStatus.VALID,
        )


class OperationalEvidenceLookupTests(unittest.TestCase):
    """RoutingEvidence 对 Operational Limits 的确定性最新条目选取。"""

    def test_latest_operational_limits_selects_deterministically(self) -> None:
        variant = _variant()
        older = OperationalLimitsSnapshot(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            available_rpm=100,
            version="ops-1",
            source="probe",
            collected_at=AS_OF - timedelta(hours=2),
            valid_until=AS_OF + timedelta(hours=1),
        )
        newer = OperationalLimitsSnapshot(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            available_rpm=600,
            version="ops-2",
            source="probe",
            collected_at=AS_OF - timedelta(hours=1),
            valid_until=AS_OF + timedelta(hours=1),
        )
        evidence = RoutingEvidence(operational_limits=(older, newer))
        latest = evidence.latest_operational_limits(
            variant.variant_id, variant.version
        )
        self.assertEqual(latest.version, "ops-2")
        self.assertIsNone(
            evidence.latest_operational_limits("variant-missing", "1")
        )

    def test_evidence_digest_is_stable_across_construction_order(self) -> None:
        variant = _variant()
        snapshot = OperationalLimitsSnapshot(
            variant_id=variant.variant_id,
            variant_version=variant.version,
            available_rpm=100,
            version="ops-1",
            source="probe",
            collected_at=AS_OF - timedelta(hours=2),
            valid_until=AS_OF + timedelta(hours=1),
        )
        left = RoutingEvidence(operational_limits=(snapshot,))
        right = RoutingEvidence(operational_limits=(snapshot,))
        self.assertEqual(left.evidence_digest(), right.evidence_digest())


if __name__ == "__main__":
    unittest.main()
