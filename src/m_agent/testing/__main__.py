"""Thin, offline-only CLI for the Ticket 07 Acceptance Pack foundation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
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
    PackExecution,
    ScenarioEvidenceBundle,
)


_CORE_LIFECYCLE_CHECKS = frozenset(
    {
        "core.lifecycle",
        "core.lifecycle.unknown-definition",
        "core.lifecycle.public-namespaces",
        "core.lifecycle.dependency-direction",
        "core.lifecycle.expand-compatibility",
        "core.lifecycle.bundle-tamper",
    }
)


def _read_manifest(path: Path) -> AcceptanceManifest:
    return AcceptanceManifest.model_validate_json(path.read_text())


def _read_bundle(path: Path) -> ScenarioEvidenceBundle:
    return ScenarioEvidenceBundle.model_validate_json(path.read_text())


def _verified_bundle(arguments: argparse.Namespace) -> ScenarioEvidenceBundle:
    manifest = _read_manifest(arguments.manifest)
    validate_installed_identity(manifest, artifact=arguments.wheel)
    bundle = _read_bundle(arguments.bundle)
    bundle.verify(manifest)
    return bundle


def _run(arguments: argparse.Namespace) -> int:
    manifest = _read_manifest(arguments.manifest)
    validate_installed_identity(manifest, artifact=arguments.wheel)
    if manifest.scenarios != ("core-lifecycle",) or {
        check.check_id for check in manifest.required_checks
    } != _CORE_LIFECYCLE_CHECKS:
        raise ValueError("Ticket 07 CLI supports only the core-lifecycle Scenario")
    execution = PackExecution.create(
        manifest, execution_id=f"core-lifecycle-{uuid4().hex}"
    ).start(manifest)
    checks, evidence_view, independent_evidence = asyncio.run(
        run_core_lifecycle(fixture_digest=manifest.fixture_digest)
    )
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
    all_checks = (*checks, mutation_result)
    completed = execution.complete(manifest, all_checks)
    candidate = ScenarioEvidenceBundle.create(
        manifest=manifest,
        execution=completed,
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
    _publish_bundle(arguments.output_dir, bundle)
    return completed.exit_code or 0


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
    for check in bundle.checks:
        print(f"- {check.check_id}: {check.status}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m m_agent.testing")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run the offline core-lifecycle Scenario")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--wheel", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.set_defaults(handler=_run)
    inspect = subparsers.add_parser("inspect", help="print a Bundle's public JSON")
    inspect.add_argument("--manifest", type=Path, required=True)
    inspect.add_argument("--wheel", type=Path, required=True)
    inspect.add_argument("--bundle", type=Path, required=True)
    inspect.set_defaults(handler=_inspect)
    verify = subparsers.add_parser("verify", help="verify Bundle integrity")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--wheel", type=Path, required=True)
    verify.add_argument("--bundle", type=Path, required=True)
    verify.set_defaults(handler=_verify)
    render = subparsers.add_parser("render", help="render a Bundle summary")
    render.add_argument("--manifest", type=Path, required=True)
    render.add_argument("--wheel", type=Path, required=True)
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
