"""Ticket 10 public adapter and telemetry contract seams.

These tests intentionally use only public Runtime, Adapter, Provider and
Testing imports.  They are the regression boundary for third-party adapters,
offline provider fixtures, local OpenTelemetry export and process isolation.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from m_agent.adapters import (
    InMemorySpanExporter,
    JsonlTelemetrySink,
    OpenTelemetrySpanScope,
    OpenTelemetryTelemetrySink,
    OpenTelemetryTraceContext,
)
from m_agent.runtime import (
    ModelAdapter,
    AgentDefinition,
    DefinitionRegistry,
    ModelCapabilities,
    ModelCapabilityError,
    ModelContract,
    ModelLimits,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
    ModelRequirements,
    OutputContract,
    RevisionStability,
    RunStatus,
    StreamingMode,
    StructuredOutputMode,
    TelemetryEvent,
    TelemetryEventType,
)
from m_agent.adapters.provider import (
    ChatCompletionsModelAdapter,
    ResponsesModelAdapter,
    chat_completion_fixture,
    chat_stream_fixture,
    malformed_response_fixture,
    provider_error_fixture,
    responses_fixture,
    responses_stream_fixture,
)
from m_agent.testing import (
    AcceptanceCheck,
    AcceptanceCheckResult,
    AcceptanceCheckStatus,
    AcceptanceManifest,
    EvidenceLevel,
    PackExecution,
    ScenarioEvidenceBundle,
    core_lifecycle_manifest,
    isolated_subprocess_environment,
    run_model_adapter_contract,
)


class ThirdPartyAdapter(ModelAdapter):
    """A third-party implementation: it imports no official adapter code."""

    capabilities = ModelCapabilities()

    @property
    def model_contract(self) -> ModelContract:
        return ModelContract(
            contract_id="third-party-test",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="third-party-test-model",
            capabilities=self.capabilities,
            limits=ModelLimits(context_window_tokens=32, max_output_tokens=8),
            input_sizer_id="third-party-sizer-v1",
            serialization_id="third-party-wire-v1",
            configuration_fingerprint="third-party-config-v1",
        )

    def definition_contract_fingerprint(self) -> str:
        return "third-party-config-v1"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(content="offline third-party response")


_FIXTURE_SCHEMA = {"type": "object", "properties": {}}


def _official_fixture_contract(
    adapter: ModelAdapter,
    *,
    capabilities: ModelCapabilities | None = None,
) -> ModelContract:
    """Declare offline fixture facts without guessing a provider deployment."""
    return ModelContract(
        contract_id=f"fixture-{type(adapter).__name__}",
        version="fixture-v1",
        revision_stability=RevisionStability.PINNED,
        model_identity=adapter.model,  # type: ignore[attr-defined]
        capabilities=adapter.capabilities if capabilities is None else capabilities,
        limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
        input_sizer_id="fixture-provider-sizer-v1",
        serialization_id="fixture-provider-wire-v1",
        configuration_fingerprint=adapter.definition_contract_fingerprint(),
    )


class AdapterContractKitTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_contract_kit_runs_a_third_party_adapter(self) -> None:
        report = await run_model_adapter_contract(
            ThirdPartyAdapter(),
            ModelRequest(input="hello", instructions="reply"),
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.contract_id, "third-party-test")
        self.assertTrue(report.contract_fingerprint)
        self.assertEqual(report.response_content, "offline third-party response")

    async def test_official_adapters_offer_instance_contracts_and_public_fixtures(
        self,
    ) -> None:
        fixtures = (
            (ChatCompletionsModelAdapter, chat_completion_fixture, "chat fixture"),
            (ResponsesModelAdapter, responses_fixture, "responses fixture"),
        )
        with patch.dict(os.environ, {"M_AGENT_OPENAI_API_KEY": "fixture-key"}):
            for adapter_type, fixture_factory, content in fixtures:
                with self.subTest(adapter=adapter_type.__name__):
                    fixture = fixture_factory(
                        content=content,
                        usage={"input_tokens": 2, "output_tokens": 1},
                    )
                    probe = adapter_type(
                        model="fixture-model",
                        base_url="https://offline.invalid/v1",
                        structured_output_schema=_FIXTURE_SCHEMA,
                    )
                    adapter = adapter_type(
                        model="fixture-model",
                        base_url="https://offline.invalid/v1",
                        transport=fixture.transport,
                        model_contract=_official_fixture_contract(probe),
                        structured_output_schema=_FIXTURE_SCHEMA,
                    )
                    try:
                        contract = adapter.model_contract
                        self.assertEqual(contract.model_identity, "fixture-model")
                        self.assertTrue(contract.fingerprint)
                        self.assertTrue(contract.configuration_fingerprint)
                        response = await adapter.generate(
                            ModelRequest(input="hello", instructions="reply")
                        )
                    finally:
                        await adapter.aclose()
                    self.assertEqual(response.content, content)
                    self.assertEqual(len(fixture.requests), 1)
                    self.assertEqual(fixture.requests[0].method, "POST")

    async def test_unknown_instance_contract_rejects_registration_without_network(
        self,
    ) -> None:
        adapter = ChatCompletionsModelAdapter(
            model="fixture-model", base_url="https://offline.invalid/v1"
        )
        try:
            registry = DefinitionRegistry()
            with self.assertRaisesRegex(ValueError, "instance ModelContract"):
                registry.register(
                    AgentDefinition.for_adapter(
                        definition_id="unknown-official-contract",
                        version="1",
                        instructions="offline registration only",
                        model_adapter=adapter,
                    )
                )
        finally:
            await adapter.aclose()
        self.assertEqual(adapter.requests, [])

    async def test_declared_instance_capability_rejects_before_fixture_dispatch(
        self,
    ) -> None:
        probe = ChatCompletionsModelAdapter(
            model="fixture-model",
            base_url="https://offline.invalid/v1",
        )
        adapter = ChatCompletionsModelAdapter(
            model="fixture-model",
            base_url="https://offline.invalid/v1",
            model_contract=_official_fixture_contract(
                probe,
                capabilities=ModelCapabilities(),
            ),
        )
        try:
            with self.assertRaises(ModelCapabilityError):
                DefinitionRegistry().register(
                    AgentDefinition.for_adapter(
                        definition_id="undeclared-streaming",
                        version="1",
                        instructions="Do not dispatch.",
                        model_requirements=ModelRequirements(
                            capabilities=ModelCapabilities(
                                streaming=StreamingMode.DELTA
                            )
                        ),
                        model_adapter=adapter,
                    )
                )
        finally:
            await adapter.aclose()
        self.assertEqual(adapter.requests, [])

    async def test_official_strict_schema_must_match_output_contract_at_registration(
        self,
    ) -> None:
        adapter_schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        matching_contract_schema = {
            "additionalProperties": False,
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
            "type": "object",
        }
        mismatched_contract_schema = {
            "type": "object",
            "properties": {"answer": {"type": "integer"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        for adapter_type in (ChatCompletionsModelAdapter, ResponsesModelAdapter):
            for label, contract_schema, accepts_registration in (
                ("matching", matching_contract_schema, True),
                ("mismatched", mismatched_contract_schema, False),
            ):
                with self.subTest(adapter=adapter_type.__name__, schema=label):
                    probe = adapter_type(
                        model="fixture-model",
                        base_url="https://offline.invalid/v1",
                        structured_output_schema=adapter_schema,
                    )
                    adapter = adapter_type(
                        model="fixture-model",
                        base_url="https://offline.invalid/v1",
                        model_contract=_official_fixture_contract(probe),
                        structured_output_schema=adapter_schema,
                    )
                    definition = AgentDefinition.for_adapter(
                        definition_id=(
                            f"{adapter_type.__name__}-{label}-output-contract"
                        ),
                        version="1",
                        instructions="Offline registration only.",
                        model_adapter=adapter,
                        output_contract=OutputContract(
                            contract_id="answer",
                            version="1",
                            schema=contract_schema,
                            structured_output=(
                                StructuredOutputMode.JSON_SCHEMA_STRICT
                            ),
                        ),
                    )
                    try:
                        registry = DefinitionRegistry()
                        if accepts_registration:
                            registry.register(definition)
                            self.assertTrue(
                                registry.is_registered(
                                    definition.definition_id, definition.version
                                )
                            )
                        else:
                            with self.assertRaisesRegex(
                                ModelCapabilityError, "Output Contract schema"
                            ):
                                registry.register(definition)
                    finally:
                        await probe.aclose()
                        await adapter.aclose()
                    self.assertEqual(adapter.requests, [])

    async def test_offline_fixture_request_log_excludes_credential_canary(self) -> None:
        credential_canary = "T10-CREDENTIAL-CANARY"
        fixture = chat_completion_fixture()
        probe = ChatCompletionsModelAdapter(
            model="fixture-model",
            base_url="https://offline.invalid/v1",
            structured_output_schema=_FIXTURE_SCHEMA,
        )
        adapter = ChatCompletionsModelAdapter(
            model="fixture-model",
            base_url="https://offline.invalid/v1",
            transport=fixture.transport,
            model_contract=_official_fixture_contract(probe),
            structured_output_schema=_FIXTURE_SCHEMA,
        )
        try:
            with patch.dict(os.environ, {"M_AGENT_OPENAI_API_KEY": credential_canary}):
                await adapter.generate(
                    ModelRequest(input="private input", instructions="private instructions")
                )
        finally:
            await adapter.aclose()
        self.assertEqual(fixture.requests[0].method, "POST")
        fixture_text = repr((fixture.requests, vars(fixture)))
        self.assertNotIn(credential_canary, fixture_text)
        self.assertNotIn("Authorization", fixture_text)

    async def test_offline_provider_error_fixture_never_needs_a_network(self) -> None:
        from m_agent.runtime import ModelFailure

        with patch.dict(os.environ, {"M_AGENT_OPENAI_API_KEY": "fixture-key"}):
            fixture = provider_error_fixture(429)
            adapter = ChatCompletionsModelAdapter(
                base_url="https://offline.invalid/v1", transport=fixture.transport
            )
            try:
                with self.assertRaises(ModelFailure) as caught:
                    await adapter.generate(ModelRequest(input="hello", instructions="reply"))
            finally:
                await adapter.aclose()
        self.assertEqual(caught.exception.code, "provider_unavailable")
        self.assertEqual(len(fixture.requests), 1)

    async def test_provider_errors_never_echo_endpoint_canaries(self) -> None:
        """Both direct and streamed official dispatches redact endpoint detail."""
        from m_agent.runtime import ModelFailure

        endpoint_canary = "T10-ENDPOINT-CANARY"
        request = ModelRequest(input="hello", instructions="reply")
        with patch.dict(os.environ, {"M_AGENT_OPENAI_API_KEY": "fixture-key"}):
            for adapter_type in (
                ChatCompletionsModelAdapter,
                ResponsesModelAdapter,
            ):
                for dispatch in ("generate", "stream"):
                    with self.subTest(adapter=adapter_type.__name__, dispatch=dispatch):
                        fixture = provider_error_fixture(429)
                        adapter = adapter_type(
                            base_url=(
                                f"https://{endpoint_canary}.invalid/v1?"
                                "credential=ignored"
                            ),
                            transport=fixture.transport,
                        )
                        try:
                            with self.assertRaises(ModelFailure) as caught:
                                if dispatch == "generate":
                                    await adapter.generate(request)
                                else:
                                    await anext(adapter.stream(request))
                        finally:
                            await adapter.aclose()
                        self.assertEqual(caught.exception.code, "provider_unavailable")
                        self.assertNotIn(endpoint_canary, str(caught.exception))
                        self.assertNotIn(endpoint_canary, caught.exception.message)

    async def test_transport_errors_do_not_chain_endpoint_canaries(self) -> None:
        """The public failure boundary also drops the original exception chain."""
        from m_agent.runtime import ModelFailure

        endpoint_canary = "T10-TRANSPORT-ENDPOINT-CANARY"
        request = ModelRequest(input="hello", instructions="reply")

        def offline_transport_error(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(endpoint_canary)

        with patch.dict(os.environ, {"M_AGENT_OPENAI_API_KEY": "fixture-key"}):
            for adapter_type in (
                ChatCompletionsModelAdapter,
                ResponsesModelAdapter,
            ):
                for dispatch in ("generate", "stream"):
                    with self.subTest(adapter=adapter_type.__name__, dispatch=dispatch):
                        adapter = adapter_type(
                            base_url=f"https://{endpoint_canary}.invalid/v1",
                            transport=httpx.MockTransport(offline_transport_error),
                        )
                        try:
                            with self.assertRaises(ModelFailure) as caught:
                                if dispatch == "generate":
                                    await adapter.generate(request)
                                else:
                                    await anext(adapter.stream(request))
                        finally:
                            await adapter.aclose()
                        rendered = "".join(
                            traceback.format_exception(caught.exception)
                        )
                        self.assertIsNone(caught.exception.__cause__)
                        self.assertNotIn(endpoint_canary, rendered)

    async def test_invalid_provider_bodies_do_not_chain_raw_content_canaries(self) -> None:
        """Malformed HTTP and SSE bodies never survive in failure tracebacks."""
        from m_agent.runtime import ModelFailure

        raw_canary = "T10-PROVIDER-RAW-CANARY"
        request = ModelRequest(input="hello", instructions="reply")
        with patch.dict(os.environ, {"M_AGENT_OPENAI_API_KEY": "fixture-key"}):
            for adapter_type in (
                ChatCompletionsModelAdapter,
                ResponsesModelAdapter,
            ):
                for dispatch, response in (
                    ("generate", httpx.Response(200, content=raw_canary)),
                    (
                        "stream",
                        httpx.Response(
                            200,
                            content=f"data: {raw_canary}\n\n",
                            headers={"content-type": "text/event-stream"},
                        ),
                    ),
                ):
                    with self.subTest(adapter=adapter_type.__name__, dispatch=dispatch):
                        adapter = adapter_type(
                            base_url="https://offline.invalid/v1",
                            transport=httpx.MockTransport(
                                lambda _request, response=response: response
                            ),
                        )
                        try:
                            with self.assertRaises(ModelFailure) as caught:
                                if dispatch == "generate":
                                    await adapter.generate(request)
                                else:
                                    await anext(adapter.stream(request))
                        finally:
                            await adapter.aclose()
                        rendered = "".join(
                            traceback.format_exception(caught.exception)
                        )
                        self.assertIsNone(caught.exception.__cause__)
                        self.assertNotIn(raw_canary, rendered)

    async def test_offline_stream_fixtures_cover_completion_and_cancellation(self) -> None:
        with patch.dict(os.environ, {"M_AGENT_OPENAI_API_KEY": "fixture-key"}):
            chat_fixture = chat_stream_fixture(
                {"choices": [{"delta": {"content": "hello"}}]},
                {"usage": {"prompt_tokens": 2, "completion_tokens": 1}},
            )
            chat = ChatCompletionsModelAdapter(
                base_url="https://offline.invalid/v1", transport=chat_fixture.transport
            )
            try:
                events = [event async for event in chat.stream(ModelRequest(input="hello", instructions="reply"))]
            finally:
                await chat.aclose()
            self.assertEqual(events[-1].content, "hello")

            response_fixture = responses_stream_fixture(
                {"type": "response.output_text.delta", "delta": "partial"},
                {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {"type": "output_text", "text": "complete"}
                                ],
                            }
                        ]
                    },
                },
            )
            responses = ResponsesModelAdapter(
                base_url="https://offline.invalid/v1", transport=response_fixture.transport
            )
            try:
                iterator = responses.stream(
                    ModelRequest(input="hello", instructions="reply")
                )
                first = await anext(iterator)
                await iterator.aclose()
            finally:
                await responses.aclose()
        self.assertEqual(first.content, "partial")
        self.assertEqual(len(response_fixture.requests), 1)

    async def test_offline_missing_field_fixture_is_a_contract_failure(self) -> None:
        from m_agent.runtime import ModelContractViolationError

        with patch.dict(os.environ, {"M_AGENT_OPENAI_API_KEY": "fixture-key"}):
            fixture = malformed_response_fixture()
            adapter = ResponsesModelAdapter(
                base_url="https://offline.invalid/v1", transport=fixture.transport
            )
            try:
                with self.assertRaises(ModelContractViolationError):
                    await adapter.generate(ModelRequest(input="hello", instructions="reply"))
            finally:
                await adapter.aclose()
        self.assertEqual(len(fixture.requests), 1)

    async def test_offline_fixture_preserves_field_level_usage_mapping(self) -> None:
        with patch.dict(os.environ, {"M_AGENT_OPENAI_API_KEY": "fixture-key"}):
            fixture = chat_completion_fixture(
                usage={
                    "prompt_tokens": 4,
                    "completion_tokens": 3,
                    "prompt_tokens_details": {"cached_tokens": 2},
                    "completion_tokens_details": {"reasoning_tokens": 1},
                }
            )
            adapter = ChatCompletionsModelAdapter(
                base_url="https://offline.invalid/v1", transport=fixture.transport
            )
            try:
                response = await adapter.generate(
                    ModelRequest(input="hello", instructions="reply")
                )
            finally:
                await adapter.aclose()
        assert response.usage is not None
        self.assertEqual(response.usage.input_tokens, 4)
        self.assertEqual(response.usage.output_tokens, 3)
        self.assertEqual(response.usage.cached_input_tokens, 2)
        self.assertEqual(response.usage.reasoning_tokens, 1)
        self.assertIn("prompt_tokens_details.cached_tokens", response.usage.normalization_source or "")


class JsonlLifecycleContractTests(unittest.TestCase):
    def test_close_is_idempotent_and_closed_sink_cannot_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            sink = JsonlTelemetrySink(path)
            sink.emit(
                TelemetryEvent(
                    event_type=TelemetryEventType.RUN_STATUS_CHANGED,
                    run_id="run-1",
                    run_status=RunStatus.CREATED,
                )
            )
            sink.flush()
            sink.close()
            sink.close()
            with self.assertRaisesRegex(RuntimeError, "closed"):
                sink.emit(
                    TelemetryEvent(
                        event_type=TelemetryEventType.RUN_STATUS_CHANGED,
                        run_id="run-1",
                        run_status=RunStatus.SUCCEEDED,
                    )
                )
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_context_manager_closes_after_an_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with self.assertRaisesRegex(RuntimeError, "probe failure"):
                with JsonlTelemetrySink(path) as sink:
                    sink.emit(
                        TelemetryEvent(
                            event_type=TelemetryEventType.RUN_STATUS_CHANGED,
                            run_id="run-1",
                            run_status=RunStatus.CREATED,
                        )
                    )
                    raise RuntimeError("probe failure")
            with self.assertRaisesRegex(RuntimeError, "closed"):
                sink.emit(
                    TelemetryEvent(
                        event_type=TelemetryEventType.RUN_STATUS_CHANGED,
                        run_id="run-1",
                        run_status=RunStatus.SUCCEEDED,
                    )
                )
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_independent_processes_append_complete_json_lines(self) -> None:
        child = """
import sys
from m_agent.adapters import JsonlTelemetrySink
from m_agent.runtime import RunStatus, TelemetryEvent, TelemetryEventType

sink = JsonlTelemetrySink(sys.argv[1])
for index in range(5):
    sink.emit(TelemetryEvent(
        event_type=TelemetryEventType.RUN_STATUS_CHANGED,
        run_id=f'{sys.argv[2]}-{index}',
        run_status=RunStatus.CREATED,
    ))
sink.close()
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            processes = [
                subprocess.Popen(
                    [sys.executable, "-I", "-c", child, str(path), str(index)],
                    env=isolated_subprocess_environment(),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for index in range(4)
            ]
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, (stdout, stderr))
            events = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(len(events), 20)
        self.assertEqual(len({event["run_id"] for event in events}), 20)


class OpenTelemetryContractTests(unittest.TestCase):
    def test_local_exporter_and_native_tracer_receive_correlated_spans(self) -> None:
        class RecordedSpan:
            def __init__(self) -> None:
                self.end_times: list[int | None] = []

            def end(self, end_time: int | None = None) -> None:
                self.end_times.append(end_time)

        class RecordingTracer:
            def __init__(self) -> None:
                self.calls: list[tuple[str, object | None, dict[str, object], int | None, RecordedSpan]] = []

            def start_span(self, name, *, context=None, attributes=None, start_time=None):
                recorded = RecordedSpan()
                self.calls.append(
                    (name, context, dict(attributes or {}), start_time, recorded)
                )
                return recorded

        exporter = InMemorySpanExporter()
        native_context = object()
        trace_context = OpenTelemetryTraceContext(
            trace_id="trace-1",
            parent_span_id="parent-1",
            native_context=native_context,
        )
        tracer = RecordingTracer()
        sink = OpenTelemetryTelemetrySink(
            exporter,
            tracer=tracer,
            trace_context=trace_context,
        )
        sink.emit(
            TelemetryEvent(
                event_type=TelemetryEventType.RUN_STATUS_CHANGED,
                run_id="run-1",
                run_status=RunStatus.RUNNING,
            )
        )
        sink.emit(
            TelemetryEvent(
                event_type=TelemetryEventType.STEP_STARTED,
                run_id="run-1",
                step_id="step-1",
                attempt_id="attempt-1",
            )
        )
        sink.emit(
            TelemetryEvent(
                event_type=TelemetryEventType.STEP_COMPLETED,
                run_id="run-1",
                step_id="step-1",
                attempt_id="attempt-1",
                model_purpose=ModelPurpose.PRIMARY,
                duration_ms=2.5,
            )
        )
        sink.close()

        self.assertEqual(
            [span.scope for span in exporter.spans],
            [
                OpenTelemetrySpanScope.RUN,
                OpenTelemetrySpanScope.STEP,
                OpenTelemetrySpanScope.ATTEMPT,
            ],
        )
        span = exporter.spans[-1]
        self.assertEqual(span.name, "m_agent.step.completed")
        self.assertEqual(span.attributes["m_agent.run_id"], "run-1")
        self.assertEqual(span.attributes["m_agent.step_id"], "step-1")
        self.assertEqual(span.attributes["m_agent.model_purpose"], "PRIMARY")
        self.assertEqual(span.trace_context, trace_context)
        self.assertEqual(span.attributes["m_agent.trace_id"], "trace-1")
        self.assertEqual(span.attributes["m_agent.parent_span_id"], "parent-1")
        self.assertNotIn("input", span.attributes)
        self.assertNotIn("payload", span.attributes)
        self.assertEqual(len(tracer.calls), 3)
        self.assertTrue(all(call[1] is native_context for call in tracer.calls))
        self.assertTrue(all(call[3] is not None for call in tracer.calls))
        self.assertTrue(all(call[4].end_times for call in tracer.calls))


class AcceptanceManifestTelemetryTests(unittest.TestCase):
    def test_telemetry_contract_and_host_rows_are_required(self) -> None:
        manifest = core_lifecycle_manifest(
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:" + "a" * 64,
            sdist_digest="sha256:" + "b" * 64,
            fixture_digest="sha256:" + "c" * 64,
            environment={"os": "linux", "python": "3.11"},
        )
        checks = {check.check_id: check for check in manifest.required_checks}
        self.assertTrue(checks["core.lifecycle.telemetry"].required)
        self.assertEqual(checks["core.lifecycle.telemetry"].evidence_level.value, "CONTRACT")
        self.assertTrue(checks["core.lifecycle.telemetry-host"].required)
        self.assertEqual(checks["core.lifecycle.telemetry-host"].evidence_level.value, "HOST")


class SubprocessIsolationTests(unittest.TestCase):
    def test_allowlist_excludes_provider_credentials_and_sensitive_endpoints(self) -> None:
        environment = isolated_subprocess_environment(
            {
                "PATH": "/usr/bin",
                "LANG": "C",
                "M_AGENT_OPENAI_API_KEY": "credential-canary",
                "OPENAI_API_KEY": "credential-canary",
                "M_AGENT_OPENAI_BASE_URL": "https://secret.invalid/v1",
                "OPENAI_BASE_URL": "https://secret.invalid/v1",
            }
        )
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertEqual(environment["LANG"], "C")
        for key in (
            "M_AGENT_OPENAI_API_KEY",
            "OPENAI_API_KEY",
            "M_AGENT_OPENAI_BASE_URL",
            "OPENAI_BASE_URL",
        ):
            self.assertNotIn(key, environment)
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import os; print(sorted(os.environ.items()))",
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("credential-canary", completed.stdout)
        self.assertNotIn("secret.invalid", completed.stdout)


class EvidenceBundleRedactionTests(unittest.TestCase):
    def test_bundle_rejects_and_scans_plaintext_security_canaries(self) -> None:
        check = AcceptanceCheck(
            check_id="telemetry.redaction",
            scenario="core-lifecycle",
            public_seam="m_agent.testing.ScenarioEvidenceBundle",
            owner="Telemetry Adapter",
            positive_check="bundle_uses_structural_evidence_only",
            negative_check="plaintext_security_canary_is_rejected",
            authoritative_evidence="telemetry_authoritative_digest",
            independent_evidence="telemetry_independent_digest",
            milestone="0_3",
            non_claim="provider_or_payload_content",
        )
        manifest = AcceptanceManifest(
            pack_version="ticket-10-test",
            profile="ticket-10-test",
            source_commit="b70919487a5aed78d9780efd24219ec77b670d92",
            artifact_digest="sha256:" + "a" * 64,
            sdist_digest="sha256:" + "b" * 64,
            fixture_digest="sha256:" + "c" * 64,
            environment={"os": "linux", "python": "3.11"},
            scenarios=("core-lifecycle",),
            required_checks=(check,),
            required_cli_commands=("run",),
        )
        result = AcceptanceCheckResult(
            check_id=check.check_id,
            status=AcceptanceCheckStatus.PASS,
            evidence_level=EvidenceLevel.CONTRACT,
            reason_code="telemetry_redaction_passed",
            evidence_digest="sha256:" + "d" * 64,
        )
        execution = PackExecution.create(
            manifest,
            execution_id="ticket-10-redaction",
        ).complete(manifest, (result,))
        clean_evidence = {check.authoritative_evidence: result.evidence_digest}
        clean_independent = {check.independent_evidence: "sha256:" + "e" * 64}
        bundle = ScenarioEvidenceBundle.create(
            manifest=manifest,
            execution=execution,
            execution_checks=(result,),
            scenario="core-lifecycle",
            checks=(result,),
            evidence_view=clean_evidence,
            independent_evidence=clean_independent,
        )
        bundle_text = bundle.model_dump_json()
        canaries = (
            "T10-CREDENTIAL-CANARY",
            "https://endpoint-secret.invalid/v1",
            "T10-PROTECTED-PAYLOAD-CANARY",
            "T10-PROVIDER-RAW-CANARY",
        )
        self.assertTrue(all(canary not in bundle_text for canary in canaries))
        for canary in canaries:
            with self.subTest(canary=canary):
                with self.assertRaisesRegex(ValueError, "structural or sha256"):
                    ScenarioEvidenceBundle.create(
                        manifest=manifest,
                        execution=execution,
                        execution_checks=(result,),
                        scenario="core-lifecycle",
                        checks=(result,),
                        evidence_view={**clean_evidence, "plaintext_canary": canary},
                        independent_evidence=clean_independent,
                    )
                with self.assertRaisesRegex(ValueError, "structural or sha256"):
                    ScenarioEvidenceBundle.create(
                        manifest=manifest,
                        execution=execution,
                        execution_checks=(result,),
                        scenario="core-lifecycle",
                        checks=(result,),
                        evidence_view=clean_evidence,
                        independent_evidence={
                            **clean_independent,
                            "plaintext_canary": canary,
                        },
                    )


if __name__ == "__main__":
    unittest.main()
