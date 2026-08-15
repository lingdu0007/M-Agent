"""Ticket 01 distribution contract through a built wheel and public Runner."""

from __future__ import annotations

import importlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_DEPENDENCIES = (
    "annotated_types",
    "pydantic",
    "pydantic_core",
    "typing_extensions",
    "typing_inspection",
)


def _clean_environment() -> dict[str, str]:
    """Keep tool discovery while preventing the source tree from leaking in."""
    excluded = {
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "M_AGENT_OPENAI_API_KEY",
        "OPENAI_API_KEY",
    }
    clean = {key: value for key, value in os.environ.items() if key not in excluded}
    clean["M_AGENT_RUN_LIVE_TESTS"] = "0"
    return clean


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(
            "command failed: "
            + " ".join(command)
            + "\nstdout:\n"
            + completed.stdout
            + "\nstderr:\n"
            + completed.stderr
        )
    return completed.stdout


def _copy_locked_runtime_dependencies(target: Path) -> None:
    """Provision interpreter-matched dependencies without a second resolve."""
    for name in _RUNTIME_DEPENDENCIES:
        module = importlib.import_module(name)
        package_paths = getattr(module, "__path__", None)
        if package_paths:
            source = Path(next(iter(package_paths))).resolve()
        else:
            source = Path(module.__file__ or "").resolve()
        destination = target / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)


_CLEAN_RUNNER_PROBE = """
import asyncio
from importlib.metadata import metadata
from pathlib import Path
import sys

import m_agent
from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
    DeterministicModelAdapter,
    InMemoryRunStore,
    PlaintextPayloadCodec,
    Runner,
    RunStatus,
)


async def main():
    assert Path(m_agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition(
            definition_id="distribution-contract",
            version="1.0",
            instructions="Reply deterministically.",
            model_adapter=DeterministicModelAdapter(responses=("ok",)),
        )
    )
    runner = Runner(
        registry=registry,
        store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
    )
    created = await runner.create_run("distribution-contract", "1.0", input="hi")
    terminal = await runner.start_run(created.run_id)
    inspection = await runner.inspect_run(created.run_id)
    assert metadata("m-agent")["Name"] == "m-agent"
    assert terminal.status is RunStatus.SUCCEEDED
    assert len(inspection.steps) == len(inspection.attempts) == len(inspection.checkpoints) == 1
    print("m-agent distribution Runner contract passed")


asyncio.run(main())
"""

_FLAGSHIP_INSTALLED_PROBE = """
from pathlib import Path
import sys

import m_agent

assert Path(m_agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
print("m-agent installed artifact import passed")
"""

_FOUNDATION_INSTALLED_PROBE = """
import copy
import hashlib
import io
import json
from importlib.metadata import metadata
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tarfile
import time
import zipfile

from m_agent.runtime import AgentDefinition, DefinitionRegistry, Runner

assert "m_agent.adapters" not in sys.modules

import m_agent
from m_agent.adapters import DeterministicModelAdapter, InMemoryRunStore, PlaintextPayloadCodec
from m_agent.testing import (
    AcceptanceCheckResult,
    AcceptanceCheckStatus,
    PackExecution,
    ScenarioEvidenceBundle,
    core_lifecycle_manifest,
    installed_identity,
)

assert Path(m_agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
wheel = Path(sys.argv[1])
sdist = Path(sys.argv[2])
with zipfile.ZipFile(wheel) as candidate:
    assert "m_agent/_sqlite_store.py" not in candidate.namelist()
identity = installed_identity(artifact=wheel, sdist=sdist)
assert identity["environment"]["installation"] == "wheel"
assert identity["source_commit"] not in {"development", "unavailable"}
assert identity["environment"]["source_state"] == "clean"
assert identity["environment"]["build_tool"].startswith("setuptools==")
assert identity["environment"]["dependency_summary"].startswith("sha256:")
assert identity["sdist_digest"].startswith("sha256:")
manifest = core_lifecycle_manifest(
    source_commit=identity["source_commit"],
    artifact_digest=identity["artifact_digest"],
    sdist_digest=identity["sdist_digest"],
    fixture_digest=identity["fixture_digest"],
    environment=identity["environment"],
)
with tempfile.TemporaryDirectory() as temporary_directory:
    manifest_path = Path(temporary_directory) / "manifest.json"
    output_dir = Path(temporary_directory) / "bundles"
    altered_wheel = Path(temporary_directory) / "altered.whl"
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(altered_wheel, "w") as altered:
        for member in source.infolist():
            altered.writestr(member, source.read(member.filename))
        altered.writestr("unexpected-member.txt", b"not installed")
    try:
        installed_identity(artifact=altered_wheel, sdist=sdist)
    except ValueError:
        pass
    else:
        raise AssertionError("identity accepted a wheel that was not installed")
    altered_sdist = Path(temporary_directory) / "altered.tar.gz"
    with tarfile.open(sdist, "r:gz") as source, tarfile.open(altered_sdist, "w:gz") as altered:
        for member in source.getmembers():
            copied = copy.copy(member)
            content = source.extractfile(member)
            payload = content.read() if content is not None else None
            if member.isfile() and member.name.endswith("/README.md"):
                assert payload is not None
                payload += b"\\ncontrolled source tamper\\n"
                copied.size = len(payload)
            altered.addfile(copied, io.BytesIO(payload) if payload is not None else None)
    try:
        installed_identity(artifact=wheel, sdist=altered_sdist)
    except ValueError:
        pass
    else:
        raise AssertionError("identity accepted an altered source distribution")
    forged_sdist_root = Path(temporary_directory) / "forged-source"
    shutil.unpack_archive(str(sdist), str(forged_sdist_root))
    forged_root = next(forged_sdist_root.iterdir())
    provenance_document = forged_root / "docs" / "adr" / "0042-reference-acceptance-pack-over-monolithic-demo.md"
    provenance_document.write_text(provenance_document.read_text() + "\\ncontrolled provenance tamper\\n")
    forged_integrity_path = forged_root / "SOURCE_INTEGRITY.json"
    forged_integrity = json.loads(forged_integrity_path.read_text())
    forged_integrity["files"][provenance_document.relative_to(forged_root).as_posix()] = (
        "sha256:" + hashlib.sha256(provenance_document.read_bytes()).hexdigest()
    )
    forged_integrity_path.write_text(json.dumps(forged_integrity, sort_keys=True, separators=(",", ":")))
    forged_sdist = Path(temporary_directory) / "forged-source.tar.gz"
    with tarfile.open(forged_sdist, "w:gz") as archive:
        archive.add(forged_root, arcname=forged_root.name)
    try:
        installed_identity(artifact=wheel, sdist=forged_sdist)
    except ValueError:
        pass
    else:
        raise AssertionError("identity accepted self-authored source provenance")
    manifest_path.write_text(manifest.model_dump_json())
    import subprocess

    def invoke(*arguments):
        return subprocess.run(
            [sys.executable, "-I", "-m", "m_agent.testing", *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    invalid_sdist = Path(temporary_directory) / "invalid-source.tar.gz"
    invalid_source = Path(temporary_directory) / "invalid-source"
    shutil.unpack_archive(str(sdist), str(invalid_source))
    invalid_root = next(invalid_source.iterdir())
    (invalid_root / "setup.py").write_text("this is not valid Python (\\n")
    integrity_path = invalid_root / "SOURCE_INTEGRITY.json"
    integrity_path.write_text(json.dumps({
        "schema_version": "1",
        "files": {
            path.relative_to(invalid_root).as_posix(): "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(invalid_root.rglob("*"))
            if path.is_file() and path != integrity_path
        },
    }, sort_keys=True, separators=(",", ":")))
    with tarfile.open(invalid_sdist, "w:gz") as archive:
        archive.add(invalid_root, arcname=invalid_root.name)
    invalid_manifest = manifest.model_copy(
        update={
            "sdist_digest": "sha256:" + hashlib.sha256(invalid_sdist.read_bytes()).hexdigest()
        }
    )
    manifest_path.write_text(invalid_manifest.model_dump_json())
    completed = invoke(
        "run", "--manifest", str(manifest_path), "--wheel", str(wheel),
        "--sdist", str(invalid_sdist), "--output-dir", str(output_dir),
    )
    assert completed.returncode == 2, completed.stderr
    manifest_path.write_text(manifest.model_dump_json())

    resumed_output_dir = Path(temporary_directory) / "resumed-bundles"
    running_state = resumed_output_dir / ".core-lifecycle-running.json"
    interrupted = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-m",
            "m_agent.testing",
            "run",
            "--manifest",
            str(manifest_path),
            "--wheel",
            str(wheel),
            "--sdist",
            str(sdist),
            "--output-dir",
            str(resumed_output_dir),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(5000):
        if running_state.is_file() or interrupted.poll() is not None:
            break
        time.sleep(0.001)
    if not running_state.is_file():
        stdout, stderr = interrupted.communicate(timeout=20)
        raise AssertionError(("running Pack state was not persisted", stdout, stderr))
    running_snapshot = json.loads(running_state.read_text())
    assert running_snapshot["execution"]["status"] == "RUNNING"
    execution_id = running_snapshot["execution"]["execution_id"]
    interrupted.terminate()
    interrupted.communicate(timeout=20)
    assert running_state.is_file()
    resumed = invoke(
        "run",
        "--manifest",
        str(manifest_path),
        "--wheel",
        str(wheel),
        "--sdist",
        str(sdist),
        "--output-dir",
        str(resumed_output_dir),
    )
    assert resumed.returncode == 0, resumed.stderr
    resumed_bundle = json.loads(Path(resumed.stdout.strip()).read_text())
    assert resumed_bundle["execution"]["execution_id"] == execution_id
    assert not running_state.exists()
    resumed_bundle_path = Path(resumed.stdout.strip())
    completed_state = {
        "schema_version": "1",
        "manifest": manifest.model_dump(mode="json"),
        "manifest_digest": manifest.digest,
        "execution": resumed_bundle["execution"],
        "bundle": resumed_bundle,
    }
    running_state.write_text(json.dumps({
        **completed_state,
        "content_digest": "sha256:" + hashlib.sha256(
            json.dumps(completed_state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }))
    resumed_bundle_path.write_bytes(b"{truncated")
    recovered = invoke(
        "run",
        "--manifest", str(manifest_path),
        "--wheel", str(wheel),
        "--sdist", str(sdist),
        "--output-dir", str(resumed_output_dir),
    )
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(resumed_bundle_path.read_text())["content_digest"] == resumed_bundle["content_digest"]
    assert not running_state.exists()

    completed = invoke(
        "run", "--manifest", str(manifest_path), "--wheel", str(wheel),
        "--sdist", str(sdist),
        "--output-dir", str(output_dir),
    )
    assert completed.returncode == 0, (
        completed.returncode,
        completed.stdout,
        completed.stderr,
        Path(completed.stdout.strip()).read_text() if completed.stdout.strip() else "",
    )
    bundle_path = Path(completed.stdout.strip())
    assert bundle_path.parent == output_dir
    bundle = json.loads(bundle_path.read_text())
    assert bundle_path.stem == bundle["content_digest"].removeprefix("sha256:")
    assert bundle["manifest"] == manifest.model_dump(mode="json")
    assert bundle["execution"]["status"] == "PASSED"
    assert bundle["execution"]["exit_code"] == 0
    assert all(check["reason_code"] and check["evidence_digest"] for check in bundle["checks"])
    checks_by_id = {check["check_id"]: check for check in bundle["checks"]}
    for declared in manifest.required_checks:
        result = checks_by_id[declared.check_id]
        if result["status"] == "PASS":
            assert bundle["evidence_view"][declared.authoritative_evidence] == result["evidence_digest"]
            assert bundle["independent_evidence"][declared.independent_evidence].startswith("sha256:")
    assert any(
        check["check_id"] == "core.lifecycle.host-wheel"
        and check["evidence_level"] == "HOST"
        and check["status"] == "PASS"
        for check in bundle["checks"]
    )
    assert all(not check["check_id"].startswith("core.lifecycle.telemetry") for check in bundle["checks"])
    assert bundle["independent_evidence"]["fixture_digest"] == identity["fixture_digest"]
    assert bundle["independent_evidence"]["host_observation_digest"].startswith("sha256:")
    assert (
        bundle["independent_evidence"]["core_lifecycle_independent_digest"]
        == bundle["independent_evidence"]["host_observation_digest"]
    )
    assert (
        bundle["independent_evidence"]["host_wheel_independent_digest"]
        == bundle["independent_evidence"]["host_wheel_identity_mutation_digest"]
    )
    assert bundle["evidence_view"]["host_wheel_identity_mismatches_rejected"] is True
    assert bundle["evidence_view"]["host_wheel_sdist_rebuild_matches"] is True
    assert bundle["independent_evidence"]["host_wheel_sdist_provenance_digest"].startswith(
        "sha256:"
    )
    assert checks_by_id["core.lifecycle.host-wheel"]["evidence_digest"] == "sha256:" + hashlib.sha256(
        json.dumps(
            {
                "host_observation_digest": bundle["independent_evidence"]["host_observation_digest"],
                "host_wheel_sdist_provenance_digest": bundle["independent_evidence"]["host_wheel_sdist_provenance_digest"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert bundle["independent_evidence"]["bundle_tamper_independent_digest"].startswith(
        "sha256:"
    )
    assert all(not key.startswith("telemetry_") for key in bundle["evidence_view"])
    assert all(not key.startswith("telemetry_") for key in bundle["independent_evidence"])
    host_observation = {
        key.removeprefix("host_"): value
        for key, value in bundle["independent_evidence"].items()
        if key.startswith("host_")
        and key
        not in {
            "host_observation_digest",
            "host_wheel_independent_digest",
            "host_wheel_identity_mutation_digest",
            "host_wheel_sdist_provenance_digest",
        }
    }
    assert host_observation == {
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
    }
    assert bundle["independent_evidence"]["host_observation_digest"] == "sha256:" + hashlib.sha256(
        json.dumps(host_observation, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert "model_digest" not in bundle["independent_evidence"]
    assert "network_disabled" not in bundle["independent_evidence"]

    manifest_path.write_text(
        manifest.model_copy(
            update={
                "required_checks": tuple(
                    check
                    for check in manifest.required_checks
                    if check.check_id != "core.lifecycle.host-wheel"
                )
            }
        ).model_dump_json()
    )
    completed = invoke(
        "run", "--manifest", str(manifest_path), "--wheel", str(wheel),
        "--sdist", str(sdist),
        "--output-dir", str(output_dir),
    )
    assert completed.returncode == 2, completed.stderr
    manifest_path.write_text(manifest.model_dump_json())

    altered_digest = "sha256:" + hashlib.sha256(altered_wheel.read_bytes()).hexdigest()
    manifest_path.write_text(
        manifest.model_copy(update={"artifact_digest": altered_digest}).model_dump_json()
    )
    completed = invoke(
        "run", "--manifest", str(manifest_path), "--wheel", str(altered_wheel),
        "--sdist", str(sdist),
        "--output-dir", str(output_dir),
    )
    assert completed.returncode == 2, completed.stderr
    manifest_path.write_text(manifest.model_dump_json())

    altered_sdist_digest = "sha256:" + hashlib.sha256(altered_sdist.read_bytes()).hexdigest()
    manifest_path.write_text(
        manifest.model_copy(update={"sdist_digest": altered_sdist_digest}).model_dump_json()
    )
    completed = invoke(
        "run", "--manifest", str(manifest_path), "--wheel", str(wheel),
        "--sdist", str(altered_sdist),
        "--output-dir", str(output_dir),
    )
    assert completed.returncode == 2, completed.stderr
    manifest_path.write_text(manifest.model_dump_json())

    completed = invoke(
        "inspect", "--manifest", str(manifest_path), "--wheel", str(wheel),
        "--sdist", str(sdist),
        "--bundle", str(bundle_path),
    )
    assert completed.returncode == 0, completed.stderr
    completed = invoke(
        "verify", "--manifest", str(manifest_path), "--wheel", str(wheel),
        "--sdist", str(sdist),
        "--bundle", str(bundle_path),
    )
    assert completed.returncode == 0, completed.stderr
    completed = invoke(
        "render", "--manifest", str(manifest_path), "--wheel", str(wheel),
        "--sdist", str(sdist),
        "--bundle", str(bundle_path),
    )
    assert completed.returncode == 0, completed.stderr
    assert "Execution: PASSED (exit 0)" in completed.stdout
    for command in ("inspect", "verify", "render"):
        completed = invoke(
            command, "--wheel", str(wheel), "--sdist", str(sdist),
            "--bundle", str(bundle_path),
        )
        assert completed.returncode == 0, completed.stderr

    second_environment = Path(temporary_directory) / "second-environment"
    completed = subprocess.run(
        [
            "uv", "venv", "--offline", "--no-project", "--python",
            sys.executable, str(second_environment),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    second_python = second_environment / "bin" / "python"
    completed = subprocess.run(
        [
            "uv", "pip", "install", "--offline", "--python", str(second_python),
            f"{wheel}[testing]",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    completed = subprocess.run(
        [
            str(second_python), "-I", "-m", "m_agent.testing", "verify",
            "--manifest", str(manifest_path), "--wheel", str(wheel),
            "--sdist", str(sdist), "--bundle", str(bundle_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    published_bundle = ScenarioEvidenceBundle.model_validate(bundle)
    repointed_bundle = ScenarioEvidenceBundle.create(
        manifest=published_bundle.manifest,
        execution=published_bundle.execution,
        execution_checks=published_bundle.execution_checks,
        scenario=published_bundle.scenario,
        checks=published_bundle.checks,
        evidence_view={**published_bundle.evidence_view, "run_succeeded": False},
        independent_evidence=published_bundle.independent_evidence,
    )
    assert repointed_bundle.content_digest != published_bundle.content_digest
    bundle_path.write_text(repointed_bundle.model_dump_json())
    for command in ("inspect", "verify", "render"):
        completed = invoke(
            command, "--manifest", str(manifest_path), "--wheel", str(wheel),
            "--sdist", str(sdist), "--bundle", str(bundle_path),
        )
        assert completed.returncode == 5, completed.stderr

    reduced_manifest = manifest.model_copy(
        update={
            "pack_version": "counterfeit",
            "profile": "reduced",
            "required_checks": (manifest.required_checks[0],),
        }
    )
    reduced_check = AcceptanceCheckResult(
        check_id=reduced_manifest.required_checks[0].check_id,
        status=AcceptanceCheckStatus.PASS,
        evidence_level=reduced_manifest.required_checks[0].evidence_level,
        reason_code="reduced_profile_result",
        evidence_digest="sha256:" + "0" * 64,
    )
    reduced_execution = PackExecution.create(
        reduced_manifest, execution_id="counterfeit-profile"
    ).complete(reduced_manifest, (reduced_check,))
    reduced_bundle = ScenarioEvidenceBundle.create(
        manifest=reduced_manifest,
        execution=reduced_execution,
        execution_checks=(reduced_check,),
        scenario="core-lifecycle",
        checks=(reduced_check,),
        evidence_view={
            "core_lifecycle_authoritative_digest": reduced_check.evidence_digest,
            "run_succeeded": True,
        },
        independent_evidence={
            "core_lifecycle_independent_digest": identity["fixture_digest"],
            "fixture_digest": identity["fixture_digest"],
        },
    )
    reduced_bundle_path = Path(temporary_directory) / (
        reduced_bundle.content_digest.removeprefix("sha256:") + ".json"
    )
    reduced_bundle_path.write_text(reduced_bundle.model_dump_json())
    for command in ("inspect", "verify", "render"):
        completed = invoke(
            command, "--wheel", str(wheel), "--sdist", str(sdist),
            "--bundle", str(reduced_bundle_path),
        )
        assert completed.returncode == 2, completed.stderr

    bundle["checks"][0]["status"] = "FAIL"
    bundle_path.write_text(json.dumps(bundle))
    for command in ("inspect", "verify", "render"):
        completed = invoke(
            command, "--manifest", str(manifest_path), "--wheel", str(wheel),
            "--sdist", str(sdist),
            "--bundle", str(bundle_path),
        )
        assert completed.returncode == 5, completed.stderr

    completed = invoke(
        "run", "--manifest", str(manifest_path), "--wheel", str(wheel),
        "--sdist", str(sdist),
        "--output-dir", str(output_dir),
    )
    assert completed.returncode == 0, completed.stderr
    second_bundle_path = Path(completed.stdout.strip())
    assert second_bundle_path != bundle_path
    assert len(list(output_dir.glob("*.json"))) == 2
    second_bundle = json.loads(second_bundle_path.read_text())
    assert second_bundle["execution"]["execution_id"] != bundle["execution"]["execution_id"]

    for update in (
        {"profile": "foundation-release", "pack_version": "counterfeit"},
        {"source_commit": "counterfeit-source"},
        {"artifact_digest": "sha256:" + "0" * 64},
        {"sdist_digest": "sha256:" + "0" * 64},
        {"fixture_digest": "sha256:" + "0" * 64},
        {"environment": {**identity["environment"], "os": "counterfeit"}},
    ):
        manifest_path.write_text(manifest.model_copy(update=update).model_dump_json())
        completed = invoke(
            "run", "--manifest", str(manifest_path), "--wheel", str(wheel),
            "--sdist", str(sdist),
            "--output-dir", str(output_dir),
        )
        assert completed.returncode == 2, completed.stderr

    manifest_path.write_text(manifest.model_dump_json())
    harness_output_dir = Path(temporary_directory) / "harness-bundles"
    no_uv_environment = {**os.environ, "PATH": "/usr/bin:/bin"}
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "m_agent.testing",
            "run",
            "--manifest",
            str(manifest_path),
            "--wheel",
            str(wheel),
            "--sdist",
            str(sdist),
            "--output-dir",
            str(harness_output_dir),
        ],
        env=no_uv_environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 3, completed.stderr
    harness_bundle = json.loads(Path(completed.stdout.strip()).read_text())
    assert harness_bundle["execution"]["status"] == "ERROR"
    assert harness_bundle["execution"]["exit_code"] == 3
    assert next(
        check["status"]
        for check in harness_bundle["checks"]
        if check["check_id"] == "core.lifecycle.host-wheel"
    ) == "ERROR"

    manifest_path.write_text(manifest.model_dump_json())
    completed = subprocess.run(
        ["uv", "pip", "install", "--offline", "--python", sys.executable, "pytest"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    completed = invoke(
        "run", "--manifest", str(manifest_path), "--wheel", str(wheel),
        "--sdist", str(sdist),
        "--output-dir", str(output_dir),
    )
    assert completed.returncode == 2, completed.stderr
assert metadata("m-agent")["Name"] == "m-agent"
print("m-agent Foundation wheel contract passed")
"""


_HOST_SUBJECT_FAILURE_PROBE = """
import subprocess
import sys
import tempfile
from pathlib import Path

from m_agent.testing import core_lifecycle_manifest, installed_identity

wheel = Path(sys.argv[1])
sdist = Path(sys.argv[2])
identity = installed_identity(artifact=wheel, sdist=sdist)
manifest = core_lifecycle_manifest(
    source_commit=identity["source_commit"],
    artifact_digest=identity["artifact_digest"],
    sdist_digest=identity["sdist_digest"],
    fixture_digest=identity["fixture_digest"],
    environment=identity["environment"],
)
with tempfile.TemporaryDirectory() as temporary_directory:
    manifest_path = Path(temporary_directory) / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json())
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "m_agent.testing",
            "run",
            "--manifest",
            str(manifest_path),
            "--wheel",
            str(wheel),
            "--sdist",
            str(sdist),
            "--output-dir",
            str(Path(temporary_directory) / "bundles"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 1, completed.stderr
print("m-agent HOST subject failure contract passed")
"""


_SDIST_DERIVED_WHEEL_IDENTITY_PROBE = """
import sys

from m_agent.testing import installed_identity


identity = installed_identity()
assert identity["source_commit"] == sys.argv[1], identity
assert identity["environment"]["source_state"] == "clean", identity
assert identity["environment"]["installation"] == "wheel", identity
print("m-agent sdist-derived wheel identity passed")
"""


_SUPPLIED_SDIST_PROVENANCE_FAILURE_PROBE = """
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

from m_agent.testing import core_lifecycle_manifest, installed_identity


wheel = Path(sys.argv[1])
sdist = Path(sys.argv[2])
identity = installed_identity(artifact=wheel)
manifest = core_lifecycle_manifest(
    source_commit=identity["source_commit"],
    artifact_digest=identity["artifact_digest"],
    sdist_digest="sha256:" + hashlib.sha256(sdist.read_bytes()).hexdigest(),
    fixture_digest=identity["fixture_digest"],
    environment=identity["environment"],
)
with tempfile.TemporaryDirectory() as temporary_directory:
    manifest_path = Path(temporary_directory) / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json())
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "m_agent.testing",
            "run",
            "--manifest",
            str(manifest_path),
            "--wheel",
            str(wheel),
            "--sdist",
            str(sdist),
            "--output-dir",
            str(Path(temporary_directory) / "bundles"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2, completed.stderr
print("m-agent supplied sdist provenance rejection passed")
"""


_EXPAND_COMPATIBILITY_FAILURE_PROBE = """
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from m_agent.testing import core_lifecycle_manifest, installed_identity

wheel = Path(sys.argv[1])
sdist = Path(sys.argv[2])
identity = installed_identity(artifact=wheel, sdist=sdist)
manifest = core_lifecycle_manifest(
    source_commit=identity["source_commit"],
    artifact_digest=identity["artifact_digest"],
    sdist_digest=identity["sdist_digest"],
    fixture_digest=identity["fixture_digest"],
    environment=identity["environment"],
)
with tempfile.TemporaryDirectory() as temporary_directory:
    manifest_path = Path(temporary_directory) / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json())
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "m_agent.testing",
            "run",
            "--manifest",
            str(manifest_path),
            "--wheel",
            str(wheel),
            "--sdist",
            str(sdist),
            "--output-dir",
            str(Path(temporary_directory) / "bundles"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 1, completed.stderr
    bundle = json.loads(Path(completed.stdout.strip()).read_text())
    assert next(
        check["status"]
        for check in bundle["checks"]
        if check["check_id"] == "core.lifecycle.expand-compatibility"
    ) == "FAIL"
print("m-agent expand compatibility contract passed")
"""


_TAMPERED_SOURCE_PROBE = """
import sys
from pathlib import Path

from m_agent.testing import installed_identity

identity = installed_identity(artifact=Path(sys.argv[1]))
assert identity["environment"]["source_state"] == "dirty"
print("m-agent modified archive provenance rejected")
"""


class DistributionIdentityTests(unittest.TestCase):
    def test_built_m_agent_distribution_runs_public_runner_in_clean_environment(
        self,
    ) -> None:
        """The published wheel is M-Agent and independently exposes Runner."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            dist_dir = temporary_root / "dist"
            clean_environment = _clean_environment()

            _run(
                [
                    "uv",
                    "build",
                    "--offline",
                    "--wheel",
                    "--sdist",
                    "--out-dir",
                    str(dist_dir),
                ],
                cwd=_ROOT,
                env=clean_environment,
            )
            wheels = list(dist_dir.glob("*.whl"))
            self.assertEqual(len(wheels), 1)

            with zipfile.ZipFile(wheels[0]) as wheel:
                metadata_name = next(
                    name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
                )
                metadata = wheel.read(metadata_name).decode("utf-8")
            self.assertIn("Name: m-agent\n", metadata)

            environment_dir = temporary_root / "environment"
            _run(
                [
                    "uv",
                    "venv",
                    "--offline",
                    "--no-project",
                    "--python",
                    sys.executable,
                    str(environment_dir),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            python = environment_dir / "bin" / "python"
            target_site = Path(
                _run(
                    [
                        str(python),
                        "-c",
                        "import site; print(site.getsitepackages()[0])",
                    ],
                    cwd=temporary_root,
                    env=clean_environment,
                ).strip()
            )
            _copy_locked_runtime_dependencies(target_site)
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--no-deps",
                    "--python",
                    str(python),
                    str(wheels[0]),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            output = _run(
                [str(python), "-c", _CLEAN_RUNNER_PROBE],
                cwd=temporary_root,
                env=clean_environment,
            )
            self.assertIn("m-agent distribution Runner contract passed", output)

    def test_built_wheel_runs_foundation_pack_from_public_namespaces_only(self) -> None:
        """The Ticket 07 Pack works outside source with isolated imports."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            dist_dir = temporary_root / "dist"
            clean_environment = _clean_environment()
            _run(
                [
                    "uv",
                    "build",
                    "--offline",
                    "--wheel",
                    "--sdist",
                    "--out-dir",
                    str(dist_dir),
                ],
                cwd=_ROOT,
                env=clean_environment,
            )
            wheel = next(dist_dir.glob("*.whl"))
            sdist = next(dist_dir.glob("*.tar.gz"))
            environment_dir = temporary_root / "environment"
            _run(
                [
                    "uv",
                    "venv",
                    "--offline",
                    "--no-project",
                    "--python",
                    sys.executable,
                    str(environment_dir),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            python = environment_dir / "bin" / "python"
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--python",
                    str(python),
                    f"{wheel}[testing]",
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            output = _run(
                [
                    str(python),
                    "-I",
                    "-c",
                    _FOUNDATION_INSTALLED_PROBE,
                    str(wheel),
                    str(sdist),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            self.assertIn("m-agent Foundation wheel contract passed", output)

    def test_clean_sdist_builds_a_wheel_with_public_clean_identity(self) -> None:
        """The normal clean source-distribution release path remains a HOST subject."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            repository_root = temporary_root / "repository"
            shutil.copytree(
                _ROOT,
                repository_root,
                ignore=shutil.ignore_patterns(
                    ".git", ".venv", ".pytest_cache", "__pycache__", "build", "dist"
                ),
            )
            clean_environment = _clean_environment()
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "ticket07@example.invalid"],
                ["git", "config", "user.name", "Ticket 07"],
                ["git", "add", "--all"],
                ["git", "commit", "-qm", "clean sdist provenance subject"],
            ):
                _run(command, cwd=repository_root, env=clean_environment)
            commit = _run(
                ["git", "rev-parse", "HEAD"], cwd=repository_root, env=clean_environment
            ).strip()
            sdist_dir = temporary_root / "sdist"
            _run(
                [
                    "uv",
                    "build",
                    "--offline",
                    "--sdist",
                    "--out-dir",
                    str(sdist_dir),
                ],
                cwd=repository_root,
                env=clean_environment,
            )
            sdist = next(sdist_dir.glob("*.tar.gz"))
            extracted = temporary_root / "extracted"
            shutil.unpack_archive(str(sdist), str(extracted))
            extracted_root = next(extracted.iterdir())
            wheel_dir = temporary_root / "wheel"
            _run(
                [
                    "uv",
                    "build",
                    "--offline",
                    "--wheel",
                    "--out-dir",
                    str(wheel_dir),
                ],
                cwd=extracted_root,
                env=clean_environment,
            )
            wheel = next(wheel_dir.glob("*.whl"))

            environment_dir = temporary_root / "environment"
            _run(
                [
                    "uv",
                    "venv",
                    "--offline",
                    "--no-project",
                    "--python",
                    sys.executable,
                    str(environment_dir),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            python = environment_dir / "bin" / "python"
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--python",
                    str(python),
                    f"{wheel}[testing]",
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            output = _run(
                [
                    str(python),
                    "-I",
                    "-c",
                    _SDIST_DERIVED_WHEEL_IDENTITY_PROBE,
                    commit,
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            self.assertIn("m-agent sdist-derived wheel identity passed", output)

    def test_foundation_cli_rejects_wheel_not_built_from_supplied_sdist(self) -> None:
        """A self-declared source map cannot bind a different installed wheel."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            clean_environment = _clean_environment()
            dist_dir = temporary_root / "dist"
            _run(
                [
                    "uv",
                    "build",
                    "--offline",
                    "--wheel",
                    "--sdist",
                    "--out-dir",
                    str(dist_dir),
                ],
                cwd=_ROOT,
                env=clean_environment,
            )
            sdist = next(dist_dir.glob("*.tar.gz"))
            source = temporary_root / "source"
            shutil.unpack_archive(str(sdist), str(source))
            source_root = next(source.iterdir())
            runtime_module = source_root / "src" / "m_agent" / "runtime" / "__init__.py"
            runtime_module.write_text(
                runtime_module.read_text() + "\nPROVENANCE_TAMPER_MARKER = True\n"
            )
            integrity_path = source_root / "SOURCE_INTEGRITY.json"
            integrity_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "files": {
                            path.relative_to(source_root).as_posix(): "sha256:"
                            + hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in sorted(source_root.rglob("*"))
                            if path.is_file() and path != integrity_path
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            altered_dist = temporary_root / "altered-dist"
            _run(
                [
                    "uv",
                    "build",
                    "--offline",
                    "--wheel",
                    "--out-dir",
                    str(altered_dist),
                ],
                cwd=source_root,
                env=clean_environment,
            )
            altered_wheel = next(altered_dist.glob("*.whl"))
            environment_dir = temporary_root / "environment"
            _run(
                [
                    "uv",
                    "venv",
                    "--offline",
                    "--no-project",
                    "--python",
                    sys.executable,
                    str(environment_dir),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            python = environment_dir / "bin" / "python"
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--python",
                    str(python),
                    f"{altered_wheel}[testing]",
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            output = _run(
                [
                    str(python),
                    "-I",
                    "-c",
                    _SUPPLIED_SDIST_PROVENANCE_FAILURE_PROBE,
                    str(altered_wheel),
                    str(sdist),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            self.assertIn("m-agent supplied sdist provenance rejection passed", output)

    def test_foundation_cli_reports_host_subject_failures_as_exit_one(self) -> None:
        """A valid HOST observation with a false subject conclusion is a failure."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            archive = temporary_root / "source.tar"
            source_root = temporary_root / "source"
            source_root.mkdir()
            clean_environment = _clean_environment()
            if (_ROOT / ".git").exists():
                _run(
                    ["git", "archive", "--format=tar", "--output", str(archive), "HEAD"],
                    cwd=_ROOT,
                    env=clean_environment,
                )
                _run(
                    ["tar", "-xf", str(archive), "-C", str(source_root)],
                    cwd=temporary_root,
                    env=clean_environment,
                )
            else:
                shutil.copytree(
                    _ROOT,
                    source_root,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".venv", ".pytest_cache", "__pycache__"),
                )
            entrypoint = source_root / "src" / "m_agent" / "testing" / "__main__.py"
            source = (_ROOT / "src" / "m_agent" / "testing" / "__main__.py").read_text()
            changed = source.replace(
                '"run_succeeded": terminal.status is RunStatus.SUCCEEDED,',
                '"run_succeeded": False,',
                1,
            )
            self.assertNotEqual(changed, source)
            entrypoint.write_text(changed)
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "ticket07@example.invalid"],
                ["git", "config", "user.name", "Ticket 07"],
                ["git", "add", "--all"],
                ["git", "commit", "-qm", "controlled host subject failure"],
            ):
                _run(command, cwd=source_root, env=clean_environment)

            dist_dir = temporary_root / "dist"
            _run(
                [
                    "uv",
                    "build",
                    "--offline",
                    "--wheel",
                    "--sdist",
                    "--out-dir",
                    str(dist_dir),
                ],
                cwd=source_root,
                env=clean_environment,
            )
            wheel = next(dist_dir.glob("*.whl"))
            sdist = next(dist_dir.glob("*.tar.gz"))
            environment_dir = temporary_root / "environment"
            _run(
                [
                    "uv",
                    "venv",
                    "--offline",
                    "--no-project",
                    "--python",
                    sys.executable,
                    str(environment_dir),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            python = environment_dir / "bin" / "python"
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--python",
                    str(python),
                    f"{wheel}[testing]",
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            output = _run(
                [
                    str(python),
                    "-I",
                    "-c",
                    _HOST_SUBJECT_FAILURE_PROBE,
                    str(wheel),
                    str(sdist),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            self.assertIn("m-agent HOST subject failure contract passed", output)

    def test_foundation_cli_rejects_misbound_legacy_root_export(self) -> None:
        """The required expand check covers semantic 0.2 root bindings."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            archive = temporary_root / "source.tar"
            source_root = temporary_root / "source"
            source_root.mkdir()
            clean_environment = _clean_environment()
            _run(
                ["git", "archive", "--format=tar", "--output", str(archive), "HEAD"],
                cwd=_ROOT,
                env=clean_environment,
            )
            _run(
                ["tar", "-xf", str(archive), "-C", str(source_root)],
                cwd=temporary_root,
                env=clean_environment,
            )
            root_package = source_root / "src" / "m_agent" / "__init__.py"
            source = root_package.read_text()
            changed = source.replace(
                "from ._clock import Clock, FakeClock, SystemClock",
                "from ._clock import Clock, SystemClock\nFakeClock = SystemClock",
                1,
            )
            self.assertNotEqual(changed, source)
            root_package.write_text(changed)
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "ticket07@example.invalid"],
                ["git", "config", "user.name", "Ticket 07"],
                ["git", "add", "--all"],
                ["git", "commit", "-qm", "controlled expand incompatibility"],
            ):
                _run(command, cwd=source_root, env=clean_environment)

            dist_dir = temporary_root / "dist"
            _run(
                [
                    "uv",
                    "build",
                    "--offline",
                    "--wheel",
                    "--sdist",
                    "--out-dir",
                    str(dist_dir),
                ],
                cwd=source_root,
                env=clean_environment,
            )
            wheel = next(dist_dir.glob("*.whl"))
            sdist = next(dist_dir.glob("*.tar.gz"))
            environment_dir = temporary_root / "environment"
            _run(
                [
                    "uv",
                    "venv",
                    "--offline",
                    "--no-project",
                    "--python",
                    sys.executable,
                    str(environment_dir),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            python = environment_dir / "bin" / "python"
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--python",
                    str(python),
                    f"{wheel}[testing]",
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            output = _run(
                [
                    str(python),
                    "-I",
                    "-c",
                    _EXPAND_COMPATIBILITY_FAILURE_PROBE,
                    str(wheel),
                    str(sdist),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            self.assertIn("m-agent expand compatibility contract passed", output)

    def test_modified_exported_source_cannot_claim_clean_provenance(self) -> None:
        """A changed Git-less export is never a clean reviewed source subject."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_root = temporary_root / "source"
            source_root.mkdir()
            archive = temporary_root / "source.tar"
            clean_environment = _clean_environment()
            _run(
                ["git", "archive", "--format=tar", "--output", str(archive), "HEAD"],
                cwd=_ROOT,
                env=clean_environment,
            )
            _run(
                ["tar", "-xf", str(archive), "-C", str(source_root)],
                cwd=temporary_root,
                env=clean_environment,
            )
            runtime_module = source_root / "src" / "m_agent" / "runtime" / "__init__.py"
            runtime_module.write_text(runtime_module.read_text() + "\nPROVENANCE_TAMPER_MARKER = True\n")
            dist_dir = temporary_root / "dist"
            _run(
                ["uv", "build", "--offline", "--wheel", "--out-dir", str(dist_dir)],
                cwd=source_root,
                env=clean_environment,
            )
            wheel = next(dist_dir.glob("*.whl"))
            environment_dir = temporary_root / "environment"
            _run(
                [
                    "uv",
                    "venv",
                    "--offline",
                    "--no-project",
                    "--python",
                    sys.executable,
                    str(environment_dir),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            python = environment_dir / "bin" / "python"
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--python",
                    str(python),
                    f"{wheel}[testing]",
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            output = _run(
                [str(python), "-I", "-c", _TAMPERED_SOURCE_PROBE, str(wheel)],
                cwd=temporary_root,
                env=clean_environment,
            )
            self.assertIn("m-agent modified archive provenance rejected", output)

    def test_ignored_nested_source_cannot_claim_ancestor_git_identity(self) -> None:
        """A copied source tree must not inherit a clean parent repository SHA."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            parent = temporary_root / "parent"
            parent.mkdir()
            clean_environment = _clean_environment()
            (parent / ".gitignore").write_text("ignored-source/\n")
            (parent / "marker").write_text("parent only\n")
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "ticket07@example.invalid"],
                ["git", "config", "user.name", "Ticket 07"],
                ["git", "add", ".gitignore", "marker"],
                ["git", "commit", "-qm", "unrelated parent"],
            ):
                _run(command, cwd=parent, env=clean_environment)
            parent_commit = _run(
                ["git", "rev-parse", "HEAD"], cwd=parent, env=clean_environment
            ).strip()
            source_root = parent / "ignored-source"
            shutil.copytree(
                _ROOT,
                source_root,
                ignore=shutil.ignore_patterns(
                    ".git", ".venv", ".pytest_cache", "__pycache__", "build", "dist"
                ),
            )
            dist_dir = temporary_root / "dist"
            _run(
                ["uv", "build", "--offline", "--wheel", "--out-dir", str(dist_dir)],
                cwd=source_root,
                env=clean_environment,
            )
            wheel = next(dist_dir.glob("*.whl"))
            with zipfile.ZipFile(wheel) as artifact:
                identity = artifact.read("m_agent/_build_identity.py").decode("utf-8")
            self.assertIn("SOURCE_COMMIT = 'unavailable'", identity)
            self.assertNotIn(parent_commit, identity)

    def test_built_sdist_runs_flagship_against_installed_m_agent(self) -> None:
        """The packaged flagship uses the installed runtime, never checkout src."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            dist_dir = temporary_root / "dist"
            clean_environment = _clean_environment()

            _run(
                ["uv", "build", "--offline", "--sdist", "--out-dir", str(dist_dir)],
                cwd=_ROOT,
                env=clean_environment,
            )
            sdists = list(dist_dir.glob("*.tar.gz"))
            self.assertEqual(len(sdists), 1)

            extracted = temporary_root / "source"
            extracted.mkdir()
            _run(
                ["tar", "-xzf", str(sdists[0]), "-C", str(extracted)],
                cwd=temporary_root,
                env=clean_environment,
            )
            project = next(extracted.iterdir())
            environment_dir = temporary_root / "environment"
            _run(
                [
                    "uv",
                    "venv",
                    "--offline",
                    "--no-project",
                    "--python",
                    sys.executable,
                    str(environment_dir),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            python = environment_dir / "bin" / "python"
            target_site = Path(
                _run(
                    [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
                    cwd=temporary_root,
                    env=clean_environment,
                ).strip()
            )
            _copy_locked_runtime_dependencies(target_site)
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--offline",
                    "--no-deps",
                    "--python",
                    str(python),
                    str(project),
                ],
                cwd=temporary_root,
                env=clean_environment,
            )
            import_output = _run(
                [str(python), "-c", _FLAGSHIP_INSTALLED_PROBE],
                cwd=temporary_root,
                env=clean_environment,
            )
            self.assertIn("m-agent installed artifact import passed", import_output)
            output = _run(
                [
                    str(python),
                    "examples/durable_support_agent/run_acceptance.py",
                ],
                cwd=project,
                env=clean_environment,
            )
            self.assertIn("11/11 checks passed", output)
