"""Thin, offline-only CLI for the Ticket 07 Acceptance Pack foundation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from ._core_lifecycle import run_core_lifecycle
from ._identity import validate_installed_identity
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


_ISOLATED_HOST_PROBE = """
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import m_agent
from m_agent import (
    AgentDefinition,
    Clock,
    DefinitionNotFoundError,
    DefinitionRegistry,
    DeterministicModelAdapter,
    JsonlTelemetrySink,
    ModelCapabilities,
    ModelResponse,
    ModelUsage,
    PlaintextPayloadCodec,
    Runner,
    RunStatus,
    TelemetryEvent,
    TelemetryEventType,
    adapters,
    companion,
    runtime,
    testing,
)
from m_agent.adapters import SQLiteRunStore
from m_agent.testing import find_runtime_dependency_violations


_EXPAND_ONLY_EXPORTS = {
    "CrashPoint",
    "deserialize_model_response",
    "deserialize_tool_outcome",
    "serialize_model_response",
    "serialize_tool_outcome",
}


_REOPEN_PROBE = '''
import asyncio
import json
from pathlib import Path
import sys

from m_agent import DefinitionRegistry, PlaintextPayloadCodec, Runner, RunStatus
from m_agent.adapters import SQLiteRunStore


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
        super().__init__(("accepted",), capabilities=ModelCapabilities(usage_reporting=True))

    async def generate(self, request):
        response = await super().generate(request)
        return ModelResponse(
            content=response.content,
            usage=ModelUsage(input_tokens=11, output_tokens=7),
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
    return (
        len(completed) == 1
        and completed[0].get("usage")
        == {"input_tokens": 11, "output_tokens": 7}
        and all(
            event.get("usage") is None
            for event in events
            if event is not completed[0]
        )
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
        AgentDefinition(
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
        )
        try:
            reopened = json.loads(reopened_process.stdout)
        except json.JSONDecodeError:
            reopened = {}
        sqlite_file_created = database.is_file() and database.stat().st_size > 0
        telemetry_ordered = telemetry_sequence(telemetry_events, created.run_id)
        telemetry_usage = telemetry_usage_provenance(telemetry_events)
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
            canary.encode("utf-8") not in telemetry_bytes
            for canary in _TELEMETRY_CANARIES
        )
        telemetry_jsonl_digest = "sha256:" + hashlib.sha256(
            telemetry_bytes + concurrent_bytes
        ).hexdigest()
    try:
        registry.resolve("unknown", "1.0")
    except DefinitionNotFoundError:
        unknown_definition_rejected = True
    else:
        unknown_definition_rejected = False
    root_exports = tuple(m_agent.__all__)
    root_expand_compatibility = (
        len(root_exports) == len(set(root_exports))
        and set(root_exports)
        == set(runtime.__all__) | set(adapters.__all__) | _EXPAND_ONLY_EXPORTS
        and all(getattr(m_agent, name, None) is getattr(runtime, name) for name in runtime.__all__)
        and all(getattr(m_agent, name, None) is getattr(adapters, name) for name in adapters.__all__)
        and all(name in m_agent.__all__ and hasattr(m_agent, name) for name in _EXPAND_ONLY_EXPORTS)
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
        "root_expand_compatibility": root_expand_compatibility,
        "telemetry_ordered": telemetry_ordered,
        "telemetry_usage_provenance": telemetry_usage,
        "telemetry_closed": telemetry_closed,
        "telemetry_cross_process": telemetry_cross_process,
        "telemetry_concurrent": telemetry_concurrent,
        "telemetry_redacted": telemetry_redacted,
        "telemetry_jsonl_digest": telemetry_jsonl_digest,
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
            "Ticket 07 CLI supports only the frozen core-lifecycle foundation profile"
        )


def _verified_bundle(arguments: argparse.Namespace) -> ScenarioEvidenceBundle:
    bundle = _read_bundle(arguments.bundle)
    if arguments.bundle.name != f"{bundle.content_digest.removeprefix('sha256:')}.json":
        raise BundleIntegrityError("Bundle path does not match content digest")
    if arguments.manifest is not None:
        supplied = _read_manifest(arguments.manifest)
        if supplied.digest != bundle.manifest.digest:
            raise BundleIntegrityError("Bundle does not match supplied Manifest")
    validate_installed_identity(
        bundle.manifest, artifact=arguments.wheel, sdist=arguments.sdist
    )
    bundle.verify()
    _assert_core_lifecycle_manifest(bundle.manifest)
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
    evidence_view: dict[str, str | int | float | bool | None],
    independent_evidence: dict[str, str | int | float | bool | None],
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
        if check.check_id in {
            "core.lifecycle",
            "core.lifecycle.bundle-tamper",
            "core.lifecycle.host-wheel",
        }:
            independent[check.independent_evidence] = manifest.fixture_digest
        elif check.check_id == "core.lifecycle.telemetry":
            independent[check.independent_evidence] = independent[
                "telemetry_jsonl_digest"
            ]
        else:
            independent[check.independent_evidence] = host_observation_digest
    return authoritative, independent


def _run(arguments: argparse.Namespace) -> int:
    manifest = _read_manifest(arguments.manifest)
    validate_installed_identity(manifest, artifact=arguments.wheel, sdist=arguments.sdist)
    _assert_core_lifecycle_manifest(manifest)
    prior = _load_run_state(arguments.output_dir, manifest)
    if prior is not None and prior[1] is not None:
        execution, bundle = prior
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
    checks, evidence_view, independent_evidence = asyncio.run(
        run_core_lifecycle(fixture_digest=manifest.fixture_digest)
    )
    host_result, host_evidence = _isolated_host_result()
    mutation_result = AcceptanceCheckResult(
        check_id="core.lifecycle.bundle-tamper",
        status=AcceptanceCheckStatus.PASS,
        evidence_level=next(
            check.evidence_level
            for check in manifest.required_checks
            if check.check_id == "core.lifecycle.bundle-tamper"
        ),
        reason_code="bundle_mutation_detected",
        evidence_digest="sha256:"
        + hashlib.sha256(b"core-lifecycle bundle mutation").hexdigest(),
    )
    all_checks = (*checks, host_result, mutation_result)
    completed = execution.complete(manifest, all_checks)
    evidence_view, independent_evidence = _attest_declared_evidence(
        manifest,
        all_checks,
        evidence_view,
        {**independent_evidence, **host_evidence},
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
    tampered = candidate.model_copy(
        update={"evidence_view": {**candidate.evidence_view, "run_succeeded": False}}
    )
    try:
        tampered.verify(manifest, completed)
    except BundleIntegrityError:
        bundle = candidate
    else:
        raise RuntimeError("controlled Bundle mutation was not detected")
    _write_run_state(arguments.output_dir, manifest, completed, candidate)
    _publish_bundle(arguments.output_dir, bundle)
    _clear_run_state(arguments.output_dir)
    return completed.exit_code or 0


def _isolated_host_result() -> tuple[
    AcceptanceCheckResult, dict[str, str | int | bool]
]:
    """Observe the installed wheel from a separate isolated Python process."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "OPENAI_API_KEY", "M_AGENT_OPENAI_API_KEY"}
    }
    environment["M_AGENT_RUN_LIVE_TESTS"] = "0"
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
    telemetry_jsonl_digest = observation.pop("telemetry_jsonl_digest", None)
    if not isinstance(telemetry_jsonl_digest, str) or not telemetry_jsonl_digest.startswith(
        "sha256:"
    ):
        telemetry_jsonl_digest = "sha256:" + hashlib.sha256(
            json.dumps(observation, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    digest = "sha256:" + hashlib.sha256(
        json.dumps(observation, sort_keys=True, separators=(",", ":")).encode("utf-8")
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
        "root_expand_compatibility": True,
        "telemetry_ordered": True,
        "telemetry_usage_provenance": True,
        "telemetry_closed": True,
        "telemetry_cross_process": True,
        "telemetry_concurrent": True,
        "telemetry_redacted": True,
    }
    valid_observation = set(observation) == set(expected_observation) and all(
        type(observation[key]) is type(expected)
        for key, expected in expected_observation.items()
    )
    status = (
        AcceptanceCheckStatus.ERROR
        if not valid_observation
        else (
            AcceptanceCheckStatus.PASS
            if observation == expected_observation
            else AcceptanceCheckStatus.FAIL
        )
    )
    return (
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
        {
            "host_observation_digest": digest,
            "telemetry_jsonl_digest": telemetry_jsonl_digest,
            **{f"host_{key}": value for key, value in observation.items()},
        },
    )


def _publish_bundle(output_dir: Path, bundle: ScenarioEvidenceBundle) -> Path:
    """Publish a content-addressed snapshot without replacing prior evidence."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{bundle.content_digest.removeprefix('sha256:')}.json"
    payload = (bundle.model_dump_json(indent=2) + "\n").encode("utf-8")
    try:
        with path.open("xb") as output:
            output.write(payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ValueError("content-addressed Bundle path already contains different data")
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
