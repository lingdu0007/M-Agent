"""Ticket 01 distribution contract through a built wheel and public Runner."""

from __future__ import annotations

import importlib
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
import hashlib
import json
from importlib.metadata import metadata
from pathlib import Path
import sys
import tempfile
import zipfile

import m_agent
from m_agent.adapters import DeterministicModelAdapter, InMemoryRunStore, PlaintextPayloadCodec
from m_agent.runtime import AgentDefinition, DefinitionRegistry, Runner
from m_agent.testing import core_lifecycle_manifest, installed_identity

assert Path(m_agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
wheel = Path(sys.argv[1])
sdist = Path(sys.argv[2])
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
    manifest_path.write_text(manifest.model_dump_json())
    import subprocess

    def invoke(*arguments):
        return subprocess.run(
            [sys.executable, "-I", "-m", "m_agent.testing", *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    completed = invoke(
        "run", "--manifest", str(manifest_path), "--wheel", str(wheel),
        "--sdist", str(sdist),
        "--output-dir", str(output_dir),
    )
    assert completed.returncode == 0, completed.stderr
    bundle_path = Path(completed.stdout.strip())
    assert bundle_path.parent == output_dir
    bundle = json.loads(bundle_path.read_text())
    assert bundle_path.stem == bundle["content_digest"].removeprefix("sha256:")
    assert bundle["manifest"] == manifest.model_dump(mode="json")
    assert bundle["execution"]["status"] == "PASSED"
    assert bundle["execution"]["exit_code"] == 0
    assert all(check["reason_code"] and check["evidence_digest"] for check in bundle["checks"])
    assert any(
        check["check_id"] == "core.lifecycle.host-wheel"
        and check["evidence_level"] == "HOST"
        and check["status"] == "PASS"
        for check in bundle["checks"]
    )
    assert bundle["independent_evidence"]["fixture_digest"] == identity["fixture_digest"]
    assert bundle["independent_evidence"]["host_observation_digest"].startswith("sha256:")
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
    for command in ("inspect", "verify", "render"):
        completed = invoke(
            command, "--wheel", str(wheel), "--sdist", str(sdist),
            "--bundle", str(bundle_path),
        )
        assert completed.returncode == 0, completed.stderr

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
