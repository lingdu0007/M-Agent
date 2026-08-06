"""Ticket 01 distribution contract through a built wheel and public Runner."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


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


_CLEAN_RUNNER_PROBE = """
import asyncio
from importlib.metadata import metadata

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
                ["uv", "venv", "--offline", "--no-project", str(environment_dir)],
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
