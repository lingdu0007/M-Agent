"""Small, immutable schemas shared by offline acceptance scenarios."""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator


_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EVIDENCE_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MANIFEST_SCHEMA_VERSION = "1"
_BUNDLE_SCHEMA_VERSION = "3"


class EvidenceLevel(StrEnum):
    CONTRACT = "CONTRACT"
    HOST = "HOST"
    PROVIDER = "PROVIDER"
    FIELD = "FIELD"


class AcceptanceCheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    NOT_RUN = "NOT_RUN"
    INCONCLUSIVE = "INCONCLUSIVE"


class PackExecutionStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"
    ERROR = "ERROR"


class BundleIntegrityError(ValueError):
    """A Scenario Evidence Bundle no longer matches its recorded digest."""


EXIT_SUCCESS = 0
EXIT_SUBJECT_FAILURE = 1
EXIT_INVALID_INVOCATION = 2
EXIT_HARNESS_ERROR = 3
EXIT_INCOMPLETE = 4
EXIT_INTEGRITY_FAILURE = 5


class AcceptanceCheck(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    scenario: str
    public_seam: str
    evidence_level: EvidenceLevel = EvidenceLevel.CONTRACT
    required: bool = True


class AcceptanceCheckResult(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    status: AcceptanceCheckStatus
    evidence_level: EvidenceLevel
    reason_code: str
    evidence_digest: str
    detail: str = ""

    @model_validator(mode="after")
    def _validate_minimal_reference(self) -> "AcceptanceCheckResult":
        if self.detail:
            raise ValueError("Acceptance Check Results cannot contain free-text detail")
        if not _EVIDENCE_KEY.fullmatch(self.reason_code):
            raise ValueError("Acceptance Check Result reason_code must be a stable identifier")
        if not _SHA256_DIGEST.fullmatch(self.evidence_digest):
            raise ValueError("Acceptance Check Result evidence_digest must be sha256")
        return self


class AcceptanceManifest(BaseModel, frozen=True):
    """Frozen identity and required check declaration for one Pack profile."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = _MANIFEST_SCHEMA_VERSION
    pack_version: str
    profile: str
    source_commit: str
    artifact_digest: str
    fixture_digest: str
    environment: Mapping[str, str] = Field(default_factory=dict)
    scenarios: tuple[str, ...] = ()
    required_checks: tuple[AcceptanceCheck, ...] = ()

    @model_validator(mode="after")
    def _validate_frozen_declarations(self) -> "AcceptanceManifest":
        if self.schema_version != _MANIFEST_SCHEMA_VERSION:
            raise ValueError("Acceptance Manifest schema version is unsupported")
        if not self.scenarios or len(set(self.scenarios)) != len(self.scenarios):
            raise ValueError("Manifest scenarios must be nonempty and unique")
        if not self.required_checks:
            raise ValueError("Manifest required checks must be nonempty")
        check_ids = [check.check_id for check in self.required_checks]
        if len(set(check_ids)) != len(check_ids):
            raise ValueError("Manifest required check_id values must be unique")
        if any(
            not check.required or check.scenario not in self.scenarios
            for check in self.required_checks
        ):
            raise ValueError(
                "Manifest required checks must be required and name a declared Scenario"
            )
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))
        return self

    @field_serializer("environment")
    def _serialize_environment(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)

    def canonical_bytes(self) -> bytes:
        payload = self.model_dump(mode="json")
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


class PackExecution(BaseModel, frozen=True):
    """An execution bound to exactly one Manifest identity."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str
    manifest_digest: str
    status: PackExecutionStatus = PackExecutionStatus.CREATED
    exit_code: int | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            PackExecutionStatus.PASSED,
            PackExecutionStatus.FAILED,
            PackExecutionStatus.INCOMPLETE,
            PackExecutionStatus.ERROR,
        }

    @classmethod
    def create(cls, manifest: AcceptanceManifest, *, execution_id: str) -> "PackExecution":
        if not execution_id:
            raise ValueError("execution_id must not be empty")
        return cls(execution_id=execution_id, manifest_digest=manifest.digest)

    def assert_matches(self, manifest: AcceptanceManifest) -> None:
        if self.manifest_digest != manifest.digest:
            raise ValueError(
                "Pack Execution is bound to a different Acceptance Manifest"
            )

    def start(self, manifest: AcceptanceManifest) -> "PackExecution":
        self.assert_matches(manifest)
        if self.status is not PackExecutionStatus.CREATED:
            raise ValueError("only a CREATED Pack Execution can start")
        return self.model_copy(update={"status": PackExecutionStatus.RUNNING})

    def complete(
        self,
        manifest: AcceptanceManifest,
        results: tuple[AcceptanceCheckResult, ...],
    ) -> "PackExecution":
        """Derive the terminal Pack result from all frozen required checks."""
        self.assert_matches(manifest)
        if self.status not in {
            PackExecutionStatus.CREATED,
            PackExecutionStatus.RUNNING,
        }:
            raise ValueError("only a nonterminal Pack Execution can complete")
        by_id = {result.check_id: result for result in results}
        if len(by_id) != len(results):
            raise ValueError("Acceptance Check Results must have unique check_id values")
        if not manifest.required_checks:
            return self.model_copy(
                update={
                    "status": PackExecutionStatus.INCOMPLETE,
                    "exit_code": EXIT_INCOMPLETE,
                }
            )
        required = [by_id.get(check.check_id) for check in manifest.required_checks]
        if any(result is None for result in required):
            return self.model_copy(
                update={
                    "status": PackExecutionStatus.INCOMPLETE,
                    "exit_code": EXIT_INCOMPLETE,
                }
            )
        if any(
            result.evidence_level is not check.evidence_level
            for check, result in zip(manifest.required_checks, required, strict=True)
            if result is not None
        ):
            return self.model_copy(
                update={
                    "status": PackExecutionStatus.ERROR,
                    "exit_code": EXIT_HARNESS_ERROR,
                }
            )
        if any(
            check.evidence_level not in {EvidenceLevel.CONTRACT, EvidenceLevel.HOST}
            for check in manifest.required_checks
        ):
            return self.model_copy(
                update={
                    "status": PackExecutionStatus.INCOMPLETE,
                    "exit_code": EXIT_INCOMPLETE,
                }
            )
        statuses = {result.status for result in required if result is not None}
        if AcceptanceCheckStatus.ERROR in statuses:
            status, exit_code = PackExecutionStatus.ERROR, EXIT_HARNESS_ERROR
        elif AcceptanceCheckStatus.FAIL in statuses:
            status, exit_code = PackExecutionStatus.FAILED, EXIT_SUBJECT_FAILURE
        elif statuses & {
            AcceptanceCheckStatus.NOT_RUN,
            AcceptanceCheckStatus.INCONCLUSIVE,
        }:
            status, exit_code = PackExecutionStatus.INCOMPLETE, EXIT_INCOMPLETE
        else:
            status, exit_code = PackExecutionStatus.PASSED, EXIT_SUCCESS
        return self.model_copy(update={"status": status, "exit_code": exit_code})


class ScenarioEvidenceBundle(BaseModel, frozen=True):
    """Immutable, content-addressed minimum evidence for one Scenario."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = _BUNDLE_SCHEMA_VERSION
    manifest_digest: str
    execution: PackExecution
    execution_checks: tuple[AcceptanceCheckResult, ...]
    scenario: str
    checks: tuple[AcceptanceCheckResult, ...]
    evidence_view: Mapping[str, str | int | float | bool | None]
    independent_evidence: Mapping[str, str | int | float | bool | None]
    content_digest: str

    @model_validator(mode="after")
    def _freeze_evidence_mappings(self) -> "ScenarioEvidenceBundle":
        if self.schema_version != _BUNDLE_SCHEMA_VERSION:
            raise ValueError("Scenario Evidence Bundle schema version is unsupported")
        object.__setattr__(
            self, "evidence_view", MappingProxyType(dict(self.evidence_view))
        )
        object.__setattr__(
            self,
            "independent_evidence",
            MappingProxyType(dict(self.independent_evidence)),
        )
        return self

    @field_serializer("evidence_view", "independent_evidence")
    def _serialize_evidence_mapping(
        self, value: Mapping[str, str | int | float | bool | None]
    ) -> dict[str, str | int | float | bool | None]:
        return dict(value)

    @staticmethod
    def _ensure_minimal_evidence(value: Mapping[str, object]) -> None:
        for key, item in value.items():
            if not _EVIDENCE_KEY.fullmatch(key):
                raise ValueError("Scenario Evidence Bundle keys must be stable identifiers")
            if item is None or isinstance(item, bool | int):
                continue
            if isinstance(item, float) and math.isfinite(item):
                continue
            if isinstance(item, str) and _SHA256_DIGEST.fullmatch(item):
                continue
            raise ValueError(
                "Scenario Evidence Bundle values must be structural or sha256 references"
            )

    @staticmethod
    def _assert_minimal_check_results(checks: tuple[AcceptanceCheckResult, ...]) -> None:
        for result in checks:
            if result.detail or (
                not _SHA256_DIGEST.fullmatch(result.evidence_digest)
            ):
                raise ValueError("Scenario Evidence Bundle check result is not minimal")

    @staticmethod
    def _assert_declared_checks(
        manifest: AcceptanceManifest,
        scenario: str,
        checks: tuple[AcceptanceCheckResult, ...],
    ) -> None:
        declared = {
            check.check_id: check
            for check in manifest.required_checks
            if check.scenario == scenario
        }
        actual = {check.check_id for check in checks}
        expected = set(declared)
        if len(actual) != len(checks) or actual != expected:
            raise ValueError("Bundle checks must exactly match Manifest checks for Scenario")
        for result in checks:
            if result.evidence_level is not declared[result.check_id].evidence_level:
                raise ValueError(
                    f"Bundle evidence level does not match Manifest for {result.check_id}"
                )

    @staticmethod
    def _assert_execution_checks(
        manifest: AcceptanceManifest,
        execution_checks: tuple[AcceptanceCheckResult, ...],
    ) -> None:
        declared = {check.check_id: check for check in manifest.required_checks}
        actual = {check.check_id for check in execution_checks}
        if len(actual) != len(execution_checks) or actual != set(declared):
            raise ValueError("Bundle execution checks must exactly match Manifest checks")
        for result in execution_checks:
            if result.evidence_level is not declared[result.check_id].evidence_level:
                raise ValueError(
                    f"Bundle execution evidence level does not match Manifest for {result.check_id}"
                )

    @staticmethod
    def _assert_terminal_execution(
        manifest: AcceptanceManifest,
        execution: PackExecution,
        execution_checks: tuple[AcceptanceCheckResult, ...],
    ) -> None:
        execution.assert_matches(manifest)
        if not execution.is_terminal or execution.exit_code is None:
            raise ValueError("Bundle execution must be terminal with an exit code")
        derived = PackExecution.create(
            manifest, execution_id=execution.execution_id
        ).complete(manifest, execution_checks)
        if (
            execution.status is not derived.status
            or execution.exit_code != derived.exit_code
        ):
            raise ValueError("Bundle execution does not match its check results")

    @classmethod
    def _digest_payload(
        cls,
        *,
        schema_version: str,
        manifest_digest: str,
        execution: PackExecution,
        execution_checks: tuple[AcceptanceCheckResult, ...],
        scenario: str,
        checks: tuple[AcceptanceCheckResult, ...],
        evidence_view: Mapping[str, str | int | float | bool | None],
        independent_evidence: Mapping[str, str | int | float | bool | None],
    ) -> str:
        payload = {
            "schema_version": schema_version,
            "manifest_digest": manifest_digest,
            "execution": execution.model_dump(mode="json"),
            "execution_checks": [
                check.model_dump(mode="json") for check in execution_checks
            ],
            "scenario": scenario,
            "checks": [check.model_dump(mode="json") for check in checks],
            "evidence_view": dict(evidence_view),
            "independent_evidence": dict(independent_evidence),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        manifest: AcceptanceManifest,
        execution: PackExecution,
        execution_checks: tuple[AcceptanceCheckResult, ...],
        scenario: str,
        checks: tuple[AcceptanceCheckResult, ...],
        evidence_view: Mapping[str, str | int | float | bool | None],
        independent_evidence: Mapping[str, str | int | float | bool | None],
    ) -> "ScenarioEvidenceBundle":
        execution.assert_matches(manifest)
        cls._assert_terminal_execution(manifest, execution, execution_checks)
        if scenario not in manifest.scenarios:
            raise ValueError(f"scenario {scenario!r} is not declared by Manifest")
        cls._assert_declared_checks(manifest, scenario, checks)
        cls._assert_minimal_check_results(checks)
        cls._assert_execution_checks(manifest, execution_checks)
        cls._assert_minimal_check_results(execution_checks)
        cls._ensure_minimal_evidence(evidence_view)
        if not independent_evidence:
            raise ValueError("Scenario Evidence Bundle requires independent evidence")
        cls._ensure_minimal_evidence(independent_evidence)
        digest = cls._digest_payload(
            schema_version=_BUNDLE_SCHEMA_VERSION,
            manifest_digest=manifest.digest,
            execution=execution,
            execution_checks=execution_checks,
            scenario=scenario,
            checks=checks,
            evidence_view=evidence_view,
            independent_evidence=independent_evidence,
        )
        return cls(
            schema_version=_BUNDLE_SCHEMA_VERSION,
            manifest_digest=manifest.digest,
            execution=execution,
            execution_checks=execution_checks,
            scenario=scenario,
            checks=checks,
            evidence_view=dict(evidence_view),
            independent_evidence=dict(independent_evidence),
            content_digest=digest,
        )

    def verify(
        self,
        manifest: AcceptanceManifest,
        execution: PackExecution | None = None,
    ) -> None:
        try:
            if execution is not None and execution != self.execution:
                raise BundleIntegrityError("Bundle execution does not match supplied execution")
            if self.schema_version != _BUNDLE_SCHEMA_VERSION:
                raise BundleIntegrityError("Scenario Evidence Bundle schema version is unsupported")
            if self.manifest_digest != manifest.digest:
                raise BundleIntegrityError("Bundle Manifest digest does not match")
            self._assert_terminal_execution(
                manifest, self.execution, self.execution_checks
            )
            self._assert_declared_checks(manifest, self.scenario, self.checks)
            self._assert_minimal_check_results(self.checks)
            self._assert_execution_checks(manifest, self.execution_checks)
            self._assert_minimal_check_results(self.execution_checks)
            self._ensure_minimal_evidence(self.evidence_view)
            if not self.independent_evidence:
                raise ValueError("Scenario Evidence Bundle requires independent evidence")
            self._ensure_minimal_evidence(self.independent_evidence)
        except (BundleIntegrityError, ValueError) as error:
            if isinstance(error, BundleIntegrityError):
                raise
            raise BundleIntegrityError(str(error)) from error
        expected = self._digest_payload(
            schema_version=self.schema_version,
            manifest_digest=self.manifest_digest,
            execution=self.execution,
            execution_checks=self.execution_checks,
            scenario=self.scenario,
            checks=self.checks,
            evidence_view=self.evidence_view,
            independent_evidence=self.independent_evidence,
        )
        if self.content_digest != expected:
            raise BundleIntegrityError("Bundle content digest does not match")
