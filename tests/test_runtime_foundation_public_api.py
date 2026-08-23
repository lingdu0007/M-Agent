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
    def test_root_facade_does_not_flatten_typed_model_contracts(self) -> None:
        """Advanced Model Contracts remain available only from runtime."""
        import m_agent
        import m_agent.runtime

        typed_contracts = (
            "ERROR_MODEL_EXECUTION_BUDGET_EXCEEDED",
            "ModelBinding",
            "ModelBindingSet",
            "ModelCapabilityCombination",
            "ModelContract",
            "ModelContractViolationError",
            "ModelExecutionBudget",
            "ModelLimits",
            "ModelPurpose",
            "ModelRequirementMatch",
            "ModelRequirementReason",
            "ModelRequirements",
            "ModelUsageGuarantees",
            "RevisionStability",
            "StreamingMode",
            "StructuredOutputMode",
            "ToolCallingMode",
            "UsageFieldGuarantee",
            "UsageProvenance",
            "UsageReportingMode",
        )

        for name in typed_contracts:
            with self.subTest(name=name):
                self.assertFalse(hasattr(m_agent, name))
                self.assertTrue(hasattr(m_agent.runtime, name))

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

    @staticmethod
    def _acceptance_check(**values: object) -> object:
        """Create complete test coverage through the public schema."""
        from m_agent.testing import AcceptanceCheck

        coverage = {
            "owner": "test-owner",
            "positive_check": "public_positive_check",
            "negative_check": "public_negative_check",
            "authoritative_evidence": "public_evidence",
            "independent_evidence": "independent_evidence",
            "milestone": "test-milestone",
            "non_claim": "not_a_release_claim",
        }
        return AcceptanceCheck(**coverage, **values)

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
                definition_type.for_adapter(
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
            sdist_digest="sha256:" + "c" * 64,
            fixture_digest="sha256:" + "b" * 64,
            environment={"python": "3.11", "os": "linux"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
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
            ("sdist_digest", "different"),
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

    def test_foundation_manifest_freezes_the_public_cli_contract_outside_bundle_results(
        self,
    ) -> None:
        """CLI acceptance is frozen without asking a Bundle to attest itself."""
        from m_agent.testing import PackExecution, core_lifecycle_manifest

        manifest = core_lifecycle_manifest(
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:" + "a" * 64,
            sdist_digest="sha256:" + "b" * 64,
            fixture_digest="sha256:" + "c" * 64,
            environment={"os": "linux"},
        )
        execution = PackExecution.create(manifest, execution_id="cli-contract")

        self.assertEqual(
            manifest.required_cli_commands, ("run", "inspect", "verify", "render")
        )
        self.assertFalse(
            any("cli" in check.check_id for check in manifest.required_checks)
        )
        changed = manifest.model_copy(update={"required_cli_commands": ("run",)})
        self.assertNotEqual(changed.digest, manifest.digest)
        with self.assertRaises(ValueError):
            execution.assert_matches(changed)

    def test_foundation_manifest_requires_a_source_distribution_and_supported_host(self) -> None:
        """The public Manifest cannot claim a HOST result without both artifacts."""
        from pydantic import ValidationError

        from m_agent.testing import AcceptanceCheck, AcceptanceManifest, EvidenceLevel

        required_check = self._acceptance_check(
            check_id="core.lifecycle.host-wheel",
            scenario="core-lifecycle",
            public_seam="python -I -m m_agent.testing",
            evidence_level=EvidenceLevel.HOST,
        )
        common = {
            "pack_version": "foundation-v1",
            "profile": "core-lifecycle-foundation",
            "source_commit": "b70919487a5aed78d9780efd24219ec77b670d92",
            "artifact_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "fixture_digest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "scenarios": ("core-lifecycle",),
            "required_checks": (required_check,),
        }
        with self.assertRaises(ValidationError):
            AcceptanceManifest(environment={"os": "linux"}, **common)
        with self.assertRaises(ValidationError):
            AcceptanceManifest(
                sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", environment={"os": "windows"}, **common
            )

    def test_manifest_rejects_a_required_check_without_complete_coverage(self) -> None:
        """Required Pack coverage cannot omit its owner, evidence, or non-claim."""
        from pydantic import ValidationError

        from m_agent.testing import AcceptanceCheck, AcceptanceManifest

        with self.assertRaises(ValidationError):
            AcceptanceManifest(
                pack_version="foundation-v1",
                profile="core-lifecycle-foundation",
                source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
                artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                environment={"os": "linux"},
                scenarios=("core-lifecycle",),
                required_checks=(
                    AcceptanceCheck(
                        check_id="core.lifecycle",
                        scenario="core-lifecycle",
                        public_seam="m_agent.runtime.Runner",
                    ),
                ),
            )

    def test_manifest_and_bundle_fail_closed_without_declared_identity_evidence(self) -> None:
        """Public Pack schemas reject empty identity and unattested PASS evidence."""
        from pydantic import ValidationError

        from m_agent.testing import (
            AcceptanceManifest,
            EvidenceLevel,
            PackExecution,
            ScenarioEvidenceBundle,
        )

        with self.assertRaises(ValidationError):
            self._acceptance_check(
                check_id="core.lifecycle",
                scenario="core-lifecycle",
                public_seam="",
            )

        required_check = self._acceptance_check(
            check_id="core.lifecycle",
            scenario="core-lifecycle",
            public_seam="m_agent.runtime.Runner",
        )
        common = {
            "pack_version": "foundation-v1",
            "profile": "core-lifecycle-foundation",
            "source_commit": "b70919487a5aed78d9780efd24219ec77b670d92",
            "artifact_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sdist_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "fixture_digest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "environment": {"python": "3.11"},
            "scenarios": ("core-lifecycle",),
            "required_checks": (required_check,),
        }
        for identity in (
            "source_commit",
            "artifact_digest",
            "sdist_digest",
            "fixture_digest",
        ):
            with self.subTest(identity=identity):
                values = dict(common)
                values[identity] = ""
                with self.assertRaises(ValidationError):
                    AcceptanceManifest(**values)
        with self.assertRaises(ValidationError):
            AcceptanceManifest(**{**common, "environment": {}})

        manifest = AcceptanceManifest(**common)
        result = self._check_result(
            "core.lifecycle", evidence_level=EvidenceLevel.CONTRACT
        )
        execution = PackExecution.create(manifest, execution_id="exec-1").complete(
            manifest, (result,)
        )
        with self.assertRaises(ValueError):
            ScenarioEvidenceBundle.create(
                manifest=manifest,
                execution=execution,
                execution_checks=(result,),
                scenario="core-lifecycle",
                checks=(result,),
                evidence_view={"observed": True},
                independent_evidence={"observed": True},
            )

    def test_core_lifecycle_foundation_profile_freezes_coverage_declarations(self) -> None:
        """The only Ticket 07 profile is not a substitute for foundation-release."""
        from m_agent.testing import core_lifecycle_manifest

        manifest = core_lifecycle_manifest(
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            environment={"os": "linux"},
        )

        self.assertEqual(manifest.profile, "core-lifecycle-foundation")
        self.assertEqual(manifest.pack_version, "foundation-v1")
        self.assertEqual(manifest.scenarios, ("core-lifecycle",))
        self.assertEqual(len(manifest.required_checks), 9)
        required_ids = {check.check_id for check in manifest.required_checks}
        self.assertIn("core.lifecycle.telemetry", required_ids)
        self.assertIn("core.lifecycle.telemetry-host", required_ids)
        for check in manifest.required_checks:
            with self.subTest(check_id=check.check_id):
                self.assertTrue(check.owner)
                self.assertTrue(check.public_seam)
                self.assertTrue(check.positive_check)
                self.assertTrue(check.negative_check)
                self.assertTrue(check.authoritative_evidence)
                self.assertTrue(check.independent_evidence)
                self.assertTrue(check.milestone)
                self.assertTrue(check.non_claim)

    def test_manifest_digest_is_canonical_and_not_python_repr(self) -> None:
        from m_agent.testing import AcceptanceCheck, AcceptanceManifest

        first = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            environment={"os": "linux", "python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
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

    def test_manifest_environment_identity_is_immutable(self) -> None:
        """A frozen Manifest cannot drift through its nested environment mapping."""
        from m_agent.testing import AcceptanceCheck, AcceptanceManifest

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
            ),
        )

        digest = manifest.digest
        with self.assertRaises(TypeError):
            manifest.environment["python"] = "3.12"  # type: ignore[index]
        self.assertEqual(manifest.digest, digest)

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
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
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
                execution_checks=(check,),
                scenario="core-lifecycle",
                checks=(check,),
                evidence_view={"run_succeeded": True, "step_count": 1},
                independent_evidence={"fixture_digest": "sha256:" + "f" * 64},
            )
        execution = PackExecution.create(manifest, execution_id="exec-1").complete(
            manifest, (check,)
        )
        with self.assertRaises(ValueError):
            ScenarioEvidenceBundle.create(
                manifest=manifest,
                execution=execution,
                execution_checks=(check,),
                scenario="core-lifecycle",
                checks=(check,),
                evidence_view={"run_succeeded": True, "step_count": 1},
                independent_evidence={},
            )
        bundle = ScenarioEvidenceBundle.create(
            manifest=manifest,
            execution=execution,
            execution_checks=(check,),
            scenario="core-lifecycle",
            checks=(check,),
            evidence_view={
                "public_evidence": check.evidence_digest,
                "run_succeeded": True,
                "step_count": 1,
            },
            independent_evidence={
                "independent_evidence": "sha256:" + "f" * 64,
                "fixture_digest": "sha256:" + "f" * 64,
            },
        )
        self.assertEqual(bundle.manifest, manifest)
        bundle.verify()
        bundle.verify(manifest, execution)
        with self.assertRaises(TypeError):
            bundle.evidence_view["run_succeeded"] = False
        tampered = bundle.model_copy(
            update={"evidence_view": {"run_succeeded": False, "step_count": 1}}
        )
        with self.assertRaises(BundleIntegrityError):
            tampered.verify(manifest, execution)
        undeclared = bundle.model_dump(mode="json")
        undeclared["scenario"] = "undeclared-scenario"
        undeclared["checks"] = []
        digest_payload = {key: value for key, value in undeclared.items() if key != "content_digest"}
        undeclared["content_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(
                digest_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaises(BundleIntegrityError):
            ScenarioEvidenceBundle.model_validate(undeclared).verify(manifest)
        schema_tampered = bundle.model_copy(update={"schema_version": "5"})
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
                execution_checks=(check,),
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
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
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
                        execution_checks=(clean_check,),
                        scenario="core-lifecycle",
                        checks=(clean_check,),
                        evidence_view=evidence_view,
                        independent_evidence=independent_evidence,
                    )

    def test_bundle_rejects_scenario_checks_that_contradict_execution(self) -> None:
        """A Bundle cannot render a PASS for an execution check that failed."""
        from m_agent.testing import (
            AcceptanceManifest,
            BundleIntegrityError,
            EvidenceLevel,
            PackExecution,
            ScenarioEvidenceBundle,
        )

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
            ),
        )
        failed = self._check_result(
            "core.lifecycle", status="FAIL", evidence_level=EvidenceLevel.CONTRACT
        )
        passed = self._check_result(
            "core.lifecycle", status="PASS", evidence_level=EvidenceLevel.CONTRACT
        )
        execution = PackExecution.create(manifest, execution_id="exec-1").complete(
            manifest, (failed,)
        )

        with self.assertRaises(ValueError):
            ScenarioEvidenceBundle.create(
                manifest=manifest,
                execution=execution,
                execution_checks=(failed,),
                scenario="core-lifecycle",
                checks=(passed,),
                evidence_view={"run_succeeded": False},
                independent_evidence={"fixture_digest": "sha256:" + "f" * 64},
            )

        consistent = ScenarioEvidenceBundle.create(
            manifest=manifest,
            execution=execution,
            execution_checks=(failed,),
            scenario="core-lifecycle",
            checks=(failed,),
            evidence_view={"run_succeeded": False},
            independent_evidence={"fixture_digest": "sha256:" + "f" * 64},
        ).model_dump(mode="json")
        consistent["checks"][0]["status"] = "PASS"
        payload = {key: value for key, value in consistent.items() if key != "content_digest"}
        consistent["content_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        with self.assertRaises(BundleIntegrityError):
            ScenarioEvidenceBundle.model_validate(consistent).verify()

    def test_multi_scenario_pack_creates_a_bundle_per_declared_scenario(self) -> None:
        """A terminal Pack can retain its full result set in each Scenario Bundle."""
        from m_agent.testing import (
            AcceptanceCheck,
            AcceptanceManifest,
            EvidenceLevel,
            PackExecution,
            ScenarioEvidenceBundle,
        )

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            environment={"python": "3.11"},
            scenarios=("first", "second"),
            required_checks=(
                self._acceptance_check(
                    check_id="first.check",
                    scenario="first",
                    public_seam="m_agent.runtime.Runner",
                ),
                self._acceptance_check(
                    check_id="second.check",
                    scenario="second",
                    public_seam="m_agent.runtime.Runner",
                ),
            ),
        )
        first = self._check_result("first.check", evidence_level=EvidenceLevel.CONTRACT)
        second = self._check_result(
            "second.check", evidence_level=EvidenceLevel.CONTRACT
        )
        execution = PackExecution.create(manifest, execution_id="pack-1").complete(
            manifest, (first, second)
        )

        bundle = ScenarioEvidenceBundle.create(
            manifest=manifest,
            execution=execution,
            execution_checks=(first, second),
            scenario="first",
            checks=(first,),
            evidence_view={
                "public_evidence": first.evidence_digest,
                "first_succeeded": True,
            },
            independent_evidence={
                "independent_evidence": "sha256:" + "f" * 64,
                "host_observation_digest": "sha256:" + "f" * 64,
            },
        )

        bundle.verify(manifest)
        self.assertEqual(bundle.checks, (first,))
        self.assertEqual(bundle.execution_checks, (first, second))

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

    def test_unsupported_manifest_and_bundle_schemas_fail_closed(self) -> None:
        from pydantic import ValidationError

        from m_agent.testing import (
            AcceptanceCheck,
            AcceptanceManifest,
            EvidenceLevel,
            PackExecution,
            ScenarioEvidenceBundle,
        )

        manifest_data = {
            "pack_version": "0.3.0",
            "profile": "0.3",
            "source_commit": "b70919487a5aed78d9780efd24219ec77b670d92",
            "artifact_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sdist_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "fixture_digest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "environment": {"python": "3.11"},
            "scenarios": ("core-lifecycle",),
            "required_checks": (
                self._acceptance_check(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
            ),
        }
        with self.assertRaises(ValidationError):
            AcceptanceManifest(schema_version="unsupported-manifest-v999", **manifest_data)

        manifest = AcceptanceManifest(**manifest_data)
        check = self._check_result("core.lifecycle", evidence_level=EvidenceLevel.CONTRACT)
        execution = PackExecution.create(manifest, execution_id="exec-1").complete(
            manifest, (check,)
        )
        bundle = ScenarioEvidenceBundle.create(
            manifest=manifest,
            execution=execution,
            execution_checks=(check,),
            scenario="core-lifecycle",
            checks=(check,),
            evidence_view={
                "public_evidence": check.evidence_digest,
                "run_succeeded": True,
            },
            independent_evidence={
                "independent_evidence": "sha256:" + "f" * 64,
                "host_observation_digest": "sha256:" + "f" * 64,
            },
        )
        serialized = bundle.model_dump(mode="json")
        serialized["schema_version"] = "unsupported-bundle-v999"
        with self.assertRaises(ValidationError):
            ScenarioEvidenceBundle.model_validate(serialized)

    def test_manifest_rejects_missing_or_nonrequired_frozen_declarations(self) -> None:
        from pydantic import ValidationError

        from m_agent.testing import AcceptanceCheck, AcceptanceManifest

        common = {
            "pack_version": "0.3.0",
            "profile": "0.3",
            "source_commit": "b70919487a5aed78d9780efd24219ec77b670d92",
            "artifact_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sdist_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "fixture_digest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "environment": {"python": "3.11"},
        }
        with self.assertRaises(ValidationError):
            AcceptanceManifest(**common)
        with self.assertRaises(ValidationError):
            AcceptanceManifest(
                **common,
                scenarios=("core-lifecycle",),
                required_checks=(
                    self._acceptance_check(
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
                    self._acceptance_check(
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
                source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
                artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                environment={"python": "3.11"},
                scenarios=("core-lifecycle",),
            )

    def test_manifest_rejects_counterfeit_subject_identity_syntax(self) -> None:
        """A public Pack subject is a full commit plus exact SHA-256 artifacts."""
        from pydantic import ValidationError

        from m_agent.testing import (
            AcceptanceManifest,
            PackExecution,
            core_lifecycle_manifest,
        )

        with self.assertRaises(ValidationError):
            AcceptanceManifest(
                pack_version="foundation-v1",
                profile="core-lifecycle-foundation",
                source_commit="not-a-commit",
                artifact_digest="not-a-digest",
                sdist_digest="still-not-a-digest",
                fixture_digest="counterfeit-fixture",
                environment={"python": "3.11"},
                scenarios=("core-lifecycle",),
                required_checks=(
                    self._acceptance_check(
                        check_id="core.lifecycle",
                        scenario="core-lifecycle",
                        public_seam="m_agent.runtime.Runner",
                    ),
                ),
            )
        valid = core_lifecycle_manifest(
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:" + "a" * 64,
            sdist_digest="sha256:" + "b" * 64,
            fixture_digest="sha256:" + "c" * 64,
            environment={"os": "linux"},
        )
        with self.assertRaises(ValidationError):
            PackExecution.create(
                valid.model_copy(update={"source_commit": "counterfeit"}),
                execution_id="counterfeit",
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
        self.assertEqual(runtime.RunStore.__module__, "m_agent._store")
        self.assertTrue(adapters.InMemoryRunStore.__module__.startswith("m_agent.adapters"))
        self.assertTrue(adapters.SQLiteRunStore.__module__.startswith("m_agent.adapters"))
        self.assertNotEqual(runtime.RunStore.__module__, adapters.InMemoryRunStore.__module__)

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

        with tempfile.TemporaryDirectory() as temporary_directory:
            source_root = Path(temporary_directory)
            package_directory = source_root / "m_agent"
            package_directory.mkdir()
            (package_directory / "__init__.py").write_text(
                "from .adapters import InMemoryRunStore\n"
            )
            violations = find_runtime_dependency_violations(source_root)

        self.assertEqual(len(violations), 1)
        self.assertIn("m_agent.adapters", violations[0])

        with tempfile.TemporaryDirectory() as temporary_directory:
            source_root = Path(temporary_directory)
            package_directory = source_root / "m_agent"
            package_directory.mkdir()
            (package_directory / "_store.py").write_text(
                "class InMemoryRunStore:\n    pass\n"
            )
            violations = find_runtime_dependency_violations(source_root)

        self.assertEqual(len(violations), 1)
        self.assertIn("InMemoryRunStore", violations[0])

        for source, expected_module in (
            ("from .. import adapters\n", "m_agent.adapters"),
            ("from m_agent import adapters\n", "m_agent.adapters"),
            ("__import__('m_agent.adapters')\n", "m_agent.adapters"),
            (
                "import importlib\nimportlib.import_module('m_agent.testing')\n",
                "m_agent.testing",
            ),
            (
                "from ._sqlite_store import SQLiteRunStore\n",
                "m_agent._sqlite_store.SQLiteRunStore",
            ),
            (
                "from ._store import InMemoryRunStore\n",
                "m_agent._store.InMemoryRunStore",
            ),
        ):
            with self.subTest(source=source):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    source_root = Path(temporary_directory)
                    if expected_module.startswith("m_agent._"):
                        package_directory = source_root / "m_agent"
                        package_directory.mkdir()
                        target = package_directory / "_runner.py"
                    else:
                        runtime_directory = source_root / "m_agent" / "runtime"
                        runtime_directory.mkdir(parents=True)
                        target = runtime_directory / "__init__.py"
                    target.write_text(source)
                    violations = find_runtime_dependency_violations(source_root)

                self.assertEqual(len(violations), 1)
                self.assertIn(expected_module, violations[0])

    def test_cli_refuses_editable_development_subjects(self) -> None:
        """Release evidence can only be produced by a clean installed wheel."""
        from m_agent.testing import AcceptanceCheck, AcceptanceManifest

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:" + "a" * 64,
            sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
                self._acceptance_check(
                    check_id="core.lifecycle.unknown-definition",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.DefinitionRegistry",
                ),
                self._acceptance_check(
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
                    "--sdist",
                    str(root / "candidate.tar.gz"),
                    "--output-dir",
                    str(root / "bundles"),
                ],
                text=True,
                capture_output=True,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("clean wheel", completed.stderr)

    def test_required_results_have_fail_closed_pack_status_and_exit_codes(self) -> None:
        """A Pack keeps harness ERROR/3 distinct from subject FAILED/1.

        A later Release or Milestone aggregate may project either result to an
        aggregate failure, but it must not rewrite this public Pack result.
        """
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
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
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

    def test_pack_marks_an_undeclared_result_as_a_harness_error(self) -> None:
        """A result outside the frozen Manifest cannot be ignored as a pass."""
        from m_agent.testing import (
            AcceptanceManifest,
            EvidenceLevel,
            PackExecution,
            PackExecutionStatus,
        )

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:" + "a" * 64,
            sdist_digest="sha256:" + "b" * 64,
            fixture_digest="sha256:" + "c" * 64,
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
            ),
        )
        completed = PackExecution.create(manifest, execution_id="undeclared-result").complete(
            manifest,
            (
                self._check_result(
                    "core.lifecycle", evidence_level=EvidenceLevel.CONTRACT
                ),
                self._check_result(
                    "undeclared.check",
                    status="ERROR",
                    evidence_level=EvidenceLevel.CONTRACT,
                ),
            ),
        )

        self.assertIs(completed.status, PackExecutionStatus.ERROR)
        self.assertEqual(completed.exit_code, 3)

    def test_pack_execution_rejects_forged_identity_and_status_exit_combinations(self) -> None:
        """Persisted Pack facts fail closed unless identity and terminal semantics agree."""
        from pydantic import ValidationError

        from m_agent.testing import PackExecution, PackExecutionStatus

        digest = "sha256:" + "a" * 64
        invalid = (
            ("forged", PackExecutionStatus.PASSED, 0),
            (digest, PackExecutionStatus.CREATED, 0),
            (digest, PackExecutionStatus.RUNNING, 1),
            (digest, PackExecutionStatus.PASSED, None),
            (digest, PackExecutionStatus.FAILED, 3),
            (digest, PackExecutionStatus.INCOMPLETE, 1),
            (digest, PackExecutionStatus.ERROR, 1),
        )
        for manifest_digest, status, exit_code in invalid:
            with self.subTest(status=status, exit_code=exit_code):
                with self.assertRaises(ValidationError):
                    PackExecution(
                        execution_id="forged",
                        manifest_digest=manifest_digest,
                        status=status,
                        exit_code=exit_code,
                    )
        execution = PackExecution(
            execution_id="pack-1",
            manifest_digest=digest,
        )
        with self.assertRaises(ValidationError):
            execution.model_copy(
                update={"status": PackExecutionStatus.PASSED, "exit_code": 3}
            )

    def test_pack_and_bundle_reject_a_forged_check_status(self) -> None:
        """Only the five public result statuses may affect a terminal Pack."""
        from pydantic import ValidationError

        from m_agent.testing import (
            AcceptanceManifest,
            BundleIntegrityError,
            EvidenceLevel,
            PackExecution,
            ScenarioEvidenceBundle,
        )

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:" + "a" * 64,
            sdist_digest="sha256:" + "b" * 64,
            fixture_digest="sha256:" + "c" * 64,
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
            ),
        )
        valid = self._check_result(
            "core.lifecycle", evidence_level=EvidenceLevel.CONTRACT
        )
        forged = valid.model_copy(update={"status": "UNRECOGNIZED"})
        execution = PackExecution.create(manifest, execution_id="forged-status")

        with self.assertRaises(ValidationError):
            execution.complete(manifest, (forged,))

        completed = execution.complete(manifest, (valid,))
        with self.assertRaises(ValidationError):
            ScenarioEvidenceBundle.create(
                manifest=manifest,
                execution=completed,
                execution_checks=(forged,),
                scenario="core-lifecycle",
                checks=(forged,),
                evidence_view={"public_evidence": valid.evidence_digest},
                independent_evidence={
                    "independent_evidence": "sha256:" + "f" * 64,
                },
            )
        bundle = ScenarioEvidenceBundle.create(
            manifest=manifest,
            execution=completed,
            execution_checks=(valid,),
            scenario="core-lifecycle",
            checks=(valid,),
            evidence_view={"public_evidence": valid.evidence_digest},
            independent_evidence={"independent_evidence": "sha256:" + "f" * 64},
        )
        with self.assertRaises(BundleIntegrityError):
            bundle.model_copy(
                update={"execution_checks": (forged,), "checks": (forged,)}
            ).verify()

    def test_known_required_failure_precedes_a_missing_required_result(self) -> None:
        """A supplied required FAIL remains the Pack verdict when coverage is missing."""
        from m_agent.testing import (
            AcceptanceManifest,
            EvidenceLevel,
            PackExecution,
            PackExecutionStatus,
        )

        manifest = AcceptanceManifest(
            pack_version="0.3.0",
            profile="0.3",
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
                    check_id="core.lifecycle",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
                self._acceptance_check(
                    check_id="core.lifecycle.other",
                    scenario="core-lifecycle",
                    public_seam="m_agent.runtime.Runner",
                ),
            ),
        )
        completed = PackExecution.create(manifest, execution_id="exec-1").complete(
            manifest,
            (
                self._check_result(
                    "core.lifecycle", status="FAIL", evidence_level=EvidenceLevel.CONTRACT
                ),
            ),
        )

        self.assertIs(completed.status, PackExecutionStatus.FAILED)
        self.assertEqual(completed.exit_code, 1)

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
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
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
           source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
           artifact_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            sdist_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
           fixture_digest="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            environment={"python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(
                self._acceptance_check(
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
