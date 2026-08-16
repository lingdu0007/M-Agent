"""Live Chat Completions / Responses Model Adapter 契约测试（Ticket 10）。

本文件分两部分，报告语义严格区分（PRD「Provider contract seam」）：

**离线部分**（无 ``live`` 标记，默认 CI 运行，不触网、不要求凭证）：
- 两类 Adapter 如实声明 streaming / tool calling / native structured
  output / usage reporting 能力；
- live 与 deterministic fake 类型和 ``deterministic`` 标记明显区分；
- Definition 注册在发出任何网络请求前拒绝能力不匹配
  （``ModelCapabilityError``，``adapter.requests`` 保持为空）；
- 构造 / 注册不要求凭证、不发起网络请求；
- 缺失凭证时请求以结构化 :class:`ModelFailure` 失败（PERMANENT /
  ``provider_credentials_missing``），错误文本不含凭证值；
- usage 提取：provider 返回时透传，缺失时显式为 None（不伪造）。

**live 部分**（``@pytest.mark.live``，默认 pytest collection 排除；测试
自身 ``setUp`` 还要求 ``M_AGENT_RUN_LIVE_TESTS=1``）：
- 双重凭证门控：未 opt-in 为 ``OPTED_OUT``；已 opt-in 但未提供凭证为
  ``MISSING_CREDENTIALS``；两者都以明确原因 ``skipTest``，绝不发网络请求；
- 每个 Adapter 通过**公共 Runner seam**（create_run -> start_run ->
  inspect_run）验证 text completion、streaming、tool calling、native
  structured output、usage reporting；
- provider failure（结构化 ``ModelFailure``）以 ``PROVIDER_FAILURE``
  前缀报告；普通 ``AssertionError`` 为 ``ASSERTION_FAILURE``；
- 离线 sentinel 覆盖凭证不进入任何持久化记录、Checkpoint、Run Update、
  Telemetry 或错误文本。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import pytest

httpx = pytest.importorskip(
    "httpx",
    reason="m-agent provider extra not installed; "
    "live adapter contract tests skipped",
)

from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
    DeterministicModelAdapter,
    InMemoryRunStore,
    JsonlTelemetrySink,
    ModelAdapter,
    ModelCapabilityCombination,
    ModelCapabilities,
    ModelCapabilityError,
    ModelContract,
    ModelFailure,
    ModelLimits,
    ModelPurpose,
    ModelRequirements,
    PlaintextPayloadCodec,
    REASON_UNCERTAIN_NON_IDEMPOTENT,
    Runner,
    RunResolution,
    RunStatus,
    RunUpdateType,
    RevisionStability,
    SQLiteRunStore,
    StepStatus,
    StepType,
    StreamingMode,
    StructuredOutputMode,
    TelemetryEventType,
    ToolEffect,
    ToolCallingMode,
    ToolOutcome,
    deserialize_model_response,
    UsageReportingMode,
)
from m_agent._model import ModelUsage, UsageProvenance
from m_agent._run import RunRecord
from m_agent._tools import DeterministicTool
from m_agent.provider import (
    CHAT_COMPLETIONS_CAPABILITIES,
    RESPONSES_CAPABILITIES,
    ChatCompletionsModelAdapter,
    LiveContractStatus,
    ResponsesModelAdapter,
    live_contract_preflight,
    live_contract_skip_reason,
)

#: 测试用假凭证值：只在错误文本 / 持久化泄漏检查中使用。
CREDENTIAL = "sk-live-contract-test-credential-7f3a91c2"

STRUCTURED_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["answer", "confidence"],
    "additionalProperties": False,
}

TOOL_INSTRUCTIONS = (
    "You must call the get_answer tool before replying. "
    "The tool result contains the answer you must repeat verbatim."
)


class LiveContractAssertionFailure(AssertionError):
    """A successful provider response violated the live contract."""

    def __init__(self, detail: str | None = None) -> None:
        message = "ASSERTION_FAILURE"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class LiveContractProviderFailure(AssertionError):
    """A dispatched provider request failed before contract assertions."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"PROVIDER_FAILURE: {detail}")


class CollectingTelemetrySink:
    """离线 contract 用的 Telemetry 接收器。"""

    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)


def make_answer_tool() -> tuple[DeterministicTool, list[int]]:
    """返回 (READ_ONLY 确定性工具, 调用计数) —— 工具调用契约案例用。"""
    calls: list[int] = []

    def handler(request) -> ToolOutcome:
        calls.append(1)
        return ToolOutcome.success(
            request.call_id, request.tool_name, "The answer is 42."
        )

    tool = DeterministicTool(
        name="get_answer",
        description="Return the definitive answer.",
        effect=ToolEffect.READ_ONLY,
        handler=handler,
    )
    return tool, calls


def configure_mock_contract(adapter):
    """Supply the explicit instance Contract required by Runner mock seams."""
    if getattr(adapter, "_model_contract", None) is None:
        adapter._model_contract = ModelContract(
            contract_id=f"mock-{type(adapter).__name__}",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity=adapter.model,
            capabilities=adapter.capabilities,
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="mock-provider-sizer-v1",
            serialization_id="mock-provider-wire-v1",
            fingerprint=adapter.definition_contract_fingerprint(),
        )
    return adapter


async def run_to_terminal(
    testcase: unittest.TestCase,
    adapter,
    *,
    instructions: str,
    required: ModelCapabilities,
    tools=(),
    input_text: str = "Please answer.",
    telemetry_sink=None,
) -> tuple[Runner, str, RunStatus, object]:
    """通过公共 Runner seam 跑一次 Run 到终态。

    provider 的结构化失败统一转成带 ``PROVIDER_FAILURE`` 前缀的
    测试失败，与普通断言失败在报告中可区分（AC：Test reporting
    distinguishes provider failure from assertion failure）。
    """
    adapter = configure_mock_contract(adapter)
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id="live-contract",
            version="1.0",
            instructions=instructions,
            model_requirements=ModelRequirements(capabilities=required),
            model_adapter=adapter,
            tools=tools,
        )
    )
    runner = Runner(
        registry=registry,
        store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        telemetry_sink=telemetry_sink,
    )
    created = await runner.create_run("live-contract", "1.0", input=input_text)
    try:
        terminal = await runner.start_run(created.run_id)
    except ModelFailure as exc:
        raise LiveContractProviderFailure(
            f"{exc.code} ({exc.message})"
        )
    inspection = await runner.inspect_run(created.run_id)
    if terminal.status is not RunStatus.SUCCEEDED:
        # Runner 把模型失败记录为 FAILED Step 并让 Run 到达 FAILED，
        # 不把异常抛给调用方：从 Attempt 的结构化错误详情中区分
        # provider failure（模型调用失败）与普通断言失败。
        errors = [
            a.error for a in inspection.attempts if a.error
        ]
        if errors:
            raise LiveContractProviderFailure(
                f"run ended {terminal.status}: {' | '.join(errors)}"
            )
        raise LiveContractProviderFailure(
            f"run ended {terminal.status} with no attempt error detail"
        )
    return runner, created.run_id, terminal.status, inspection


def model_checkpoints(inspection) -> list:
    return [
        c for c in inspection.checkpoints if c.step_type is StepType.MODEL
    ]


def last_model_response(inspection) -> object:
    checkpoints = model_checkpoints(inspection)
    assert checkpoints, "no MODEL checkpoint recorded"
    return deserialize_model_response(checkpoints[-1].output)


def extract_json(text: str | None) -> dict:
    """从模型输出提取 JSON 对象（容忍 markdown 代码块包裹）。"""
    if not text:
        raise AssertionError("model returned empty content")
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise AssertionError("model output did not contain a JSON object")
    return json.loads(text[start : end + 1])


def assert_no_credential(
    testcase: unittest.TestCase, *values: object
) -> None:
    for value in values:
        text = value if isinstance(value, str) else str(value)
        testcase.assertNotIn(
            CREDENTIAL,
            text,
            f"credential leaked into persisted data: {text!r}",
        )


def credential_environment():
    """Provide a test sentinel through the embedding-environment seam."""
    return patch.dict(
        os.environ, {"M_AGENT_OPENAI_API_KEY": CREDENTIAL}, clear=False
    )


# ---------------------------------------------------------------------------
# 离线部分（默认 CI，不触网、不要求凭证）
# ---------------------------------------------------------------------------


class LiveAdapterOfflineContractTests(unittest.TestCase):
    """无凭证、无网络时也必须通过的全部离线契约。"""

    def test_direct_unittest_opt_out_blocks_public_runner_before_transport(
        self,
    ) -> None:
        """直接 unittest 入口有 sentinel 凭证时仍不得碰网络。"""
        calls = 0

        def counter_transport(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"choices": []})

        async def attempt_through_public_runner() -> LiveContractStatus:
            status = live_contract_preflight()
            if status is not None:
                return status
            adapter = ChatCompletionsModelAdapter(
                base_url="https://live-contract.invalid/v1",
            )
            adapter._transport = httpx.MockTransport(counter_transport)
            try:
                await run_to_terminal(
                    self,
                    adapter,
                    instructions="Reply with pong.",
                    required=ModelCapabilities(),
                )
            finally:
                await adapter.aclose()
            self.fail("an opted-out live contract reached Runner")

        with patch.dict(
            os.environ,
            {
                "M_AGENT_RUN_LIVE_TESTS": "",
                "M_AGENT_OPENAI_API_KEY": CREDENTIAL,
            },
            clear=False,
        ):
            status = asyncio.run(attempt_through_public_runner())

        self.assertEqual(status, LiveContractStatus.OPTED_OUT)
        self.assertEqual(calls, 0)

    def test_direct_unittest_setup_rejects_ambient_credential_without_opt_in(
        self,
    ) -> None:
        """unittest discovery 使用 live case 的 setup gate，而非 pytest hook。"""
        case = LiveChatCompletionsContractTests(
            "test_text_completion_and_usage_reporting"
        )
        with patch.dict(
            os.environ,
            {
                "M_AGENT_RUN_LIVE_TESTS": "",
                "M_AGENT_OPENAI_API_KEY": CREDENTIAL,
            },
            clear=False,
        ):
            with self.assertRaisesRegex(unittest.SkipTest, "OPTED_OUT"):
                case.setUp()

    def test_opted_in_missing_credentials_reports_skip_without_transport(
        self,
    ) -> None:
        calls = 0

        def counter_transport(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"choices": []})

        async def attempt_through_public_runner() -> LiveContractStatus:
            status = live_contract_preflight()
            if status is not None:
                return status
            adapter = ChatCompletionsModelAdapter(
                base_url="https://live-contract.invalid/v1",
            )
            adapter._transport = httpx.MockTransport(counter_transport)
            try:
                await run_to_terminal(
                    self,
                    adapter,
                    instructions="Reply with pong.",
                    required=ModelCapabilities(),
                )
            finally:
                await adapter.aclose()
            self.fail("a credential-missing live contract reached Runner")

        with patch.dict(
            os.environ,
            {"M_AGENT_RUN_LIVE_TESTS": "1"},
            clear=True,
        ):
            status = asyncio.run(attempt_through_public_runner())

        self.assertEqual(status, LiveContractStatus.MISSING_CREDENTIALS)
        self.assertEqual(calls, 0)

    def test_chat_completions_capability_declaration(self) -> None:
        # AC：Chat Completions Adapter 如实声明四类能力。
        self.assertTrue(CHAT_COMPLETIONS_CAPABILITIES.streaming)
        self.assertTrue(CHAT_COMPLETIONS_CAPABILITIES.tool_calling)
        self.assertTrue(CHAT_COMPLETIONS_CAPABILITIES.structured_output)
        self.assertTrue(CHAT_COMPLETIONS_CAPABILITIES.usage_reporting)
        adapter = ChatCompletionsModelAdapter()
        self.assertEqual(adapter.capabilities, CHAT_COMPLETIONS_CAPABILITIES)

    def test_responses_capability_declaration(self) -> None:
        # AC：Responses Adapter 如实声明四类能力。
        self.assertTrue(RESPONSES_CAPABILITIES.streaming)
        self.assertTrue(RESPONSES_CAPABILITIES.tool_calling)
        self.assertTrue(RESPONSES_CAPABILITIES.structured_output)
        self.assertTrue(RESPONSES_CAPABILITIES.usage_reporting)
        adapter = ResponsesModelAdapter()
        self.assertEqual(adapter.capabilities, RESPONSES_CAPABILITIES)

    def test_live_adapters_visibly_distinct_from_fake(self) -> None:
        # AC：确定性 fake 的成功不可能被误认为 live 兼容性验证。
        fake = DeterministicModelAdapter(responses=("x",))
        self.assertTrue(fake.deterministic)
        for adapter in (
            ChatCompletionsModelAdapter(),
            ResponsesModelAdapter(),
        ):
            self.assertFalse(adapter.deterministic)
            self.assertNotIsInstance(adapter, DeterministicModelAdapter)
            self.assertIsInstance(adapter, ModelAdapter)

    def test_construction_requires_no_credentials_and_no_network(
        self,
    ) -> None:
        # AC：构造不触网、不要求凭证（凭证只在真正发出请求时必需）。
        for adapter in (
            ChatCompletionsModelAdapter(),
            ResponsesModelAdapter(),
        ):
            self.assertEqual(adapter.requests, [])
            self.assertFalse(adapter.deterministic)

    def test_plain_requests_omit_structured_output_parameters(self) -> None:
        """JSON output is opt-in; ordinary Runner calls stay provider-neutral."""
        request = _request()
        chat = ChatCompletionsModelAdapter()._build_payload(request)
        responses = ResponsesModelAdapter()._build_payload(request)

        self.assertNotIn("response_format", chat)
        self.assertNotIn("text", responses)

        structured_request = request.model_copy(
            update={"structured_output": StructuredOutputMode.NATIVE}
        )
        structured_chat = ChatCompletionsModelAdapter(
            structured_output_schema=STRUCTURED_SCHEMA
        )._build_payload(structured_request)
        structured_responses = ResponsesModelAdapter(
            structured_output_schema=STRUCTURED_SCHEMA
        )._build_payload(structured_request)
        self.assertIn("response_format", structured_chat)
        self.assertIn("text", structured_responses)

    def test_chat_json_object_mode_uses_native_json_output(self) -> None:
        """A Chat-compatible provider can explicitly select JSON-object mode."""
        payload = ChatCompletionsModelAdapter(
            structured_output_schema=STRUCTURED_SCHEMA,
            structured_output_mode="json_object",
        )._build_payload(
            _request().model_copy(
                update={"structured_output": StructuredOutputMode.NATIVE}
            )
        )

        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_resume_rejects_adapter_configuration_drift_without_network(
        self,
    ) -> None:
        """A re-registered version cannot change native output semantics."""

        async def invoke(
            structured_output_mode: str, timeout: float
        ) -> tuple[int, list[str], str, object]:
            original = ChatCompletionsModelAdapter(
                base_url="https://live-contract.invalid/v1",
                structured_output_schema=STRUCTURED_SCHEMA,
                structured_output_mode="json_object",
            )
            configure_mock_contract(original)
            original_definition = AgentDefinition.for_adapter(
                definition_id="frozen-adapter-contract",
                version="1.0",
                instructions="Return JSON.",
                model_adapter=original,
            )
            snapshot = original_definition.frozen_snapshot()
            store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
            await store.create_run(
                RunRecord(
                    run_id="frozen-adapter-contract-run",
                    definition_id="frozen-adapter-contract",
                    definition_version="1.0",
                    input="ping",
                    status=RunStatus.RUNNING,
                    snapshot=snapshot,
                )
            )
            calls = 0

            def counter_transport(request):
                nonlocal calls
                calls += 1
                return httpx.Response(200, json={"choices": []})

            changed = ChatCompletionsModelAdapter(
                base_url="https://live-contract.invalid/v1",
                structured_output_schema=STRUCTURED_SCHEMA,
                structured_output_mode=structured_output_mode,
                timeout=timeout,
            )
            changed._transport = httpx.MockTransport(counter_transport)
            configure_mock_contract(changed)
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="frozen-adapter-contract",
                    version="1.0",
                    instructions="Return JSON.",
                    model_adapter=changed,
                )
            )
            runner = Runner(registry=registry, store=store)
            try:
                with self.assertRaisesRegex(
                    RuntimeError, "snapshot Model Contract"
                ):
                    await runner.resume_run("frozen-adapter-contract-run")
                restored = await runner.get_run("frozen-adapter-contract-run")
                return (
                    calls,
                    changed.requests,
                    restored.snapshot.model_bindings.for_purpose(
                        ModelPurpose.PRIMARY
                    ).contract.fingerprint,
                    restored,
                )
            finally:
                await original.aclose()
                await changed.aclose()

        for mode, timeout in (("json_schema", 120.0), ("json_object", 0.1)):
            with self.subTest(mode=mode, timeout=timeout):
                with credential_environment():
                    calls, requests, fingerprint, restored = asyncio.run(
                        invoke(mode, timeout)
                    )

                self.assertEqual(calls, 0)
                self.assertEqual(requests, [])
                self.assertTrue(fingerprint)
                assert_no_credential(self, fingerprint)
                self.assertEqual(restored.status, RunStatus.RUNNING)
                self.assertEqual(restored.version, 1)
                self.assertIsNone(restored.lease_owner)
                self.assertIsNone(restored.lease_expires_at)

    def test_resolution_rejects_adapter_configuration_drift_before_network(
        self,
    ) -> None:
        """WAITING resolution cannot change a frozen adapter's semantics."""

        async def invoke() -> tuple[int, list[str], object]:
            original = ChatCompletionsModelAdapter(
                base_url="https://live-contract.invalid/v1",
                structured_output_schema=STRUCTURED_SCHEMA,
                structured_output_mode="json_object",
            )
            configure_mock_contract(original)
            snapshot = AgentDefinition.for_adapter(
                definition_id="frozen-resolution-contract",
                version="1.0",
                instructions="Return JSON.",
                model_adapter=original,
            ).frozen_snapshot()
            store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
            await store.create_run(
                RunRecord(
                    run_id="frozen-resolution-contract-run",
                    definition_id="frozen-resolution-contract",
                    definition_version="1.0",
                    input="ping",
                    status=RunStatus.WAITING,
                    snapshot=snapshot,
                    waiting_reason=REASON_UNCERTAIN_NON_IDEMPOTENT,
                    waiting_step_id="unconfirmed-tool-step",
                )
            )
            calls = 0

            def counter_transport(request):
                nonlocal calls
                calls += 1
                return httpx.Response(200, json={"choices": []})

            changed = ChatCompletionsModelAdapter(
                base_url="https://live-contract.invalid/v1",
                structured_output_schema=STRUCTURED_SCHEMA,
                structured_output_mode="json_schema",
            )
            changed._transport = httpx.MockTransport(counter_transport)
            configure_mock_contract(changed)
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="frozen-resolution-contract",
                    version="1.0",
                    instructions="Return JSON.",
                    model_adapter=changed,
                )
            )
            runner = Runner(registry=registry, store=store)
            try:
                with self.assertRaisesRegex(
                RuntimeError, "snapshot Model Contract"
                ):
                    await runner.resolve_run(
                        "frozen-resolution-contract-run",
                        RunResolution.confirm_step("confirmed"),
                        expected_version=1,
                    )
                restored = await runner.get_run(
                    "frozen-resolution-contract-run"
                )
                return calls, changed.requests, restored
            finally:
                await original.aclose()
                await changed.aclose()

        with credential_environment():
            calls, requests, restored = asyncio.run(invoke())

        self.assertEqual(calls, 0)
        self.assertEqual(requests, [])
        self.assertEqual(restored.status, RunStatus.WAITING)
        self.assertEqual(restored.version, 1)
        self.assertIsNone(restored.lease_owner)
        self.assertIsNone(restored.lease_expires_at)

    def test_registration_rejects_live_adapter_without_fingerprint(
        self,
    ) -> None:
        """Every non-deterministic adapter must freeze a stable contract."""

        class UnfrozenLiveAdapter(ModelAdapter):
            capabilities = ModelCapabilities()

            async def generate(self, request):
                raise AssertionError("must not be dispatched")

        registry = DefinitionRegistry()
        with self.assertRaisesRegex(ValueError, "instance ModelContract"):
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="unfrozen-live-adapter",
                    version="1.0",
                    instructions="i",
                    model_adapter=UnfrozenLiveAdapter(),
                )
            )

    def test_step_started_mutation_blocks_model_dispatch(self) -> None:
        """Telemetry cannot mutate a frozen adapter between guards and I/O."""

        async def invoke(mutation: str) -> tuple[RunStatus, int, list[str]]:
            calls = 0

            def counter_transport(request):
                nonlocal calls
                calls += 1
                return httpx.Response(
                    200,
                    content=(
                        'data: {"choices":[{"delta":{"content":"pong"}}]}\n\n'
                        'data: {"usage":{"prompt_tokens":1,'
                        '"completion_tokens":1}}\n\n'
                    ),
                    headers={"content-type": "text/event-stream"},
                )

            adapter = ChatCompletionsModelAdapter(
                base_url="https://live-contract.invalid/v1",
                structured_output_schema=STRUCTURED_SCHEMA,
                structured_output_mode="json_object",
            )
            adapter._transport = httpx.MockTransport(counter_transport)
            configure_mock_contract(adapter)

            class MutatingSink:
                def emit(self, event) -> None:
                    if (
                        event.event_type is TelemetryEventType.STEP_STARTED
                        and event.step_type is StepType.MODEL
                    ):
                        if mutation == "mode":
                            adapter.structured_output_mode = "json_schema"
                        else:
                            adapter.capabilities = ModelCapabilities(
                                streaming=StreamingMode.DELTA,
                                tool_calling=ToolCallingMode.NATIVE,
                                structured_output=StructuredOutputMode.NONE,
                                usage_reporting=UsageReportingMode.PROVIDER_REPORTED,
                                supported_combinations=(
                                    ModelCapabilityCombination(
                                        streaming=StreamingMode.DELTA,
                                        tool_calling=ToolCallingMode.NATIVE,
                                        usage_reporting=UsageReportingMode.PROVIDER_REPORTED,
                                    ),
                                ),
                            )

            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="telemetry-mutated-adapter",
                    version="1.0",
                    instructions="Return JSON.",
                    model_adapter=adapter,
                )
            )
            runner = Runner(
                registry=registry,
                store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
                telemetry_sink=MutatingSink(),
            )
            created = await runner.create_run(
                "telemetry-mutated-adapter", "1.0", input="ping"
            )
            try:
                terminal = await runner.start_run(created.run_id)
                return terminal.status, calls, adapter.requests
            finally:
                await adapter.aclose()

        for mutation in ("mode", "capability"):
            with self.subTest(mutation=mutation):
                with credential_environment():
                    status, calls, requests = asyncio.run(invoke(mutation))

                self.assertEqual(status, RunStatus.FAILED)
                self.assertEqual(calls, 0)
                self.assertEqual(requests, [])

    def test_base_url_userinfo_is_stripped(self) -> None:
        # 安全（ADR 0033）：base_url 中嵌入的 userinfo（可能为凭证）
        # 必须被剥离，不进入请求记录或错误文本。
        adapter = ChatCompletionsModelAdapter(
            base_url="https://user:supersecret@example.com/v1"
        )
        self.assertNotIn("supersecret", adapter.endpoint_url)
        self.assertNotIn("supersecret", adapter.base_url)
        self.assertEqual(adapter.endpoint_url, "https://example.com/v1/chat/completions")

    def test_live_adapters_reject_constructor_credential_values(self) -> None:
        """Embedding environment is the sole credential source."""
        for adapter_cls in (
            ChatCompletionsModelAdapter,
            ResponsesModelAdapter,
        ):
            with self.subTest(adapter=adapter_cls.__name__):
                with self.assertRaises(TypeError):
                    adapter_cls(api_key=CREDENTIAL)

    def test_base_url_sensitive_components_never_reach_durable_failure(
        self,
    ) -> None:
        """URL query / fragment are provider configuration, never evidence."""

        async def invoke(adapter_cls, path_name: str, path: str):
            adapter = adapter_cls(
                base_url=(
                    "https://user:"
                    f"{CREDENTIAL}@example.com/v1?access_token="
                    f"{CREDENTIAL}#private-fragment"
                ),
                **{path_name: path},
            )
            adapter._transport = httpx.MockTransport(
                lambda request: httpx.Response(401)
            )
            configure_mock_contract(adapter)
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="base-url-redaction",
                    version="1.0",
                    instructions="Reply with pong.",
                    model_adapter=adapter,
                )
            )
            runner = Runner(
                registry=registry,
                store=InMemoryRunStore(
                    payload_codec=PlaintextPayloadCodec()
                ),
            )
            created = await runner.create_run(
                "base-url-redaction", "1.0", input="ping"
            )
            terminal = await runner.start_run(created.run_id)
            inspection = await runner.inspect_run(created.run_id)
            requests = list(adapter.requests)
            endpoint = adapter.endpoint_url
            await adapter.aclose()
            return terminal, inspection, endpoint, requests

        cases = (
            (
                ChatCompletionsModelAdapter,
                "chat_completions_path",
                f"/chat/completions?access_token={CREDENTIAL}#private",
                "https://example.com/v1/chat/completions",
            ),
            (
                ResponsesModelAdapter,
                "responses_path",
                f"/responses?access_token={CREDENTIAL}#private",
                "https://example.com/v1/responses",
            ),
        )
        with patch.dict(
            os.environ,
            {"M_AGENT_OPENAI_API_KEY": CREDENTIAL},
            clear=True,
        ):
            for adapter_cls, path_name, path, expected_endpoint in cases:
                with self.subTest(adapter=adapter_cls.__name__):
                    terminal, inspection, endpoint, requests = asyncio.run(
                        invoke(adapter_cls, path_name, path)
                    )

                    self.assertEqual(terminal.status, RunStatus.FAILED)
                    self.assertEqual(endpoint, expected_endpoint)
                    assert_no_credential(
                        self,
                        endpoint,
                        str(requests),
                        inspection.run.model_dump_json(),
                        *(
                            attempt.model_dump_json()
                            for attempt in inspection.attempts
                        ),
                    )

    def test_registration_rejects_capability_mismatch_without_network(
        self,
    ) -> None:
        # AC：Definition 注册在发出任何网络请求前拒绝能力不匹配。
        # live adapter 类固定声明四能力；用覆写 capabilities 的子类
        # 模拟“声明不完整”的 live adapter，验证注册拒绝发生在任何
        # 网络请求之前。
        class PartialLiveAdapter(ChatCompletionsModelAdapter):
            capabilities = ModelCapabilities(streaming=StreamingMode.DELTA)

        calls = 0

        def counter_transport(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"choices": []})

        adapter = PartialLiveAdapter()
        adapter._transport = httpx.MockTransport(counter_transport)
        configure_mock_contract(adapter)
        registry = DefinitionRegistry()
        with self.assertRaises(ModelCapabilityError):
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="needs-tool-calling",
                    version="1.0",
                    instructions="i",
                    model_requirements=ModelRequirements(
                        capabilities=ModelCapabilities(
                            tool_calling=ToolCallingMode.NATIVE
                        )
                    ),
                    model_adapter=adapter,
                )
            )
        # 注册被拒发生在任何网络请求之前：无请求被发出。
        self.assertEqual(
            adapter.requests, [], "registration must not touch network"
        )
        self.assertEqual(calls, 0, "registration must not dispatch transport")
        asyncio.run(adapter.aclose())

    def test_registration_accepts_matching_capabilities(self) -> None:
        for adapter_cls in (
            ChatCompletionsModelAdapter,
            ResponsesModelAdapter,
        ):
            adapter = adapter_cls()
            configure_mock_contract(adapter)
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="assistant",
                    version="1.0",
                    instructions="i",
                    model_requirements=ModelRequirements(
                        capabilities=adapter.capabilities
                    ),
                    model_adapter=adapter,
                )
            )
            self.assertTrue(registry.is_registered("assistant", "1.0"))
            self.assertEqual(adapter.requests, [])

    def test_provider_instance_contract_can_narrow_class_capabilities(self) -> None:
        """An instance Contract is an intersection, not the protocol ceiling."""
        adapter = ChatCompletionsModelAdapter(
            base_url="https://live-contract.invalid/v1",
        )
        adapter._model_contract = ModelContract(
            contract_id="chat-text-only",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="chat:text-only",
            capabilities=ModelCapabilities(),
            limits=ModelLimits(context_window_tokens=128, max_output_tokens=32),
            input_sizer_id="mock-provider-sizer-v1",
            serialization_id="mock-provider-wire-v1",
            fingerprint=adapter.definition_contract_fingerprint(),
        )
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="chat-text-only",
                version="1",
                instructions="Reply.",
                model_adapter=adapter,
            )
        )

        self.assertTrue(registry.is_registered("chat-text-only", "1"))
        self.assertEqual(adapter.requests, [])
        asyncio.run(adapter.aclose())

    def test_provider_contract_fingerprint_must_match_current_configuration(
        self,
    ) -> None:
        """A fresh provider instance cannot reuse another target's Contract."""
        original = ChatCompletionsModelAdapter(
            model="model-original",
            base_url="https://original.invalid/v1",
        )
        configure_mock_contract(original)
        changed = ChatCompletionsModelAdapter(
            model="model-changed",
            base_url="https://changed.invalid/v1",
            model_contract=original.model_contract,
        )
        try:
            with self.assertRaisesRegex(ValueError, "configuration fingerprint"):
                AgentDefinition.for_adapter(
                    definition_id="stale-provider-contract",
                    version="1",
                    instructions="Never dispatch.",
                    model_adapter=changed,
                )
            self.assertEqual(changed.requests, [])
        finally:
            asyncio.run(original.aclose())
            asyncio.run(changed.aclose())

    def test_missing_credentials_fail_structured_without_key_value(
        self,
    ) -> None:
        # AC：无凭证时结构化失败（PERMANENT / provider_credentials_missing），
        # 错误文本只含配置指引的变量名，不含任何密钥值。
        with patch.dict(os.environ, {}, clear=True):
            for adapter_cls in (
                ChatCompletionsModelAdapter,
                ResponsesModelAdapter,
            ):
                adapter = adapter_cls()  # 不传凭证
                with self.assertRaises(ModelFailure) as ctx:
                    asyncio.run(_generate_no_credentials(adapter))
                self.assertEqual(
                    ctx.exception.classification.value, "PERMANENT"
                )
                self.assertEqual(
                    ctx.exception.code, "provider_credentials_missing"
                )
                assert_no_credential(self, ctx.exception.message)
                self.assertIn(
                    "M_AGENT_OPENAI_API_KEY", ctx.exception.message
                )

    def test_provider_error_text_never_contains_credential_value(
        self,
    ) -> None:
        # MockTransport 模拟 transport failure；默认离线测试不打开 TCP。
        calls = 0

        def transport_failure(request):
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("simulated connection failure", request=request)

        async def attempt() -> None:
            adapter = ChatCompletionsModelAdapter(
                base_url="https://live-contract.invalid/v1",
            )
            adapter._transport = httpx.MockTransport(transport_failure)
            try:
                await adapter.generate(_request())
            finally:
                await adapter.aclose()

        with credential_environment():
            with self.assertRaises(ModelFailure) as ctx:
                asyncio.run(attempt())
        self.assertEqual(ctx.exception.classification.value, "TRANSIENT")
        assert_no_credential(self, ctx.exception.message)
        self.assertEqual(calls, 1)

    def test_responses_sse_failure_never_persists_provider_error_content(
        self,
    ) -> None:
        """Responses SSE error fields are not safe durable diagnostics."""

        async def invoke():
            adapter = ResponsesModelAdapter(
                base_url="https://live-contract.invalid/v1",
            )
            adapter._transport = httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=(
                        "data: "
                        + json.dumps(
                            {
                                "type": "response.failed",
                                "error": {"code": CREDENTIAL},
                            }
                        )
                        + "\n\n"
                    ),
                    headers={"content-type": "text/event-stream"},
                )
            )
            configure_mock_contract(adapter)
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="responses-failure",
                    version="1.0",
                    instructions="Reply with pong.",
                    model_adapter=adapter,
                )
            )
            runner = Runner(
                registry=registry,
                store=InMemoryRunStore(
                    payload_codec=PlaintextPayloadCodec()
                ),
            )
            created = await runner.create_run(
                "responses-failure", "1.0", input="ping"
            )
            terminal = await runner.start_run(created.run_id)
            inspection = await runner.inspect_run(created.run_id)
            await adapter.aclose()
            return terminal, inspection

        with credential_environment():
            terminal, inspection = asyncio.run(invoke())
        self.assertEqual(terminal.status, RunStatus.FAILED)
        self.assertEqual(
            inspection.attempts[0].error_code, "provider_response_invalid"
        )
        assert_no_credential(
            self, inspection.attempts[0].error or ""
        )

    def test_invalid_sse_content_type_never_persists_provider_value(
        self,
    ) -> None:
        """Content-Type is external response data, not durable diagnostics."""

        async def invoke(adapter_cls):
            adapter = adapter_cls(
                base_url="https://live-contract.invalid/v1",
            )
            adapter._transport = httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={
                        "content-type": f"text/plain; value={CREDENTIAL}"
                    },
                )
            )
            configure_mock_contract(adapter)
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="invalid-sse-content-type",
                    version="1.0",
                    instructions="Reply with pong.",
                    model_adapter=adapter,
                )
            )
            runner = Runner(
                registry=registry,
                store=InMemoryRunStore(
                    payload_codec=PlaintextPayloadCodec()
                ),
            )
            created = await runner.create_run(
                "invalid-sse-content-type", "1.0", input="ping"
            )
            terminal = await runner.start_run(created.run_id)
            inspection = await runner.inspect_run(created.run_id)
            await adapter.aclose()
            return terminal, inspection

        with credential_environment():
            for adapter_cls in (
                ChatCompletionsModelAdapter,
                ResponsesModelAdapter,
            ):
                with self.subTest(adapter=adapter_cls.__name__):
                    terminal, inspection = asyncio.run(invoke(adapter_cls))
                    self.assertEqual(terminal.status, RunStatus.FAILED)
                    self.assertEqual(
                        inspection.attempts[0].error_code,
                        "provider_response_invalid",
                    )
                    assert_no_credential(
                        self, inspection.attempts[0].error or ""
                    )

    def test_usage_mapping_surfaces_and_represents_absence(self) -> None:
        # AC：provider 返回 usage 时透传；缺失时显式为 None（不伪造）。
        from m_agent.provider import extract_usage

        chat_usage = extract_usage(
            {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        )
        self.assertEqual(
            chat_usage,
            ModelUsage(
                input_tokens=10,
                output_tokens=5,
                raw_unit="tokens",
                normalization_source=(
                    "openai-compatible-usage-v1:prompt_tokens->input_tokens,"
                    "completion_tokens->output_tokens"
                ),
            ),
        )
        assert chat_usage is not None
        self.assertEqual(chat_usage.raw_unit, "tokens")
        self.assertEqual(
            chat_usage.normalization_source,
            "openai-compatible-usage-v1:prompt_tokens->input_tokens,"
            "completion_tokens->output_tokens",
        )
        responses_usage = extract_usage(
            {"usage": {"input_tokens": 7, "output_tokens": 3}}
        )
        self.assertEqual(
            responses_usage,
            ModelUsage(
                input_tokens=7,
                output_tokens=3,
                raw_unit="tokens",
                normalization_source=(
                    "openai-compatible-usage-v1:input_tokens->input_tokens,"
                    "output_tokens->output_tokens"
                ),
            ),
        )
        assert responses_usage is not None
        self.assertEqual(responses_usage.raw_unit, "tokens")
        self.assertEqual(
            responses_usage.normalization_source,
            "openai-compatible-usage-v1:input_tokens->input_tokens,"
            "output_tokens->output_tokens",
        )
        # 合法的 0 不是缺失；不得被 truthy/falsy 映射吞掉。
        self.assertEqual(
            extract_usage({"usage": {"prompt_tokens": 0, "completion_tokens": 0}}),
            ModelUsage(
                input_tokens=0,
                output_tokens=0,
                raw_unit="tokens",
                normalization_source=(
                    "openai-compatible-usage-v1:prompt_tokens->input_tokens,"
                    "completion_tokens->output_tokens"
                ),
            ),
        )
        self.assertEqual(
            extract_usage(
                {
                    "usage": {
                        "input_tokens": 0,
                        "prompt_tokens": 99,
                        "output_tokens": 0,
                        "completion_tokens": 99,
                    }
                }
            ),
            ModelUsage(
                input_tokens=0,
                output_tokens=0,
                raw_unit="tokens",
                normalization_source=(
                    "openai-compatible-usage-v1:input_tokens->input_tokens,"
                    "output_tokens->output_tokens"
                ),
            ),
        )
        # 缺失 usage：显式 None，绝不猜测。
        self.assertIsNone(extract_usage({}))
        self.assertIsNone(extract_usage({"usage": {}}))
        self.assertIsNone(extract_usage({"usage": {"foo": 1}}))

    def test_provider_response_retains_actual_model_revision(self) -> None:
        """Returned provider revisions survive normalization for inspection."""

        async def invoke(adapter_cls, payload):
            adapter = adapter_cls(
                base_url="https://live-contract.invalid/v1",
            )
            adapter._transport = httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )
            try:
                with credential_environment():
                    return await adapter.generate(_request())
            finally:
                await adapter.aclose()

        cases = (
            (
                ChatCompletionsModelAdapter,
                {
                    "model": "provider-revision-chat-123",
                    "choices": [{"message": {"content": "pong"}}],
                },
            ),
            (
                ResponsesModelAdapter,
                {
                    "model": "provider-revision-responses-456",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "pong"}
                            ],
                        }
                    ],
                },
            ),
        )
        for adapter_cls, payload in cases:
            with self.subTest(adapter=adapter_cls.__name__):
                response = asyncio.run(invoke(adapter_cls, payload))
                self.assertEqual(response.actual_revision, payload["model"])

    def test_structured_output_diagnostic_never_echoes_provider_content(
        self,
    ) -> None:
        with self.assertRaises(AssertionError) as ctx:
            extract_json(CREDENTIAL)
        assert_no_credential(self, str(ctx.exception))

    def test_mock_transport_usage_reaches_run_attempt_and_telemetry(
        self,
    ) -> None:
        """完整、部分、全零和缺失 usage 均走公开 Runner seam。"""

        def sse_response(adapter_cls, usage):
            if adapter_cls is ChatCompletionsModelAdapter:
                events = [{"choices": [{"delta": {"content": "pong"}}]}]
                if usage is not None:
                    events.append({"usage": usage})
            else:
                completed = {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            {
                                "type": "message",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "pong",
                                    }
                                ],
                            }
                        ]
                    },
                }
                if usage is not None:
                    completed["response"]["usage"] = usage
                events = [
                    {"type": "response.output_text.delta", "delta": "pong"},
                    completed,
                ]
            return "".join(
                f"data: {json.dumps(event)}\n\n" for event in events
            )

        async def invoke(adapter_cls, usage, expected):
            calls = 0

            def handler(request):
                nonlocal calls
                calls += 1
                return httpx.Response(
                    200,
                    content=sse_response(adapter_cls, usage),
                    headers={"content-type": "text/event-stream"},
                )

            sink = CollectingTelemetrySink()
            adapter = adapter_cls(
                base_url="https://live-contract.invalid/v1",
            )
            adapter._transport = httpx.MockTransport(handler)
            try:
                _, _, status, inspection = await run_to_terminal(
                    self,
                    adapter,
                    instructions="Reply with pong.",
                    required=ModelCapabilities(
                        streaming=StreamingMode.DELTA
                    ),
                    telemetry_sink=sink,
                )
            finally:
                await adapter.aclose()
            response = last_model_response(inspection)
            attempts = [attempt for attempt in inspection.attempts if attempt.output]
            completed = [
                event
                for event in sink.events
                if event.event_type is TelemetryEventType.STEP_COMPLETED
                and event.step_type is StepType.MODEL
            ]
            return status, response, attempts, completed, calls

        cases = (
            (
                "complete",
                {"input_tokens": 7, "output_tokens": 3},
                ModelUsage(
                    input_tokens=7,
                    output_tokens=3,
                    raw_unit="tokens",
                    normalization_source=(
                        "openai-compatible-usage-v1:input_tokens->input_tokens,"
                        "output_tokens->output_tokens"
                    ),
                ),
            ),
            (
                "partial",
                {"input_tokens": 7},
                ModelUsage(
                    input_tokens=7,
                    output_tokens=None,
                    raw_unit="tokens",
                    normalization_source=(
                        "openai-compatible-usage-v1:input_tokens->input_tokens"
                    ),
                ),
            ),
            (
                "all_zero",
                {"input_tokens": 0, "output_tokens": 0},
                ModelUsage(
                    input_tokens=0,
                    output_tokens=0,
                    raw_unit="tokens",
                    normalization_source=(
                        "openai-compatible-usage-v1:input_tokens->input_tokens,"
                        "output_tokens->output_tokens"
                    ),
                ),
            ),
            (
                "missing",
                None,
                ModelUsage(provenance=UsageProvenance.UNAVAILABLE),
            ),
        )
        with credential_environment():
            for adapter_cls in (
                ChatCompletionsModelAdapter,
                ResponsesModelAdapter,
            ):
                for name, usage, expected in cases:
                    with self.subTest(adapter=adapter_cls.__name__, usage=name):
                        status, response, attempts, completed, calls = asyncio.run(
                            invoke(adapter_cls, usage, expected)
                        )
                        self.assertEqual(status, RunStatus.SUCCEEDED)
                        self.assertEqual(response.usage, expected)
                        self.assertEqual(calls, 1)
                        self.assertEqual(len(attempts), 1)
                        self.assertEqual(
                            deserialize_model_response(
                                attempts[0].output
                            ).usage,
                            expected,
                        )
                        self.assertEqual(len(completed), 1)
                        self.assertEqual(completed[0].usage, expected)

    def test_provider_failure_keeps_sentinel_out_of_all_durable_surfaces(
        self,
    ) -> None:
        """provider error body is not retained beyond safe failure metadata."""

        async def invoke(db_path: str, telemetry_path: str):
            def handler(request):
                return httpx.Response(
                    401, json={"error": {"message": CREDENTIAL}}
                )

            store = SQLiteRunStore(
                db_path, payload_codec=PlaintextPayloadCodec()
            )
            sink = JsonlTelemetrySink(telemetry_path)
            adapter = ChatCompletionsModelAdapter(
                base_url="https://live-contract.invalid/v1",
            )
            adapter._transport = httpx.MockTransport(handler)
            configure_mock_contract(adapter)
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="provider-failure",
                    version="1.0",
                    instructions="Reply with pong.",
                    model_adapter=adapter,
                )
            )
            runner = Runner(
                registry=registry, store=store, telemetry_sink=sink
            )
            created = await runner.create_run(
                "provider-failure", "1.0", input="ping"
            )
            updates = []

            async def collect() -> None:
                async for update in runner.subscribe_run(created.run_id):
                    updates.append(update)
                    if (
                        update.update_type is RunUpdateType.STATUS_CHANGED
                        and update.status is RunStatus.FAILED
                    ):
                        return

            collector = asyncio.create_task(collect())
            await asyncio.sleep(0)
            terminal = await runner.start_run(created.run_id)
            await collector
            inspection = await runner.inspect_run(created.run_id)
            await adapter.aclose()
            sink.close()
            store.close()
            return terminal, inspection, updates

        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "run.db")
            telemetry_path = os.path.join(directory, "events.jsonl")
            with credential_environment():
                terminal, inspection, updates = asyncio.run(
                    invoke(db_path, telemetry_path)
                )
            with open(db_path, "rb") as database:
                raw_database = database.read()
            with open(telemetry_path, "rb") as telemetry:
                raw_telemetry = telemetry.read()

        self.assertEqual(terminal.status, RunStatus.FAILED)
        self.assertEqual(inspection.attempts[0].status, StepStatus.FAILED)
        self.assertEqual(
            inspection.attempts[0].classification.value, "PERMANENT"
        )
        self.assertEqual(
            inspection.attempts[0].error_code, "provider_request_failed"
        )
        assert_no_credential(
            self,
            inspection.run.model_dump_json(),
            *(attempt.model_dump_json() for attempt in inspection.attempts),
            *(checkpoint.output for checkpoint in inspection.checkpoints),
            *(update.model_dump_json() for update in updates),
        )
        self.assertNotIn(CREDENTIAL.encode(), raw_database)
        self.assertNotIn(CREDENTIAL.encode(), raw_telemetry)

    def test_successful_live_adapter_keeps_sentinel_out_of_durable_surfaces(
        self,
    ) -> None:
        """live adapter configuration never becomes Run payload or telemetry."""

        async def invoke(db_path: str, telemetry_path: str):
            def handler(request):
                return httpx.Response(
                    200,
                    content=(
                        'data: {"choices":[{"delta":{"content":"pong"}}]}\n\n'
                        'data: {"usage":{"prompt_tokens":0,'
                        '"completion_tokens":0}}\n\n'
                    ),
                    headers={"content-type": "text/event-stream"},
                )

            store = SQLiteRunStore(
                db_path, payload_codec=PlaintextPayloadCodec()
            )
            sink = JsonlTelemetrySink(telemetry_path)
            adapter = ChatCompletionsModelAdapter(
                base_url="https://live-contract.invalid/v1",
            )
            adapter._transport = httpx.MockTransport(handler)
            configure_mock_contract(adapter)
            registry = DefinitionRegistry()
            registry.register(
                AgentDefinition.for_adapter(
                    definition_id="credential-isolation",
                    version="1.0",
                    instructions="Reply with pong.",
                    model_requirements=ModelRequirements(
                        capabilities=ModelCapabilities(
                            streaming=StreamingMode.DELTA
                        )
                    ),
                    model_adapter=adapter,
                )
            )
            runner = Runner(
                registry=registry, store=store, telemetry_sink=sink
            )
            created = await runner.create_run(
                "credential-isolation", "1.0", input="ping"
            )
            updates = []

            async def collect() -> None:
                async for update in runner.subscribe_run(created.run_id):
                    updates.append(update)
                    if (
                        update.update_type is RunUpdateType.STATUS_CHANGED
                        and update.status is RunStatus.SUCCEEDED
                    ):
                        return

            collector = asyncio.create_task(collect())
            await asyncio.sleep(0)
            terminal = await runner.start_run(created.run_id)
            await collector
            inspection = await runner.inspect_run(created.run_id)
            await adapter.aclose()
            sink.close()
            store.close()
            return terminal, inspection, updates, adapter.requests

        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "run.db")
            telemetry_path = os.path.join(directory, "events.jsonl")
            with credential_environment():
                terminal, inspection, updates, requests = asyncio.run(
                    invoke(db_path, telemetry_path)
                )
            with open(db_path, "rb") as database:
                raw_database = database.read()
            with open(telemetry_path, "rb") as telemetry:
                raw_telemetry = telemetry.read()

        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        self.assertTrue(inspection.checkpoints)
        self.assertTrue(inspection.attempts)
        assert_no_credential(
            self,
            inspection.run.model_dump_json(),
            *(attempt.model_dump_json() for attempt in inspection.attempts),
            *(checkpoint.output for checkpoint in inspection.checkpoints),
            *(update.model_dump_json() for update in updates),
            str(requests),
        )
        self.assertNotIn(CREDENTIAL.encode(), raw_database)
        self.assertNotIn(CREDENTIAL.encode(), raw_telemetry)


def _request():
    from m_agent import ModelRequest

    return ModelRequest(input="hi", instructions="say hi")


async def _generate_no_credentials(adapter) -> None:
    from m_agent import ModelRequest

    request = ModelRequest(input="hi", instructions="say hi")
    try:
        await adapter.generate(request)
    finally:
        await adapter.aclose()


# ---------------------------------------------------------------------------
# live 部分（凭证门控；默认 CI 经 `-m "not live"` 排除）
# ---------------------------------------------------------------------------


class _LiveAdapterContractMixin:
    """共享 live 契约：凭证门控 + 公共 Runner seam。

    普通 mixin（非 TestCase），避免被 unittest / pytest 当作测试类
    收集；子类同时继承本 mixin 与 :class:`IsolatedAsyncioTestCase`。
    """

    adapter_cls = None
    failureException = LiveContractAssertionFailure

    def report_verified(self, *capabilities: str) -> None:
        """Emit non-sensitive evidence for a passed live capability case."""
        print(
            f"VERIFIED: adapter={type(self.adapter).__name__} "
            f"model={self.adapter.model} capabilities={','.join(capabilities)}"
        )

    def setUp(self) -> None:
        if self.adapter_cls is None:
            self.skipTest("abstract base class")
        status = live_contract_preflight()
        if status is not None:
            self.skipTest(live_contract_skip_reason(status))
        self.adapter = self.adapter_cls()

    async def asyncTearDown(self) -> None:
        if getattr(self, "adapter", None) is not None:
            await self.adapter.aclose()

    # -- 逐项能力契约案例（AC：验证 text completion 与每个声明能力） ----

    async def test_text_completion_and_usage_reporting(self) -> None:
        _, _, status, inspection = await run_to_terminal(
            self,
            self.adapter,
            instructions="Reply with the single word 'pong'.",
            required=ModelCapabilities(),
            input_text="ping",
        )
        self.assertEqual(status, RunStatus.SUCCEEDED)
        response = last_model_response(inspection)
        self.assertTrue(response.content and response.content.strip())
        # AC：usage reporting 是“返回时透传”；合法缺失保留为 None，
        # 不把无 usage 的真实 provider 响应误报成能力不匹配。
        if response.usage is None:
            self.assertIsNone(response.usage)
        else:
            self.assertIsInstance(response.usage, ModelUsage)
        self.report_verified("text_completion", "usage_reporting")

    async def test_streaming_deltas_through_runner(self) -> None:
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition.for_adapter(
                definition_id="live-contract",
                version="1.0",
                instructions="Say 'hello live streaming'.",
                model_requirements=ModelRequirements(
                    capabilities=ModelCapabilities(
                        streaming=StreamingMode.DELTA
                    )
                ),
                model_adapter=self.adapter,
            )
        )
        runner = Runner(
            registry=registry,
            store=InMemoryRunStore(payload_codec=PlaintextPayloadCodec()),
        )
        created = await runner.create_run(
            "live-contract", "1.0", input="ping"
        )
        deltas: list[str] = []

        async def collect() -> None:
            async for update in runner.subscribe_run(created.run_id):
                if update.update_type is RunUpdateType.MODEL_DELTA:
                    deltas.append(update.content or "")
                if (
                    update.update_type is RunUpdateType.STATUS_CHANGED
                    and update.status in (
                        RunStatus.SUCCEEDED,
                        RunStatus.FAILED,
                        RunStatus.REJECTED,
                        RunStatus.CANCELLED,
                    )
                ):
                    break

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)
        try:
            terminal = await runner.start_run(created.run_id)
        except ModelFailure as exc:
            raise LiveContractProviderFailure(
                f"{exc.code} ({exc.message})"
            )
        await collector

        if terminal.status is not RunStatus.SUCCEEDED:
            inspection = await runner.inspect_run(created.run_id)
            detail = "".join(a.error or "" for a in inspection.attempts)
            raise LiveContractProviderFailure(
                f"run ended {terminal.status}: {detail}"
            )
        self.assertTrue(
            deltas, "streaming adapter produced no MODEL_DELTA updates"
        )
        # 只有完整响应成为 checkpoint（ADR 0011）。
        inspection = await runner.inspect_run(created.run_id)
        response = last_model_response(inspection)
        streamed_text = "".join(deltas)
        self.assertEqual(streamed_text, response.content or "")
        self.assertTrue(response.content and response.content.strip())
        self.report_verified("streaming")

    async def test_tool_calling_through_runner(self) -> None:
        tool, calls = make_answer_tool()
        _, _, status, inspection = await run_to_terminal(
            self,
            self.adapter,
            instructions=TOOL_INSTRUCTIONS,
            required=ModelCapabilities(tool_calling=ToolCallingMode.NATIVE),
            tools=(tool,),
        )
        self.assertEqual(status, RunStatus.SUCCEEDED)
        self.assertGreater(
            len(calls), 0, "model did not call the declared tool"
        )
        tool_steps = [
            s for s in inspection.steps if s.step_type is StepType.TOOL
        ]
        self.assertTrue(
            tool_steps, "no TOOL step recorded through the public seam"
        )
        final = last_model_response(inspection)
        self.assertTrue(final.content and final.content.strip())
        self.report_verified("tool_calling")

    async def test_native_structured_output_through_runner(self) -> None:
        adapter = self.adapter_cls(
            structured_output_schema=STRUCTURED_SCHEMA,
        )
        try:
            _, _, status, inspection = await run_to_terminal(
                self,
                adapter,
                instructions=(
                    "Return a JSON object with an 'answer' string and a "
                    "'confidence' number between 0 and 1."
                ),
                required=ModelCapabilities(
                    structured_output=StructuredOutputMode.NATIVE
                ),
            )
        finally:
            await adapter.aclose()
        self.assertEqual(status, RunStatus.SUCCEEDED)
        response = last_model_response(inspection)
        payload = extract_json(response.content)
        self.assertEqual(set(payload), {"answer", "confidence"})
        self.assertIsInstance(payload["answer"], str)
        self.assertIsInstance(payload["confidence"], (int, float))
        self.assertNotIsInstance(payload["confidence"], bool)
        self.report_verified("native_structured_output")

@pytest.mark.live
class LiveChatCompletionsContractTests(
    _LiveAdapterContractMixin, unittest.IsolatedAsyncioTestCase
):
    """Chat Completions 风格 Adapter 的凭证门控契约测试。"""

    adapter_cls = ChatCompletionsModelAdapter


@pytest.mark.live
class LiveResponsesContractTests(
    _LiveAdapterContractMixin, unittest.IsolatedAsyncioTestCase
):
    """Responses 风格 Adapter 的凭证门控契约测试。"""

    adapter_cls = ResponsesModelAdapter


if __name__ == "__main__":
    unittest.main()
