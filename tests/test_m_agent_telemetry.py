"""Ticket 09 测试：Telemetry Sink 契约、本地 JSONL、payload 脱敏与失败隔离。

只通过公开 Runner 构造参数（``telemetry_sink``）驱动，断言外部可观测
的 Telemetry 事件与落盘 JSONL；验证：

- AC 1-3：sink 可在构造 Runner 时附加；事件带 run_id / step_id /
  attempt_id 关联、事件类型、耗时、状态、错误分类与可用 usage；
- AC 4：默认 telemetry 不含 Run input、模型内容、Context Item 内容、
  Tool Outcome 载荷、resolution 载荷与凭证（sentinel 泄密测试）；
- AC 5：从 JSONL 可重建成功与失败 Run 的可观测时序，但不是状态真相
  （RunStore 仍是唯一权威）；
- AC 6：Sink 失败被隔离，绝不覆盖或伪造 RunStore 状态；
- AC 7：本地 JSONL sink 无需 observability 服务器即可使用。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from m_agent.runtime import (
    AgentDefinition,
    ContextItem,
    DefinitionRegistry,
    FailureClassification,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ResolutionAction,
    RunResolution,
    Runner,
    RunStatus,
    StepStatus,
    StepType,
    TelemetryEvent,
    TelemetryEventType,
    ToolCall,
    ToolEffect,
    ToolFailure,
    ToolOutcome,
    ToolRequest,
)
from m_agent.adapters import (
    DeterministicContextProvider,
    DeterministicModelAdapter,
    DeterministicTool,
    InMemoryRunStore,
    JsonlTelemetrySink,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
    Runner,
    RunStatus,
)
from m_agent.runtime import ModelRequirements, ToolCallingMode, UsageProvenance

#: 穿过 Run input / 模型 / Context / Tool / resolution 路径的哨兵值，
#: 断言它们绝不出现在 telemetry JSONL 中。
SENTINEL_INPUT = "SENTINEL-RUN-INPUT-9f3a"
SENTINEL_MODEL = "SENTINEL-MODEL-CONTENT-7b21"
SENTINEL_CONTEXT = "SENTINEL-CONTEXT-ITEM-4c88"
SENTINEL_ARG = "SENTINEL-TOOL-ARG-5e77"
SENTINEL_TOOL = "SENTINEL-TOOL-RESULT-1d05"
SENTINEL_RESOLUTION = "SENTINEL-RESOLUTION-6a94"
SENTINEL_ERROR_CODE = "SENTINEL-ERROR-CODE-credential-payload-42a7"
SENTINEL_FAILURE_MESSAGE = "SENTINEL-FAILURE-MESSAGE-provider-payload-791e"

_ALL_SENTINELS = (
    SENTINEL_INPUT,
    SENTINEL_MODEL,
    SENTINEL_CONTEXT,
    SENTINEL_ARG,
    SENTINEL_TOOL,
    SENTINEL_RESOLUTION,
)

_TOOL_CALLING = ModelCapabilities(tool_calling=ToolCallingMode.NATIVE)


class CollectingSink:
    """测试用内存 sink：收集全部事件供断言。"""

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []

    def emit(self, event: TelemetryEvent) -> None:
        self.events.append(event)


class CollectingJsonlSink:
    """同时保留结构化事件并写入真实 JSONL 的测试 sink。"""

    def __init__(self, path: Path) -> None:
        self.events: list[TelemetryEvent] = []
        self._jsonl = JsonlTelemetrySink(path)

    def emit(self, event: TelemetryEvent) -> None:
        self.events.append(event)
        self._jsonl.emit(event)

    def close(self) -> None:
        self._jsonl.close()


class ExplodingSink:
    """始终抛异常的 sink：验证 Runner 的错误隔离（AC 6）。"""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error if error is not None else RuntimeError("sink down")
        self.calls = 0

    def emit(self, event: TelemetryEvent) -> None:
        self.calls += 1
        raise self._error


class UsageReportingAdapter(DeterministicModelAdapter):
    """携带 usage 的确定性模型：验证 usage 进入 STEP_COMPLETED。"""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        response = await super().generate(request)
        return ModelResponse(
            content=response.content,
            usage=ModelUsage(
                input_tokens=11,
                output_tokens=7,
                provenance=UsageProvenance.RUNTIME_SIZED,
            ),
        )


class ToolCallingFinalModel(DeterministicModelAdapter):
    """第一次请求工具，收到 outcome 后给出含 sentinel 的最终响应。"""

    def __init__(
        self,
        tool_arguments: str,
        final_content: str,
        tool_name: str = "lookup",
    ) -> None:
        super().__init__(capabilities=_TOOL_CALLING)
        self._tool_arguments = tool_arguments
        self._final_content = final_content
        self._tool_name = tool_name
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        if not request.tool_outcomes:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="call-1",
                        tool_name=self._tool_name,
                        arguments=self._tool_arguments,
                    ),
                )
            )
        return ModelResponse(content=self._final_content)


class ReturningTool(DeterministicTool):
    """READ_ONLY 工具：返回含 sentinel 的 SUCCESS outcome。"""

    def __init__(self, result_text: str) -> None:
        super().__init__(name="lookup", effect=ToolEffect.READ_ONLY)
        self._result_text = result_text

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        return ToolOutcome.success(
            request.call_id, self.name, result=self._result_text
        )


class BrokenTool(DeterministicTool):
    """READ_ONLY 工具：抛 PERMANENT 结构化失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(name="lookup", effect=ToolEffect.READ_ONLY)
        self._code = code
        self._message = message

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        raise ToolFailure(
            FailureClassification.PERMANENT, self._code, self._message
        )


class UncertainNotifier(DeterministicTool):
    """NON_IDEMPOTENT 工具：执行后抛 UNCERTAIN（进入 WAITING）。"""

    def __init__(self) -> None:
        super().__init__(name="notify", effect=ToolEffect.NON_IDEMPOTENT)

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        raise ToolFailure(
            FailureClassification.UNCERTAIN,
            "effect_unconfirmed",
            "delivery outcome is unknown",
        )


def build_registry(
    *,
    model: DeterministicModelAdapter,
    tools: tuple[DeterministicTool, ...] = (),
    context_provider: DeterministicContextProvider | None = None,
) -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id="telemetry_agent",
            version="1.0",
            instructions="Answer deterministically.",
            model_requirements=ModelRequirements(capabilities=model.capabilities),
            model_adapter=model,
            tools=tools,
            context_provider=context_provider,
        )
    )
    return registry


def make_store() -> InMemoryRunStore:
    return InMemoryRunStore(payload_codec=PlaintextPayloadCodec())


def read_jsonl(path: Path) -> list[dict]:
    """逐行读取 JSONL 文件并解析（行序即事件序）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def managed_jsonl_sink(
    test_case: unittest.TestCase, path: Path
) -> JsonlTelemetrySink:
    """Give each JSONL test deterministic, exception-safe ownership."""
    sink = JsonlTelemetrySink(path)
    test_case.addCleanup(sink.close)
    return sink


class TelemetryEventContractTests(unittest.IsolatedAsyncioTestCase):
    """AC 1-3：sink 附加、关联标识、事件类型 / 耗时 / 状态 / usage。"""

    async def test_sink_attached_at_runner_construction(self) -> None:
        # AC 1：构造 Runner 时附加 Telemetry Sink。
        sink = CollectingSink()
        registry = build_registry(model=DeterministicModelAdapter())
        runner = Runner(
            registry=registry, store=make_store(), telemetry_sink=sink
        )
        created = await runner.create_run("telemetry_agent", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        self.assertGreater(len(sink.events), 0)
        self.assertTrue(
            all(isinstance(e, TelemetryEvent) for e in sink.events)
        )

    async def test_successful_run_emits_correlated_events(self) -> None:
        # AC 2/3：简单成功 Run 发射 RUN_STATUS_CHANGED + STEP 事件，
        # run_id / step_id / attempt_id 关联一致，完成事件带耗时。
        sink = CollectingSink()
        registry = build_registry(model=DeterministicModelAdapter())
        runner = Runner(
            registry=registry, store=make_store(), telemetry_sink=sink
        )
        created = await runner.create_run("telemetry_agent", "1.0", input="hi")
        await runner.start_run(created.run_id)

        statuses = [
            e.run_status
            for e in sink.events
            if e.event_type is TelemetryEventType.RUN_STATUS_CHANGED
        ]
        self.assertEqual(
            statuses, [RunStatus.CREATED, RunStatus.RUNNING, RunStatus.SUCCEEDED]
        )
        step_events = [
            e
            for e in sink.events
            if e.event_type
            in (TelemetryEventType.STEP_STARTED, TelemetryEventType.STEP_COMPLETED)
        ]
        started = [e for e in step_events if e.event_type is TelemetryEventType.STEP_STARTED]
        completed = [
            e for e in step_events if e.event_type is TelemetryEventType.STEP_COMPLETED
        ]
        self.assertEqual(len(started), 1)
        self.assertEqual(len(completed), 1)
        for event in sink.events:
            self.assertEqual(event.run_id, created.run_id)
        self.assertEqual(started[0].step_id, completed[0].step_id)
        self.assertEqual(started[0].attempt_id, completed[0].attempt_id)
        self.assertEqual(completed[0].step_type, StepType.MODEL)
        self.assertEqual(completed[0].step_status, StepStatus.SUCCEEDED)
        self.assertIsNotNone(completed[0].duration_ms)
        self.assertGreaterEqual(completed[0].duration_ms, 0)

    async def test_context_and_tool_steps_correlate_ids(self) -> None:
        # AC 2：Context / Model / Tool Step 的 STARTED -> COMPLETED 事件
        # 携带一致的 step_id / attempt_id，全部关联同一 run_id。
        sink = CollectingSink()
        context = DeterministicContextProvider(
            [ContextItem(item_id="i1", content="context", source="s1")]
        )
        model = ToolCallingFinalModel(
            tool_arguments='{"q":"x"}', final_content="done"
        )
        tool = ReturningTool(result_text="result")
        registry = build_registry(
            model=model, tools=(tool,), context_provider=context
        )
        runner = Runner(
            registry=registry, store=make_store(), telemetry_sink=sink
        )
        created = await runner.create_run("telemetry_agent", "1.0", input="hi")
        await runner.start_run(created.run_id)

        by_step: dict[str, dict] = {}
        for e in sink.events:
            if e.step_id is None:
                continue
            by_step.setdefault(e.step_id, {})[e.event_type] = e
        # Context、Model、Tool 三个 Step 各自成对出现。
        self.assertEqual(
            sorted(by_step.keys()),
            sorted(
                e.step_id
                for e in sink.events
                if e.event_type is TelemetryEventType.STEP_STARTED
            ),
        )
        for step_id, events in by_step.items():
            self.assertIn(TelemetryEventType.STEP_STARTED, events)
            self.assertIn(TelemetryEventType.STEP_COMPLETED, events)
            started = events[TelemetryEventType.STEP_STARTED]
            completed = events[TelemetryEventType.STEP_COMPLETED]
            self.assertEqual(started.attempt_id, completed.attempt_id)
            self.assertEqual(started.step_type, completed.step_type)
            self.assertIsNotNone(completed.duration_ms)
        step_types = {e.step_type for e in sink.events if e.step_type}
        self.assertEqual(
            step_types, {StepType.CONTEXT, StepType.MODEL, StepType.TOOL}
        )

    async def test_model_completion_carries_usage_when_supplied(self) -> None:
        # AC 3：Adapter 提供 usage 时，MODEL STEP_COMPLETED 携带 usage。
        sink = CollectingSink()
        registry = build_registry(model=UsageReportingAdapter())
        runner = Runner(
            registry=registry, store=make_store(), telemetry_sink=sink
        )
        created = await runner.create_run("telemetry_agent", "1.0", input="hi")
        await runner.start_run(created.run_id)
        completed = [
            e
            for e in sink.events
            if e.event_type is TelemetryEventType.STEP_COMPLETED
            and e.step_type is StepType.MODEL
        ]
        self.assertEqual(len(completed), 1)
        self.assertIsNotNone(completed[0].usage)
        self.assertEqual(completed[0].usage.input_tokens, 11)
        self.assertEqual(completed[0].usage.output_tokens, 7)

    async def test_failed_run_emits_classified_error_metadata(self) -> None:
        # AC 3：失败 Run 发射 ATTEMPT_FAILED（分类 + 错误码 + 耗时）与
        # FAILED 状态事件，且没有该 Tool Step 的 COMPLETED 事件。
        sink = CollectingSink()
        model = ToolCallingFinalModel(
            tool_arguments="{}", final_content="never reached"
        )
        tool = BrokenTool(code="lookup_broken", message="backend unavailable")
        registry = build_registry(model=model, tools=(tool,))
        runner = Runner(
            registry=registry, store=make_store(), telemetry_sink=sink
        )
        created = await runner.create_run("telemetry_agent", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)
        self.assertEqual(terminal.status, RunStatus.FAILED)

        failed = [
            e
            for e in sink.events
            if e.event_type is TelemetryEventType.ATTEMPT_FAILED
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].step_type, StepType.TOOL)
        self.assertEqual(failed[0].step_status, StepStatus.FAILED)
        self.assertEqual(
            failed[0].classification, FailureClassification.PERMANENT
        )
        self.assertEqual(failed[0].error_code, "lookup_broken")
        self.assertIsNotNone(failed[0].duration_ms)
        self.assertGreaterEqual(failed[0].duration_ms, 0)
        # 失败 Step 没有 COMPLETED 事件。
        self.assertFalse(
            any(
                e.event_type is TelemetryEventType.STEP_COMPLETED
                and e.step_id == failed[0].step_id
                for e in sink.events
            )
        )
        statuses = [
            e.run_status
            for e in sink.events
            if e.event_type is TelemetryEventType.RUN_STATUS_CHANGED
        ]
        self.assertEqual(statuses[-1], RunStatus.FAILED)


class JsonlRedactionTests(unittest.IsolatedAsyncioTestCase):
    """AC 4/7：本地 JSONL 逐行可解析、默认不含任何 payload / 凭证。"""

    async def test_jsonl_redacts_input_model_context_tool_secrets(self) -> None:
        # sentinel 穿过 Run input、模型内容、Context Item、Tool 参数与
        # 结果路径；落盘 JSONL 逐行可解析且不含任何 sentinel。
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = Path(tmp) / "telemetry.jsonl"
            context = DeterministicContextProvider(
                [
                    ContextItem(
                        item_id="i1",
                        content=f"context {SENTINEL_CONTEXT}",
                        source="s1",
                    )
                ]
            )
            model = ToolCallingFinalModel(
                tool_arguments=json.dumps(
                    {"api_key": SENTINEL_ARG}  # 模拟凭证穿过工具路径
                ),
                final_content=f"final {SENTINEL_MODEL}",
            )
            tool = ReturningTool(result_text=f"result {SENTINEL_TOOL}")
            registry = build_registry(
                model=model, tools=(tool,), context_provider=context
            )
            runner = Runner(
                registry=registry,
                store=make_store(),
                telemetry_sink=managed_jsonl_sink(self, jsonl_path),
            )
            created = await runner.create_run(
                "telemetry_agent", "1.0", input=f"input {SENTINEL_INPUT}"
            )
            terminal = await runner.start_run(created.run_id)
            self.assertEqual(terminal.status, RunStatus.SUCCEEDED)

            lines = read_jsonl(jsonl_path)
            self.assertGreaterEqual(len(lines), 4)
            # 每行都有 run_id 且关联同一 Run。
            for line in lines:
                self.assertEqual(line["run_id"], created.run_id)
            text = jsonl_path.read_text(encoding="utf-8")
            for sentinel in _ALL_SENTINELS:
                self.assertNotIn(
                    sentinel, text, f"payload {sentinel!r} leaked to JSONL"
                )

    async def test_public_runner_masks_unsafe_adapter_error_code_everywhere(
        self,
    ) -> None:
        """Adapter code 是非可信输入，不能进入 metadata 或 telemetry。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.sqlite"
            jsonl_path = Path(tmp) / "telemetry.jsonl"
            store = SQLiteRunStore(db_path, PlaintextPayloadCodec())
            sink = CollectingJsonlSink(jsonl_path)
            self.addCleanup(sink.close)
            self.addCleanup(store.close)
            model = ToolCallingFinalModel(
                tool_arguments="{}", final_content="never reached"
            )
            tool = BrokenTool(
                code=SENTINEL_ERROR_CODE,
                message=SENTINEL_FAILURE_MESSAGE,
            )
            runner = Runner(
                registry=build_registry(model=model, tools=(tool,)),
                store=store,
                telemetry_sink=sink,
            )
            created = await runner.create_run("telemetry_agent", "1.0", input="hi")
            terminal = await runner.start_run(created.run_id)
            self.assertEqual(terminal.status, RunStatus.FAILED)

            failed_events = [
                event
                for event in sink.events
                if event.event_type is TelemetryEventType.ATTEMPT_FAILED
            ]
            self.assertEqual(len(failed_events), 1)
            self.assertNotEqual(failed_events[0].error_code, SENTINEL_ERROR_CODE)
            self.assertEqual(
                failed_events[0].classification, FailureClassification.PERMANENT
            )
            self.assertIsNotNone(failed_events[0].duration_ms)

            inspection = await runner.inspect_run(created.run_id)
            failed_attempt = next(
                attempt
                for attempt in inspection.attempts
                if attempt.status is StepStatus.FAILED
            )
            self.assertNotEqual(failed_attempt.error_code, SENTINEL_ERROR_CODE)
            self.assertNotIn(SENTINEL_FAILURE_MESSAGE, failed_attempt.error or "")
            self.assertEqual(
                failed_attempt.classification, FailureClassification.PERMANENT
            )

            sink.close()
            self.assertNotIn(
                SENTINEL_ERROR_CODE, jsonl_path.read_text(encoding="utf-8")
            )
            self.assertNotIn(
                SENTINEL_FAILURE_MESSAGE,
                jsonl_path.read_text(encoding="utf-8"),
            )
            with sqlite3.connect(db_path) as connection:
                metadata = connection.execute(
                    "SELECT error_code FROM step_attempts"
                ).fetchall()
                payloads = connection.execute(
                    "SELECT encoded FROM run_payloads"
                ).fetchall()
            self.assertNotIn(
                SENTINEL_ERROR_CODE,
                "\n".join(row[0] or "" for row in metadata),
            )
            self.assertNotIn(
                SENTINEL_ERROR_CODE.encode(),
                b"".join(bytes(row[0]) for row in payloads),
            )
            self.assertNotIn(
                SENTINEL_FAILURE_MESSAGE.encode(),
                b"".join(bytes(row[0]) for row in payloads),
            )

    async def test_jsonl_redacts_resolution_payload(self) -> None:
        # sentinel 穿过 resolution（CONFIRM_STEP result）路径；JSONL
        # 仍不含该值，且 WAITING -> RUNNING -> SUCCEEDED 状态可观测。
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = Path(tmp) / "telemetry.jsonl"
            model = ToolCallingFinalModel(
                tool_arguments="{}",
                final_content="done",
                tool_name="notify",
            )
            tool = UncertainNotifier()
            registry = build_registry(model=model, tools=(tool,))
            runner = Runner(
                registry=registry,
                store=make_store(),
                telemetry_sink=managed_jsonl_sink(self, jsonl_path),
            )
            created = await runner.create_run(
                "telemetry_agent", "1.0", input=f"input {SENTINEL_INPUT}"
            )
            waiting = await runner.start_run(created.run_id)
            self.assertEqual(waiting.status, RunStatus.WAITING)
            terminal = await runner.resolve_run(
                created.run_id,
                RunResolution(
                    action=ResolutionAction.CONFIRM_STEP,
                    result=f"confirmed {SENTINEL_RESOLUTION}",
                    waiting_step_id=waiting.waiting_step_id,
                ),
                expected_version=waiting.version,
            )
            self.assertEqual(terminal.status, RunStatus.SUCCEEDED)

            text = jsonl_path.read_text(encoding="utf-8")
            self.assertNotIn(SENTINEL_RESOLUTION, text)
            self.assertNotIn(SENTINEL_INPUT, text)
            lines = read_jsonl(jsonl_path)
            statuses = [
                line["run_status"]
                for line in lines
                if line["event_type"] == "RUN_STATUS_CHANGED"
            ]
            self.assertEqual(
                statuses,
                ["CREATED", "RUNNING", "WAITING", "RUNNING", "SUCCEEDED"],
            )

    async def test_jsonl_is_line_parseable_without_server(self) -> None:
        # AC 3/7：JSONL 每一行都是独立可解析的 JSON 对象，字段集稳定；
        # 本地文件检查即可，无 observability 服务器 / Dashboard。
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = Path(tmp) / "telemetry.jsonl"
            registry = build_registry(model=DeterministicModelAdapter())
            runner = Runner(
                registry=registry,
                store=make_store(),
                telemetry_sink=managed_jsonl_sink(self, jsonl_path),
            )
            created = await runner.create_run("telemetry_agent", "1.0", input="hi")
            await runner.start_run(created.run_id)

            lines = jsonl_path.read_text(encoding="utf-8").splitlines()
            self.assertGreater(len(lines), 0)
            for line in lines:
                obj = json.loads(line)  # 逐行独立解析
                self.assertEqual(obj["run_id"], created.run_id)
                self.assertIn("event_type", obj)
                self.assertIn("created_at", obj)


class SinkFailureIsolationTests(unittest.IsolatedAsyncioTestCase):
    """AC 6：Sink 失败不能覆盖或伪造 RunStore 权威状态。"""

    async def test_exploding_sink_does_not_affect_runstore(self) -> None:
        sink = ExplodingSink()
        observed_errors: list[Exception] = []
        registry = build_registry(model=DeterministicModelAdapter())
        store = make_store()
        runner = Runner(
            registry=registry,
            store=store,
            telemetry_sink=sink,
            telemetry_error_callback=observed_errors.append,
        )
        created = await runner.create_run("telemetry_agent", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)
        # Run 正常到达终态，权威记录未被 Sink 故障污染。
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
        self.assertGreater(sink.calls, 0)
        self.assertGreater(len(observed_errors), 0)
        inspection = await runner.inspect_run(created.run_id)
        self.assertEqual(inspection.run.status, RunStatus.SUCCEEDED)
        self.assertEqual(
            [s.status for s in inspection.steps], [StepStatus.SUCCEEDED]
        )
        self.assertEqual(len(inspection.attempts), 1)
        self.assertEqual(len(inspection.checkpoints), 1)

    async def test_error_callback_exception_is_also_contained(self) -> None:
        # 隔离层自身（error callback）抛异常同样被吞掉，Run 继续成功。
        sink = ExplodingSink()

        def bad_callback(exc: Exception) -> None:
            raise ValueError("callback broken")

        registry = build_registry(model=DeterministicModelAdapter())
        runner = Runner(
            registry=registry,
            store=make_store(),
            telemetry_sink=sink,
            telemetry_error_callback=bad_callback,
        )
        created = await runner.create_run("telemetry_agent", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)

    async def test_sink_failure_cannot_fake_terminal_status(self) -> None:
        # 失败 Run：Sink 故障不改变权威 FAILED 状态（不伪造成功）。
        sink = ExplodingSink()
        model = ToolCallingFinalModel(
            tool_arguments="{}", final_content="never reached"
        )
        tool = BrokenTool(code="lookup_broken", message="backend unavailable")
        registry = build_registry(model=model, tools=(tool,))
        runner = Runner(
            registry=registry, store=make_store(), telemetry_sink=sink
        )
        created = await runner.create_run("telemetry_agent", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)
        self.assertEqual(terminal.status, RunStatus.FAILED)


class JsonlTelemetrySinkLifecycleTests(unittest.TestCase):
    """本地 JSONL 文件由调用方显式关闭，且 close 可安全重复调用。"""

    def test_context_manager_flushes_and_close_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = Path(tmp) / "telemetry.jsonl"
            with JsonlTelemetrySink(jsonl_path) as sink:
                sink.emit(
                    TelemetryEvent(
                        event_type=TelemetryEventType.RUN_STATUS_CHANGED,
                        run_id="lifecycle-run",
                        run_status=RunStatus.CREATED,
                    )
                )
            sink.close()
            sink.close()
            self.assertEqual(
                read_jsonl(jsonl_path)[0]["run_id"], "lifecycle-run"
            )


class TelemetryReconstructionTests(unittest.IsolatedAsyncioTestCase):
    """AC 5：从 JSONL 重建成功与失败 Run 的可观测时序（非状态真相）。"""

    async def test_successful_run_sequence_reconstructable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = Path(tmp) / "telemetry.jsonl"
            registry = build_registry(model=DeterministicModelAdapter())
            runner = Runner(
                registry=registry,
                store=make_store(),
                telemetry_sink=managed_jsonl_sink(self, jsonl_path),
            )
            created = await runner.create_run("telemetry_agent", "1.0", input="hi")
            await runner.start_run(created.run_id)

            lines = read_jsonl(jsonl_path)
            sequence = [line["event_type"] for line in lines]
            # 状态转换与 Step 事件按可观测顺序出现。
            self.assertIn("RUN_STATUS_CHANGED", sequence)
            self.assertIn("STEP_STARTED", sequence)
            self.assertIn("STEP_COMPLETED", sequence)
            status_seq = [
                line["run_status"]
                for line in lines
                if line["event_type"] == "RUN_STATUS_CHANGED"
            ]
            self.assertEqual(
                status_seq, ["CREATED", "RUNNING", "SUCCEEDED"]
            )
            # 事件序列足以重建时序，但终态判断必须来自 RunStore：
            # telemetry 中只有"事件"，没有 input/output payload。
            for line in lines:
                self.assertNotIn("input", line)
                self.assertNotIn("output", line)
                self.assertNotIn("content", line)

    async def test_failed_run_sequence_reconstructable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = Path(tmp) / "telemetry.jsonl"
            model = ToolCallingFinalModel(
                tool_arguments="{}", final_content="never reached"
            )
            tool = BrokenTool(code="lookup_broken", message="backend unavailable")
            registry = build_registry(model=model, tools=(tool,))
            runner = Runner(
                registry=registry,
                store=make_store(),
                telemetry_sink=managed_jsonl_sink(self, jsonl_path),
            )
            created = await runner.create_run("telemetry_agent", "1.0", input="hi")
            terminal = await runner.start_run(created.run_id)
            self.assertEqual(terminal.status, RunStatus.FAILED)

            lines = read_jsonl(jsonl_path)
            status_seq = [
                line["run_status"]
                for line in lines
                if line["event_type"] == "RUN_STATUS_CHANGED"
            ]
            self.assertEqual(
                status_seq, ["CREATED", "RUNNING", "FAILED"]
            )
            failed = [
                line
                for line in lines
                if line["event_type"] == "ATTEMPT_FAILED"
            ]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["classification"], "PERMANENT")
            self.assertEqual(failed[0]["error_code"], "lookup_broken")
            # 权威状态来自 RunStore（telemetry 非真相源）。
            authoritative = await runner.get_run(created.run_id)
            self.assertEqual(authoritative.status, RunStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
