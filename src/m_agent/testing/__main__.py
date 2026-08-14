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
    ScenarioEvidenceBundle,
    core_lifecycle_manifest,
)


_ISOLATED_HOST_PROBE = """
import asyncio
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
    PlaintextPayloadCodec,
    Runner,
    RunStatus,
    adapters,
    companion,
    runtime,
    testing,
)
from m_agent.adapters import SQLiteRunStore
from m_agent.testing import find_runtime_dependency_violations


_REOPEN_PROBE = '''
import asyncio
import json
import sys

from m_agent import DefinitionRegistry, PlaintextPayloadCodec, Runner, RunStatus
from m_agent.adapters import SQLiteRunStore


async def inspect():
    store = SQLiteRunStore(sys.argv[1], payload_codec=PlaintextPayloadCodec())
    try:
        inspection = await Runner(DefinitionRegistry(), store).inspect_run(sys.argv[2])
        print(json.dumps({
            "run_succeeded": inspection.run.status is RunStatus.SUCCEEDED,
            "step_count": len(inspection.steps),
            "attempt_count": len(inspection.attempts),
            "checkpoint_count": len(inspection.checkpoints),
        }, sort_keys=True))
    finally:
        store.close()


asyncio.run(inspect())
'''


async def observe():
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition(
            definition_id="core-lifecycle",
            version="1.0",
            instructions="Use the deterministic fixture.",
            model_adapter=DeterministicModelAdapter(("accepted",)),
        )
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        database = Path(temporary_directory) / "core-lifecycle.sqlite3"
        store = SQLiteRunStore(database, payload_codec=PlaintextPayloadCodec())
        runner = Runner(registry=registry, store=store)
        created = await runner.create_run("core-lifecycle", "1.0", "fixture")
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)
        store.close()
        reopened_process = subprocess.run(
            [sys.executable, "-I", "-c", _REOPEN_PROBE, str(database), created.run_id],
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            reopened = json.loads(reopened_process.stdout)
        except json.JSONDecodeError:
            reopened = {}
        sqlite_file_created = database.is_file() and database.stat().st_size > 0
    try:
        registry.resolve("unknown", "1.0")
    except DefinitionNotFoundError:
        unknown_definition_rejected = True
    else:
        unknown_definition_rejected = False
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
    }, sort_keys=True))


asyncio.run(observe())
"""


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


def _run(arguments: argparse.Namespace) -> int:
    manifest = _read_manifest(arguments.manifest)
    validate_installed_identity(manifest, artifact=arguments.wheel, sdist=arguments.sdist)
    _assert_core_lifecycle_manifest(manifest)
    execution = PackExecution.create(
        manifest, execution_id=f"core-lifecycle-{uuid4().hex}"
    ).start(manifest)
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
    candidate = ScenarioEvidenceBundle.create(
        manifest=manifest,
        execution=completed,
        execution_checks=all_checks,
        scenario="core-lifecycle",
        checks=all_checks,
        evidence_view=evidence_view,
        independent_evidence={**independent_evidence, **host_evidence},
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
    _publish_bundle(arguments.output_dir, bundle)
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
