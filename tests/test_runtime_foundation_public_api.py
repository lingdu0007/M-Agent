"""Public-contract tracer bullets for the Runtime Foundation surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class LayeredRuntimePublicApiTests(unittest.TestCase):
    @staticmethod
    def _installed_manifest_identity() -> tuple[str, str, dict[str, str]]:
        from m_agent.testing import installed_identity

        identity = installed_identity()
        return (
            str(identity["source_commit"]),
            str(identity["artifact_digest"]),
            dict(identity["environment"]),
        )

    @staticmethod
    def _check_result(
        check_id: str,
        *,
        status: str = "PASS",
        evidence_level: object = "CONTRACT",
    ) -> object:
        from m_agent.testing import AcceptanceCheckResult

        return AcceptanceCheckResult(
            check_id=check_id,
            status=status,
            evidence_level=evidence_level,
            reason_code="test_result",
            evidence_digest="sha256:" + "0" * 64,
        )

    def test_root_facade_and_runtime_namespace_complete_one_lifecycle(self) -> None:
        """An integrator can use only public imports for a deterministic Run."""
        from m_agent import (
            AgentDefinition,
            DefinitionRegistry,
            DeterministicModelAdapter,
            InMemoryRunStore,
            PlaintextPayloadCodec,
            Runner,
        )
        from m_agent.adapters import (
            DeterministicModelAdapter as SemanticDeterministicModelAdapter,
        )
        from m_agent.adapters import InMemoryRunStore as SemanticInMemoryRunStore
        from m_agent.adapters import PlaintextPayloadCodec as SemanticPlaintextPayloadCodec
        from m_agent.runtime import (
            AgentDefinition as SemanticAgentDefinition,
        )
        from m_agent.runtime import DefinitionRegistry as SemanticDefinitionRegistry
        from m_agent.runtime import Runner as SemanticRunner
        from m_agent.runtime import RunStatus

        async def exercise(
            definition_type: object,
            registry_type: object,
            adapter_type: object,
            store_type: object,
            codec_type: object,
            runner_type: object,
        ) -> tuple[object, object]:
            registry = registry_type()
            registry.register(
                definition_type(
                    definition_id="core-lifecycle",
                    version="1.0",
                    instructions="Use the deterministic fixture.",
                    model_adapter=adapter_type(("accepted",)),
                )
            )
            runner = runner_type(
                registry=registry,
                store=store_type(payload_codec=codec_type()),
            )
            created = await runner.create_run("core-lifecycle", "1.0", "fixture")
            terminal = await runner.start_run(created.run_id)
            inspection = await runner.inspect_run(created.run_id)
            return terminal, inspection

        legacy_terminal, legacy_inspection = asyncio.run(
            exercise(
                AgentDefinition,
                DefinitionRegistry,
                DeterministicModelAdapter,
                InMemoryRunStore,
                PlaintextPayloadCodec,
                Runner,
            )
        )
        semantic_terminal, semantic_inspection = asyncio.run(
            exercise(
                SemanticAgentDefinition,
                SemanticDefinitionRegistry,
                SemanticDeterministicModelAdapter,
                SemanticInMemoryRunStore,
                SemanticPlaintextPayloadCodec,
                SemanticRunner,
            )
        )
        for terminal, inspection in (
            (legacy_terminal, legacy_inspection),
            (semantic_terminal, semantic_inspection),
        ):
            self.assertIs(terminal.status, RunStatus.SUCCEEDED)
            self.assertEqual(len(inspection.steps), 1)
            self.assertEqual(len(inspection.attempts), 1)
            self.assertEqual(len(inspection.checkpoints), 1)
        self.assertEqual(legacy_terminal.output, semantic_terminal.output)

    def test_manifest_freezes_identity_and_execution_rejects_mismatch(self) -> None:
        from m_agent.testing import (
            AcceptanceCheck,
            AcceptanceManifest,
            PackExecution,
        )

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:" + "a" * 64,
            fixture_digest="sha256:" + "b" * 64,
            environment={"python": "3.11", "os": "linux"},
            scenarios=("core-lifecycle",),
            required_checks=(
                AcceptanceCheck(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
            ),
        )
        execution = PackExecution.create(manifest, execution_id="exec-1")
        self.assertEqual(execution.manifest_digest, manifest.digest)
        self.assertEqual(execution.status.value, "CREATED")
        running = execution.start(manifest)
        self.assertEqual(running.status.value, "RUNNING")
        self.assertEqual(
            PackExecution.create(manifest, execution_id="exec-2").manifest_digest,
            execution.manifest_digest,
        )
        for field, value in (
            ("schema_version", "2"),
            ("source_commit", "different"),
            ("artifact_digest", "different"),
            ("fixture_digest", "different"),
            ("environment", {"python": "3.12", "os": "linux"}),
            ("scenarios", ("other-scenario",)),
            ("required_checks", ()),
        ):
            with self.subTest(field=field):
                changed = manifest.model_copy(update={field: value})
                self.assertNotEqual(changed.digest, manifest.digest)
                with self.assertRaises(ValueError):
                    execution.assert_matches(changed)

    def test_manifest_digest_is_canonical_and_not_python_repr(self) -> None:
        from m_agent.testing import AcceptanceCheck, AcceptanceManifest

        first = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="source",
            artifact_digest="artifact",
            fixture_digest="fixture",
            environment={"os": "linux", "python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                AcceptanceCheck(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
            ),
        )
        second = first.model_copy(
            update={"environment": {"python": "3.11", "os": "linux"}}
        )
        expected = hashlib.sha256(first.canonical_bytes()).hexdigest()
        self.assertEqual(first.digest, "sha256:" + expected)
        self.assertEqual(first.digest, second.digest)

    def test_bundle_verification_detects_tampering_and_sensitive_views(self) -> None:
        from m_agent.testing import (
            AcceptanceCheck,
            AcceptanceCheckResult,
            AcceptanceManifest,
            BundleIntegrityError,
            EvidenceLevel,
            PackExecution,
            ScenarioEvidenceBundle,
        )

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="source",
            artifact_digest="artifact",
            fixture_digest="fixture",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                AcceptanceCheck(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
            ),
        )
        check = self._check_result("core.lifecycle", evidence_level=EvidenceLevel.CONTRACT)
        with self.assertRaises(ValueError):
            ScenarioEvidenceBundle.create(
                manifest=manifest,
                execution=PackExecution.create(manifest, execution_id="created"),
                scenario="core-lifecycle",
                checks=(check,),
                evidence_view={"run_succeeded": True, "step_count": 1},
                independent_evidence={"fixture_digest": "sha256:" + "f" * 64},
            )
        execution = PackExecution.create(manifest, execution_id="exec-1").complete(
            manifest, (check,)
        )
        bundle = ScenarioEvidenceBundle.create(
            manifest=manifest,
            execution=execution,
            scenario="core-lifecycle",
            checks=(check,),
            evidence_view={"run_succeeded": True, "step_count": 1},
            independent_evidence={"fixture_digest": "sha256:" + "f" * 64},
        )
        bundle.verify(manifest, execution)
        with self.assertRaises(TypeError):
            bundle.evidence_view["run_succeeded"] = False
        tampered = bundle.model_copy(
            update={"evidence_view": {"run_succeeded": False, "step_count": 1}}
        )
        with self.assertRaises(BundleIntegrityError):
            tampered.verify(manifest, execution)
        schema_tampered = bundle.model_copy(update={"schema_version": "3"})
        with self.assertRaises(BundleIntegrityError):
            schema_tampered.verify(manifest, execution)
        redaction_tampered = bundle.model_copy(
            update={"evidence_view": {"apiKey": "must fail closed"}}
        )
        with self.assertRaises(BundleIntegrityError):
            redaction_tampered.verify(manifest, execution)
        with self.assertRaises(ValueError):
            ScenarioEvidenceBundle.create(
                manifest=manifest,
                execution=execution,
                scenario="core-lifecycle",
                checks=(),
                evidence_view={"raw_prompt": "must not persist"},
                independent_evidence={},
            )

    def test_bundle_rejects_unstructured_evidence_and_check_text(self) -> None:
        """Public Bundles retain only structural evidence and digest references."""
        from pydantic import ValidationError

        from m_agent.testing import (
            AcceptanceCheck,
            AcceptanceCheckResult,
            AcceptanceManifest,
            EvidenceLevel,
            PackExecution,
            ScenarioEvidenceBundle,
        )

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="source",
            artifact_digest="artifact",
            fixture_digest="fixture",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                AcceptanceCheck(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
            ),
        )
        clean_check = self._check_result(
            "core.lifecycle", evidence_level=EvidenceLevel.CONTRACT
        )
        execution = PackExecution.create(manifest, execution_id="exec-1").complete(
            manifest, (clean_check,)
        )
        with self.assertRaises(ValidationError):
            AcceptanceCheckResult(
                check_id="core.lifecycle",
                status="PASS",
                evidence_level=EvidenceLevel.CONTRACT,
                reason_code="check_failed",
                evidence_digest="sha256:" + "0" * 64,
                detail="Authorization: Bearer secret",
            )
        for evidence_view, independent_evidence in (
            ({"input": "raw prompt"}, {}),
            ({}, {"header": "Bearer secret"}),
            ({"Authorization: Bearer secret": True}, {}),
        ):
            with self.subTest(evidence_view=evidence_view):
                with self.assertRaises(ValueError):
                    ScenarioEvidenceBundle.create(
                        manifest=manifest,
                        execution=execution,
                        scenario="core-lifecycle",
                        checks=(clean_check,),
                        evidence_view=evidence_view,
                        independent_evidence=independent_evidence,
                    )

    def test_check_results_require_reason_codes_and_evidence_references(self) -> None:
        from pydantic import ValidationError

        from m_agent.testing import AcceptanceCheckResult, EvidenceLevel

        with self.assertRaises(ValidationError):
            AcceptanceCheckResult(
                check_id="core.lifecycle",
                status="PASS",
                evidence_level=EvidenceLevel.CONTRACT,
            )
        with self.assertRaises(ValidationError):
            AcceptanceCheckResult(
                check_id="core.lifecycle",
                status="PASS",
                evidence_level=EvidenceLevel.CONTRACT,
                reason_code="not stable",
                evidence_digest="not-a-digest",
            )

    def test_manifest_rejects_missing_or_nonrequired_frozen_declarations(self) -> None:
        from pydantic import ValidationError

        from m_agent.testing import AcceptanceCheck, AcceptanceManifest

        common = {
            "pack_version": "0.3.0",
            "profile": "0.3",
            "source_commit": "source",
            "artifact_digest": "artifact",
            "fixture_digest": "fixture",
            "environment": {"python": "3.11"},
        }
        with self.assertRaises(ValidationError):
            AcceptanceManifest(**common)
        with self.assertRaises(ValidationError):
            AcceptanceManifest(
                **common,
                scenarios=("core-lifecycle",),
                required_checks=(
                    AcceptanceCheck(
                        check_id="wrong-scenario",
                        scenario="other",
                        public_seam="m_agent.runtime.Runner",
                    ),
                ),
            )
        with self.assertRaises(ValidationError):
            AcceptanceManifest(
                **common,
                scenarios=("core-lifecycle",),
                required_checks=(
                    AcceptanceCheck(
                        check_id="not-required",
                        scenario="core-lifecycle",
                        public_seam="m_agent.runtime.Runner",
                        required=False,
                    ),
                ),
            )

    def test_manifest_requires_at_least_one_frozen_required_check(self) -> None:
        """A Pack without declared required coverage cannot become PASSED."""
        from pydantic import ValidationError

        from m_agent.testing import AcceptanceManifest

        with self.assertRaises(ValidationError):
            AcceptanceManifest(
                pack_version="0.3.0",
                profile="0.3",
                source_commit="source",
                artifact_digest="artifact",
                fixture_digest="fixture",
                environment={"python": "3.11"},
                scenarios=("core-lifecycle",),
            )
    def test_all_layers_are_public_and_runtime_reverse_dependency_fails(self) -> None:
        from m_agent import adapters, companion, runtime, testing
        from m_agent.testing import find_runtime_dependency_violations

        self.assertIsNotNone(runtime.Runner)
        self.assertIsNotNone(adapters.DeterministicModelAdapter)
        self.assertFalse(hasattr(runtime, "DeterministicModelAdapter"))
        self.assertFalse(hasattr(runtime, "InMemoryRunStore"))
        self.assertEqual(companion.__all__, [])
        self.assertTrue(hasattr(testing, "AcceptanceManifest"))
        self.assertEqual(find_runtime_dependency_violations(), ())

        with tempfile.TemporaryDirectory() as temporary_directory:
            source_root = Path(temporary_directory)
            package_directory = source_root / "m_agent"
            package_directory.mkdir()
            (package_directory / "_runner.py").write_text(
                "from .provider import ChatCompletionsModelAdapter\n"
            )
            violations = find_runtime_dependency_violations(source_root)

        self.assertEqual(len(violations), 1)
        self.assertIn("m_agent.provider", violations[0])

        with tempfile.TemporaryDirectory() as temporary_directory:
            source_root = Path(temporary_directory)
            runtime_directory = source_root / "m_agent" / "runtime"
            runtime_directory.mkdir(parents=True)
            (runtime_directory / "__init__.py").write_text(
                "from ..adapters import DeterministicModelAdapter\n"
            )
            violations = find_runtime_dependency_violations(source_root)

        self.assertEqual(len(violations), 1)
        self.assertIn("m_agent.adapters", violations[0])

    def test_cli_refuses_editable_development_subjects(self) -> None:
        """Release evidence can only be produced by a clean installed wheel."""
        from m_agent.testing import AcceptanceCheck, AcceptanceManifest

        source_commit, artifact_digest, environment = self._installed_manifest_identity()
        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit=source_commit,
            artifact_digest=artifact_digest,
            fixture_digest="fixture",
            environment=environment,
            scenarios=("core-lifecycle",),
            required_checks=(
                AcceptanceCheck(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
                AcceptanceCheck(
                    check_id="core.lifecycle.unknown-definition",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.DefinitionRegistry",
                ),
                AcceptanceCheck(
                    check_id="core.lifecycle.bundle-tamper",
                    scenario="core-lifecycle",
                    public_seam="m_agent.testing.ScenarioEvidenceBundle",
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(manifest.model_dump_json())
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "m_agent.testing",
                    "run",
                    "--manifest",
                    str(manifest_path),
                    "--wheel",
                    str(root / "candidate.whl"),
                    "--output-dir",
                    str(root / "bundles"),
                ],
                text=True,
                capture_output=True,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("clean wheel", completed.stderr)

    def test_required_results_have_fail_closed_pack_status_and_exit_codes(self) -> None:
        from m_agent.testing import (
            AcceptanceCheck,
            AcceptanceCheckResult,
            AcceptanceCheckStatus,
            AcceptanceManifest,
            EvidenceLevel,
            PackExecution,
            PackExecutionStatus,
        )

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="source",
            artifact_digest="artifact",
            fixture_digest="fixture",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                AcceptanceCheck(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
            ),
        )
        execution = PackExecution.create(manifest, execution_id="exec-1")
        for check_status, expected_status, expected_exit in (
            (AcceptanceCheckStatus.PASS, PackExecutionStatus.PASSED, 0),
            (AcceptanceCheckStatus.FAIL, PackExecutionStatus.FAILED, 1),
            (AcceptanceCheckStatus.ERROR, PackExecutionStatus.ERROR, 3),
            (AcceptanceCheckStatus.NOT_RUN, PackExecutionStatus.INCOMPLETE, 4),
            (AcceptanceCheckStatus.INCONCLUSIVE, PackExecutionStatus.INCOMPLETE, 4),
        ):
            with self.subTest(check_status=check_status):
                completed = execution.complete(
                    manifest,
                    (
                        AcceptanceCheckResult(
                            check_id="core.lifecycle",
                            status=check_status,
                            evidence_level=EvidenceLevel.CONTRACT,
                            reason_code="test_result",
                            evidence_digest="sha256:" + "0" * 64,
                        ),
                    ),
                )
                self.assertIs(completed.status, expected_status)
                self.assertEqual(completed.exit_code, expected_exit)

    def test_pack_marks_mismatched_required_evidence_level_as_harness_error(self) -> None:
        """A CONTRACT result cannot satisfy a required HOST declaration."""
        from m_agent.testing import (
            AcceptanceCheck,
            AcceptanceCheckResult,
            AcceptanceManifest,
            EvidenceLevel,
            PackExecution,
            PackExecutionStatus,
        )

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="source",
            artifact_digest="artifact",
            fixture_digest="fixture",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                AcceptanceCheck(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                    evidence_level=EvidenceLevel.HOST,
                ),
            ),
        )

        completed = PackExecution.create(manifest, execution_id="exec-1").complete(
            manifest,
            (
                AcceptanceCheckResult(
                    check_id="core.lifecycle",
                    status="PASS",
                    evidence_level=EvidenceLevel.CONTRACT,
                    reason_code="test_result",
                    evidence_digest="sha256:" + "0" * 64,
                ),
            ),
        )

        self.assertIs(completed.status, PackExecutionStatus.ERROR)
        self.assertEqual(completed.exit_code, 3)

    def test_required_nonrelease_evidence_cannot_report_a_release_pass(self) -> None:
        from m_agent.testing import (
            AcceptanceCheck,
            AcceptanceManifest,
            EvidenceLevel,
            PackExecution,
            PackExecutionStatus,
        )

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="source",
            artifact_digest="artifact",
            fixture_digest="fixture",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                AcceptanceCheck(
                    check_id="provider.contract",
                    scenario="core-lifecycle",
                    public_seam="m_agent.adapters.provider",
                    evidence_level=EvidenceLevel.PROVIDER,
                ),
            ),
        )
        result = self._check_result(
            "provider.contract", evidence_level=EvidenceLevel.PROVIDER
        )
        completed = PackExecution.create(manifest, execution_id="exec-1").complete(
            manifest, (result,)
        )

        self.assertIs(completed.status, PackExecutionStatus.INCOMPLETE)
        self.assertEqual(completed.exit_code, 4)

if __name__ == "__main__":
    unittest.main()
