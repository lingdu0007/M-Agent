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
    excluded = {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
    return {key: value for key, value in os.environ.items() if key not in excluded}


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
                ["uv", "build", "--offline", "--wheel", "--out-dir", str(dist_dir)],
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
