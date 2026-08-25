"""Small, immutable schemas shared by offline acceptance scenarios."""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator


_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_EVIDENCE_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MANIFEST_SCHEMA_VERSION = "1"
_BUNDLE_SCHEMA_VERSION = "4"
_SUPPORTED_HOST_OSES = frozenset({"darwin", "linux"})


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
    owner: str = ""
    positive_check: str = ""
    negative_check: str = ""
    authoritative_evidence: str = ""
    independent_evidence: str = ""
    milestone: str = ""
    non_claim: str = ""
    evidence_level: EvidenceLevel = EvidenceLevel.CONTRACT
    required: bool = True

    @model_validator(mode="after")
    def _validate_complete_coverage(self) -> "AcceptanceCheck":
        coverage = (
            self.check_id,
            self.scenario,
            self.public_seam,
            self.owner,
            self.positive_check,
            self.negative_check,
            self.authoritative_evidence,
            self.independent_evidence,
            self.milestone,
            self.non_claim,
        )
        if any(not value.strip() for value in coverage):
            raise ValueError("Acceptance Check coverage declarations must be nonempty")
        if not _EVIDENCE_KEY.fullmatch(self.authoritative_evidence) or not _EVIDENCE_KEY.fullmatch(
            self.independent_evidence
        ):
            raise ValueError("Acceptance Check evidence declarations must be stable keys")
        return self


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
    sdist_digest: str
    fixture_digest: str
    environment: Mapping[str, str] = Field(default_factory=dict)
    scenarios: tuple[str, ...] = ()
    required_checks: tuple[AcceptanceCheck, ...] = ()
    required_cli_commands: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_frozen_declarations(self) -> "AcceptanceManifest":
        if self.schema_version != _MANIFEST_SCHEMA_VERSION:
            raise ValueError("Acceptance Manifest schema version is unsupported")
        if any(
            not value.strip()
            for value in (
                self.pack_version,
                self.profile,
                self.source_commit,
                self.artifact_digest,
                self.sdist_digest,
                self.fixture_digest,
            )
        ):
            raise ValueError("Manifest identity declarations must be nonempty")
        if not _SOURCE_COMMIT.fullmatch(self.source_commit):
            raise ValueError("Manifest source_commit must be a full Git commit")
        if any(
            not _SHA256_DIGEST.fullmatch(value)
            for value in (
                self.artifact_digest,
                self.sdist_digest,
                self.fixture_digest,
            )
        ):
            raise ValueError("Manifest artifact identities must be sha256 digests")
        if not self.environment or any(
            not key.strip() or not value.strip()
            for key, value in self.environment.items()
        ):
            raise ValueError("Manifest environment identity must be nonempty")
        if not self.scenarios or len(
            {scenario.casefold() for scenario in self.scenarios}
        ) != len(self.scenarios):
            raise ValueError(
                "Manifest scenarios must be nonempty and unique under case folding"
            )
        if any(not scenario.strip() for scenario in self.scenarios):
            raise ValueError("Manifest scenario declarations must be nonempty")
        if not self.required_checks:
            raise ValueError("Manifest required checks must be nonempty")
        check_ids = [check.check_id for check in self.required_checks]
        if len({check_id.casefold() for check_id in check_ids}) != len(check_ids):
            raise ValueError(
                "Manifest required check_id values must be unique under case folding"
            )
        if any(
            not check.required or check.scenario not in self.scenarios
            for check in self.required_checks
        ):
            raise ValueError(
                "Manifest required checks must be required and name a declared Scenario"
            )
        if len(set(self.required_cli_commands)) != len(self.required_cli_commands) or any(
            not command.strip() for command in self.required_cli_commands
        ):
            raise ValueError("Manifest required CLI commands must be unique and nonempty")
        host_os = self.environment.get("os")
        if host_os is not None and host_os not in _SUPPORTED_HOST_OSES:
            raise ValueError(
                "Manifest environment os must use a supported lowercase identity"
            )
        if (
            any(
                check.evidence_level is EvidenceLevel.HOST
                for check in self.required_checks
            )
            and host_os == "windows"
        ):
            raise ValueError("Windows is unsupported for Acceptance Pack HOST evidence")
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))
        return self

    @field_serializer("environment")
    def _serialize_environment(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        """Keep identity validation active for controlled Manifest mutations."""
        values = self.model_dump()
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)

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


CORE_LIFECYCLE_PACK_VERSION = "foundation-v1"
CORE_LIFECYCLE_PROFILE = "core-lifecycle-foundation"
CORE_LIFECYCLE_SCENARIO = "core-lifecycle"
DURABLE_EFFECTS_SCENARIO = "durable-effects-recovery"
SESSION_CONVERSATION_SCENARIO = "session-conversation"
SESSION_CONVERSATION_PACK_VERSION = "session-foundation-v1"
SESSION_CONVERSATION_PROFILE = "session-conversation-foundation"
RUNTIME_BASELINE_PACK_VERSION = "runtime-baseline-v1"
RUNTIME_BASELINE_PROFILE = "runtime-baseline-0-3"
CONTEXT_COMPRESSION_SCENARIO = "context-budget-compression"
CONTEXT_COMPRESSION_PACK_VERSION = "context-compression-v1"
CONTEXT_COMPRESSION_PROFILE = "context-compression-foundation"
_CORE_LIFECYCLE_REQUIRED_CHECKS = (
    AcceptanceCheck(
        check_id="core.lifecycle",
        scenario=CORE_LIFECYCLE_SCENARIO,
        owner="Runtime Core",
        public_seam="m_agent.runtime.Runner",
        positive_check="create_start_inspect_deterministic_run",
        negative_check="non_success_terminal_is_fail",
        authoritative_evidence="core_lifecycle_authoritative_digest",
        independent_evidence="core_lifecycle_independent_digest",
        milestone="foundation",
        non_claim="provider_or_production_execution",
    ),
    AcceptanceCheck(
        check_id="core.lifecycle.telemetry",
        scenario=CORE_LIFECYCLE_SCENARIO,
        owner="Telemetry Adapter",
        public_seam="m_agent.runtime.TelemetrySink,m_agent.adapters.JsonlTelemetrySink",
        positive_check="jsonl_correlates_ordered_run_step_attempt_usage_and_inspection",
        negative_check="payload_or_credential_leak_or_unreconciled_event_is_fail",
        authoritative_evidence="telemetry_authoritative_digest",
        independent_evidence="telemetry_jsonl_digest",
        milestone="0_3",
        non_claim="provider_endpoint_or_production_observability",
    ),
    AcceptanceCheck(
        check_id="core.lifecycle.unknown-definition",
        scenario=CORE_LIFECYCLE_SCENARIO,
        owner="Runtime Core",
        public_seam="m_agent.runtime.DefinitionRegistry",
        positive_check="unknown_definition_raises_public_error",
        negative_check="successful_unknown_resolution_is_fail",
        authoritative_evidence="unknown_definition_authoritative_digest",
        independent_evidence="unknown_definition_independent_digest",
        milestone="foundation",
        non_claim="definition_persistence",
    ),
    AcceptanceCheck(
        check_id="core.lifecycle.public-namespaces",
        scenario=CORE_LIFECYCLE_SCENARIO,
        owner="Distribution API",
        public_seam="m_agent.runtime,m_agent.adapters,m_agent.companion,m_agent.testing",
        positive_check="four_public_namespaces_import",
        negative_check="missing_or_wrong_binding_is_fail",
        authoritative_evidence="public_namespaces_authoritative_digest",
        independent_evidence="public_namespaces_independent_digest",
        milestone="foundation",
        non_claim="future_companion_capabilities",
    ),
    AcceptanceCheck(
        check_id="core.lifecycle.dependency-direction",
        scenario=CORE_LIFECYCLE_SCENARIO,
        owner="Runtime Core",
        public_seam="m_agent.testing.find_runtime_dependency_violations",
        positive_check="installed_core_has_no_reverse_layer_import",
        negative_check="forbidden_import_is_fail",
        authoritative_evidence="dependency_direction_authoritative_digest",
        independent_evidence="dependency_direction_independent_digest",
        milestone="foundation",
        non_claim="dynamic_behavior_outside_core_files",
    ),
    AcceptanceCheck(
        check_id="core.lifecycle.expand-compatibility",
        scenario=CORE_LIFECYCLE_SCENARIO,
        owner="Distribution API",
        public_seam="m_agent,m_agent.runtime",
        positive_check="all_0_2_root_exports_retain_semantic_bindings",
        negative_check="changed_binding_is_fail",
        authoritative_evidence="expand_compatibility_authoritative_digest",
        independent_evidence="expand_compatibility_independent_digest",
        milestone="0_2_expand",
        non_claim="0_3_root_retention",
    ),
    AcceptanceCheck(
        check_id="core.lifecycle.bundle-tamper",
        scenario=CORE_LIFECYCLE_SCENARIO,
        owner="Testing",
        public_seam="m_agent.testing.ScenarioEvidenceBundle",
        positive_check="controlled_bundle_mutation_is_detected",
        negative_check="undetected_mutation_is_harness_error",
        authoritative_evidence="bundle_tamper_authoritative_digest",
        independent_evidence="bundle_tamper_independent_digest",
        milestone="foundation",
        non_claim="external_ledger_integrity",
    ),
    AcceptanceCheck(
        check_id="core.lifecycle.host-wheel",
        scenario=CORE_LIFECYCLE_SCENARIO,
        owner="Testing",
        public_seam="python -I -m m_agent.testing",
        positive_check="exact_wheel_and_sdist_install_in_clean_external_venv",
        negative_check="source_artifact_fixture_or_environment_mismatch_is_rejected",
        authoritative_evidence="host_wheel_authoritative_digest",
        independent_evidence="host_wheel_independent_digest",
        milestone="foundation",
        non_claim="live_provider_behavior",
        evidence_level=EvidenceLevel.HOST,
    ),
    AcceptanceCheck(
        check_id="core.lifecycle.telemetry-host",
        scenario=CORE_LIFECYCLE_SCENARIO,
        owner="Telemetry Adapter",
        public_seam="m_agent.adapters.JsonlTelemetrySink,python -I -m m_agent.testing",
        positive_check="installed_wheel_jsonl_reopens_and_reconciles_with_sqlite_inspection",
        negative_check="credential_inheritance_plaintext_or_process_integrity_failure_is_fail",
        authoritative_evidence="telemetry_host_authoritative_digest",
        independent_evidence="telemetry_host_independent_digest",
        milestone="0_3",
        non_claim="external_collector_or_live_provider_behavior",
        evidence_level=EvidenceLevel.HOST,
    ),
)


def core_lifecycle_manifest(
    *,
    source_commit: str,
    artifact_digest: str,
    sdist_digest: str,
    fixture_digest: str,
    environment: Mapping[str, str],
) -> AcceptanceManifest:
    """Build the complete, one-scenario foundation declaration."""
    return AcceptanceManifest(
        pack_version=CORE_LIFECYCLE_PACK_VERSION,
        profile=CORE_LIFECYCLE_PROFILE,
        source_commit=source_commit,
        artifact_digest=artifact_digest,
        sdist_digest=sdist_digest,
        fixture_digest=fixture_digest,
        environment=environment,
        scenarios=(CORE_LIFECYCLE_SCENARIO,),
        required_checks=tuple(
            _RUNTIME_BASELINE_MIGRATION_CHECK
            if check.check_id == "core.lifecycle.expand-compatibility"
            else check
            for check in _CORE_LIFECYCLE_REQUIRED_CHECKS
        ),
        required_cli_commands=("run", "inspect", "verify", "render"),
    )


_RUNTIME_BASELINE_MIGRATION_CHECK = AcceptanceCheck(
    check_id="core.lifecycle.migration",
    scenario=CORE_LIFECYCLE_SCENARIO,
    owner="Distribution API",
    public_seam="m_agent,m_agent.runtime,m_agent.adapters",
    positive_check="root_facade_and_directional_migration_errors_match_wheel",
    negative_check="legacy_root_or_provider_import_is_fail",
    authoritative_evidence="migration_authoritative_digest",
    independent_evidence="migration_table_digest",
    milestone="0_3",
    non_claim="backward_compatible_0_2_public_surface",
)


_DURABLE_EFFECTS_REQUIRED_CHECKS = (
    AcceptanceCheck(
        check_id="durable.effects.recovery-windows",
        scenario=DURABLE_EFFECTS_SCENARIO,
        owner="Runtime Core",
        public_seam="m_agent.runtime.Runner.resume_run,m_agent.runtime.Runner.inspect_run",
        positive_check="real_child_hard_exit_sqlite_reopen_repeats_cleanly",
        negative_check="duplicate_effect_or_budget_reset_is_fail",
        authoritative_evidence="recovery_windows_authoritative_digest",
        independent_evidence="recovery_windows_journal_digest",
        milestone="0_3",
        non_claim="exactly_once_external_effect",
    ),
    AcceptanceCheck(
        check_id="durable.effects.budget-fail-closed",
        scenario=DURABLE_EFFECTS_SCENARIO,
        owner="Runtime Core",
        public_seam="m_agent.runtime.Runner.resume_run,m_agent.runtime.RunInspection",
        positive_check="pre_dispatch_reservation_survives_hard_exit",
        negative_check="recovered_budget_capacity_is_fail",
        authoritative_evidence="budget_fail_closed_authoritative_digest",
        independent_evidence="budget_fail_closed_journal_digest",
        milestone="0_3",
        non_claim="provider_quota_or_cost_budget",
    ),
    AcceptanceCheck(
        check_id="durable.effects.waiting-resolution",
        scenario=DURABLE_EFFECTS_SCENARIO,
        owner="Runtime Core",
        public_seam="m_agent.runtime.Runner.resolve_run",
        positive_check="uncertain_non_idempotent_effect_waits_for_confirm_resolution",
        negative_check="automatic_effect_replay_is_fail",
        authoritative_evidence="waiting_resolution_authoritative_digest",
        independent_evidence="waiting_resolution_journal_digest",
        milestone="0_3",
        non_claim="automatic_uncertain_effect_resolution",
    ),
    AcceptanceCheck(
        check_id="durable.effects.mutation",
        scenario=DURABLE_EFFECTS_SCENARIO,
        owner="Testing",
        public_seam="m_agent.testing.reconcile_recovery_window",
        positive_check="controlled_duplicate_effect_mutation_is_detected",
        negative_check="undetected_mutation_is_harness_error",
        authoritative_evidence="mutation_authoritative_digest",
        independent_evidence="mutation_independent_digest",
        milestone="0_3",
        non_claim="business_effect_evidence",
    ),
    AcceptanceCheck(
        check_id="durable.effects.host-wheel",
        scenario=DURABLE_EFFECTS_SCENARIO,
        owner="Testing",
        public_seam="python -I -m m_agent.testing",
        positive_check="installed_wheel_runs_child_exit_and_sqlite_reopen",
        negative_check="source_import_or_artifact_identity_mismatch_is_fail",
        authoritative_evidence="durable_host_authoritative_digest",
        independent_evidence="durable_host_independent_digest",
        milestone="0_3",
        non_claim="live_provider_or_production_effect",
        evidence_level=EvidenceLevel.HOST,
    ),
)


def runtime_baseline_manifest(
    *,
    source_commit: str,
    artifact_digest: str,
    sdist_digest: str,
    fixture_digest: str,
    environment: Mapping[str, str],
) -> AcceptanceManifest:
    """Freeze the two required 0.3 Scenarios for one exact candidate."""
    core_checks = tuple(
        _RUNTIME_BASELINE_MIGRATION_CHECK
        if check.check_id == "core.lifecycle.expand-compatibility"
        else check
        for check in _CORE_LIFECYCLE_REQUIRED_CHECKS
    )
    return AcceptanceManifest(
        pack_version=RUNTIME_BASELINE_PACK_VERSION,
        profile=RUNTIME_BASELINE_PROFILE,
        source_commit=source_commit,
        artifact_digest=artifact_digest,
        sdist_digest=sdist_digest,
        fixture_digest=fixture_digest,
        environment=environment,
        scenarios=(CORE_LIFECYCLE_SCENARIO, DURABLE_EFFECTS_SCENARIO),
        required_checks=(*core_checks, *_DURABLE_EFFECTS_REQUIRED_CHECKS),
        required_cli_commands=("run", "inspect", "verify", "render"),
    )


_SESSION_CONVERSATION_REQUIRED_CHECKS = (
    AcceptanceCheck(
        check_id="session.conversation.recovery-windows",
        scenario=SESSION_CONVERSATION_SCENARIO,
        owner="Session Companion",
        public_seam=(
            "m_agent.companion.SessionRunner.resume,"
            "m_agent.companion.SQLiteSessionStore,m_agent.runtime.Runner.get_run"
        ),
        positive_check=(
            "three_cross_store_crash_windows_reopen_and_recover_repeatedly"
        ),
        negative_check="partial_commit_or_rewritten_core_terminal_is_fail",
        authoritative_evidence="recovery_windows_authoritative_digest",
        independent_evidence="recovery_windows_journal_digest",
        milestone="0_4",
        non_claim="exactly_once_external_effect",
    ),
    AcceptanceCheck(
        check_id="session.conversation.claim-no-ttl",
        scenario=SESSION_CONVERSATION_SCENARIO,
        owner="Session Companion",
        public_seam=(
            "m_agent.companion.SessionStore.claim_run,"
            "m_agent.companion.SessionStore.get_claim"
        ),
        positive_check=(
            "restart_and_stale_owner_keep_exactly_one_active_session_claim"
        ),
        negative_check="silent_second_run_admission_is_fail",
        authoritative_evidence="claim_no_ttl_authoritative_digest",
        independent_evidence="claim_no_ttl_journal_digest",
        milestone="0_4",
        non_claim="time_based_claim_recovery",
    ),
    AcceptanceCheck(
        check_id="session.conversation.payload-protection",
        scenario=SESSION_CONVERSATION_SCENARIO,
        owner="Session Companion",
        public_seam=(
            "m_agent.companion.SQLiteSessionStore,m_agent.runtime.PayloadCodec"
        ),
        positive_check=(
            "independent_session_codec_protects_history_and_wrong_key_fails_closed"
        ),
        negative_check="plaintext_history_or_silent_wrong_key_decode_is_fail",
        authoritative_evidence="payload_protection_authoritative_digest",
        independent_evidence="payload_protection_independent_digest",
        milestone="0_4",
        non_claim="encryption_strength_or_key_management",
    ),
    AcceptanceCheck(
        check_id="session.conversation.scope-isolation",
        scenario=SESSION_CONVERSATION_SCENARIO,
        owner="Session Companion",
        public_seam="m_agent.companion.SessionScope,m_agent.companion.SessionStore",
        positive_check="cross_scope_access_fails_closed_without_side_effects",
        negative_check="cross_scope_visibility_or_mutation_is_fail",
        authoritative_evidence="scope_isolation_authoritative_digest",
        independent_evidence="scope_isolation_independent_digest",
        milestone="0_4",
        non_claim="authorization_or_multitenant_isolation",
    ),
    AcceptanceCheck(
        check_id="session.conversation.mutation",
        scenario=SESSION_CONVERSATION_SCENARIO,
        owner="Testing",
        public_seam=(
            "m_agent.testing.reconcile_session_recovery,"
            "m_agent.testing.reconcile_session_protection,"
            "m_agent.testing.ScenarioEvidenceBundle"
        ),
        positive_check=(
            "tamper_wrong_scope_wrong_key_and_duplicate_submit_mutations_detected"
        ),
        negative_check="undetected_mutation_is_harness_error",
        authoritative_evidence="mutation_authoritative_digest",
        independent_evidence="mutation_independent_digest",
        milestone="0_4",
        non_claim="external_ledger_integrity",
    ),
)


def session_conversation_manifest(
    *,
    source_commit: str,
    artifact_digest: str,
    sdist_digest: str,
    fixture_digest: str,
    environment: Mapping[str, str],
) -> AcceptanceManifest:
    """Freeze the durable Session recovery Scenario declaration.

    HOST wheel 证据与 0.4 release profile（ADR 0042）由后续发行票补充；
    本 Manifest 冻结当前可离线重复验证的五个 required CONTRACT 检查。
    """
    return AcceptanceManifest(
        pack_version=SESSION_CONVERSATION_PACK_VERSION,
        profile=SESSION_CONVERSATION_PROFILE,
        source_commit=source_commit,
        artifact_digest=artifact_digest,
        sdist_digest=sdist_digest,
        fixture_digest=fixture_digest,
        environment=environment,
        scenarios=(SESSION_CONVERSATION_SCENARIO,),
        required_checks=_SESSION_CONVERSATION_REQUIRED_CHECKS,
        required_cli_commands=(),
    )


_CONTEXT_COMPRESSION_REQUIRED_CHECKS = (
    AcceptanceCheck(
        check_id="context.compression.plan-order",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam=(
            "m_agent.runtime.Runner.start_run,m_agent.runtime.Runner.inspect_run"
        ),
        positive_check="plan_order_and_scopes_match_frozen_context_plan",
        negative_check="model_step_scope_never_triggered_for_compression",
        authoritative_evidence="plan_order_authoritative_digest",
        independent_evidence="plan_order_sentinel_digest",
        milestone="0_4",
        non_claim="nested_compression",
    ),
    AcceptanceCheck(
        check_id="context.compression.frame-checkpoints",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam=(
            "m_agent.runtime.Runner.inspect_run,m_agent.runtime.CompressionResult"
        ),
        positive_check="source_items_preserved_and_derived_provenance_intact",
        negative_check="compression_recomputed_or_external_source_reread_after_recovery",
        authoritative_evidence="frame_checkpoints_authoritative_digest",
        independent_evidence="frame_checkpoints_sentinel_digest",
        milestone="0_4",
        non_claim="lossless_transform",
    ),
    AcceptanceCheck(
        check_id="context.compression.hard-budget",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam=(
            "m_agent.runtime.check_frame_budget,m_agent.runtime.ModelInputSizer"
        ),
        positive_check="over_limit_frame_fails_closed_with_zero_business_dispatch",
        negative_check="under_counting_sizer_would_admit_over_limit_frame",
        authoritative_evidence="hard_budget_authoritative_digest",
        independent_evidence="hard_budget_sentinel_digest",
        milestone="0_4",
        non_claim="soft_budget_or_best_effort_compression",
    ),
    AcceptanceCheck(
        check_id="context.compression.protected-channels",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam=(
            "m_agent.runtime.Runner.start_run,m_agent.runtime.CompressionContract"
        ),
        positive_check="protected_sources_history_run_input_instructions_never_compressed",
        negative_check="canaries_leaked_into_compression_input",
        authoritative_evidence="protected_channels_authoritative_digest",
        independent_evidence="protected_channels_sentinel_digest",
        milestone="0_4",
        non_claim="all_context_channels_are_compressible",
    ),
    AcceptanceCheck(
        check_id="context.compression.no-recursion",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam=(
            "m_agent.runtime.Runner.start_run,m_agent.runtime.ModelPurpose.CONTEXT_COMPRESSION"
        ),
        positive_check="compression_tool_calls_fail_closed_with_zero_business_dispatch",
        negative_check="compression_triggers_pipeline_or_tools_or_repair",
        authoritative_evidence="no_recursion_authoritative_digest",
        independent_evidence="no_recursion_sentinel_digest",
        milestone="0_4",
        non_claim="compression_is_a_business_step",
    ),
    AcceptanceCheck(
        check_id="context.compression.mutation",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam=(
            "m_agent.testing.reconcile_compression_observation"
        ),
        positive_check="stale_tampered_undercounting_recursion_mutations_all_detected",
        negative_check="mutation_accepted_silently",
        authoritative_evidence="mutation_authoritative_digest",
        independent_evidence="mutation_independent_digest",
        milestone="0_4",
        non_claim="single_source_verification",
    ),
)


def context_compression_manifest(
    *,
    source_commit: str,
    artifact_digest: str,
    sdist_digest: str,
    fixture_digest: str,
    environment: Mapping[str, str],
) -> AcceptanceManifest:
    """Freeze the Semantic Compression Scenario declaration.

    Six required CONTRACT checks cover plan order / scopes, Stage / Frame
    checkpoint recovery, hard budget, protected channels, no-recursion,
    and mutation detection (stale source, tampered provenance,
    under-counting sizer, recursion).
    """
    return AcceptanceManifest(
        pack_version=CONTEXT_COMPRESSION_PACK_VERSION,
        profile=CONTEXT_COMPRESSION_PROFILE,
        source_commit=source_commit,
        artifact_digest=artifact_digest,
        sdist_digest=sdist_digest,
        fixture_digest=fixture_digest,
        environment=environment,
        scenarios=(CONTEXT_COMPRESSION_SCENARIO,),
        required_checks=_CONTEXT_COMPRESSION_REQUIRED_CHECKS,
        required_cli_commands=(),
    )


FOUNDATION_RELEASE_0_4_PACK_VERSION = "foundation-release-0-4-v1"
FOUNDATION_RELEASE_0_4_PROFILE = "foundation-release-0-4"

# ADR 0042：0.4 发布 profile 在同一 RC 身份下重跑 0.3 两个
# required Scenario 并新增 Session / Context 场景。Session / Context 检查
# 相对单场景 Manifest 使用带前缀的证据槽位（``session_`` / ``context_``），
# 避免与 durable 检查的同名槽位（如 ``recovery_windows_*``）在合并后的
# Evidence View 中发生覆盖——这是发布 profile 的冻结声明，不回写单场景
# Manifest。
_RELEASE_0_4_SESSION_CHECKS = (
    AcceptanceCheck(
        check_id="session.conversation.recovery-windows",
        scenario=SESSION_CONVERSATION_SCENARIO,
        owner="Session Companion",
        public_seam=(
            "m_agent.companion.SessionRunner.resume,"
            "m_agent.companion.SQLiteSessionStore,m_agent.runtime.Runner.get_run"
        ),
        positive_check="three_cross_store_crash_windows_reopen_and_recover_repeatedly",
        negative_check="partial_commit_or_rewritten_core_terminal_is_fail",
        authoritative_evidence="session_recovery_windows_authoritative_digest",
        independent_evidence="session_recovery_windows_journal_digest",
        milestone="0_4",
        non_claim="exactly_once_external_effect",
    ),
    AcceptanceCheck(
        check_id="session.conversation.claim-no-ttl",
        scenario=SESSION_CONVERSATION_SCENARIO,
        owner="Session Companion",
        public_seam=(
            "m_agent.companion.SessionStore.claim_run,"
            "m_agent.companion.SessionStore.get_claim"
        ),
        positive_check="restart_and_stale_owner_keep_exactly_one_active_session_claim",
        negative_check="silent_second_run_admission_is_fail",
        authoritative_evidence="session_claim_no_ttl_authoritative_digest",
        independent_evidence="session_claim_no_ttl_journal_digest",
        milestone="0_4",
        non_claim="time_based_claim_recovery",
    ),
    AcceptanceCheck(
        check_id="session.conversation.payload-protection",
        scenario=SESSION_CONVERSATION_SCENARIO,
        owner="Session Companion",
        public_seam=(
            "m_agent.companion.SQLiteSessionStore,m_agent.runtime.PayloadCodec"
        ),
        positive_check=(
            "independent_session_codec_protects_history_and_wrong_key_fails_closed"
        ),
        negative_check="plaintext_history_or_silent_wrong_key_decode_is_fail",
        authoritative_evidence="session_payload_protection_authoritative_digest",
        independent_evidence="session_payload_protection_independent_digest",
        milestone="0_4",
        non_claim="encryption_strength_or_key_management",
    ),
    AcceptanceCheck(
        check_id="session.conversation.scope-isolation",
        scenario=SESSION_CONVERSATION_SCENARIO,
        owner="Session Companion",
        public_seam="m_agent.companion.SessionScope,m_agent.companion.SessionStore",
        positive_check="cross_scope_access_fails_closed_without_side_effects",
        negative_check="cross_scope_visibility_or_mutation_is_fail",
        authoritative_evidence="session_scope_isolation_authoritative_digest",
        independent_evidence="session_scope_isolation_independent_digest",
        milestone="0_4",
        non_claim="authorization_or_multitenant_isolation",
    ),
    AcceptanceCheck(
        check_id="session.conversation.mutation",
        scenario=SESSION_CONVERSATION_SCENARIO,
        owner="Testing",
        public_seam=(
            "m_agent.testing.reconcile_session_recovery,"
            "m_agent.testing.reconcile_session_protection,"
            "m_agent.testing.ScenarioEvidenceBundle"
        ),
        positive_check=(
            "tamper_wrong_scope_wrong_key_and_duplicate_submit_mutations_detected"
        ),
        negative_check="undetected_mutation_is_harness_error",
        authoritative_evidence="session_mutation_authoritative_digest",
        independent_evidence="session_mutation_independent_digest",
        milestone="0_4",
        non_claim="external_ledger_integrity",
    ),
    AcceptanceCheck(
        check_id="session.conversation.host-wheel",
        scenario=SESSION_CONVERSATION_SCENARIO,
        owner="Session Companion",
        public_seam="python -I -m m_agent.testing",
        positive_check=(
            "installed_wheel_runs_cross_store_crash_windows_and_store_contract_kit"
        ),
        negative_check="source_import_or_artifact_identity_mismatch_is_fail",
        authoritative_evidence="session_host_authoritative_digest",
        independent_evidence="session_host_independent_digest",
        milestone="0_4",
        non_claim="live_provider_or_remote_session_store",
        evidence_level=EvidenceLevel.HOST,
    ),
)

_RELEASE_0_4_CONTEXT_CHECKS = (
    AcceptanceCheck(
        check_id="context.compression.plan-order",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam=(
            "m_agent.runtime.Runner.start_run,m_agent.runtime.Runner.inspect_run"
        ),
        positive_check="plan_order_and_scopes_match_frozen_context_plan",
        negative_check="model_step_scope_never_triggered_for_compression",
        authoritative_evidence="context_plan_order_authoritative_digest",
        independent_evidence="context_plan_order_sentinel_digest",
        milestone="0_4",
        non_claim="nested_compression",
    ),
    AcceptanceCheck(
        check_id="context.compression.frame-checkpoints",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam=(
            "m_agent.runtime.Runner.inspect_run,m_agent.runtime.CompressionResult"
        ),
        positive_check="source_items_preserved_and_derived_provenance_intact",
        negative_check="compression_recomputed_or_external_source_reread_after_recovery",
        authoritative_evidence="context_frame_checkpoints_authoritative_digest",
        independent_evidence="context_frame_checkpoints_sentinel_digest",
        milestone="0_4",
        non_claim="lossless_transform",
    ),
    AcceptanceCheck(
        check_id="context.compression.hard-budget",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam=(
            "m_agent.runtime.check_frame_budget,m_agent.runtime.ModelInputSizer"
        ),
        positive_check="over_limit_frame_fails_closed_with_zero_business_dispatch",
        negative_check="under_counting_sizer_would_admit_over_limit_frame",
        authoritative_evidence="context_hard_budget_authoritative_digest",
        independent_evidence="context_hard_budget_sentinel_digest",
        milestone="0_4",
        non_claim="soft_budget_or_best_effort_compression",
    ),
    AcceptanceCheck(
        check_id="context.compression.protected-channels",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam=(
            "m_agent.runtime.Runner.start_run,m_agent.runtime.CompressionContract"
        ),
        positive_check="protected_sources_history_run_input_instructions_never_compressed",
        negative_check="canaries_leaked_into_compression_input",
        authoritative_evidence="context_protected_channels_authoritative_digest",
        independent_evidence="context_protected_channels_sentinel_digest",
        milestone="0_4",
        non_claim="all_context_channels_are_compressible",
    ),
    AcceptanceCheck(
        check_id="context.compression.no-recursion",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam=(
            "m_agent.runtime.Runner.start_run,m_agent.runtime.ModelPurpose.CONTEXT_COMPRESSION"
        ),
        positive_check="compression_tool_calls_fail_closed_with_zero_business_dispatch",
        negative_check="compression_triggers_pipeline_or_tools_or_repair",
        authoritative_evidence="context_no_recursion_authoritative_digest",
        independent_evidence="context_no_recursion_sentinel_digest",
        milestone="0_4",
        non_claim="compression_is_a_business_step",
    ),
    AcceptanceCheck(
        check_id="context.compression.mutation",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam=(
            "m_agent.testing.reconcile_compression_observation"
        ),
        positive_check="stale_tampered_undercounting_recursion_mutations_all_detected",
        negative_check="mutation_accepted_silently",
        authoritative_evidence="context_mutation_authoritative_digest",
        independent_evidence="context_mutation_independent_digest",
        milestone="0_4",
        non_claim="single_source_verification",
    ),
    AcceptanceCheck(
        check_id="context.compression.host-wheel",
        scenario=CONTEXT_COMPRESSION_SCENARIO,
        owner="Runtime",
        public_seam="python -I -m m_agent.testing",
        positive_check=(
            "installed_wheel_runs_budget_compression_and_recovery_probes"
        ),
        negative_check="source_import_or_artifact_identity_mismatch_is_fail",
        authoritative_evidence="context_host_authoritative_digest",
        independent_evidence="context_host_independent_digest",
        milestone="0_4",
        non_claim="live_provider_or_business_context_quality",
        evidence_level=EvidenceLevel.HOST,
    ),
)


def foundation_release_0_4_manifest(
    *,
    source_commit: str,
    artifact_digest: str,
    sdist_digest: str,
    fixture_digest: str,
    environment: Mapping[str, str],
) -> AcceptanceManifest:
    """Freeze the four required 0.4 Scenarios for one exact RC candidate.

    0.3 基线（``core-lifecycle`` 与 ``durable-effects-recovery``）在同一
    RC 身份下原样重跑；``session-conversation`` 与
    ``context-budget-compression`` 追加 CONTRACT 与 HOST required 证据。
    不同 RC（artifact / sdist digest 或环境不同）的 Manifest digest 必然
    不同，任何旧 RC Bundle 都无法通过本 Manifest 的执行验证。
    """
    core_checks = tuple(
        _RUNTIME_BASELINE_MIGRATION_CHECK
        if check.check_id == "core.lifecycle.expand-compatibility"
        else check
        for check in _CORE_LIFECYCLE_REQUIRED_CHECKS
    )
    return AcceptanceManifest(
        pack_version=FOUNDATION_RELEASE_0_4_PACK_VERSION,
        profile=FOUNDATION_RELEASE_0_4_PROFILE,
        source_commit=source_commit,
        artifact_digest=artifact_digest,
        sdist_digest=sdist_digest,
        fixture_digest=fixture_digest,
        environment=environment,
        scenarios=(
            CORE_LIFECYCLE_SCENARIO,
            DURABLE_EFFECTS_SCENARIO,
            SESSION_CONVERSATION_SCENARIO,
            CONTEXT_COMPRESSION_SCENARIO,
        ),
        required_checks=(
            *core_checks,
            *_DURABLE_EFFECTS_REQUIRED_CHECKS,
            *_RELEASE_0_4_SESSION_CHECKS,
            *_RELEASE_0_4_CONTEXT_CHECKS,
        ),
        required_cli_commands=("run", "inspect", "verify", "render"),
    )


class PackExecution(BaseModel, frozen=True):
    """An execution bound to exactly one Manifest identity."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str
    manifest_digest: str
    status: PackExecutionStatus = PackExecutionStatus.CREATED
    exit_code: int | None = None

    @model_validator(mode="after")
    def _validate_frozen_execution_state(self) -> "PackExecution":
        if not self.execution_id.strip():
            raise ValueError("execution_id must not be empty")
        if not _SHA256_DIGEST.fullmatch(self.manifest_digest):
            raise ValueError("manifest_digest must be sha256")
        expected_exit_code = {
            PackExecutionStatus.CREATED: None,
            PackExecutionStatus.RUNNING: None,
            PackExecutionStatus.PASSED: EXIT_SUCCESS,
            PackExecutionStatus.FAILED: EXIT_SUBJECT_FAILURE,
            PackExecutionStatus.INCOMPLETE: EXIT_INCOMPLETE,
            PackExecutionStatus.ERROR: EXIT_HARNESS_ERROR,
        }[self.status]
        if self.exit_code != expected_exit_code:
            raise ValueError("Pack Execution status and exit_code must agree")
        return self

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        values = self.model_dump()
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)

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
        manifest = AcceptanceManifest.model_validate(manifest.model_dump(mode="json"))
        return cls(execution_id=execution_id, manifest_digest=manifest.digest)

    def assert_matches(self, manifest: AcceptanceManifest) -> None:
        manifest = AcceptanceManifest.model_validate(manifest.model_dump(mode="json"))
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
        results = tuple(
            AcceptanceCheckResult.model_validate(dict(result))
            for result in results
        )
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
        if set(by_id) - {check.check_id for check in manifest.required_checks}:
            return self.model_copy(
                update={
                    "status": PackExecutionStatus.ERROR,
                    "exit_code": EXIT_HARNESS_ERROR,
                }
            )
        required = [by_id.get(check.check_id) for check in manifest.required_checks]
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
        statuses = {result.status for result in required if result is not None}
        if AcceptanceCheckStatus.ERROR in statuses:
            status, exit_code = PackExecutionStatus.ERROR, EXIT_HARNESS_ERROR
        elif AcceptanceCheckStatus.FAIL in statuses:
            status, exit_code = PackExecutionStatus.FAILED, EXIT_SUBJECT_FAILURE
        elif any(result is None for result in required) or any(
            check.evidence_level not in {EvidenceLevel.CONTRACT, EvidenceLevel.HOST}
            for check in manifest.required_checks
        ):
            status, exit_code = PackExecutionStatus.INCOMPLETE, EXIT_INCOMPLETE
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
    manifest: AcceptanceManifest
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
    def _assert_scenario_checks_match_execution(
        checks: tuple[AcceptanceCheckResult, ...],
        execution_checks: tuple[AcceptanceCheckResult, ...],
    ) -> None:
        execution_by_id = {result.check_id: result for result in execution_checks}
        if any(execution_by_id.get(result.check_id) != result for result in checks):
            raise ValueError("Bundle scenario checks must match execution checks")

    @staticmethod
    def _assert_declared_evidence_bindings(
        manifest: AcceptanceManifest,
        results: tuple[AcceptanceCheckResult, ...],
        evidence_view: Mapping[str, str | int | float | bool | None],
        independent_evidence: Mapping[str, str | int | float | bool | None],
    ) -> None:
        """Every required PASS must name its frozen dual-source evidence slots."""
        declared = {check.check_id: check for check in manifest.required_checks}
        for result in results:
            if result.status is not AcceptanceCheckStatus.PASS:
                continue
            check = declared[result.check_id]
            if evidence_view.get(check.authoritative_evidence) != result.evidence_digest:
                raise ValueError(
                    f"Bundle authoritative evidence does not attest {result.check_id}"
                )
            independent = independent_evidence.get(check.independent_evidence)
            if not isinstance(independent, str) or not _SHA256_DIGEST.fullmatch(
                independent
            ):
                raise ValueError(
                    f"Bundle independent evidence does not attest {result.check_id}"
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
        manifest: AcceptanceManifest,
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
            "manifest": manifest.model_dump(mode="json"),
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
        cls._assert_scenario_checks_match_execution(checks, execution_checks)
        cls._ensure_minimal_evidence(evidence_view)
        if not independent_evidence:
            raise ValueError("Scenario Evidence Bundle requires independent evidence")
        cls._ensure_minimal_evidence(independent_evidence)
        cls._assert_declared_evidence_bindings(
            manifest, execution_checks, evidence_view, independent_evidence
        )
        digest = cls._digest_payload(
            schema_version=_BUNDLE_SCHEMA_VERSION,
            manifest=manifest,
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
            manifest=manifest,
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
        manifest: AcceptanceManifest | None = None,
        execution: PackExecution | None = None,
    ) -> None:
        try:
            declared_manifest = self.manifest
            if manifest is not None and manifest.digest != declared_manifest.digest:
                raise BundleIntegrityError("Bundle does not match supplied Manifest")
            if execution is not None and execution != self.execution:
                raise BundleIntegrityError("Bundle execution does not match supplied execution")
            if self.schema_version != _BUNDLE_SCHEMA_VERSION:
                raise BundleIntegrityError("Scenario Evidence Bundle schema version is unsupported")
            if self.manifest_digest != declared_manifest.digest:
                raise BundleIntegrityError("Bundle Manifest digest does not match")
            self._assert_terminal_execution(
                declared_manifest, self.execution, self.execution_checks
            )
            if self.scenario not in declared_manifest.scenarios:
                raise ValueError(f"scenario {self.scenario!r} is not declared by Manifest")
            self._assert_declared_checks(declared_manifest, self.scenario, self.checks)
            self._assert_minimal_check_results(self.checks)
            self._assert_execution_checks(declared_manifest, self.execution_checks)
            self._assert_minimal_check_results(self.execution_checks)
            self._assert_scenario_checks_match_execution(
                self.checks, self.execution_checks
            )
            self._ensure_minimal_evidence(self.evidence_view)
            if not self.independent_evidence:
                raise ValueError("Scenario Evidence Bundle requires independent evidence")
            self._ensure_minimal_evidence(self.independent_evidence)
            self._assert_declared_evidence_bindings(
                declared_manifest,
                self.execution_checks,
                self.evidence_view,
                self.independent_evidence,
            )
        except (BundleIntegrityError, ValueError) as error:
            if isinstance(error, BundleIntegrityError):
                raise
            raise BundleIntegrityError(str(error)) from error
        expected = self._digest_payload(
            schema_version=self.schema_version,
            manifest=self.manifest,
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
