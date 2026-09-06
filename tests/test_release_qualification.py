"""Release-tool CLI checks; archived evidence is a fixture, not a new release."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from m_agent.testing import (
    AcceptanceManifest,
    PackExecution,
    ScenarioEvidenceBundle,
    foundation_release_0_5_manifest,
)

ROOT = Path(__file__).parents[1]
TOOL = ROOT / "tools" / "release_qualification.py"
ARCHIVE = ROOT / "rel05-evidence"
SOURCE = "ed7177d047a528070c23bc06c84fb10c7817d2fb"
WHEEL = "sha256:4cf648474e6461e0285459a00d4f43ca3f26eff88ffe4c35fdece630bf74cb27"
SDIST = "sha256:09d458160b66509a4e0f393e41e6eafb0f235cab992689bb23189492bdb97263"
FIXTURE = "sha256:76d7fee8fbe3293841daf42091e723ab14e5c974121a23f66dfc21ee8b56562d"


class ReleaseQualificationTests(unittest.TestCase):
    def run_tool(
        self, directory: Path, *extra: str, command: str = "verify-cell"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(TOOL),
                command,
                "--directory",
                str(directory),
                "--source-commit",
                SOURCE,
                "--wheel-digest",
                WHEEL,
                "--sdist-digest",
                SDIST,
                "--fixture-digest",
                FIXTURE,
                "--version",
                "0.5.0",
                *extra,
            ],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def fixture(self, directory: Path) -> None:
        shutil.copy(ARCHIVE / "manifest.json", directory / "manifest.json")
        shutil.copytree(ARCHIVE / "bundles", directory / "bundles")

    def synthetic_matrix(self, directory: Path) -> Path:
        """Synthetic records test the verifier; they are not HOST observations."""
        templates = [
            ScenarioEvidenceBundle.model_validate_json(path.read_text())
            for path in (ARCHIVE / "bundles").glob("*.json")
        ]
        for platform, minor in (
            ("linux", "3.11"),
            ("linux", "3.12"),
            ("linux", "3.13"),
            ("linux", "3.14"),
            ("darwin", "3.11"),
            ("darwin", "3.14"),
        ):
            manifest = foundation_release_0_5_manifest(
                source_commit=SOURCE,
                artifact_digest=WHEEL,
                sdist_digest=SDIST,
                fixture_digest=FIXTURE,
                environment={
                    **templates[0].manifest.environment,
                    "os": platform,
                    "python": f"{minor}.15",
                    "architecture": "arm64",
                },
            )
            execution = (
                PackExecution.create(
                    manifest, execution_id=f"synthetic-{platform}-{minor}"
                )
                .start(manifest)
                .complete(manifest, templates[0].execution_checks)
            )
            cell = directory / f"cell-{platform}-{minor}"
            (cell / "bundles").mkdir(parents=True)
            (cell / "manifest.json").write_text(manifest.model_dump_json())
            for template in templates:
                bundle = ScenarioEvidenceBundle.create(
                    manifest=manifest,
                    execution=execution,
                    execution_checks=template.execution_checks,
                    scenario=template.scenario,
                    checks=template.checks,
                    evidence_view=template.evidence_view,
                    independent_evidence=template.independent_evidence,
                )
                (cell / "bundles" / f"{template.scenario}.json").write_text(
                    bundle.model_dump_json()
                )
            if platform == "linux" and minor == "3.11":
                (cell / "benchmarks").mkdir()
                for name in ("durable-run", "session", "context", "eval"):
                    report = json.loads(
                        (
                            ROOT
                            / "benchmarks/results"
                            / f"release-0.5-{name}-python311-macos-arm64.json"
                        ).read_text()
                    )
                    report["environment"]["os"]["system"] = "Linux"
                    report["baseline_identity"]["manifest_digest"] = manifest.digest
                    (cell / "benchmarks" / f"{name}.json").write_text(
                        json.dumps(report)
                    )
        primary = directory / "cell-linux-3.11"
        self.index_benchmarks(primary)
        return primary

    def index_benchmarks(self, cell: Path) -> None:
        manifest = AcceptanceManifest.model_validate_json(
            (cell / "manifest.json").read_text()
        )
        index = {
            "schema_version": 1,
            "artifact_digest": WHEEL,
            "manifest_digest": manifest.digest,
            "reports": {},
        }
        index["reports"] = {
            path.name: "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (cell / "benchmarks").glob("*.json")
            if path.name != "index.json"
        }
        (cell / "benchmarks/index.json").write_text(json.dumps(index))

    def test_complete_archived_cell_verifies_at_its_own_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            result = self.run_tool(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["scenario_count"], 6)
            self.assertEqual(report["required_checks"], 48)
            self.assertEqual(report["artifact_digest"], WHEEL)

    def test_missing_duplicate_and_tampered_bundles_are_refused(self) -> None:
        for mutation in (
            "missing",
            "duplicate",
            "tampered",
            "profile",
            "mixed-execution",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                self.fixture(directory)
                bundle = next((directory / "bundles").glob("*.json"))
                if mutation == "missing":
                    bundle.unlink()
                elif mutation == "duplicate":
                    shutil.copy(bundle, bundle.parent / "duplicate.json")
                elif mutation == "tampered":
                    data = json.loads(bundle.read_text())
                    data["content_digest"] = "sha256:" + "0" * 64
                    bundle.write_text(json.dumps(data))
                elif mutation == "mixed-execution":
                    data = json.loads(bundle.read_text())
                    data["execution"]["execution_id"] = "another-execution"
                    bundle.write_text(json.dumps(data))
                else:
                    path = directory / "manifest.json"
                    data = json.loads(path.read_text())
                    data["required_checks"].pop()
                    path.write_text(json.dumps(data))
                result = self.run_tool(directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('"status": "PASS"', result.stdout)

    def test_wrong_candidate_identity_is_refused(self) -> None:
        for field, value in (
            ("--source-commit", "0" * 40),
            ("--wheel-digest", "sha256:" + "0" * 64),
            ("--sdist-digest", "sha256:" + "0" * 64),
            ("--fixture-digest", "sha256:" + "0" * 64),
            ("--version", "0.5.1"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                self.fixture(directory)
                result = self.run_tool(directory, field, value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("expected clean release candidate", result.stderr)

    def test_directory_label_cannot_supply_platform_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cell = directory / "cell-darwin-3.11"
            cell.mkdir()
            self.fixture(cell)
            result = self.run_tool(
                directory,
                "--output",
                str(directory / "summary"),
                command="aggregate",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("outside the frozen platform matrix", result.stderr)
            self.assertFalse((directory / "summary").exists())

    def test_empty_platform_matrix_cannot_qualify(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = self.run_tool(
                directory,
                "--output",
                str(directory / "summary"),
                command="aggregate",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing required matrix observations", result.stderr)
            self.assertFalse((directory / "summary").exists())

    def test_complete_synthetic_matrix_verifies_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.synthetic_matrix(directory)
            result = self.run_tool(
                directory, "--output", str(directory / "summary"), command="aggregate"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["platform_cells"], 7)

    def test_benchmark_evidence_must_be_complete_bound_and_intact(self) -> None:
        for mutation in (
            "minimal",
            "environment",
            "measurement",
            "validation",
            "tamper",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                primary = self.synthetic_matrix(directory)
                path = primary / "benchmarks/durable-run.json"
                report = json.loads(path.read_text())
                if mutation == "minimal":
                    report = {
                        "validation": report["validation"],
                        "baseline_identity": report["baseline_identity"],
                    }
                elif mutation == "environment":
                    report["environment"]["python"]["version"] = "3.14.15"
                elif mutation == "measurement":
                    report["measurement"].pop("run_count")
                elif mutation == "validation":
                    report["validation"]["terminal_runs"] = 99
                else:
                    report["metrics"]["throughput_runs_per_second"] *= 2
                path.write_text(json.dumps(report))
                if mutation != "tamper":
                    self.index_benchmarks(primary)
                result = self.run_tool(
                    directory,
                    "--output",
                    str(directory / "summary"),
                    command="aggregate",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((directory / "summary").exists())

    def test_every_workload_requires_correctness_and_measurements(self) -> None:
        for workload in ("durable-run", "session", "context", "eval"):
            for mutation in ("problems", "database", "metrics"):
                with (
                    self.subTest(workload=workload, mutation=mutation),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    directory = Path(tmp)
                    primary = self.synthetic_matrix(directory)
                    path = primary / "benchmarks" / f"{workload}.json"
                    report = json.loads(path.read_text())
                    if mutation == "problems":
                        report["validation"]["problems"] = ["missing persisted row"]
                    elif mutation == "database":
                        store = {
                            "durable-run": "database",
                            "session": "session_database",
                            "context": "run_database",
                            "eval": "eval_store",
                        }[workload]
                        report[store]["integrity_check"] = "corrupt"
                    else:
                        report["metrics"] = {}
                    path.write_text(json.dumps(report))
                    self.index_benchmarks(primary)
                    result = self.run_tool(
                        directory,
                        "--output",
                        str(directory / "summary"),
                        command="aggregate",
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((directory / "summary").exists())


if __name__ == "__main__":
    unittest.main()
