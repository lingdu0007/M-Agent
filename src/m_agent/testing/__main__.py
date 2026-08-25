"""Thin, offline-only CLI for the Acceptance Pack foundation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, NoReturn
from uuid import uuid4

from ._core_lifecycle import run_core_lifecycle
from ._durable_effects import run_durable_effects_recovery
from ._identity import (
    AcceptanceHarnessError,
    assert_sdist_builds_candidate_wheel,
    installed_identity,
    validate_installed_identity,
)
from ._pack import (
    AcceptanceCheckResult,
    AcceptanceCheckStatus,
    AcceptanceManifest,
    BundleIntegrityError,
    EXIT_HARNESS_ERROR,
    EXIT_INTEGRITY_FAILURE,
    EXIT_INVALID_INVOCATION,
    EvidenceLevel,
    PackExecution,
    PackExecutionStatus,
    ScenarioEvidenceBundle,
    core_lifecycle_manifest,
)
from ._subprocess import isolated_subprocess_environment


_ISOLATED_HOST_PROBE = """
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import m_agent
from m_agent.runtime import (
    AgentDefinition,
    Clock,
    DefinitionNotFoundError,
    DefinitionRegistry,
    FailureClassification,
    ModelCapabilities,
    ModelFailure,
    ModelResponse,
    ModelUsage,
    Runner,
    RunStatus,
    TelemetryEvent,
    TelemetryEventType,
)
from m_agent import runtime, companion, testing
import m_agent.adapters as adapters
from m_agent.adapters import (
    DeterministicModelAdapter,
    JsonlTelemetrySink,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent.testing import find_runtime_dependency_violations


def child_environment():
    allowed = {"HOME", "LANG", "LC_ALL", "PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "M_AGENT_RUN_LIVE_TESTS"}
    return {key: value for key, value in os.environ.items() if key in allowed}


_EXPAND_ONLY_EXPORTS = {
    "CrashPoint",
    "deserialize_model_response",
    "deserialize_tool_outcome",
    "serialize_model_response",
    "serialize_tool_outcome",
}
_EXPAND_ADAPTER_EXPORTS = {
    "DeterministicContextProvider",
    "DeterministicModelAdapter",
    "DeterministicStreamingModelAdapter",
    "DeterministicTool",
    "FakeClock",
    "InMemoryRunStore",
    "JsonlTelemetrySink",
    "PlaintextPayloadCodec",
    "SQLiteRunStore",
    "SystemClock",
}
_ROOT_TYPED_MODEL_CONTRACT_EXPORTS = {
    "ERROR_MODEL_EXECUTION_BUDGET_EXCEEDED",
    "ModelBinding",
    "ModelBindingSet",
    "ModelCapabilityCombination",
    "ModelContract",
    "ModelContractViolationError",
    "ModelExecutionBudget",
    "ModelLimits",
    "ModelPurpose",
    "ModelRequirementMatch",
    "ModelRequirementReason",
    "ModelRequirements",
    "ModelUsageGuarantees",
    "RevisionStability",
    "StreamingMode",
    "StructuredOutputMode",
    "ToolCallingMode",
    "UsageFieldGuarantee",
    "UsageProvenance",
    "UsageReportingMode",
}


_REOPEN_PROBE = '''
import asyncio
import json
from pathlib import Path
import sys

from m_agent.runtime import DefinitionRegistry, Runner, RunStatus
from m_agent.adapters import PlaintextPayloadCodec, SQLiteRunStore


async def inspect():
    store = SQLiteRunStore(sys.argv[1], payload_codec=PlaintextPayloadCodec())
    try:
        inspection = await Runner(DefinitionRegistry(), store).inspect_run(sys.argv[2])
        telemetry = [
            json.loads(line)
            for line in Path(sys.argv[3]).read_text(encoding="utf-8").splitlines()
            if line
        ]
        print(json.dumps({
            "run_succeeded": inspection.run.status is RunStatus.SUCCEEDED,
            "step_count": len(inspection.steps),
            "attempt_count": len(inspection.attempts),
            "checkpoint_count": len(inspection.checkpoints),
            "telemetry_readable": bool(telemetry) and all(
                event.get("run_id") == sys.argv[2] for event in telemetry
            ),
        }, sort_keys=True))
    finally:
        store.close()


asyncio.run(inspect())
'''


_TELEMETRY_CANARIES = (
    "T07-INPUT-CANARY",
    "T07-INSTRUCTION-CANARY",
    "T07-SECRET-CANARY",
)


class UsageModel(DeterministicModelAdapter):
    def __init__(self):
        super().__init__(
            ("accepted",),
            capabilities=ModelCapabilities(usage_reporting="PROVIDER_REPORTED"),
        )

    async def generate(self, request):
        response = await super().generate(request)
        return ModelResponse(
            content=response.content,
            usage=ModelUsage(
                input_tokens=11,
                output_tokens=7,
                raw_unit="tokens",
                normalization_source="deterministic-usage-v1",
            ),
        )


class FailureModel(DeterministicModelAdapter):
    def __init__(self):
        super().__init__(("unused",))

    async def generate(self, request):
        raise ModelFailure(
            FailureClassification.PERMANENT,
            "rate_limited",
            _TELEMETRY_CANARIES[2],
        )


def telemetry_sequence(events, run_id):
    return (
        [event.get("event_type") for event in events]
        == [
            "RUN_STATUS_CHANGED",
            "RUN_STATUS_CHANGED",
            "STEP_STARTED",
            "STEP_COMPLETED",
            "RUN_STATUS_CHANGED",
        ]
        and all(event.get("run_id") == run_id for event in events)
        and [
            event.get("run_status")
            for event in events
            if event.get("event_type") == "RUN_STATUS_CHANGED"
        ]
        == ["CREATED", "RUNNING", "SUCCEEDED"]
    )


def telemetry_usage_provenance(events):
    completed = [
        event for event in events if event.get("event_type") == "STEP_COMPLETED"
    ]
    usage = completed[0].get("usage") if len(completed) == 1 else None
    return (
        isinstance(usage, dict)
        and usage.get("input_tokens") == 11
        and usage.get("output_tokens") == 7
        and usage.get("provenance") == "PROVIDER_REPORTED"
        and all(
            event.get("usage") is None
            for event in events
            if event is not completed[0]
        )
    )


def telemetry_inspection_reconciled(events, inspection):
    if (
        len(inspection.steps) != 1
        or len(inspection.attempts) != 1
        or len(inspection.checkpoints) != 1
    ):
        return False
    step = inspection.steps[0]
    attempt = inspection.attempts[0]
    checkpoint = inspection.checkpoints[0]
    step_events = [
        event for event in events
        if event.get("event_type") in {"STEP_STARTED", "STEP_COMPLETED"}
    ]
    return (
        len(step_events) == 2
        and step.step_id == attempt.step_id == checkpoint.step_id
        and attempt.attempt_id == checkpoint.attempt_id
        and all(
            event.get("step_id") == step.step_id
            and event.get("attempt_id") == attempt.attempt_id
            and event.get("step_type") == step.step_type.value
            for event in step_events
        )
    )


def telemetry_failure_observed(events, inspection):
    if len(inspection.steps) != 1 or len(inspection.attempts) != 1:
        return False
    step = inspection.steps[0]
    attempt = inspection.attempts[0]
    failed = [event for event in events if event.get("event_type") == "ATTEMPT_FAILED"]
    return (
        len(failed) == 1
        and failed[0].get("step_id") == step.step_id
        and failed[0].get("attempt_id") == attempt.attempt_id
        and failed[0].get("step_type") == step.step_type.value
        and failed[0].get("step_status") == "FAILED"
        and failed[0].get("classification") == "PERMANENT"
        and failed[0].get("error_code") == "rate_limited"
        and isinstance(failed[0].get("duration_ms"), (int, float))
        and failed[0]["duration_ms"] >= 0
    )


def concurrent_telemetry(path):
    sink = JsonlTelemetrySink(path)

    def emit(index):
        for status in (RunStatus.CREATED, RunStatus.SUCCEEDED):
            sink.emit(TelemetryEvent(
                event_type=TelemetryEventType.RUN_STATUS_CHANGED,
                run_id=f"telemetry-concurrent-{index}",
                run_status=status,
            ))

    with ThreadPoolExecutor(max_workers=4) as executor:
        tuple(executor.map(emit, range(4)))
    sink.close()
    sink.close()
    events = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
    ]
    return len(events) == 8 and all(
        [event["run_status"] for event in events if event["run_id"] == f"telemetry-concurrent-{index}"]
        == ["CREATED", "SUCCEEDED"]
        for index in range(4)
    )


async def observe():
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id="core-lifecycle",
            version="1.0",
            instructions="Use the deterministic fixture. " + _TELEMETRY_CANARIES[1],
            model_adapter=UsageModel(),
        )
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        database = Path(temporary_directory) / "core-lifecycle.sqlite3"
        telemetry_path = Path(temporary_directory) / "core-lifecycle.jsonl"
        store = SQLiteRunStore(database, payload_codec=PlaintextPayloadCodec())
        sink = JsonlTelemetrySink(telemetry_path)
        runner = Runner(registry=registry, store=store, telemetry_sink=sink)
        created = await runner.create_run(
            "core-lifecycle",
            "1.0",
            "fixture " + _TELEMETRY_CANARIES[0] + " " + _TELEMETRY_CANARIES[2],
        )
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)
        sink.close()
        sink.close()
        telemetry_bytes = telemetry_path.read_bytes()
        telemetry_lines = telemetry_bytes.decode("utf-8").splitlines()
        telemetry_events = [json.loads(line) for line in telemetry_lines if line]
        failure_path = Path(temporary_directory) / "failure.jsonl"
        failure_sink = JsonlTelemetrySink(failure_path)
        failure_registry = DefinitionRegistry()
        failure_registry.register(AgentDefinition.for_adapter(
            definition_id="telemetry-failure",
            version="1.0",
            instructions=_TELEMETRY_CANARIES[1],
            model_adapter=FailureModel(),
        ))
        failure_store = SQLiteRunStore(
            Path(temporary_directory) / "failure.sqlite3",
            payload_codec=PlaintextPayloadCodec(),
        )
        failure_runner = Runner(
            registry=failure_registry,
            store=failure_store,
            telemetry_sink=failure_sink,
        )
        failed_created = await failure_runner.create_run(
            "telemetry-failure",
            "1.0",
            "failure " + _TELEMETRY_CANARIES[0] + " " + _TELEMETRY_CANARIES[2],
        )
        failed_terminal = await failure_runner.start_run(failed_created.run_id)
        failed_inspection = await failure_runner.inspect_run(failed_created.run_id)
        failure_sink.close()
        failure_sink.close()
        failure_store.close()
        failure_bytes = failure_path.read_bytes()
        failure_events = [
            json.loads(line)
            for line in failure_bytes.decode("utf-8").splitlines()
            if line
        ]

        concurrent_path = Path(temporary_directory) / "concurrent.jsonl"
        telemetry_concurrent = concurrent_telemetry(concurrent_path)
        concurrent_bytes = concurrent_path.read_bytes()
        store.close()
        reopened_process = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                _REOPEN_PROBE,
                str(database),
                created.run_id,
                str(telemetry_path),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=child_environment(),
        )
        try:
            reopened = json.loads(reopened_process.stdout)
        except json.JSONDecodeError:
            reopened = {}
        sqlite_file_created = database.is_file() and database.stat().st_size > 0
        telemetry_ordered = telemetry_sequence(telemetry_events, created.run_id)
        telemetry_reconciled = telemetry_inspection_reconciled(
            telemetry_events, inspection
        )
        telemetry_usage = telemetry_usage_provenance(telemetry_events)
        completions = [
            event for event in telemetry_events
            if event.get("event_type") == "STEP_COMPLETED"
        ]
        model_events = [
            event
            for event in telemetry_events
            if event.get("event_type") in {"STEP_STARTED", "STEP_COMPLETED"}
            and event.get("step_type") == "MODEL"
        ]
        telemetry_model_purpose = (
            len(model_events) == 2
            and all(
                event.get("model_purpose") == "PRIMARY"
                for event in model_events
            )
        )
        telemetry_duration = (
            telemetry_model_purpose
            and isinstance(completions[0].get("duration_ms"), (int, float))
            and completions[0]["duration_ms"] >= 0
        )
        telemetry_error = (
            failed_terminal.status is RunStatus.FAILED
            and telemetry_failure_observed(failure_events, failed_inspection)
        )
        telemetry_closed = (
            len(telemetry_events) == 5
            and telemetry_bytes.endswith(b"\\n")
            and all(telemetry_lines)
        )
        telemetry_cross_process = (
            reopened_process.returncode == 0
            and reopened.get("telemetry_readable") is True
        )
        telemetry_redacted = all(
            canary.encode("utf-8") not in payload
            for canary in _TELEMETRY_CANARIES
            for payload in (telemetry_bytes, failure_bytes, concurrent_bytes)
        )
        telemetry_jsonl_digest = "sha256:" + hashlib.sha256(
            telemetry_bytes + failure_bytes + concurrent_bytes
        ).hexdigest()
        permission_directory = Path(temporary_directory) / "no-write"
        permission_directory.mkdir()
        permission_directory.chmod(0o500)
        try:
            (permission_directory / "denied").write_text("denied", encoding="utf-8")
        except PermissionError:
            filesystem_permission_boundary_observed = True
        else:
            filesystem_permission_boundary_observed = False
        finally:
            permission_directory.chmod(0o700)
    try:
        registry.resolve("unknown", "1.0")
    except DefinitionNotFoundError:
        unknown_definition_rejected = True
    else:
        unknown_definition_rejected = False
    root_exports = tuple(m_agent.__all__)
    root_runtime_exports = set(runtime.__all__) - _ROOT_TYPED_MODEL_CONTRACT_EXPORTS
    root_migration_reset = (
        len(root_exports) == 7
        and set(root_exports)
        == {
            "AgentDefinition", "DefinitionRegistry", "Runner", "SyncRunner",
            "RunStatus", "RunRecord", "RunInspection",
        }
    )
    print(json.dumps({
        "module_under_prefix": Path(m_agent.__file__).resolve().is_relative_to(
            Path(sys.prefix).resolve()
        ),
        "run_succeeded": terminal.status is RunStatus.SUCCEEDED,
        "step_count": len(inspection.steps),
        "attempt_count": len(inspection.attempts),
        "checkpoint_count": len(inspection.checkpoints),
        "sqlite_file_created": sqlite_file_created,
        "restart_observed": reopened_process.returncode == 0,
        "reopened_run_succeeded": reopened.get("run_succeeded") is True,
        "reopened_step_count": reopened.get("step_count"),
        "reopened_attempt_count": reopened.get("attempt_count"),
        "reopened_checkpoint_count": reopened.get("checkpoint_count"),
        "unknown_definition_rejected": unknown_definition_rejected,
        "public_layers_available": (
            Runner is runtime.Runner
            and Clock is runtime.Clock
            and adapters.DeterministicModelAdapter is DeterministicModelAdapter
            and hasattr(companion, "__all__")
            and testing.AcceptanceManifest is not None
        ),
        "runtime_dependency_violation_count": len(find_runtime_dependency_violations()),
        "root_migration_reset": root_migration_reset,
        "telemetry_ordered": telemetry_ordered,
        "telemetry_inspection_reconciled": telemetry_reconciled,
        "telemetry_usage_provenance": telemetry_usage,
        "telemetry_error_observed": telemetry_error,
        "telemetry_model_purpose_observed": telemetry_model_purpose,
        "telemetry_duration_observed": telemetry_duration,
        "telemetry_closed": telemetry_closed,
        "telemetry_cross_process": telemetry_cross_process,
        "telemetry_concurrent": telemetry_concurrent,
        "telemetry_redacted": telemetry_redacted,
        "filesystem_permission_boundary_observed": filesystem_permission_boundary_observed,
        "telemetry_jsonl_digest": telemetry_jsonl_digest,
        "credential_canary_absent": all(
            name not in os.environ
            for name in (
                "M_AGENT_OPENAI_API_KEY",
                "OPENAI_API_KEY",
                "M_AGENT_OPENAI_BASE_URL",
                "OPENAI_BASE_URL",
                "AGENT_BASE_URL",
            )
        ),
    }, sort_keys=True))


asyncio.run(observe())
"""

_RUN_STATE_FILE = ".core-lifecycle-running.json"
_RUN_STATE_SCHEMA_VERSION = "1"


def _read_manifest(path: Path) -> AcceptanceManifest:
    return AcceptanceManifest.model_validate_json(path.read_text())


def _read_bundle(path: Path) -> ScenarioEvidenceBundle:
    return ScenarioEvidenceBundle.model_validate_json(path.read_text())


def _assert_core_lifecycle_manifest(manifest: AcceptanceManifest) -> None:
    expected = core_lifecycle_manifest(
        source_commit=manifest.source_commit,
        artifact_digest=manifest.artifact_digest,
        sdist_digest=manifest.sdist_digest,
        fixture_digest=manifest.fixture_digest,
        environment=manifest.environment,
    )
    if manifest != expected:
        raise ValueError(
            "CLI supports only the frozen core-lifecycle foundation profile"
        )


def _assert_runtime_baseline_manifest(manifest: AcceptanceManifest) -> None:
    from ._pack import runtime_baseline_manifest

    expected = runtime_baseline_manifest(
        source_commit=manifest.source_commit,
        artifact_digest=manifest.artifact_digest,
        sdist_digest=manifest.sdist_digest,
        fixture_digest=manifest.fixture_digest,
        environment=manifest.environment,
    )
    if manifest != expected:
        raise ValueError("Manifest is not the frozen 0.3 Runtime Baseline profile")


def _assert_supported_manifest(manifest: AcceptanceManifest) -> None:
    if manifest.profile == "core-lifecycle-foundation":
        _assert_core_lifecycle_manifest(manifest)
    elif manifest.profile == "runtime-baseline-0-3":
        _assert_runtime_baseline_manifest(manifest)
    elif manifest.profile == "foundation-release-0-4":
        _assert_foundation_release_manifest(manifest)
    else:
        raise ValueError(f"unsupported Acceptance Pack profile: {manifest.profile}")


def _assert_foundation_release_manifest(manifest: AcceptanceManifest) -> None:
    from ._pack import foundation_release_0_4_manifest

    expected = foundation_release_0_4_manifest(
        source_commit=manifest.source_commit,
        artifact_digest=manifest.artifact_digest,
        sdist_digest=manifest.sdist_digest,
        fixture_digest=manifest.fixture_digest,
        environment=manifest.environment,
    )
    if manifest != expected:
        raise ValueError("Manifest is not the frozen 0.4 foundation-release profile")


def _verified_bundle(arguments: argparse.Namespace) -> ScenarioEvidenceBundle:
    bundle = _read_bundle(arguments.bundle)
    if arguments.bundle.name != f"{bundle.content_digest.removeprefix('sha256:')}.json":
        raise BundleIntegrityError("Bundle path does not match content digest")
    if arguments.manifest is not None:
        supplied = _read_manifest(arguments.manifest)
        if supplied.digest != bundle.manifest.digest:
            raise BundleIntegrityError("Bundle does not match supplied Manifest")
    validate_installed_identity(
        bundle.manifest,
        artifact=arguments.wheel,
        sdist=arguments.sdist,
        verify_sdist_build=False,
    )
    bundle.verify()
    _assert_supported_manifest(bundle.manifest)
    return bundle


def _state_digest(payload: object) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _run_state_path(output_dir: Path) -> Path:
    return output_dir / _RUN_STATE_FILE


def _write_run_state(
    output_dir: Path,
    manifest: AcceptanceManifest,
    execution: PackExecution,
    bundle: ScenarioEvidenceBundle | None = None,
) -> None:
    """Atomically persist the one Scenario that this thin CLI can resume."""
    payload: dict[str, object] = {
        "schema_version": _RUN_STATE_SCHEMA_VERSION,
        "manifest": manifest.model_dump(mode="json"),
        "manifest_digest": manifest.digest,
        "execution": execution.model_dump(mode="json"),
    }
    if bundle is not None:
        bundle.verify(manifest, execution)
        payload["bundle"] = bundle.model_dump(mode="json")
    state = {**payload, "content_digest": _state_digest(payload)}
    output_dir.mkdir(parents=True, exist_ok=True)
    path = _run_state_path(output_dir)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as output:
        output.write(json.dumps(state, ensure_ascii=True, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _load_run_state(
    output_dir: Path, manifest: AcceptanceManifest
) -> tuple[PackExecution, ScenarioEvidenceBundle | None] | None:
    path = _run_state_path(output_dir)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            raise ValueError("Pack state is not an object")
        content_digest = state.pop("content_digest")
        if not isinstance(content_digest, str) or content_digest != _state_digest(state):
            raise ValueError("Pack state content digest does not match")
        has_bundle = "bundle" in state
        expected = {
            "schema_version",
            "manifest",
            "manifest_digest",
            "execution",
        } | ({"bundle"} if has_bundle else set())
        if set(state) != expected or state["schema_version"] != _RUN_STATE_SCHEMA_VERSION:
            raise ValueError("Pack state schema is unsupported")
        saved_manifest = AcceptanceManifest.model_validate(state["manifest"])
        if saved_manifest != manifest or state["manifest_digest"] != manifest.digest:
            raise ValueError("Pack state belongs to a different Manifest")
        execution = PackExecution.model_validate(state["execution"])
        execution.assert_matches(manifest)
        if has_bundle:
            bundle = ScenarioEvidenceBundle.model_validate(state["bundle"])
            bundle.verify(manifest, execution)
            return execution, bundle
        if execution.status is not PackExecutionStatus.RUNNING:
            raise ValueError("Pack state is not resumable")
        return execution, None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise BundleIntegrityError(f"Pack state is invalid: {error}") from error


def _clear_run_state(output_dir: Path) -> None:
    _run_state_path(output_dir).unlink(missing_ok=True)


def _attest_declared_evidence(
    manifest: AcceptanceManifest,
    results: tuple[AcceptanceCheckResult, ...],
    evidence_view: Mapping[str, str | int | float | bool | None],
    independent_evidence: Mapping[str, str | int | float | bool | None],
    *,
    host_observation_digest: str,
) -> tuple[
    dict[str, str | int | float | bool | None],
    dict[str, str | int | float | bool | None],
]:
    """Attach every frozen evidence slot to the result it actually attests."""
    declared = {check.check_id: check for check in manifest.required_checks}
    authoritative = dict(evidence_view)
    independent = dict(independent_evidence)
    for result in results:
        check = declared[result.check_id]
        authoritative[check.authoritative_evidence] = result.evidence_digest
        if check.check_id == "core.lifecycle":
            independent[check.independent_evidence] = host_observation_digest
        elif check.check_id == "core.lifecycle.host-wheel":
            independent[check.independent_evidence] = independent[
                "host_wheel_identity_mutation_digest"
            ]
        elif check.check_id == "core.lifecycle.bundle-tamper":
            independent[check.independent_evidence] = independent[
                "bundle_mutation_independent_digest"
            ]
        elif check.check_id == "core.lifecycle.telemetry":
            independent[check.independent_evidence] = independent[
                "telemetry_jsonl_digest"
            ]
        elif check.check_id == "core.lifecycle.telemetry-host":
            independent[check.independent_evidence] = independent[
                "telemetry_host_observation_digest"
            ]
        elif check.check_id == "core.lifecycle.migration":
            independent[check.independent_evidence] = independent["migration_table_digest"]
        elif check.check_id == "durable.effects.host-wheel":
            independent[check.independent_evidence] = independent[
                "durable_host_independent_digest"
            ]
        elif check.check_id == "durable.effects.recovery-windows":
            independent[check.independent_evidence] = independent[
                "recovery_windows_journal_digest"
            ]
        elif check.check_id == "durable.effects.budget-fail-closed":
            independent[check.independent_evidence] = independent[
                "budget_fail_closed_journal_digest"
            ]
        elif check.check_id == "durable.effects.waiting-resolution":
            independent[check.independent_evidence] = independent[
                "waiting_resolution_journal_digest"
            ]
        elif check.check_id == "durable.effects.mutation":
            independent[check.independent_evidence] = independent[
                "mutation_independent_digest"
            ]
        else:
            independent[check.independent_evidence] = host_observation_digest
    return authoritative, independent


def _controlled_bundle_mutation_evidence(
    bundle: ScenarioEvidenceBundle,
    manifest: AcceptanceManifest,
    execution: PackExecution,
) -> tuple[str, str]:
    """Derive evidence only after the public integrity seam rejects a mutation."""
    tampered = bundle.model_copy(
        update={"evidence_view": {**bundle.evidence_view, "run_succeeded": False}}
    )
    try:
        tampered.verify(manifest, execution)
    except BundleIntegrityError:
        mutated_payload_digest = "sha256:" + hashlib.sha256(
            tampered.model_dump_json().encode("utf-8")
        ).hexdigest()
        observation_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                {
                    "original_content_digest": bundle.content_digest,
                    "mutated_payload_digest": mutated_payload_digest,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return observation_digest, mutated_payload_digest
    raise RuntimeError("controlled Bundle mutation was not detected")


def _controlled_identity_mutation_evidence(
    manifest: AcceptanceManifest, *, artifact: Path, sdist: Path
) -> str:
    """Prove the public identity gate rejects every frozen subject mutation."""
    mutations: tuple[tuple[str, object], ...] = (
        ("source_commit", "0" * 40),
        ("artifact_digest", "sha256:" + "0" * 64),
        ("sdist_digest", "sha256:" + "0" * 64),
        ("fixture_digest", "sha256:" + "0" * 64),
        (
            "environment",
            {
                **dict(manifest.environment),
                "os": "counterfeit" if manifest.environment.get("os") != "counterfeit" else "other",
            },
        ),
    )
    rejected: list[str] = []
    for field, value in mutations:
        try:
            candidate = manifest.model_copy(update={field: value})
            validate_installed_identity(
                candidate,
                artifact=artifact,
                sdist=sdist,
                verify_sdist_build=False,
            )
        except ValueError:
            rejected.append(field)
        else:
            raise RuntimeError(f"identity mutation was accepted: {field}")
    expected = [field for field, _ in mutations]
    if rejected != expected:
        raise RuntimeError("identity mutation evidence is incomplete")
    measured = installed_identity(artifact=artifact, sdist=sdist)
    payload = {
        "manifest_digest": manifest.digest,
        "measured_identity": measured,
        "rejected_fields": rejected,
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _evidence_digest(payload: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _complete_host_wheel_terminal(
    output_dir: Path,
    manifest: AcceptanceManifest,
    execution: PackExecution,
    *,
    status: AcceptanceCheckStatus,
    reason_code: str,
) -> int:
    """Persist a completed HOST-wheel observation with its Pack verdict."""
    results = tuple(
        AcceptanceCheckResult(
            check_id=check.check_id,
            status=(
                status
                if check.check_id == "core.lifecycle.host-wheel"
                else AcceptanceCheckStatus.NOT_RUN
            ),
            evidence_level=check.evidence_level,
            reason_code=(
                reason_code
                if check.check_id == "core.lifecycle.host-wheel"
                else "not_run_after_host_wheel_terminal"
            ),
            evidence_digest=_evidence_digest(
                {
                    "check_id": check.check_id,
                    "manifest_digest": manifest.digest,
                    "status": (
                        status.value
                        if check.check_id == "core.lifecycle.host-wheel"
                        else "NOT_RUN"
                    ),
                }
            ),
        )
        for check in manifest.required_checks
    )
    completed = execution.complete(manifest, results)
    bundle = ScenarioEvidenceBundle.create(
        manifest=manifest,
        execution=completed,
        execution_checks=results,
        scenario="core-lifecycle",
        checks=results,
        evidence_view={"host_wheel_sdist_rebuild_matches": False},
        independent_evidence={
            "host_wheel_sdist_provenance_digest": _evidence_digest(
                {"manifest_digest": manifest.digest, "outcome": reason_code}
            )
        },
    )
    _write_run_state(output_dir, manifest, completed, bundle)
    _publish_bundle(output_dir, bundle)
    _clear_run_state(output_dir)
    return completed.exit_code or EXIT_HARNESS_ERROR


def _durable_host_result(
    durable_checks: tuple[AcceptanceCheckResult, ...],
    durable_view: dict[str, str | int | bool],
    provenance_digest: str,
) -> AcceptanceCheckResult:
    """Derive the HOST durable check from the wheel-isolated observation."""
    return next(
        result for result in durable_checks if result.check_id == "durable.effects.host-wheel"
    ).model_copy(
        update={
            "status": (
                AcceptanceCheckStatus.PASS
                if all(
                    durable_view[key]
                    for key in (
                        "recovery_windows_clean",
                        "recovery_windows_no_budget_reset",
                        "waiting_semantics_observed",
                    )
                )
                else AcceptanceCheckStatus.FAIL
            ),
            "reason_code": "isolated_wheel_durable_recovery_observed",
            "evidence_digest": _evidence_digest({
                "durable_view": durable_view,
                "provenance_digest": provenance_digest,
            }),
        }
    )


def _complete_release_host_wheel_terminal(
    output_dir: Path,
    manifest: AcceptanceManifest,
    execution: PackExecution,
    *,
    status: AcceptanceCheckStatus,
    reason_code: str,
) -> int:
    """Persist a completed HOST-wheel observation with one Bundle per Scenario."""
    results = tuple(
        AcceptanceCheckResult(
            check_id=check.check_id,
            status=(
                status
                if check.check_id == "core.lifecycle.host-wheel"
                else AcceptanceCheckStatus.NOT_RUN
            ),
            evidence_level=check.evidence_level,
            reason_code=(
                reason_code
                if check.check_id == "core.lifecycle.host-wheel"
                else "not_run_after_host_wheel_terminal"
            ),
            evidence_digest=_evidence_digest(
                {
                    "check_id": check.check_id,
                    "manifest_digest": manifest.digest,
                    "status": (
                        status.value
                        if check.check_id == "core.lifecycle.host-wheel"
                        else "NOT_RUN"
                    ),
                }
            ),
        )
        for check in manifest.required_checks
    )
    completed = execution.complete(manifest, results)
    declared_by_id = {check.check_id: check for check in manifest.required_checks}
    evidence_view = {"host_wheel_sdist_rebuild_matches": False}
    independent_evidence = {
        "host_wheel_sdist_provenance_digest": _evidence_digest(
            {"manifest_digest": manifest.digest, "outcome": reason_code}
        )
    }
    for scenario in manifest.scenarios:
        bundle = ScenarioEvidenceBundle.create(
            manifest=manifest,
            execution=completed,
            execution_checks=results,
            scenario=scenario,
            checks=tuple(
                result for result in results
                if declared_by_id[result.check_id].scenario == scenario
            ),
            evidence_view=dict(evidence_view),
            independent_evidence=dict(independent_evidence),
        )
        _publish_bundle(output_dir, bundle)
    _write_run_state(output_dir, manifest, completed)
    _clear_run_state(output_dir)
    return completed.exit_code or EXIT_HARNESS_ERROR


def _attest_release_evidence(
    manifest: AcceptanceManifest,
    results: tuple[AcceptanceCheckResult, ...],
    evidence_view: Mapping[str, str | int | float | bool | None],
    independent_evidence: Mapping[str, str | int | float | bool | None],
    *,
    host_observation_digest: str,
    session_host_independent_digest: str,
    context_host_independent_digest: str,
) -> tuple[
    dict[str, str | int | float | bool | None],
    dict[str, str | int | float | bool | None],
]:
    """Attach every frozen 0.4 evidence slot to the result it attests.

    Session / Context 的 CONTRACT 槽位在进入本函数前已由带前缀的场景
    证据填充；本函数只处理必须绑定 HOST 观察、变异实验或 identity
    实验的检查。
    """
    declared = {check.check_id: check for check in manifest.required_checks}
    authoritative = dict(evidence_view)
    independent = dict(independent_evidence)
    host_bound_slots = {
        "core.lifecycle": host_observation_digest,
        "core.lifecycle.unknown-definition": host_observation_digest,
        "core.lifecycle.public-namespaces": host_observation_digest,
        "core.lifecycle.dependency-direction": host_observation_digest,
        "core.lifecycle.host-wheel": independent["host_wheel_identity_mutation_digest"],
        "core.lifecycle.telemetry-host": independent["telemetry_host_observation_digest"],
        "core.lifecycle.bundle-tamper": independent["bundle_mutation_independent_digest"],
        "session.conversation.host-wheel": session_host_independent_digest,
        "context.compression.host-wheel": context_host_independent_digest,
    }
    for result in results:
        check = declared[result.check_id]
        authoritative[check.authoritative_evidence] = result.evidence_digest
        if check.check_id in host_bound_slots:
            independent[check.independent_evidence] = host_bound_slots[check.check_id]
    return authoritative, independent


def _run_foundation_release_0_4(
    arguments: argparse.Namespace, manifest: AcceptanceManifest
) -> int:
    """Execute all four 0.4 Scenarios against one exact wheel-bound execution."""
    from ._context_compression import run_context_budget_compression
    from ._release import (
        CONTEXT_HOST_PROBE,
        SESSION_HOST_PROBE,
        observe_isolated_scenario_probe,
    )
    from ._session_conversation import run_session_conversation

    _assert_foundation_release_manifest(manifest)
    prior = _load_run_state(arguments.output_dir, manifest)
    if prior is not None and prior[1] is not None:
        execution, bundle = prior
        assert bundle is not None  # narrowed by the prior[1] check above
        _publish_bundle(arguments.output_dir, bundle)
        _clear_run_state(arguments.output_dir)
        return execution.exit_code or 0
    execution = (
        prior[0]
        if prior is not None
        else PackExecution.create(
            manifest, execution_id=f"foundation-release-0-4-{uuid4().hex}"
        ).start(manifest)
    )
    _write_run_state(arguments.output_dir, manifest, execution)
    try:
        provenance_digest = assert_sdist_builds_candidate_wheel(
            arguments.sdist, arguments.wheel
        )
    except AcceptanceHarnessError:
        return _complete_release_host_wheel_terminal(
            arguments.output_dir, manifest, execution,
            status=AcceptanceCheckStatus.ERROR,
            reason_code="supplied_sdist_rebuild_harness_error",
        )
    except ValueError:
        return _complete_release_host_wheel_terminal(
            arguments.output_dir, manifest, execution,
            status=AcceptanceCheckStatus.FAIL,
            reason_code="supplied_sdist_rebuild_failed",
        )

    core_checks, core_view, core_independent = asyncio.run(
        run_core_lifecycle(fixture_digest=manifest.fixture_digest)
    )
    durable_checks, durable_view, durable_independent = run_durable_effects_recovery()
    session_checks, session_view, session_independent = run_session_conversation()
    context_checks, context_view, context_independent = (
        run_context_budget_compression()
    )
    host_results, host_evidence = _isolated_host_result()
    host_wheel_identity_mutation_digest = _controlled_identity_mutation_evidence(
        manifest, artifact=arguments.wheel, sdist=arguments.sdist
    )
    durable_host = _durable_host_result(durable_checks, durable_view, provenance_digest)
    durable_checks = tuple(
        durable_host if result.check_id == durable_host.check_id else result
        for result in durable_checks
    )
    session_host, session_host_evidence = observe_isolated_scenario_probe(
        probe_source=SESSION_HOST_PROBE,
        check_id="session.conversation.host-wheel",
    )
    context_host, context_host_evidence = observe_isolated_scenario_probe(
        probe_source=CONTEXT_HOST_PROBE,
        check_id="context.compression.host-wheel",
    )
    session_host = session_host.model_copy(
        update={
            "evidence_digest": _evidence_digest({
                "host": session_host.evidence_digest,
                "provenance_digest": provenance_digest,
            })
        }
    )
    context_host = context_host.model_copy(
        update={
            "evidence_digest": _evidence_digest({
                "host": context_host.evidence_digest,
                "provenance_digest": provenance_digest,
            })
        }
    )
    host_results = tuple(
        result.model_copy(
            update={
                "evidence_digest": _evidence_digest({
                    "host": result.evidence_digest,
                    "provenance_digest": provenance_digest,
                })
            }
        )
        for result in host_results
    )

    required_ids = {check.check_id for check in manifest.required_checks}
    core_checks = tuple(result for result in core_checks if result.check_id in required_ids)
    durable_checks = tuple(
        result for result in durable_checks if result.check_id in required_ids
    )
    session_checks = tuple(
        result for result in session_checks if result.check_id in required_ids
    )
    context_checks = tuple(
        result for result in context_checks if result.check_id in required_ids
    )

    def prefixed(
        view: Mapping[str, str | int | bool], prefix: str
    ) -> dict[str, str | int | bool]:
        return {f"{prefix}_{key}": value for key, value in view.items()}

    session_evidence_view = prefixed(session_view, "session")
    context_evidence_view = prefixed(context_view, "context")
    session_independent_view = prefixed(session_independent, "session")
    context_independent_view = prefixed(context_independent, "context")

    bundle_check = next(
        check for check in manifest.required_checks if check.check_id == "core.lifecycle.bundle-tamper"
    )
    provisional_bundle_result = AcceptanceCheckResult(
        check_id=bundle_check.check_id,
        status=AcceptanceCheckStatus.PASS,
        evidence_level=bundle_check.evidence_level,
        reason_code="bundle_mutation_detected",
        evidence_digest=_evidence_digest({"manifest": manifest.digest, "mutation": True}),
    )
    checks_without_bundle = (
        *core_checks,
        *host_results,
        *durable_checks,
        *session_checks,
        session_host,
        *context_checks,
        context_host,
    )
    provisional_results = (*checks_without_bundle, provisional_bundle_result)
    provisional_execution = execution.complete(manifest, provisional_results)
    evidence_view: dict[str, str | int | float | bool | None] = {
        **core_view,
        **durable_view,
        **session_evidence_view,
        **context_evidence_view,
        "host_wheel_sdist_rebuild_matches": True,
        "host_wheel_identity_mismatches_rejected": True,
        "host_wheel_sdist_provenance_digest": provenance_digest,
    }
    independent_evidence: dict[str, str | int | float | bool | None] = {
        **core_independent,
        **durable_independent,
        **host_evidence,
        **session_independent_view,
        **context_independent_view,
        "host_wheel_independent_digest": host_evidence["host_observation_digest"],
        "telemetry_host_independent_digest": host_evidence["telemetry_host_observation_digest"],
        "durable_host_independent_digest": durable_independent["recovery_windows_journal_digest"],
        "migration_table_digest": _evidence_digest({"migration": "0.4-release"}),
        "host_wheel_identity_mutation_digest": host_wheel_identity_mutation_digest,
        "bundle_tamper_independent_digest": _evidence_digest({"mutation": True}),
        "bundle_mutation_independent_digest": _evidence_digest({"mutation": True}),
    }
    evidence_view, independent_evidence = _attest_release_evidence(
        manifest, provisional_results, evidence_view, independent_evidence,
        host_observation_digest=str(host_evidence["host_observation_digest"]),
        session_host_independent_digest=str(session_host_evidence["probe_stdout_digest"]),
        context_host_independent_digest=str(context_host_evidence["probe_stdout_digest"]),
    )
    provisional_core_bundle = ScenarioEvidenceBundle.create(
        manifest=manifest, execution=provisional_execution,
        execution_checks=provisional_results, scenario="core-lifecycle",
        checks=tuple(result for result in provisional_results if result.check_id.startswith("core.lifecycle")),
        evidence_view=evidence_view, independent_evidence=independent_evidence,
    )
    mutation_digest, mutation_independent_digest = _controlled_bundle_mutation_evidence(
        provisional_core_bundle, manifest, provisional_execution
    )
    final_results = tuple(
        provisional_bundle_result.model_copy(update={"evidence_digest": mutation_digest})
        if result.check_id == provisional_bundle_result.check_id else result
        for result in provisional_results
    )
    completed = execution.complete(manifest, final_results)
    independent_evidence["bundle_tamper_independent_digest"] = mutation_independent_digest
    evidence_view, independent_evidence = _attest_release_evidence(
        manifest, final_results, evidence_view, independent_evidence,
        host_observation_digest=str(host_evidence["host_observation_digest"]),
        session_host_independent_digest=str(session_host_evidence["probe_stdout_digest"]),
        context_host_independent_digest=str(context_host_evidence["probe_stdout_digest"]),
    )
    declared_by_id = {check.check_id: check for check in manifest.required_checks}
    for scenario in manifest.scenarios:
        scenario_results = tuple(
            result for result in final_results
            if declared_by_id[result.check_id].scenario == scenario
        )
        bundle = ScenarioEvidenceBundle.create(
            manifest=manifest, execution=completed,
            execution_checks=final_results, scenario=scenario,
            checks=scenario_results, evidence_view=evidence_view,
            independent_evidence=independent_evidence,
        )
        _publish_bundle(arguments.output_dir, bundle)
    _write_run_state(arguments.output_dir, manifest, completed)
    _clear_run_state(arguments.output_dir)
    return completed.exit_code or 0


def _run_runtime_baseline(
    arguments: argparse.Namespace, manifest: AcceptanceManifest
) -> int:
    """Execute both 0.3 Scenarios against one exact wheel-bound execution."""
    _assert_runtime_baseline_manifest(manifest)
    prior = _load_run_state(arguments.output_dir, manifest)
    if prior is not None and prior[1] is not None:
        execution, bundle = prior
        assert bundle is not None  # narrowed by the prior[1] check above
        _publish_bundle(arguments.output_dir, bundle)
        _clear_run_state(arguments.output_dir)
        return execution.exit_code or 0
    execution = (
        prior[0]
        if prior is not None
        else PackExecution.create(
            manifest, execution_id=f"runtime-baseline-{uuid4().hex}"
        ).start(manifest)
    )
    _write_run_state(arguments.output_dir, manifest, execution)
    try:
        provenance_digest = assert_sdist_builds_candidate_wheel(
            arguments.sdist, arguments.wheel
        )
    except AcceptanceHarnessError:
        return _complete_host_wheel_terminal(
            arguments.output_dir, manifest, execution,
            status=AcceptanceCheckStatus.ERROR,
            reason_code="supplied_sdist_rebuild_harness_error",
        )
    except ValueError:
        return _complete_host_wheel_terminal(
            arguments.output_dir, manifest, execution,
            status=AcceptanceCheckStatus.FAIL,
            reason_code="supplied_sdist_rebuild_failed",
        )

    core_checks, core_view, core_independent = asyncio.run(
        run_core_lifecycle(fixture_digest=manifest.fixture_digest)
    )
    durable_checks, durable_view, durable_independent = run_durable_effects_recovery()
    host_results, host_evidence = _isolated_host_result()
    host_wheel_identity_mutation_digest = _controlled_identity_mutation_evidence(
        manifest, artifact=arguments.wheel, sdist=arguments.sdist
    )
    durable_host = _durable_host_result(durable_checks, durable_view, provenance_digest)
    durable_checks = tuple(
        durable_host if result.check_id == durable_host.check_id else result
        for result in durable_checks
    )
    host_results = tuple(
        result.model_copy(
            update={
                "evidence_digest": _evidence_digest({
                    "host": result.evidence_digest,
                    "provenance_digest": provenance_digest,
                })
            }
        )
        for result in host_results
    )
    required_ids = {check.check_id for check in manifest.required_checks}
    core_checks = tuple(result for result in core_checks if result.check_id in required_ids)
    checks_without_bundle = (*core_checks, *host_results, *durable_checks)
    bundle_check = next(
        check for check in manifest.required_checks if check.check_id == "core.lifecycle.bundle-tamper"
    )
    provisional_bundle_result = AcceptanceCheckResult(
        check_id=bundle_check.check_id,
        status=AcceptanceCheckStatus.PASS,
        evidence_level=bundle_check.evidence_level,
        reason_code="bundle_mutation_detected",
        evidence_digest=_evidence_digest({"manifest": manifest.digest, "mutation": True}),
    )
    provisional_results = tuple(
        (*checks_without_bundle, provisional_bundle_result)
    )
    provisional_execution = execution.complete(manifest, provisional_results)
    evidence_view: dict[str, str | int | float | bool | None] = {
        **core_view,
        **durable_view,
        "host_wheel_sdist_rebuild_matches": True,
        "host_wheel_identity_mismatches_rejected": True,
        "host_wheel_sdist_provenance_digest": provenance_digest,
    }
    independent_evidence: dict[str, str | int | float | bool | None] = {
        **core_independent,
        **durable_independent,
        **host_evidence,
        "host_wheel_independent_digest": host_evidence["host_observation_digest"],
        "telemetry_host_independent_digest": host_evidence["telemetry_host_observation_digest"],
        "durable_host_independent_digest": durable_independent["recovery_windows_journal_digest"],
        "migration_table_digest": _evidence_digest({"migration": "0.3-reset"}),
        "host_wheel_identity_mutation_digest": host_wheel_identity_mutation_digest,
        "bundle_tamper_independent_digest": _evidence_digest({"mutation": True}),
        "bundle_mutation_independent_digest": _evidence_digest({"mutation": True}),
    }
    evidence_view, independent_evidence = _attest_declared_evidence(
        manifest, provisional_results, evidence_view, independent_evidence,
        host_observation_digest=str(host_evidence["host_observation_digest"]),
    )
    provisional_core_bundle = ScenarioEvidenceBundle.create(
        manifest=manifest, execution=provisional_execution,
        execution_checks=provisional_results, scenario="core-lifecycle",
        checks=tuple(result for result in provisional_results if result.check_id.startswith("core.lifecycle")),
        evidence_view=evidence_view, independent_evidence=independent_evidence,
    )
    mutation_digest, mutation_independent_digest = _controlled_bundle_mutation_evidence(
        provisional_core_bundle, manifest, provisional_execution
    )
    final_results = tuple(
        provisional_bundle_result.model_copy(update={"evidence_digest": mutation_digest})
        if result.check_id == provisional_bundle_result.check_id else result
        for result in provisional_results
    )
    completed = execution.complete(manifest, final_results)
    independent_evidence["bundle_tamper_independent_digest"] = mutation_independent_digest
    evidence_view, independent_evidence = _attest_declared_evidence(
        manifest, final_results, evidence_view, independent_evidence,
        host_observation_digest=str(host_evidence["host_observation_digest"]),
    )
    for scenario in manifest.scenarios:
        scenario_results = tuple(
            result for result in final_results
            if next(check for check in manifest.required_checks if check.check_id == result.check_id).scenario == scenario
        )
        bundle = ScenarioEvidenceBundle.create(
            manifest=manifest, execution=completed,
            execution_checks=final_results, scenario=scenario,
            checks=scenario_results, evidence_view=evidence_view,
            independent_evidence=independent_evidence,
        )
        _publish_bundle(arguments.output_dir, bundle)
    _write_run_state(arguments.output_dir, manifest, completed)
    _clear_run_state(arguments.output_dir)
    return completed.exit_code or 0


def _run(arguments: argparse.Namespace) -> int:
    manifest = _read_manifest(arguments.manifest)
    validate_installed_identity(
        manifest,
        artifact=arguments.wheel,
        sdist=arguments.sdist,
        verify_sdist_build=False,
    )
    if manifest.profile == "runtime-baseline-0-3":
        return _run_runtime_baseline(arguments, manifest)
    if manifest.profile == "foundation-release-0-4":
        return _run_foundation_release_0_4(arguments, manifest)
    _assert_core_lifecycle_manifest(manifest)
    prior = _load_run_state(arguments.output_dir, manifest)
    if prior is not None and prior[1] is not None:
        execution, bundle = prior
        assert bundle is not None  # narrowed by the prior[1] check above
        _publish_bundle(arguments.output_dir, bundle)
        _clear_run_state(arguments.output_dir)
        return execution.exit_code or 0
    execution = (
        prior[0]
        if prior is not None
        else PackExecution.create(
            manifest, execution_id=f"core-lifecycle-{uuid4().hex}"
        ).start(manifest)
    )
    _write_run_state(arguments.output_dir, manifest, execution)
    try:
        host_wheel_sdist_provenance_digest = assert_sdist_builds_candidate_wheel(
            arguments.sdist, arguments.wheel
        )
    except AcceptanceHarnessError:
        return _complete_host_wheel_terminal(
            arguments.output_dir,
            manifest,
            execution,
            status=AcceptanceCheckStatus.ERROR,
            reason_code="supplied_sdist_rebuild_harness_error",
        )
    except ValueError:
        return _complete_host_wheel_terminal(
            arguments.output_dir,
            manifest,
            execution,
            status=AcceptanceCheckStatus.FAIL,
            reason_code="supplied_sdist_rebuild_failed",
        )
    checks, evidence_view_base, independent_evidence_base = asyncio.run(
        run_core_lifecycle(fixture_digest=manifest.fixture_digest)
    )
    required_ids = {check.check_id for check in manifest.required_checks}
    checks = tuple(check for check in checks if check.check_id in required_ids)
    host_results, host_evidence = _isolated_host_result()
    host_wheel_authoritative_digest = _evidence_digest(
        {
            "host_observation_digest": host_results[0].evidence_digest,
            "host_wheel_sdist_provenance_digest": host_wheel_sdist_provenance_digest,
        }
    )
    host_results = (
        host_results[0].model_copy(
            update={"evidence_digest": host_wheel_authoritative_digest}
        ),
        host_results[1],
    )
    host_wheel_identity_mutation_digest = _controlled_identity_mutation_evidence(
        manifest, artifact=arguments.wheel, sdist=arguments.sdist
    )
    evidence_view: dict[str, str | int | float | bool | None] = {
        **evidence_view_base,
        "host_wheel_identity_mismatches_rejected": True,
        "host_wheel_sdist_rebuild_matches": True,
    }
    provisional_mutation_result = AcceptanceCheckResult(
        check_id="core.lifecycle.bundle-tamper",
        status=AcceptanceCheckStatus.PASS,
        evidence_level=next(
            check.evidence_level
            for check in manifest.required_checks
            if check.check_id == "core.lifecycle.bundle-tamper"
        ),
        reason_code="bundle_mutation_detected",
        evidence_digest="sha256:"
        + hashlib.sha256(
            json.dumps(
                {"execution_id": execution.execution_id, "manifest": manifest.digest},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    )
    provisional_mutation_independent_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            {"execution_id": execution.execution_id, "manifest": manifest.digest},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    provisional_checks = (*checks, *host_results, provisional_mutation_result)
    provisional_completed = execution.complete(manifest, provisional_checks)
    evidence_view, independent_evidence = _attest_declared_evidence(
        manifest,
        provisional_checks,
        evidence_view,
        {
            **independent_evidence_base,
            **host_evidence,
            "host_wheel_identity_mutation_digest": host_wheel_identity_mutation_digest,
            "host_wheel_sdist_provenance_digest": host_wheel_sdist_provenance_digest,
            "bundle_mutation_independent_digest": provisional_mutation_independent_digest,
        },
        host_observation_digest=str(host_evidence["host_observation_digest"]),
    )
    provisional_bundle = ScenarioEvidenceBundle.create(
        manifest=manifest,
        execution=provisional_completed,
        execution_checks=provisional_checks,
        scenario="core-lifecycle",
        checks=provisional_checks,
        evidence_view=evidence_view,
        independent_evidence=independent_evidence,
    )
    mutation_digest, mutation_independent_digest = _controlled_bundle_mutation_evidence(
        provisional_bundle, manifest, provisional_completed
    )
    mutation_result = provisional_mutation_result.model_copy(
        update={"evidence_digest": mutation_digest}
    )
    all_checks = (*checks, *host_results, mutation_result)
    completed = execution.complete(manifest, all_checks)
    evidence_view, independent_evidence = _attest_declared_evidence(
        manifest,
        all_checks,
        evidence_view,
        {
            **independent_evidence,
            "host_wheel_identity_mutation_digest": host_wheel_identity_mutation_digest,
            "host_wheel_sdist_provenance_digest": host_wheel_sdist_provenance_digest,
            "bundle_mutation_independent_digest": mutation_independent_digest,
        },
        host_observation_digest=str(host_evidence["host_observation_digest"]),
    )
    candidate = ScenarioEvidenceBundle.create(
        manifest=manifest,
        execution=completed,
        execution_checks=all_checks,
        scenario="core-lifecycle",
        checks=all_checks,
        evidence_view=evidence_view,
        independent_evidence=independent_evidence,
    )
    _controlled_bundle_mutation_evidence(candidate, manifest, completed)
    _write_run_state(arguments.output_dir, manifest, completed, candidate)
    _publish_bundle(arguments.output_dir, candidate)
    _clear_run_state(arguments.output_dir)
    return completed.exit_code or 0


def _isolated_host_result() -> tuple[
    tuple[AcceptanceCheckResult, ...], dict[str, str | int | bool]
]:
    """Observe the installed wheel from a separate isolated Python process."""
    environment = isolated_subprocess_environment()
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _ISOLATED_HOST_PROBE],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    observation = {"returncode": completed.returncode}
    if completed.returncode == 0:
        try:
            observed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            observed = None
        if isinstance(observed, dict):
            observation = observed
    core_observation = {
        key: value
        for key, value in observation.items()
        if not key.startswith("telemetry_")
        and key != "filesystem_permission_boundary_observed"
        and key != "credential_canary_absent"
    }
    telemetry_observation = {
        key: value
        for key, value in observation.items()
        if key.startswith("telemetry_")
        or key in {"filesystem_permission_boundary_observed", "credential_canary_absent"}
    }
    digest = "sha256:" + hashlib.sha256(
        json.dumps(core_observation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    telemetry_digest = "sha256:" + hashlib.sha256(
        json.dumps(telemetry_observation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    expected_observation = {
        "module_under_prefix": True,
        "run_succeeded": True,
        "step_count": 1,
        "attempt_count": 1,
        "checkpoint_count": 1,
        "sqlite_file_created": True,
        "restart_observed": True,
        "reopened_run_succeeded": True,
        "reopened_step_count": 1,
        "reopened_attempt_count": 1,
        "reopened_checkpoint_count": 1,
        "unknown_definition_rejected": True,
        "public_layers_available": True,
        "runtime_dependency_violation_count": 0,
        "root_migration_reset": True,
    }
    valid_observation = set(core_observation) == set(expected_observation) and all(
        type(core_observation[key]) is type(expected)
        for key, expected in expected_observation.items()
    )
    status = (
        AcceptanceCheckStatus.ERROR
        if not valid_observation
        else (
            AcceptanceCheckStatus.PASS
            if core_observation == expected_observation
            else AcceptanceCheckStatus.FAIL
        )
    )
    expected_telemetry = {
        "telemetry_ordered": True,
        "telemetry_inspection_reconciled": True,
        "telemetry_usage_provenance": True,
        "telemetry_error_observed": True,
        "telemetry_model_purpose_observed": True,
        "telemetry_duration_observed": True,
        "telemetry_closed": True,
        "telemetry_cross_process": True,
        "telemetry_concurrent": True,
        "telemetry_redacted": True,
        "telemetry_jsonl_digest": str,
        "filesystem_permission_boundary_observed": bool,
        "credential_canary_absent": True,
    }
    valid_telemetry = set(telemetry_observation) == set(expected_telemetry) and all(
        isinstance(telemetry_observation[key], expected)
        if isinstance(expected, type)
        else type(telemetry_observation[key]) is type(expected)
        for key, expected in expected_telemetry.items()
    )
    telemetry_status = (
        AcceptanceCheckStatus.ERROR
        if not valid_telemetry
        else (
            AcceptanceCheckStatus.PASS
            if all(
                value is True
                for key, value in telemetry_observation.items()
                if key not in {
                    "telemetry_jsonl_digest",
                    "filesystem_permission_boundary_observed",
                }
            )
            else AcceptanceCheckStatus.FAIL
        )
    )
    return (
        (
            AcceptanceCheckResult(
                check_id="core.lifecycle.host-wheel",
                status=status,
                evidence_level=EvidenceLevel.HOST,
                reason_code=(
                    "isolated_wheel_lifecycle_observed"
                    if status is AcceptanceCheckStatus.PASS
                    else (
                        "isolated_wheel_lifecycle_failed"
                        if status is AcceptanceCheckStatus.FAIL
                        else "isolated_wheel_lifecycle_error"
                    )
                ),
                evidence_digest=digest,
            ),
            AcceptanceCheckResult(
                check_id="core.lifecycle.telemetry-host",
                status=telemetry_status,
                evidence_level=EvidenceLevel.HOST,
                reason_code=(
                    "isolated_wheel_telemetry_observed"
                    if telemetry_status is AcceptanceCheckStatus.PASS
                    else (
                        "isolated_wheel_telemetry_failed"
                        if telemetry_status is AcceptanceCheckStatus.FAIL
                        else "isolated_wheel_telemetry_error"
                    )
                ),
                evidence_digest=telemetry_digest,
            ),
        ),
        {
            "host_observation_digest": digest,
            "telemetry_host_observation_digest": telemetry_digest,
            **{f"host_{key}": value for key, value in core_observation.items()},
            **{
                f"telemetry_host_{key}": value
                for key, value in telemetry_observation.items()
            },
        },
    )


def _publish_bundle(output_dir: Path, bundle: ScenarioEvidenceBundle) -> Path:
    """Atomically publish or repair a content-addressed snapshot."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{bundle.content_digest.removeprefix('sha256:')}.json"
    payload = (bundle.model_dump_json(indent=2) + "\n").encode("utf-8")
    if path.is_file() and path.read_bytes() == payload:
        print(path)
        return path
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("xb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    print(path)
    return path


def _inspect(arguments: argparse.Namespace) -> int:
    print(_verified_bundle(arguments).model_dump_json(indent=2))
    return 0


def _verify(arguments: argparse.Namespace) -> int:
    _verified_bundle(arguments)
    print("Bundle integrity: PASS")
    return 0


def _render(arguments: argparse.Namespace) -> int:
    bundle = _verified_bundle(arguments)
    print(f"# {bundle.scenario}")
    print()
    print(f"Bundle: {bundle.content_digest}")
    print(f"Execution: {bundle.execution.status} (exit {bundle.execution.exit_code})")
    for check in bundle.checks:
        print(f"- {check.check_id}: {check.status}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m m_agent.testing")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run the offline core-lifecycle Scenario")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--wheel", type=Path, required=True)
    run.add_argument("--sdist", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.set_defaults(handler=_run)
    inspect = subparsers.add_parser("inspect", help="print a Bundle's public JSON")
    inspect.add_argument("--manifest", type=Path)
    inspect.add_argument("--wheel", type=Path, required=True)
    inspect.add_argument("--sdist", type=Path, required=True)
    inspect.add_argument("--bundle", type=Path, required=True)
    inspect.set_defaults(handler=_inspect)
    verify = subparsers.add_parser("verify", help="verify Bundle integrity")
    verify.add_argument("--manifest", type=Path)
    verify.add_argument("--wheel", type=Path, required=True)
    verify.add_argument("--sdist", type=Path, required=True)
    verify.add_argument("--bundle", type=Path, required=True)
    verify.set_defaults(handler=_verify)
    render = subparsers.add_parser("render", help="render a Bundle summary")
    render.add_argument("--manifest", type=Path)
    render.add_argument("--wheel", type=Path, required=True)
    render.add_argument("--sdist", type=Path, required=True)
    render.add_argument("--bundle", type=Path, required=True)
    render.set_defaults(handler=_render)
    return parser


def main() -> NoReturn:
    arguments = _parser().parse_args()
    try:
        raise SystemExit(arguments.handler(arguments))
    except BundleIntegrityError as error:
        print(f"Bundle integrity error: {error}", file=sys.stderr)
        raise SystemExit(EXIT_INTEGRITY_FAILURE) from error
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f"Acceptance Pack invocation error: {error}", file=sys.stderr)
        raise SystemExit(EXIT_INVALID_INVOCATION) from error
    except Exception as error:
        print(f"Acceptance Pack harness error: {error}", file=sys.stderr)
        raise SystemExit(EXIT_HARNESS_ERROR) from error


if __name__ == "__main__":
    main()
