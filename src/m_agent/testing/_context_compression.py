"""Offline context-budget-compression scenario.

ADR 0040 / ADR 0042：显式 Semantic Compression 的完整离线
证明。Scenario 只用公开 seam（``Runner`` / ``DefinitionRegistry`` /
``inspect_run`` / 公开 Context 契约类型 / ``InMemoryRunStore`` /
``PayloadCodec``）驱动七个运行：

- **plan order / scopes**：冻结 Plan 的 RUN_INPUT Stage 先执行、显式
  ``CONTEXT_COMPRESSION`` Model Step 居中、业务 Model Step 收尾，
  checkpoint 顺序与 Plan 声明一致；MODEL_STEP Scope Stage 绝不为
  compression 步骤额外触发；
- **Stage/Frame checkpoint 与恢复复用**：原始 Items 保留在 Stage
  checkpoint、派生 Items 携带契约 provenance；compression checkpoint
  落盘后崩溃恢复零重算，外部数据源零重读（stale source 不改写已冻结
  的 Run 上下文）；
- **完整 sizing 与零 dispatch**：压缩请求自身通过独立 binding 的完整
  硬预算检查；无可压缩 Item 时零 compression dispatch；压缩后业务
  完整请求仍超预算时以 ``CONTEXT_BUDGET_EXCEEDED`` fail closed 且零
  业务 dispatch——完整 sizing 的公开重算与运行时判定一致，一个故意
  低估的 sizer 会与权威事实矛盾并被对账检出；
- **protected channels**：受保护来源 Item 原样传递、Conversation
  History / run input / instructions 永不进入压缩请求（canary 验证）；
- **独立 compression 与 no-recursion**：``CONTEXT_COMPRESSION``
  purpose 的独立 attempt / usage 记账；compression 请求业务 Tools 时
  fail closed 且零业务 dispatch。

独立 sentinel（记录每次请求 / 读取的 adapter 与 provider，不经过
Run Store）与公开 Run View 双源对账：
``reconcile_compression_observation`` 检出 stale source、tampered
provenance、under-counting sizer 与 recursion 四类受控变异。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from typing import Any, Mapping, Sequence

from ..adapters import (
    DeterministicContextProvider,
    DeterministicModelAdapter,
    FakeClock,
    InMemoryRunStore,
    PlaintextPayloadCodec,
)
from ..runtime import (
    ERROR_CONTEXT_BUDGET_EXCEEDED,
    AgentDefinition,
    CompressionContract,
    CompressionResult,
    ContextBudget,
    ContextFrame,
    ContextItem,
    ContextPlan,
    ContextScope,
    ContextStage,
    ContextStageIdentity,
    ContextTransformType,
    ConversationMessage,
    ConversationRole,
    CrashPoint,
    DefinitionRegistry,
    FrameSizingResult,
    ModelBinding,
    ModelBindingSet,
    ModelCapabilities,
    ModelCapabilityCombination,
    ModelContract,
    ModelExecutionBudget,
    ModelInputSizer,
    ModelLimits,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    PayloadCodec,
    RevisionStability,
    RetryPolicy,
    Runner,
    RunStatus,
    StepType,
    Tool,
    ToolCallingMode,
    ToolEffect,
    ToolOutcome,
    UsageReportingMode,
    check_frame_budget,
    compression_step_id,
    parse_stage_result,
)

from ._pack import AcceptanceCheckResult, AcceptanceCheckStatus, EvidenceLevel

ALLOWED_SOURCE = "docs:articles"
PROTECTED_SOURCE = "internal:protected"
RUN_INPUT_CANARY = "RUN-INPUT-SECRET-CANARY-T15"
HISTORY_CANARY = "HISTORY-SECRET-CANARY-T15"

COMPRESSION_CONTRACT = CompressionContract(
    contract_id="scenario-summarize",
    version="1",
    instructions="Summarize the allowed articles and retain key facts only.",
    allowed_sources=(ALLOWED_SOURCE,),
    retained_categories=("facts", "entities"),
    omitted_categories=("verbatim-text",),
    derived_categories=("summary",),
    max_output_items=1,
)
COMPRESSION_STEP = compression_step_id(COMPRESSION_CONTRACT)


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _item(item_id: str, content: str, source: str = ALLOWED_SOURCE) -> ContextItem:
    return ContextItem(item_id=item_id, content=content, source=source)


def _model_contract(
    contract_id: str,
    *,
    context_window_tokens: int = 1_000_000,
    usage_reporting: bool = False,
    tool_calling: bool = False,
) -> ModelContract:
    tool_mode = ToolCallingMode.NATIVE if tool_calling else ToolCallingMode.NONE
    capabilities = ModelCapabilities(tool_calling=tool_mode)
    if usage_reporting:
        capabilities = ModelCapabilities(
            tool_calling=tool_mode,
            usage_reporting=UsageReportingMode.PROVIDER_REPORTED,
            supported_combinations=(
                ModelCapabilityCombination(
                    tool_calling=tool_mode,
                    usage_reporting=UsageReportingMode.PROVIDER_REPORTED,
                ),
            ),
        )
    return ModelContract(
        contract_id=contract_id,
        version="1",
        revision_stability=RevisionStability.PINNED,
        model_identity=f"deterministic:{contract_id}",
        capabilities=capabilities,
        limits=ModelLimits(
            context_window_tokens=context_window_tokens,
            max_output_tokens=100_000,
        ),
        input_sizer_id="deterministic-v1",
        serialization_id="deterministic-text-v1",
    )


def _bindings(
    primary: ModelContract, compression: ModelContract
) -> ModelBindingSet:
    base = ModelBinding(purpose=ModelPurpose.PRIMARY, contract=primary)
    return ModelBindingSet(
        bindings=(
            base,
            ModelBinding(
                purpose=ModelPurpose.CONTEXT_COMPRESSION,
                contract=compression,
            ),
            base.model_copy(
                update={
                    "purpose": ModelPurpose.OUTPUT_REPAIR,
                    "source_purpose": ModelPurpose.PRIMARY,
                }
            ),
        )
    )


def _run_input_plan() -> ContextPlan:
    return ContextPlan(
        stages=(
            ContextStage(
                identity=ContextStageIdentity(
                    stage_id="scenario-provide",
                    scope=ContextScope.RUN_INPUT,
                    transform_type=ContextTransformType.PROVIDE,
                )
            ),
        )
    )


def _model_step_plan() -> ContextPlan:
    return ContextPlan(
        stages=(
            ContextStage(
                identity=ContextStageIdentity(
                    stage_id="scenario-provide",
                    scope=ContextScope.RUN_INPUT,
                    transform_type=ContextTransformType.PROVIDE,
                )
            ),
            ContextStage(
                identity=ContextStageIdentity(
                    stage_id="scenario-model-step",
                    scope=ContextScope.MODEL_STEP,
                    transform_type=ContextTransformType.PROVIDE,
                )
            ),
        )
    )


class SentinelCompressionAdapter(DeterministicModelAdapter):
    """Scenario sentinel：记录每次 compression 请求，可注入故障。

    请求快照只存在于本对象（不经过 Run Store），供双源对账。
    """

    deterministic: bool = True

    def __init__(
        self,
        *,
        model_contract: ModelContract | None = None,
        raw_output: str | None = None,
        tool_calls: tuple = (),
        usage: ModelUsage | None = None,
    ) -> None:
        super().__init__(responses=("",), model_contract=model_contract)
        self._raw_output = raw_output
        self._tool_calls = tuple(tool_calls)
        self._usage = usage
        self.requests: list[ModelRequest] = []

    def _fingerprint_excluded_state(self) -> frozenset[str]:
        return frozenset({"_last_request", "call_count", "requests"})

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        if self._tool_calls:
            return ModelResponse(tool_calls=self._tool_calls)
        if self._raw_output is not None:
            return ModelResponse(content=self._raw_output)
        payload = {
            "items": [
                {
                    "item_id": "scenario-summary-1",
                    "content": "condensed scenario summary",
                    "source_item_ids": [
                        item.item_id for item in request.context_items
                    ],
                }
            ]
        }
        return ModelResponse(content=json.dumps(payload), usage=self._usage)


class SentinelBusinessAdapter(DeterministicModelAdapter):
    """Scenario sentinel：记录每次业务请求；首轮可选请求工具。"""

    deterministic: bool = True

    def __init__(
        self,
        *,
        model_contract: ModelContract | None = None,
        tool_calls: tuple = (),
    ) -> None:
        super().__init__(
            responses=("scenario final answer",),
            model_contract=model_contract,
        )
        self._tool_calls = tuple(tool_calls)
        self.requests: list[ModelRequest] = []

    def _fingerprint_excluded_state(self) -> frozenset[str]:
        return frozenset({"_last_request", "call_count", "requests"})

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        if self._tool_calls and self.call_count == 1:
            return ModelResponse(tool_calls=self._tool_calls)
        return ModelResponse(content="scenario final answer")


class MutableProvider(DeterministicContextProvider):
    """可替换内容的外部数据源 sentinel（stale source 变异载体）。"""

    def __init__(self, items: Sequence[ContextItem]) -> None:
        super().__init__(items)
        self._mutable_items: tuple[ContextItem, ...] = tuple(items)

    def replace_items(self, items: Sequence[ContextItem]) -> None:
        self._mutable_items = tuple(items)

    async def provide(self, request):  # type: ignore[no-untyped-def]
        self.call_count += 1
        return list(self._mutable_items)


class TamperingCodec(PayloadCodec):
    """decode 边界篡改 compression checkpoint 的 provenance。

    ``decode`` 是 Store 读取持久化载荷的公共边界：检测到 Compression
    Result envelope（按冻结契约身份识别）时向 ``source_item_ids`` 注入
    phantom 引用——恢复路径必须以 ``COMPRESSION_CONTRACT_VIOLATION``
    fail closed，绝不静默接受被篡改的 provenance。
    """

    name = "scenario-tampering"

    def __init__(self) -> None:
        self.tampered_payloads: list[str] = []

    def encode(self, payload: str) -> bytes:
        return ("scenario-plain:" + payload).encode("utf-8")

    def decode(self, encoded: bytes) -> str:
        text = encoded.decode("utf-8")
        if not text.startswith("scenario-plain:"):
            raise ValueError("payload does not use the scenario codec")
        body = text[len("scenario-plain:") :]
        try:
            envelope = json.loads(body)
        except ValueError:
            return body
        if not isinstance(envelope, dict) or "content" not in envelope:
            return body
        content = envelope["content"]
        if not isinstance(content, str):
            return body
        try:
            result = CompressionResult.deserialize(content)
        except Exception:
            return body
        if result.contract_id != COMPRESSION_CONTRACT.contract_id:
            return body
        tampered = result.model_copy(
            update={"source_item_ids": (*result.source_item_ids, "phantom-item")}
        )
        self.tampered_payloads.append(COMPRESSION_STEP)
        envelope["content"] = tampered.serialize()
        return json.dumps(envelope)


class ScenarioEchoTool(Tool):
    """READ_ONLY sentinel 工具（no-recursion 运行的业务侧工具）。"""

    def __init__(self) -> None:
        super().__init__(
            name="scenario_echo",
            description="Echo back.",
            effect=ToolEffect.READ_ONLY,
        )

    async def invoke(self, request):  # type: ignore[no-untyped-def]
        return ToolOutcome.success(
            request.call_id, self.name, result="scenario tool result"
        )


def _base_items() -> tuple[ContextItem, ...]:
    return (
        _item("doc-1", "first scenario article " * 24),
        _item("doc-2", "second scenario article " * 24),
        _item("prot-1", "protected scenario evidence " * 6, PROTECTED_SOURCE),
    )


def _history() -> tuple[ConversationMessage, ...]:
    return (
        ConversationMessage(
            role=ConversationRole.USER, content=f"prior turn {HISTORY_CANARY}"
        ),
        ConversationMessage(
            role=ConversationRole.ASSISTANT, content="prior scenario reply"
        ),
    )


def _make_definition(
    *,
    compression_adapter: SentinelCompressionAdapter,
    business_adapter: SentinelBusinessAdapter,
    provider: DeterministicContextProvider,
    plan: ContextPlan,
    model_execution_budget: ModelExecutionBudget | None = None,
    tools: tuple[Tool, ...] = (),
) -> AgentDefinition:
    kwargs: dict[str, Any] = dict(
        definition_id="scenario-compression-agent",
        version="1.0",
        instructions="Answer using the provided scenario context.",
        model_bindings=_bindings(
            business_adapter.model_contract
            or _model_contract("scenario-business"),
            compression_adapter.model_contract
            or _model_contract("scenario-compression"),
        ),
        model_execution_budget=(
            model_execution_budget or ModelExecutionBudget()
        ),
        model_adapter=business_adapter,
        model_adapters={ModelPurpose.CONTEXT_COMPRESSION: compression_adapter},
        context_provider=provider,
        compression_contract=COMPRESSION_CONTRACT,
        context_plan=plan,
    )
    if tools:
        kwargs["tools"] = tools
    return AgentDefinition(**kwargs)


def _first_crash_hook(point: CrashPoint):
    """目标崩溃点首次触发时注入崩溃（恢复 Runner 不带 hook）。"""
    fired: list[CrashPoint] = []

    def hook(candidate: CrashPoint, run_id: str) -> None:
        if candidate is point and candidate not in fired:
            fired.append(candidate)
            raise RuntimeError("scenario injected crash")

    return hook


async def _checkpoint_labels(
    runner: Runner, run_id: str
) -> list[tuple[str, str]]:
    """公开 Run View：checkpoint 序列的 (step_type, purpose) 标签。"""
    inspection = await runner.inspect_run(run_id)
    attempts = {attempt.attempt_id: attempt for attempt in inspection.attempts}
    labels: list[tuple[str, str]] = []
    for checkpoint in inspection.checkpoints:
        attempt = attempts.get(checkpoint.attempt_id)
        purpose = (
            attempt.model_purpose.value
            if attempt is not None and attempt.model_purpose is not None
            else "PROVIDER"
        )
        labels.append((checkpoint.step_type.value, purpose))
    return labels


async def _stage_boundaries(
    runner: Runner, run_id: str, stage_id: str
) -> list[int]:
    """公开 Run View：一个 Stage 已完成 invocation 的 boundary 序列。"""
    inspection = await runner.inspect_run(run_id)
    boundaries: list[int] = []
    for checkpoint in inspection.checkpoints:
        if checkpoint.step_type is not StepType.CONTEXT:
            continue
        parsed = parse_stage_result(checkpoint.output)
        if parsed is not None and parsed.stage_id == stage_id:
            boundaries.append(parsed.boundary)
    return sorted(boundaries)


async def _compression_result_from_run_view(
    runner: Runner, run_id: str
) -> CompressionResult | None:
    """公开 Run View：compression checkpoint 载荷中的 CompressionResult。"""
    inspection = await runner.inspect_run(run_id)
    for checkpoint in inspection.checkpoints:
        if checkpoint.step_id != COMPRESSION_STEP:
            continue
        envelope = json.loads(checkpoint.output)
        return CompressionResult.deserialize(envelope["content"])
    return None


async def _run_happy_path() -> dict[str, Any]:
    """R1：完整成功路径——plan order、独立 step、protected、provenance。"""
    compression = SentinelCompressionAdapter(
        model_contract=_model_contract(
            "scenario-compression", usage_reporting=True
        ),
        usage=ModelUsage(
            input_tokens=42,
            output_tokens=7,
            raw_unit="tokens",
            normalization_source="deterministic-v1",
        ),
    )
    business = SentinelBusinessAdapter(
        model_contract=_model_contract("scenario-business")
    )
    provider = DeterministicContextProvider(items=_base_items())
    definition = _make_definition(
        compression_adapter=compression,
        business_adapter=business,
        provider=provider,
        plan=_run_input_plan(),
    )
    registry = DefinitionRegistry()
    registry.register(definition)
    clock = FakeClock()
    store = InMemoryRunStore(
        payload_codec=PlaintextPayloadCodec(), clock=clock
    )
    runner = Runner(registry=registry, store=store)
    created = await runner.create_run(
        definition.definition_id,
        definition.version,
        input=f"scenario run input {RUN_INPUT_CANARY}",
        history=_history(),
    )
    terminal = await runner.start_run(created.run_id)
    inspection = await runner.inspect_run(created.run_id)

    labels = await _checkpoint_labels(runner, created.run_id)
    compression_request = compression.requests[0]
    compression_request_json = json.dumps(
        compression_request.model_dump(mode="json")
    )
    business_request = business.requests[0]
    compression_attempts = [
        attempt
        for attempt in inspection.attempts
        if attempt.model_purpose is ModelPurpose.CONTEXT_COMPRESSION
    ]
    result = await _compression_result_from_run_view(runner, created.run_id)
    assert result is not None

    stage_items = 0
    for checkpoint in inspection.checkpoints:
        if checkpoint.step_type is StepType.CONTEXT:
            parsed = parse_stage_result(checkpoint.output)
            if parsed is not None:
                stage_items = len(parsed.output_items)
    return {
        "run_succeeded": terminal.status is RunStatus.SUCCEEDED,
        "checkpoint_labels": labels,
        "compression_request_item_ids": [
            item.item_id for item in compression_request.context_items
        ],
        "compression_request_tool_count": len(compression_request.tools),
        "compression_request_history_count": len(compression_request.history),
        "compression_input_free_of_canaries": (
            RUN_INPUT_CANARY not in compression_request_json
            and HISTORY_CANARY not in compression_request_json
        ),
        "compression_instructions_from_contract": (
            compression_request.instructions == COMPRESSION_CONTRACT.instructions
        ),
        "business_context_item_ids": [
            item.item_id for item in business_request.context_items
        ],
        "business_history_intact": tuple(business_request.history) == _history(),
        "business_run_input_intact": business_request.input
        == f"scenario run input {RUN_INPUT_CANARY}",
        "business_instructions_intact": business_request.instructions
        == definition.instructions,
        "compression_attempt_purpose_independent": (
            len(compression_attempts) == 1
            and compression_attempts[0].usage is not None
            and compression_attempts[0].usage.input_tokens == 42
        ),
        "source_items_preserved_count": stage_items,
        "derived_provenance_source_item_ids": list(
            result.output_items[0].provenance.source_item_ids
        ),
        "sentinel_compression_dispatches": compression.call_count,
        "sentinel_business_dispatches": business.call_count,
        "sentinel_provider_reads": provider.call_count,
    }


async def _run_zero_compression_dispatch() -> dict[str, Any]:
    """R2：契约范围内无可压缩 Item——零 compression dispatch。"""
    compression = SentinelCompressionAdapter(
        model_contract=_model_contract("scenario-compression-empty")
    )
    business = SentinelBusinessAdapter(
        model_contract=_model_contract("scenario-business-empty")
    )
    provider = DeterministicContextProvider(
        items=(_item("prot-1", "only protected evidence", PROTECTED_SOURCE),)
    )
    definition = _make_definition(
        compression_adapter=compression,
        business_adapter=business,
        provider=provider,
        plan=_run_input_plan(),
    )
    registry = DefinitionRegistry()
    registry.register(definition)
    clock = FakeClock()
    store = InMemoryRunStore(
        payload_codec=PlaintextPayloadCodec(), clock=clock
    )
    runner = Runner(registry=registry, store=store)
    created = await runner.create_run(
        definition.definition_id, definition.version, input="empty compression input"
    )
    terminal = await runner.start_run(created.run_id)
    business_request = business.requests[0]
    return {
        "zero_compression_dispatch_observed": (
            terminal.status is RunStatus.SUCCEEDED
            and compression.call_count == 0
        ),
        "protected_items_passed_through": [
            item.item_id for item in business_request.context_items
        ]
        == ["prot-1"],
        "sentinel_compression_dispatches": compression.call_count,
        "sentinel_business_dispatches": business.call_count,
    }


async def _run_hard_budget() -> dict[str, Any]:
    """R3：压缩成功但业务完整请求仍超预算——fail closed 零 dispatch。"""
    compression = SentinelCompressionAdapter(
        model_contract=_model_contract("scenario-compression-budget")
    )
    tiny_business_contract = _model_contract(
        "scenario-business-tiny", context_window_tokens=16
    )
    business = SentinelBusinessAdapter(model_contract=tiny_business_contract)
    provider = DeterministicContextProvider(items=_base_items())
    definition = _make_definition(
        compression_adapter=compression,
        business_adapter=business,
        provider=provider,
        plan=_run_input_plan(),
    )
    registry = DefinitionRegistry()
    registry.register(definition)
    clock = FakeClock()
    store = InMemoryRunStore(
        payload_codec=PlaintextPayloadCodec(), clock=clock
    )
    runner = Runner(registry=registry, store=store)
    created = await runner.create_run(
        definition.definition_id,
        definition.version,
        input=f"scenario run input {RUN_INPUT_CANARY}",
        history=_history(),
    )
    terminal = await runner.start_run(created.run_id)
    inspection = await runner.inspect_run(created.run_id)
    budget_exceeded = (
        terminal.status is RunStatus.FAILED
        and terminal.error_code == ERROR_CONTEXT_BUDGET_EXCEEDED
    )
    # 公开重算：压缩派生 + protected 的完整业务 Frame 必须超限，
    # 与运行时判定一致；同时用完整 Frame 验证 sizing 覆盖全部 channel。
    compressed_items = (
        _item("scenario-summary-1", "condensed scenario summary", "derived"),
        _item("prot-1", "protected scenario evidence " * 6, PROTECTED_SOURCE),
    )
    frame = ContextFrame(
        instructions=definition.instructions,
        run_input=f"scenario run input {RUN_INPUT_CANARY}",
        history=_history(),
        context_items=compressed_items,
    )
    sizer = ModelInputSizer.for_contract(tiny_business_contract)
    budget = ContextBudget.from_model_limits(tiny_business_contract.limits)
    sizing = check_frame_budget(frame, budget, sizer)
    full_sizing_over_limit = sizing.total_tokens > budget.total_token_limit
    sizing_channels_complete = set(sizing.channel_breakdown) == {
        "instructions",
        "run_input",
        "history",
        "context_items",
        "tool_outcomes",
        "tools",
        "protocol_overhead",
    }
    # 一个故意低估的 sizer 会声称该 Frame 在预算内（若被采纳将错误
    # 放行超限 dispatch）——权威完整 sizing 与运行时判定一致地拒绝。
    under_counting_total = 1

    class _UnderCountingSizer(ModelInputSizer):
        def size_frame(self, frame: ContextFrame):  # type: ignore[override]
            return FrameSizingResult(
                input_tokens=under_counting_total,
                reserved_output_tokens=0,
                total_tokens=under_counting_total,
                channel_breakdown={"everything": under_counting_total},
                sizing_mode=self.mode,
            )

    under_counting_sizing = _UnderCountingSizer(
        sizer_id="scenario-undercounting"
    ).size_frame(frame)
    return {
        "budget_exceeded_observed": budget_exceeded,
        "budget_zero_business_dispatch": business.call_count == 0,
        "budget_compression_dispatched_first": compression.call_count == 1,
        "budget_full_sizing_over_limit": full_sizing_over_limit,
        "budget_sizing_channels_complete": sizing_channels_complete,
        "budget_under_counting_sizer_would_admit": (
            under_counting_sizing.total_tokens <= budget.total_token_limit
        ),
        "budget_original_evidence_preserved": any(
            checkpoint.step_type is StepType.CONTEXT
            for checkpoint in inspection.checkpoints
        ),
        "sentinel_compression_dispatches": compression.call_count,
        "sentinel_business_dispatches": business.call_count,
    }


async def _run_recovery_stale_source() -> dict[str, Any]:
    """R4：compression checkpoint 后崩溃 + 外部数据源变化。"""
    compression = SentinelCompressionAdapter(
        model_contract=_model_contract("scenario-compression-recovery")
    )
    business = SentinelBusinessAdapter(
        model_contract=_model_contract("scenario-business-recovery")
    )
    provider = MutableProvider(_base_items())
    definition = _make_definition(
        compression_adapter=compression,
        business_adapter=business,
        provider=provider,
        plan=_run_input_plan(),
        model_execution_budget=ModelExecutionBudget(
            run_max_attempts=8,
            context_compression_max_attempts=4,
        ),
    )
    registry = DefinitionRegistry()
    registry.register(definition)
    clock = FakeClock()
    store = InMemoryRunStore(
        payload_codec=PlaintextPayloadCodec(), clock=clock
    )
    runner = Runner(
        registry=registry,
        store=store,
        crash_hook=_first_crash_hook(CrashPoint.AFTER_MODEL_CHECKPOINT),
    )
    created = await runner.create_run(
        definition.definition_id,
        definition.version,
        input=f"scenario run input {RUN_INPUT_CANARY}",
        history=_history(),
    )
    try:
        await runner.start_run(created.run_id)
        crashed = False
    except RuntimeError:
        crashed = True
    # stale source：外部数据源在崩溃后发生变化（新 Item + 内容漂移）。
    provider.replace_items(
        (
            _item("doc-1", "CHANGED first article content"),
            _item("doc-2", "CHANGED second article content"),
            _item(
                "prot-1", "CHANGED protected evidence", PROTECTED_SOURCE
            ),
            _item("doc-3", "newly appeared article"),
        )
    )
    clock.advance(timedelta(seconds=60))
    resumed = await Runner(registry=registry, store=store).resume_run(
        created.run_id
    )
    business_request = business.requests[0]
    return {
        "recovery_crash_injected": crashed,
        "recovery_succeeded": resumed.status is RunStatus.SUCCEEDED,
        "recovery_zero_recompute": compression.call_count == 1,
        "recovery_provider_not_reread": provider.call_count == 1,
        "recovery_stale_source_ignored": [
            item.item_id for item in business_request.context_items
        ]
        == ["prot-1", "scenario-summary-1"],
        "sentinel_compression_dispatches": compression.call_count,
        "sentinel_business_dispatches": business.call_count,
        "sentinel_provider_reads": provider.call_count,
    }


async def _run_tampered_provenance() -> dict[str, Any]:
    """R5：恢复时 compression checkpoint provenance 被篡改——fail closed。"""
    compression = SentinelCompressionAdapter(
        model_contract=_model_contract("scenario-compression-tamper")
    )
    business = SentinelBusinessAdapter(
        model_contract=_model_contract("scenario-business-tamper")
    )
    provider = DeterministicContextProvider(items=_base_items())
    definition = _make_definition(
        compression_adapter=compression,
        business_adapter=business,
        provider=provider,
        plan=_run_input_plan(),
        model_execution_budget=ModelExecutionBudget(
            run_max_attempts=8,
            context_compression_max_attempts=4,
        ),
    )
    registry = DefinitionRegistry()
    registry.register(definition)
    codec = TamperingCodec()
    clock = FakeClock()
    store = InMemoryRunStore(payload_codec=codec, clock=clock)
    runner = Runner(
        registry=registry,
        store=store,
        crash_hook=_first_crash_hook(CrashPoint.AFTER_MODEL_CHECKPOINT),
    )
    created = await runner.create_run(
        definition.definition_id, definition.version, input="tamper scenario input"
    )
    try:
        await runner.start_run(created.run_id)
    except RuntimeError:
        pass
    clock.advance(timedelta(seconds=60))
    resumed = await Runner(registry=registry, store=store).resume_run(
        created.run_id
    )
    # 恢复读取 compression checkpoint 时 decode 边界确实发生了篡改。
    assert codec.tampered_payloads, "tampering codec never fired"
    return {
        "tampered_provenance_fails_closed": (
            resumed.status is RunStatus.FAILED
            and resumed.error_code == "COMPRESSION_CONTRACT_VIOLATION"
        ),
        "tampered_provenance_zero_business_dispatch": business.call_count == 0,
        "sentinel_compression_dispatches": compression.call_count,
        "sentinel_business_dispatches": business.call_count,
    }


async def _run_recursion_blocked() -> dict[str, Any]:
    """R6：compression 请求业务 Tools——no-recursion fail closed。"""
    from ..runtime import ToolCall

    compression = SentinelCompressionAdapter(
        model_contract=_model_contract("scenario-compression-recursion"),
        tool_calls=(
            ToolCall(
                call_id="scenario-call-1",
                tool_name="scenario_echo",
                arguments="{}",
            ),
        ),
    )
    business = SentinelBusinessAdapter(
        model_contract=_model_contract("scenario-business-recursion")
    )
    provider = DeterministicContextProvider(items=_base_items())
    definition = _make_definition(
        compression_adapter=compression,
        business_adapter=business,
        provider=provider,
        plan=_run_input_plan(),
    )
    registry = DefinitionRegistry()
    registry.register(definition)
    clock = FakeClock()
    store = InMemoryRunStore(
        payload_codec=PlaintextPayloadCodec(), clock=clock
    )
    runner = Runner(registry=registry, store=store)
    created = await runner.create_run(
        definition.definition_id, definition.version, input="recursion scenario input"
    )
    terminal = await runner.start_run(created.run_id)
    inspection = await runner.inspect_run(created.run_id)
    failed_compression_attempts = [
        attempt
        for attempt in inspection.attempts
        if attempt.model_purpose is ModelPurpose.CONTEXT_COMPRESSION
        and attempt.status.value == "FAILED"
    ]
    return {
        "recursion_blocked": (
            terminal.status is RunStatus.FAILED
            and len(failed_compression_attempts) == 1
        ),
        "recursion_zero_business_dispatch": business.call_count == 0,
        "sentinel_compression_dispatches": compression.call_count,
        "sentinel_business_dispatches": business.call_count,
    }


async def _run_model_step_isolation() -> dict[str, Any]:
    """R7：MODEL_STEP Scope Stage 绝不为 compression 步骤触发。"""
    compression = SentinelCompressionAdapter(
        model_contract=_model_contract("scenario-compression-isolated")
    )
    business = SentinelBusinessAdapter(
        model_contract=_model_contract(
            "scenario-business-isolated", tool_calling=True
        ),
        tool_calls=(
            _business_tool_call(),
        ),
    )
    provider = DeterministicContextProvider(items=_base_items())
    definition = _make_definition(
        compression_adapter=compression,
        business_adapter=business,
        provider=provider,
        plan=_model_step_plan(),
        tools=(ScenarioEchoTool(),),
    )
    registry = DefinitionRegistry()
    registry.register(definition)
    clock = FakeClock()
    store = InMemoryRunStore(
        payload_codec=PlaintextPayloadCodec(), clock=clock
    )
    runner = Runner(registry=registry, store=store)
    created = await runner.create_run(
        definition.definition_id, definition.version, input="isolation scenario input"
    )
    terminal = await runner.start_run(created.run_id)
    boundaries = await _stage_boundaries(
        runner, created.run_id, "scenario-model-step"
    )
    inspection = await runner.inspect_run(created.run_id)
    compression_checkpoints = [
        checkpoint
        for checkpoint in inspection.checkpoints
        if checkpoint.step_id == COMPRESSION_STEP
    ]
    return {
        "isolation_run_succeeded": terminal.status is RunStatus.SUCCEEDED,
        "model_step_stage_boundaries": boundaries,
        "model_step_stage_isolated": boundaries == [1, 2],
        "single_compression_checkpoint": len(compression_checkpoints) == 1,
        "sentinel_compression_dispatches": compression.call_count,
        "sentinel_business_dispatches": business.call_count,
    }


def _business_tool_call():
    from ..runtime import ToolCall

    return ToolCall(
        call_id="scenario-business-call-1",
        tool_name="scenario_echo",
        arguments="{}",
    )


_EXPECTED_CHECKPOINT_ORDER = [
    ("CONTEXT", "PROVIDER"),
    ("MODEL", "CONTEXT_COMPRESSION"),
    ("MODEL", "PRIMARY"),
]


def reconcile_compression_observation(
    observation: Mapping[str, object],
) -> list[str]:
    """对账一个 compression Scenario 观察记录，返回问题列表。

    输入是 :func:`run_context_budget_compression` 权威 evidence view
    的同构记录：公开 Run View 派生事实与独立 sentinel 事实已经合并。
    空列表表示双源一致；任何受控变异（stale source、tampered
    provenance、under-counting sizer、recursion）都必须产生非空
    problems——这是 Scenario 的机器可读对账 seam。
    """
    problems: list[str] = []
    if not observation.get("run_succeeded"):
        problems.append("happy_path_run_failed")
    raw_labels = observation.get("checkpoint_labels", ())
    normalized_labels = (
        [list(label) for label in raw_labels]
        if isinstance(raw_labels, (list, tuple))
        else []
    )
    if normalized_labels != [
        list(label) for label in _EXPECTED_CHECKPOINT_ORDER
    ]:
        problems.append("checkpoint_order_violated")
    if not (
        observation.get("compression_request_item_ids") == ["doc-1", "doc-2"]
        and observation.get("compression_request_tool_count") == 0
        and observation.get("compression_request_history_count") == 0
        and observation.get("compression_input_free_of_canaries")
        and observation.get("compression_instructions_from_contract")
    ):
        problems.append("compression_input_not_isolated")
    if not (
        observation.get("business_context_item_ids") == ["prot-1", "scenario-summary-1"]
        and observation.get("business_history_intact")
        and observation.get("business_run_input_intact")
        and observation.get("business_instructions_intact")
    ):
        problems.append("protected_channels_violated")
    if not (
        observation.get("compression_attempt_purpose_independent")
        and observation.get("source_items_preserved_count") == 3
        and observation.get("derived_provenance_source_item_ids")
        == ["doc-1", "doc-2"]
    ):
        problems.append("compression_step_not_independent")
    if not observation.get("zero_compression_dispatch_observed"):
        problems.append("empty_compression_dispatched")
    if not observation.get("protected_items_passed_through"):
        problems.append("protected_items_not_passed_through")
    if not observation.get("budget_exceeded_observed"):
        problems.append("over_limit_frame_dispatched")
    if not observation.get("budget_zero_business_dispatch"):
        problems.append("business_dispatched_past_hard_budget")
    if not observation.get("budget_full_sizing_over_limit"):
        # under-counting sizer 变异：权威 Run 以 CONTEXT_BUDGET_EXCEEDED
        # fail closed，完整 sizing 重算必须与之一致；声称未超限即低估。
        problems.append("sizing_undercounted_vs_authoritative_run")
    if not observation.get("budget_sizing_channels_complete"):
        problems.append("sizing_channels_incomplete")
    if not observation.get("budget_under_counting_sizer_would_admit"):
        problems.append("under_counting_control_missing")
    if not (
        observation.get("recovery_crash_injected")
        and observation.get("recovery_succeeded")
    ):
        problems.append("recovery_incomplete")
    if not observation.get("recovery_zero_recompute"):
        problems.append("compression_recomputed_after_recovery")
    if not observation.get("recovery_provider_not_reread"):
        problems.append("external_source_reread_after_recovery")
    if not observation.get("recovery_stale_source_ignored"):
        problems.append("stale_source_changed_frozen_context")
    if not observation.get("tampered_provenance_fails_closed"):
        problems.append("tampered_provenance_accepted")
    if not observation.get("tampered_provenance_zero_business_dispatch"):
        problems.append("business_dispatched_after_tamper")
    if not observation.get("recursion_blocked"):
        problems.append("compression_recursion_allowed")
    if not observation.get("recursion_zero_business_dispatch"):
        problems.append("business_dispatched_despite_recursion")
    if not observation.get("model_step_stage_isolated"):
        problems.append("model_step_stage_triggered_for_compression")
    if not observation.get("single_compression_checkpoint"):
        problems.append("nested_compression_observed")
    return problems


def run_context_budget_compression() -> tuple[
    tuple[AcceptanceCheckResult, ...],
    dict[str, str | int | bool],
    dict[str, str],
]:
    """运行 context-budget-compression Scenario 的完整离线证明。

    返回 ``(checks, evidence_view, independent_evidence)``，与
    :func:`m_agent.testing.run_session_conversation` 相同的形态。
    """

    async def execute() -> dict[str, Any]:
        happy = await _run_happy_path()
        zero = await _run_zero_compression_dispatch()
        budget = await _run_hard_budget()
        recovery = await _run_recovery_stale_source()
        tampered = await _run_tampered_provenance()
        recursion = await _run_recursion_blocked()
        isolation = await _run_model_step_isolation()
        return {
            "happy": happy,
            "zero": zero,
            "budget": budget,
            "recovery": recovery,
            "tampered": tampered,
            "recursion": recursion,
            "isolation": isolation,
        }

    runs = asyncio.run(execute())

    def merged() -> dict[str, Any]:
        observation: dict[str, Any] = {}
        for part in runs.values():
            observation.update(part)
        return observation

    evidence_observation = merged()
    problems = reconcile_compression_observation(evidence_observation)
    clean = not problems

    # 受控变异：对同一权威观察注入四类谎言，对账 seam 必须逐一检出。
    stale_mutated = reconcile_compression_observation(
        {**evidence_observation, "recovery_provider_not_reread": False}
    )
    tamper_mutated = reconcile_compression_observation(
        {**evidence_observation, "tampered_provenance_fails_closed": False}
    )
    under_counting_mutated = reconcile_compression_observation(
        {**evidence_observation, "budget_full_sizing_over_limit": False}
    )
    recursion_mutated = reconcile_compression_observation(
        {**evidence_observation, "recursion_blocked": False}
    )

    happy = runs["happy"]
    zero = runs["zero"]
    budget = runs["budget"]
    recovery = runs["recovery"]
    tampered = runs["tampered"]
    recursion = runs["recursion"]
    isolation = runs["isolation"]

    plan_order_view = {
        "checkpoint_labels": happy["checkpoint_labels"],
        "model_step_stage_boundaries": isolation["model_step_stage_boundaries"],
    }
    plan_order_sentinel = {
        "compression_dispatches": isolation["sentinel_compression_dispatches"],
        "business_dispatches": isolation["sentinel_business_dispatches"],
    }
    frame_view = {
        "source_items_preserved_count": happy["source_items_preserved_count"],
        "derived_provenance_source_item_ids": happy[
            "derived_provenance_source_item_ids"
        ],
        "recovery_zero_recompute": recovery["recovery_zero_recompute"],
        "recovery_provider_not_reread": recovery["recovery_provider_not_reread"],
        "recovery_stale_source_ignored": recovery["recovery_stale_source_ignored"],
    }
    frame_sentinel = {
        "compression_dispatches": recovery["sentinel_compression_dispatches"],
        "business_dispatches": recovery["sentinel_business_dispatches"],
        "provider_reads": recovery["sentinel_provider_reads"],
    }
    budget_view = {
        "budget_exceeded_observed": budget["budget_exceeded_observed"],
        "budget_zero_business_dispatch": budget["budget_zero_business_dispatch"],
        "budget_compression_dispatched_first": budget[
            "budget_compression_dispatched_first"
        ],
        "budget_full_sizing_over_limit": budget["budget_full_sizing_over_limit"],
        "budget_sizing_channels_complete": budget[
            "budget_sizing_channels_complete"
        ],
        "zero_compression_dispatch_observed": zero[
            "zero_compression_dispatch_observed"
        ],
        "protected_items_passed_through": zero["protected_items_passed_through"],
    }
    budget_sentinel = {
        "compression_dispatches": budget["sentinel_compression_dispatches"],
        "business_dispatches": budget["sentinel_business_dispatches"],
        "empty_contract_compression_dispatches": zero[
            "sentinel_compression_dispatches"
        ],
    }
    protected_view = {
        "compression_request_item_ids": happy["compression_request_item_ids"],
        "compression_input_free_of_canaries": happy[
            "compression_input_free_of_canaries"
        ],
        "compression_instructions_from_contract": happy[
            "compression_instructions_from_contract"
        ],
        "business_context_item_ids": happy["business_context_item_ids"],
        "business_history_intact": happy["business_history_intact"],
    }
    protected_sentinel = {
        "compression_request_tool_count": happy[
            "compression_request_tool_count"
        ],
        "compression_request_history_count": happy[
            "compression_request_history_count"
        ],
        "business_run_input_intact": happy["business_run_input_intact"],
        "business_instructions_intact": happy["business_instructions_intact"],
    }
    recursion_view = {
        "recursion_blocked": recursion["recursion_blocked"],
        "recursion_zero_business_dispatch": recursion[
            "recursion_zero_business_dispatch"
        ],
        "model_step_stage_isolated": isolation["model_step_stage_isolated"],
        "single_compression_checkpoint": isolation["single_compression_checkpoint"],
        "compression_attempt_purpose_independent": happy[
            "compression_attempt_purpose_independent"
        ],
    }
    recursion_sentinel = {
        "compression_dispatches": recursion["sentinel_compression_dispatches"],
        "business_dispatches": recursion["sentinel_business_dispatches"],
    }
    mutation_view = {
        "stale_source_detected": bool(stale_mutated),
        "tampered_provenance_detected": bool(tamper_mutated),
        "under_counting_sizer_detected": bool(under_counting_mutated),
        "recursion_detected": bool(recursion_mutated),
    }
    all_mutation_detected = (
        mutation_view["stale_source_detected"]
        and mutation_view["tampered_provenance_detected"]
        and mutation_view["under_counting_sizer_detected"]
        and mutation_view["recursion_detected"]
    )
    mutation_independent = {
        "stale_source_problems": stale_mutated,
        "tampered_provenance_problems": tamper_mutated,
        "under_counting_sizer_problems": under_counting_mutated,
        "recursion_problems": recursion_mutated,
    }

    plan_order_digest = _digest(plan_order_view)
    frame_digest = _digest(frame_view)
    budget_digest = _digest(budget_view)
    protected_digest = _digest(protected_view)
    recursion_digest = _digest(recursion_view)
    mutation_digest = _digest(mutation_view)
    plan_order_sentinel_digest = _digest(plan_order_sentinel)
    frame_sentinel_digest = _digest(frame_sentinel)
    budget_sentinel_digest = _digest(budget_sentinel)
    protected_sentinel_digest = _digest(protected_sentinel)
    recursion_sentinel_digest = _digest(recursion_sentinel)
    mutation_independent_digest = _digest(mutation_independent)

    evidence_view: dict[str, str | int | bool] = {
        "scenario_runs_clean": clean,
        "plan_order_observed": clean,
        "frame_checkpoints_observed": clean,
        "hard_budget_observed": clean,
        "protected_channels_observed": clean,
        "no_recursion_observed": clean,
        "mutation_detected": all_mutation_detected,
        "mutation_stale_source_detected": mutation_view["stale_source_detected"],
        "mutation_tampered_provenance_detected": mutation_view[
            "tampered_provenance_detected"
        ],
        "mutation_under_counting_sizer_detected": mutation_view[
            "under_counting_sizer_detected"
        ],
        "mutation_recursion_detected": mutation_view["recursion_detected"],
        "plan_order_authoritative_digest": plan_order_digest,
        "frame_checkpoints_authoritative_digest": frame_digest,
        "hard_budget_authoritative_digest": budget_digest,
        "protected_channels_authoritative_digest": protected_digest,
        "no_recursion_authoritative_digest": recursion_digest,
        "mutation_authoritative_digest": mutation_digest,
    }
    independent_evidence = {
        "plan_order_sentinel_digest": plan_order_sentinel_digest,
        "frame_checkpoints_sentinel_digest": frame_sentinel_digest,
        "hard_budget_sentinel_digest": budget_sentinel_digest,
        "protected_channels_sentinel_digest": protected_sentinel_digest,
        "no_recursion_sentinel_digest": recursion_sentinel_digest,
        "mutation_independent_digest": mutation_independent_digest,
    }

    def result(check_id: str, passed: bool, digest: str) -> AcceptanceCheckResult:
        return AcceptanceCheckResult(
            check_id=check_id,
            status=(
                AcceptanceCheckStatus.PASS if passed else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
            reason_code="context_compression_observed",
            evidence_digest=digest,
        )

    checks = (
        result("context.compression.plan-order", clean, plan_order_digest),
        result("context.compression.frame-checkpoints", clean, frame_digest),
        result("context.compression.hard-budget", clean, budget_digest),
        result(
            "context.compression.protected-channels", clean, protected_digest
        ),
        result("context.compression.no-recursion", clean, recursion_digest),
        result(
            "context.compression.mutation",
            all_mutation_detected,
            mutation_digest,
        ),
    )
    return checks, evidence_view, independent_evidence
