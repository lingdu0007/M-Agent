"""Bind release evidence to one candidate using the public Testing contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

from _release_benchmarks import WORKLOADS, benchmark_index, verify_benchmarks
from packaging.version import Version

from m_agent.testing import (
    FOUNDATION_PLATFORM_MATRIX_0_5,
    AcceptanceCheckStatus,
    AcceptanceManifest,
    PackExecutionStatus,
    PlatformMatrixEvidence,
    PlatformMatrixObservation,
    ScenarioEvidenceBundle,
    coverage_matrix_gaps,
    foundation_release_0_5_manifest,
    installed_identity,
    render_release_demo,
)

ROOT = Path(__file__).parents[1]


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare(arguments: argparse.Namespace) -> dict[str, Any]:
    identity = installed_identity(artifact=arguments.wheel, sdist=arguments.sdist)
    environment = cast(dict[str, str], identity["environment"])
    if (
        identity["source_commit"] != arguments.source_commit
        or environment["version"] != arguments.version
        or environment["installation"] != "wheel"
        or environment["source_state"] != "clean"
    ):
        raise ValueError("installed subject is not the expected clean candidate wheel")
    manifest = foundation_release_0_5_manifest(
        source_commit=arguments.source_commit,
        artifact_digest=str(identity["artifact_digest"]),
        sdist_digest=str(identity["sdist_digest"]),
        fixture_digest=str(identity["fixture_digest"]),
        environment=environment,
    )
    arguments.directory.mkdir(parents=True, exist_ok=False)
    write_json(arguments.directory / "manifest.json", manifest.model_dump(mode="json"))
    return {"manifest_digest": manifest.digest, **identity}


def verify_cell(
    directory: Path,
    source_commit: str,
    wheel_digest: str,
    sdist_digest: str,
    fixture_digest: str,
    version: str,
) -> dict[str, Any]:
    manifest = AcceptanceManifest.model_validate_json(
        (directory / "manifest.json").read_text(encoding="utf-8")
    )
    expected = foundation_release_0_5_manifest(
        source_commit=source_commit,
        artifact_digest=wheel_digest,
        sdist_digest=sdist_digest,
        fixture_digest=fixture_digest,
        environment=manifest.environment,
    )
    if (
        manifest != expected
        or manifest.environment["source_state"] != "clean"
        or manifest.environment["installation"] != "wheel"
        or manifest.environment["version"] != version
    ):
        raise ValueError("Manifest does not match the expected clean release candidate")
    bundles = tuple(
        ScenarioEvidenceBundle.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted((directory / "bundles").glob("*.json"))
    )
    if len(bundles) != len(manifest.scenarios) or {
        bundle.scenario for bundle in bundles
    } != set(manifest.scenarios):
        raise ValueError("release cell does not contain every Scenario exactly once")
    execution = bundles[0].execution
    for bundle in bundles:
        bundle.verify(manifest, execution)
    if execution.status is not PackExecutionStatus.PASSED or execution.exit_code != 0:
        raise ValueError("release cell has a non-passing Pack Execution")
    return {
        "status": "PASS",
        "source_commit": manifest.source_commit,
        "artifact_digest": manifest.artifact_digest,
        "sdist_digest": manifest.sdist_digest,
        "fixture_digest": manifest.fixture_digest,
        "manifest_digest": manifest.digest,
        "environment": dict(manifest.environment),
        "execution_id": execution.execution_id,
        "scenario_count": len(bundles),
        "required_checks": len(manifest.required_checks),
        "bundle_digests": sorted(bundle.content_digest for bundle in bundles),
    }


def expected_identity(arguments: argparse.Namespace) -> dict[str, str]:
    return {
        key: getattr(arguments, key)
        for key in (
            "source_commit",
            "wheel_digest",
            "sdist_digest",
            "fixture_digest",
            "version",
        )
    }


def aggregate(arguments: argparse.Namespace) -> dict[str, Any]:
    reports = []
    observations = []
    primary = None
    for directory in sorted(arguments.directory.glob("cell-*")):
        report = verify_cell(directory, **expected_identity(arguments))
        reports.append(report)
        environment = report["environment"]
        minor = ".".join(map(str, Version(environment["python"]).release[:2]))
        entries = [
            entry
            for entry in FOUNDATION_PLATFORM_MATRIX_0_5
            if entry.platform == environment["os"] and entry.python == minor
        ]
        if not entries:
            raise ValueError("cell environment is outside the frozen platform matrix")
        for entry in entries:
            digest = hashlib.sha256(
                json.dumps(
                    {"cell": report, "level": entry.level.value},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            observations.append(
                PlatformMatrixObservation(
                    **entry.model_dump(),
                    status=AcceptanceCheckStatus.PASS,
                    evidence_source=f"{directory.name}: verified six-Scenario Bundle set",
                    evidence_digest="sha256:" + digest,
                    artifact_digest=arguments.wheel_digest,
                )
            )
            if entry.role == "primary":
                primary = directory
    if not observations:
        raise ValueError("missing required matrix observations")
    matrix = PlatformMatrixEvidence.create(observations=tuple(observations))
    if matrix.overall_status != "PASS" or primary is None:
        raise ValueError("release platform matrix is not complete and passing")
    manifest = AcceptanceManifest.model_validate_json(
        (primary / "manifest.json").read_text(encoding="utf-8")
    )
    gaps = coverage_matrix_gaps(
        (ROOT / "docs/acceptance-coverage-matrix.md").read_text(encoding="utf-8"),
        manifest,
    )
    if gaps:
        raise ValueError(f"release Coverage Matrix has required gaps: {gaps}")
    verify_benchmarks(primary / "benchmarks", manifest)
    bundles = tuple(
        ScenarioEvidenceBundle.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted((primary / "bundles").glob("*.json"))
    )
    arguments.output.mkdir(parents=True, exist_ok=False)
    write_json(
        arguments.output / "platform-matrix.json", matrix.model_dump(mode="json")
    )
    write_json(arguments.output / "cells.json", reports)
    write_json(
        arguments.output / "coverage.json",
        {"required_checks": len(manifest.required_checks), "gaps": list(gaps)},
    )
    for mode in ("full", "short"):
        (arguments.output / f"demo-{mode}.md").write_text(
            render_release_demo(bundles, mode=mode), encoding="utf-8"
        )
    return {
        "status": "PASS",
        **expected_identity(arguments),
        "platform_cells": len(observations),
        "scenario_executions": sum(report["scenario_count"] for report in reports),
    }


def benchmarks(arguments: argparse.Namespace) -> dict[str, Any]:
    report = verify_cell(arguments.directory, **expected_identity(arguments))
    output = arguments.directory / "benchmarks"
    output.mkdir(exist_ok=False)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT"}
    }
    environment.update(
        M_AGENT_RUN_LIVE_TESTS="0",
        M_AGENT_BENCHMARK_ARTIFACT_DIGEST=report["artifact_digest"],
        M_AGENT_BENCHMARK_MANIFEST_DIGEST=report["manifest_digest"],
    )
    with tempfile.TemporaryDirectory(prefix="release-benchmarks-") as temporary:
        for name, filename in WORKLOADS.items():
            command = [
                sys.executable,
                str(ROOT / "benchmarks" / filename),
                "--database",
                f"{name}.sqlite",
                "--json-output",
                str(output.resolve() / f"{name}.json"),
            ]
            if name == "session":
                command.extend(["--run-database", "session-runs.sqlite"])
            subprocess.run(
                command, cwd=temporary, env=environment, check=True, timeout=120
            )
    manifest = AcceptanceManifest.model_validate_json(
        (arguments.directory / "manifest.json").read_text(encoding="utf-8")
    )
    write_json(output / "index.json", benchmark_index(output, manifest))
    return {"status": "PASS", "workloads": list(WORKLOADS)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    for name in (
        "source-commit",
        "wheel-digest",
        "sdist-digest",
        "fixture-digest",
        "version",
    ):
        common.add_argument(f"--{name}", required=True)
    for name in ("verify-cell", "aggregate", "benchmarks"):
        command = commands.add_parser(name, parents=[common])
        command.add_argument("--directory", type=Path, required=True)
        if name == "aggregate":
            command.add_argument("--output", type=Path, required=True)
    preparation = commands.add_parser("prepare")
    for name in ("source-commit", "version"):
        preparation.add_argument(f"--{name}", required=True)
    for name in ("directory", "wheel", "sdist"):
        preparation.add_argument(f"--{name}", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "prepare":
        result = prepare(arguments)
    elif arguments.command == "aggregate":
        result = aggregate(arguments)
    elif arguments.command == "benchmarks":
        result = benchmarks(arguments)
    else:
        result = verify_cell(arguments.directory, **expected_identity(arguments))
    if arguments.command == "aggregate":
        write_json(arguments.output / "qualification.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
