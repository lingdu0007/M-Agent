"""Small, immutable schemas shared by offline acceptance scenarios."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    detail: str = ""
    evidence_digest: str | None = None


class AcceptanceManifest(BaseModel, frozen=True):
    """Frozen identity and required check declaration for one Pack profile."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
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
        if not self.scenarios or len(set(self.scenarios)) != len(self.scenarios):
            raise ValueError("Manifest scenarios must be nonempty and unique")
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
        return self

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
        required = [by_id.get(check.check_id) for check in manifest.required_checks]
        if any(result is None for result in required):
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

    schema_version: str = "1"
    manifest_digest: str
    execution_id: str
    scenario: str
    checks: tuple[AcceptanceCheckResult, ...]
    evidence_view: Mapping[str, str | int | float | bool | None]
    independent_evidence: Mapping[str, str | int | float | bool | None]
    content_digest: str

    @staticmethod
    def _ensure_redacted(value: Mapping[str, object]) -> None:
        forbidden = {
            "rawprompt",
            "conversationhistory",
            "credential",
            "credentials",
            "apikey",
            "endpointsecret",
            "liveresponse",
            "authorization",
            "token",
            "password",
        }
        present = {
            str(key)
            for key in value
            if "".join(character for character in str(key).lower() if character.isalnum())
            in forbidden
        }
        if present:
            raise ValueError(
                "Scenario Evidence Bundle cannot contain sensitive fields: "
                + ", ".join(sorted(present))
            )

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

    @classmethod
    def _digest_payload(
        cls,
        *,
        schema_version: str,
        manifest_digest: str,
        execution_id: str,
        scenario: str,
        checks: tuple[AcceptanceCheckResult, ...],
        evidence_view: Mapping[str, str | int | float | bool | None],
        independent_evidence: Mapping[str, str | int | float | bool | None],
    ) -> str:
        payload = {
            "schema_version": schema_version,
            "manifest_digest": manifest_digest,
            "execution_id": execution_id,
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
        scenario: str,
        checks: tuple[AcceptanceCheckResult, ...],
        evidence_view: Mapping[str, str | int | float | bool | None],
        independent_evidence: Mapping[str, str | int | float | bool | None],
    ) -> "ScenarioEvidenceBundle":
        execution.assert_matches(manifest)
        if scenario not in manifest.scenarios:
            raise ValueError(f"scenario {scenario!r} is not declared by Manifest")
        cls._assert_declared_checks(manifest, scenario, checks)
        cls._ensure_redacted(evidence_view)
        cls._ensure_redacted(independent_evidence)
        digest = cls._digest_payload(
            schema_version="1",
            manifest_digest=manifest.digest,
            execution_id=execution.execution_id,
            scenario=scenario,
            checks=checks,
            evidence_view=evidence_view,
            independent_evidence=independent_evidence,
        )
        return cls(
            manifest_digest=manifest.digest,
            execution_id=execution.execution_id,
            scenario=scenario,
            checks=checks,
            evidence_view=dict(evidence_view),
            independent_evidence=dict(independent_evidence),
            content_digest=digest,
        )

    def verify(
        self,
        manifest: AcceptanceManifest,
        execution: PackExecution,
    ) -> None:
        try:
            execution.assert_matches(manifest)
            if self.manifest_digest != manifest.digest:
                raise BundleIntegrityError("Bundle Manifest digest does not match")
            if self.execution_id != execution.execution_id:
                raise BundleIntegrityError("Bundle execution identity does not match")
            self._assert_declared_checks(manifest, self.scenario, self.checks)
            self._ensure_redacted(self.evidence_view)
            self._ensure_redacted(self.independent_evidence)
        except (BundleIntegrityError, ValueError) as error:
            if isinstance(error, BundleIntegrityError):
                raise
            raise BundleIntegrityError(str(error)) from error
        expected = self._digest_payload(
            schema_version=self.schema_version,
            manifest_digest=self.manifest_digest,
            execution_id=self.execution_id,
            scenario=self.scenario,
            checks=self.checks,
            evidence_view=self.evidence_view,
            independent_evidence=self.independent_evidence,
        )
        if self.content_digest != expected:
            raise BundleIntegrityError("Bundle content digest does not match")
