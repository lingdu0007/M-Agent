"""Ticket 15 测试：显式 Semantic Compression 与 Context 场景。

验收要求（.scratch/agent-runtime-foundation/issues/15-...md）：

1. Compression Contract 版本化声明输入范围、保留/省略/派生信息、
   输出约束与 provenance 要求；结果明确标记为 lossy transform。
2. Semantic Compression 使用独立 ``CONTEXT_COMPRESSION`` purpose、
   binding、Model Contract、attempt、usage 与 Model Execution Budget。
3. Compression 只处理允许的 Context Items，不自动压缩 Conversation
   History、instructions、run input、Tool Outcomes 或 protected evidence。
4. Compression 不递归运行 Context Pipeline、不调用业务 Tools、不执行
   Output Repair 或另一层 Compression；违约路径稳定失败。
5. completed compression checkpoint 在恢复时复用；crash-before/
   after-dispatch 不造成隐藏重算、预算重置或来源漂移。
6. 无 binding、能力不匹配、输出无效或压缩后仍超预算均 fail closed，
   并保留原始与失败证据。

所有断言只通过公开 Runner 控制入口与公开数据模型驱动。
"""

from __future__ import annotations

import json
import unittest

from m_agent.adapters import (
    DeterministicContextProvider,
    DeterministicModelAdapter,
    DeterministicTool,
    FakeClock,
    InMemoryRunStore,
    PlaintextPayloadCodec,
)
from m_agent.runtime import (
    ERROR_CONTEXT_BUDGET_EXCEEDED,
    ERROR_MODEL_EXECUTION_BUDGET_EXCEEDED,
    AgentDefinition,
    CompressionContract,
    CompressionResult,
    CompressionProvenance,
    CompressedContextItem,
    CompressionContractViolationError,
    ContextItem,
    ContextPlan,
    ContextScope,
    ContextStage,
    ContextStageIdentity,
    ContextTransformType,
    CrashPoint,
    DefinitionRegistry,
    ModelBinding,
    ModelBindingSet,
    ModelCapabilities,
    ModelCapabilityCombination,
    ModelContract,
    ModelExecutionBudget,
    ModelLimits,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ModelRequirements,
    RevisionStability,
    RetryPolicy,
    Runner,
    RunStatus,
    StepType,
    StructuredOutputMode,
    ToolCallingMode,
    UsageReportingMode,
    apply_compression,
    compression_step_id,
    parse_compression_output,
    validate_compression_result,
)
from m_agent._failure import ModelFailure
from m_agent._tools import ToolCall, ToolEffect, ToolOutcome
from m_agent._history import ConversationMessage, ConversationRole
from datetime import timedelta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALLOWED_SOURCE = "docs:articles"
PROTECTED_SOURCE = "internal:protected"

CONTRACT = CompressionContract(
    contract_id="summarize-articles",
    version="1",
    instructions="Summarize the provided articles, retaining key facts.",
    allowed_sources=(ALLOWED_SOURCE,),
    retained_categories=("facts", "entities"),
    omitted_categories=("verbatim-text", "formatting"),
    derived_categories=("summary",),
    max_output_items=2,
)


def _item(item_id: str, content: str, source: str = ALLOWED_SOURCE) -> ContextItem:
    return ContextItem(item_id=item_id, content=content, source=source)


def _contract_model(
    contract_id: str,
    *,
    context_window_tokens: int = 1_000_000,
    usage_reporting: bool = False,
    tool_calling: bool = False,
) -> ModelContract:
    capabilities = ModelCapabilities(
        tool_calling=(ToolCallingMode.NATIVE if tool_calling else ToolCallingMode.NONE)
    )
    if usage_reporting:
        capabilities = ModelCapabilities(
            tool_calling=capabilities.tool_calling,
            usage_reporting=UsageReportingMode.PROVIDER_REPORTED,
            supported_combinations=(
                ModelCapabilityCombination(
                    tool_calling=capabilities.tool_calling,
                    usage_reporting=UsageReportingMode.PROVIDER_REPORTED,
                ),
            )
            if tool_calling
            else (),
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


class _EchoTool(DeterministicTool):
    """READ_ONLY 工具：返回固定结果。"""

    def __init__(self) -> None:
        super().__init__(
            name="echo",
            description="Echo back.",
            effect=ToolEffect.READ_ONLY,
        )

    async def invoke(self, request) -> ToolOutcome:
        return ToolOutcome.success(request.call_id, self.name, result="tool result")


class CompressionModelAdapter(DeterministicModelAdapter):
    """确定性 compression fake：回显压缩输入的派生 Item。

    记录每次请求（供无递归 / 输入隔离断言）。可注入故障输出。
    """

    deterministic: bool = True

    def __init__(
        self,
        *,
        content: str = "condensed summary of the articles",
        model_contract: ModelContract | None = None,
        raw_output: str | None = None,
        failure: Exception | None = None,
        tool_calls: tuple[ToolCall, ...] = (),
        usage: ModelUsage | None = None,
    ) -> None:
        super().__init__(
            responses=("",), model_contract=model_contract
        )
        self._content = content
        self._raw_output = raw_output
        self._failure = failure
        self._tool_calls = tuple(tool_calls)
        self._usage = usage
        self.requests: list[ModelRequest] = []

    def _fingerprint_excluded_state(self) -> frozenset[str]:
        return frozenset({"_last_request", "call_count", "requests"})

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        if self._failure is not None:
            raise self._failure
        if self._raw_output is not None:
            return ModelResponse(content=self._raw_output)
        if self._tool_calls:
            return ModelResponse(tool_calls=self._tool_calls)
        payload = {
            "items": [
                {
                    "item_id": f"summary-{self.call_count}",
                    "content": self._content,
                    "source_item_ids": [
                        item.item_id for item in request.context_items
                    ],
                }
            ]
        }
        return ModelResponse(
            content=json.dumps(payload), usage=self._usage
        )


class BusinessModelAdapter(DeterministicModelAdapter):
    """确定性业务模型：记录请求，可选在首轮请求工具。"""

    deterministic: bool = True

    def __init__(
        self,
        *,
        model_contract: ModelContract | None = None,
        tool_calls: tuple[ToolCall, ...] = (),
    ) -> None:
        super().__init__(responses=("final answer",), model_contract=model_contract)
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
        return ModelResponse(content=f"final answer {self.call_count}")


def _bindings(
    primary_contract: ModelContract,
    compression_contract: ModelContract,
) -> ModelBindingSet:
    primary = ModelBinding(
        purpose=ModelPurpose.PRIMARY, contract=primary_contract
    )
    return ModelBindingSet(
        bindings=(
            primary,
            ModelBinding(
                purpose=ModelPurpose.CONTEXT_COMPRESSION,
                contract=compression_contract,
            ),
            primary.model_copy(
                update={
                    "purpose": ModelPurpose.OUTPUT_REPAIR,
                    "source_purpose": ModelPurpose.PRIMARY,
                }
            ),
        )
    )


def make_compression_definition(
    *,
    compression_adapter: CompressionModelAdapter,
    business_adapter: BusinessModelAdapter,
    provider: DeterministicContextProvider | None = None,
    compression_contract: CompressionContract | None = CONTRACT,
    model_execution_budget: ModelExecutionBudget | None = None,
    retry_policy: RetryPolicy | None = None,
    context_plan: ContextPlan | None = None,
    definition_id: str = "compression-agent",
    instructions: str = "Answer using the provided context.",
    history: tuple[ConversationMessage, ...] = (),
) -> AgentDefinition:
    kwargs: dict = dict(
        definition_id=definition_id,
        version="1.0",
        instructions=instructions,
        model_bindings=_bindings(
            business_adapter.model_contract,
            compression_adapter.model_contract,
        ),
        model_execution_budget=(
            model_execution_budget or ModelExecutionBudget()
        ),
        model_adapter=business_adapter,
        model_adapters={
            ModelPurpose.CONTEXT_COMPRESSION: compression_adapter
        },
    )
    if provider is not None:
        kwargs["context_provider"] = provider
    if compression_contract is not None:
        kwargs["compression_contract"] = compression_contract
    if retry_policy is not None:
        kwargs["retry_policy"] = retry_policy
    if context_plan is not None:
        kwargs["context_plan"] = context_plan
    definition = AgentDefinition(**kwargs)
    return definition


def _default_provider() -> DeterministicContextProvider:
    return DeterministicContextProvider(
        items=(
            _item("doc-1", "first article " * 20),
            _item("doc-2", "second article " * 20),
            _item("prot-1", "protected evidence " * 5, PROTECTED_SOURCE),
        )
    )


def _run_input_plan() -> ContextPlan:
    return ContextPlan(
        stages=(
            ContextStage(
                identity=ContextStageIdentity(
                    stage_id="provide-run-input",
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
                    stage_id="provide-run-input",
                    scope=ContextScope.RUN_INPUT,
                    transform_type=ContextTransformType.PROVIDE,
                )
            ),
            ContextStage(
                identity=ContextStageIdentity(
                    stage_id="provide-model-step",
                    scope=ContextScope.MODEL_STEP,
                    transform_type=ContextTransformType.PROVIDE,
                )
            ),
        )
    )


async def _completed_run(
    definition: AgentDefinition,
    *,
    store: InMemoryRunStore | None = None,
    history: tuple[ConversationMessage, ...] = (),
):
    registry = DefinitionRegistry()
    registry.register(definition)
    store = store or InMemoryRunStore(
        payload_codec=PlaintextPayloadCodec(), clock=FakeClock()
    )
    runner = Runner(registry=registry, store=store)
    created = await runner.create_run(
        definition.definition_id, definition.version, input="answer", history=history
    )
    terminal = await runner.start_run(created.run_id)
    return runner, store, created, terminal


# ---------------------------------------------------------------------------
# 1. Compression Contract（版本化、lossy、provenance）
# ---------------------------------------------------------------------------


class CompressionContractTests(unittest.TestCase):
    """AC 1: 版本化、显式有损的 Compression Contract。"""

    def test_contract_freezes_versioned_lossy_declaration(self):
        contract = CONTRACT
        self.assertEqual(contract.contract_id, "summarize-articles")
        self.assertEqual(contract.version, "1")
        self.assertIs(contract.lossy, True)
        self.assertEqual(contract.allowed_sources, (ALLOWED_SOURCE,))
        self.assertEqual(contract.retained_categories, ("facts", "entities"))
        self.assertEqual(contract.omitted_categories, ("verbatim-text", "formatting"))

    def test_contract_cannot_declare_lossless(self):
        with self.assertRaises(Exception):
            CompressionContract(
                contract_id="x",
                version="1",
                instructions="i",
                allowed_sources=(ALLOWED_SOURCE,),
                retained_categories=("facts",),
                omitted_categories=("noise",),
                lossy=False,
            )

    def test_contract_requires_declared_input_range_and_categories(self):
        base = dict(
            contract_id="x",
            version="1",
            instructions="i",
            retained_categories=("facts",),
            omitted_categories=("noise",),
        )
        with self.assertRaises(Exception):
            CompressionContract(**base, allowed_sources=())
        with self.assertRaises(Exception):
            CompressionContract(
                **base, allowed_sources=(ALLOWED_SOURCE, " ")
            )
        with self.assertRaises(Exception):
            CompressionContract(**base, allowed_sources=(ALLOWED_SOURCE,), retained_categories=())
        with self.assertRaises(Exception):
            CompressionContract(**base, allowed_sources=(ALLOWED_SOURCE,), omitted_categories=())

    def test_allows_item_filters_by_declared_source(self):
        self.assertTrue(CONTRACT.allows_item(_item("a", "c")))
        self.assertFalse(CONTRACT.allows_item(_item("b", "c", PROTECTED_SOURCE)))

    def test_derived_item_source_is_stable(self):
        self.assertEqual(
            CONTRACT.derived_item_source(), "compression:summarize-articles:1"
        )

    def test_contract_frozen_into_definition_snapshot(self):
        adapter = BusinessModelAdapter()
        compression = CompressionModelAdapter()
        definition = make_compression_definition(
            compression_adapter=compression,
            business_adapter=adapter,
            provider=_default_provider(),
        )
        snapshot = definition.frozen_snapshot()
        assert snapshot.compression_contract is not None
        self.assertEqual(snapshot.compression_contract.contract_id, CONTRACT.contract_id)
        self.assertEqual(snapshot.compression_contract.version, CONTRACT.version)
        # Snapshot 序列化 roundtrip 保留契约（纯数据）。
        restored = type(snapshot).model_validate_json(snapshot.model_dump_json())
        self.assertEqual(restored.compression_contract, CONTRACT)


# ---------------------------------------------------------------------------
# 2. Compression 输出解析与校验（fail closed）
# ---------------------------------------------------------------------------


class CompressionOutputParsingTests(unittest.TestCase):
    """AC 1/6: 输出约束与 provenance 校验。"""

    def _sources(self):
        return (_item("doc-1", "first article " * 10), _item("doc-2", "second article " * 10))

    def test_valid_output_parses_with_stamped_provenance(self):
        content = json.dumps(
            {
                "items": [
                    {
                        "item_id": "summary-1",
                        "content": "condensed summary",
                        "source_item_ids": ["doc-1", "doc-2"],
                    }
                ]
            }
        )
        result = parse_compression_output(
            content, contract=CONTRACT, source_items=self._sources()
        )
        self.assertEqual(result.source_item_ids, ("doc-1", "doc-2"))
        self.assertEqual(len(result.output_items), 1)
        derived = result.output_items[0]
        self.assertEqual(derived.item.item_id, "summary-1")
        self.assertEqual(derived.item.source, "compression:summarize-articles:1")
        self.assertEqual(
            derived.provenance,
            CompressionProvenance(
                contract_id="summarize-articles",
                contract_version="1",
                source_item_ids=("doc-1", "doc-2"),
            ),
        )
        self.assertEqual(result.omitted_categories, CONTRACT.omitted_categories)
        self.assertEqual(result.measurement["source_item_count"], 2)

    def test_non_json_output_fails_closed(self):
        for content in (None, "not json", "[]", '{"unexpected": true}'):
            with self.subTest(content=content):
                with self.assertRaises(CompressionContractViolationError):
                    parse_compression_output(
                        content, contract=CONTRACT, source_items=self._sources()
                    )

    def test_missing_provenance_fails_closed(self):
        content = json.dumps(
            {"items": [{"item_id": "s", "content": "summary"}]}
        )
        with self.assertRaises(CompressionContractViolationError):
            parse_compression_output(
                content, contract=CONTRACT, source_items=self._sources()
            )

    def test_tampered_provenance_fails_closed(self):
        content = json.dumps(
            {
                "items": [
                    {
                        "item_id": "s",
                        "content": "summary",
                        "source_item_ids": ["doc-1", "phantom-item"],
                    }
                ]
            }
        )
        with self.assertRaises(CompressionContractViolationError):
            parse_compression_output(
                content, contract=CONTRACT, source_items=self._sources()
            )

    def test_item_id_collision_with_source_fails_closed(self):
        content = json.dumps(
            {
                "items": [
                    {
                        "item_id": "doc-1",
                        "content": "summary",
                        "source_item_ids": ["doc-1"],
                    }
                ]
            }
        )
        with self.assertRaises(CompressionContractViolationError):
            parse_compression_output(
                content, contract=CONTRACT, source_items=self._sources()
            )

    def test_max_output_items_fails_closed(self):
        items = [
            {
                "item_id": f"s-{i}",
                "content": "summary",
                "source_item_ids": ["doc-1"],
            }
            for i in range(3)
        ]
        content = json.dumps({"items": items})
        with self.assertRaises(CompressionContractViolationError):
            parse_compression_output(
                content, contract=CONTRACT, source_items=self._sources()
            )

    def test_expansion_output_fails_closed(self):
        content = json.dumps(
            {
                "items": [
                    {
                        "item_id": "s",
                        "content": "expanded summary " * 200,
                        "source_item_ids": ["doc-1"],
                    }
                ]
            }
        )
        with self.assertRaises(CompressionContractViolationError):
            parse_compression_output(
                content, contract=CONTRACT, source_items=self._sources()
            )

    def test_validate_result_detects_contract_identity_drift(self):
        result = CompressionResult(
            contract_id="other-contract",
            contract_version="2",
            source_item_ids=("doc-1",),
        )
        with self.assertRaises(CompressionContractViolationError):
            validate_compression_result(
                result, contract=CONTRACT, source_items=self._sources()
            )

    def test_result_serialize_roundtrip(self):
        result = parse_compression_output(
            json.dumps(
                {
                    "items": [
                        {
                            "item_id": "s",
                            "content": "summary",
                            "source_item_ids": ["doc-1"],
                        }
                    ]
                }
            ),
            contract=CONTRACT,
            source_items=self._sources(),
        )
        restored = CompressionResult.deserialize(result.serialize())
        self.assertEqual(restored, result)

    def test_apply_compression_replaces_consumed_and_keeps_others(self):
        sources = self._sources() + (_item("prot", "protected", PROTECTED_SOURCE),)
        result = CompressionResult(
            contract_id=CONTRACT.contract_id,
            contract_version=CONTRACT.version,
            source_item_ids=("doc-1", "doc-2"),
            output_items=(
                CompressedContextItem(
                    item=_item("s", "summary", CONTRACT.derived_item_source()),
                    provenance=CompressionProvenance(
                        contract_id=CONTRACT.contract_id,
                        contract_version=CONTRACT.version,
                        source_item_ids=("doc-1", "doc-2"),
                    ),
                ),
            ),
        )
        applied = apply_compression(sources, result)
        self.assertEqual([item.item_id for item in applied], ["prot", "s"])

    def test_apply_compression_detects_drifted_sources(self):
        result = CompressionResult(
            contract_id=CONTRACT.contract_id,
            contract_version=CONTRACT.version,
            source_item_ids=("doc-1", "doc-2"),
        )
        with self.assertRaises(CompressionContractViolationError):
            apply_compression((_item("doc-1", "only one"),), result)


# ---------------------------------------------------------------------------
# 3. 显式独立 compression Model Step
# ---------------------------------------------------------------------------


class ExplicitCompressionStepTests(unittest.IsolatedAsyncioTestCase):
    """AC 2/3: 独立 purpose/binding/attempt/usage；只压缩允许 Items。"""

    async def test_compression_runs_as_independent_model_step(self):
        compression = CompressionModelAdapter(
            model_contract=_contract_model("compression-model", usage_reporting=True),
            usage=ModelUsage(
                input_tokens=42,
                output_tokens=7,
                raw_unit="tokens",
                normalization_source="deterministic-v1",
            ),
        )
        business = BusinessModelAdapter(
            model_contract=_contract_model("business-model")
        )
        provider = _default_provider()
        definition = make_compression_definition(
            compression_adapter=compression,
            business_adapter=business,
            provider=provider,
            context_plan=_run_input_plan(),
        )
        runner, store, created, terminal = await _completed_run(definition)
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)

        # compression dispatch 恰好一次，业务 dispatch 恰好一次。
        self.assertEqual(compression.call_count, 1)
        self.assertEqual(business.call_count, 1)

        # compression 请求只携带允许的 Items，绝不携带 history/tools/
        # tool_outcomes，instructions 来自 Compression Contract。
        request = compression.requests[0]
        self.assertEqual(
            [item.item_id for item in request.context_items], ["doc-1", "doc-2"]
        )
        self.assertEqual(request.tools, ())
        self.assertEqual(request.tool_outcomes, ())
        self.assertEqual(request.history, ())
        self.assertEqual(request.instructions, CONTRACT.instructions)
        self.assertEqual(
            request.input,
            json.dumps(
                {
                    "task": "semantic-compression",
                    "contract_id": CONTRACT.contract_id,
                    "contract_version": CONTRACT.version,
                    "retained_categories": list(CONTRACT.retained_categories),
                    "omitted_categories": list(CONTRACT.omitted_categories),
                    "derived_categories": list(CONTRACT.derived_categories),
                    "max_output_items": CONTRACT.max_output_items,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

        # 业务请求收到压缩派生 Item + 未压缩的 protected Item；
        # history / run input / instructions 保持权威。
        business_request = business.requests[0]
        self.assertEqual(
            [item.item_id for item in business_request.context_items],
            ["prot-1", "summary-1"],
        )
        self.assertEqual(
            business_request.context_items[1].source,
            "compression:summarize-articles:1",
        )
        self.assertEqual(business_request.instructions, definition.instructions)
        self.assertEqual(business_request.input, "answer")

        # Model Attempt 以 CONTEXT_COMPRESSION purpose 记账，usage 保留。
        attempts = await store.get_attempts(created.run_id)
        compression_attempts = [
            a for a in attempts if a.model_purpose is ModelPurpose.CONTEXT_COMPRESSION
        ]
        self.assertEqual(len(compression_attempts), 1)
        self.assertEqual(compression_attempts[0].usage.input_tokens, 42)
        self.assertEqual(compression_attempts[0].usage.output_tokens, 7)

        # compression checkpoint 是独立的 MODEL checkpoint，载荷携带
        # CompressionResult envelope。
        checkpoints = await store.get_checkpoints(created.run_id)
        compression_checkpoints = [
            c
            for c in checkpoints
            if c.step_id == compression_step_id(CONTRACT)
        ]
        self.assertEqual(len(compression_checkpoints), 1)
        self.assertEqual(
            compression_checkpoints[0].step_type, StepType.MODEL
        )
        result = CompressionResult.deserialize(
            json.loads(compression_checkpoints[0].output)["content"]
        )
        self.assertEqual(result.source_item_ids, ("doc-1", "doc-2"))

        # 原始 Items 保留在 PROVIDE Stage checkpoint 中，绝不改写。
        context_checkpoints = [
            c for c in checkpoints if c.step_type is StepType.CONTEXT
        ]
        self.assertEqual(len(context_checkpoints), 1)
        stage_payload = json.loads(context_checkpoints[0].output)
        stage_item_ids = [
            wrapped["item"]["item_id"]
            for wrapped in stage_payload["output_items"]
        ]
        self.assertEqual(stage_item_ids, ["doc-1", "doc-2", "prot-1"])

    async def test_no_contract_means_no_compression_step(self):
        compression = CompressionModelAdapter()
        business = BusinessModelAdapter()
        definition = make_compression_definition(
            compression_adapter=compression,
            business_adapter=business,
            provider=_default_provider(),
            compression_contract=None,
        )
        runner, store, created, terminal = await _completed_run(definition)
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(compression.call_count, 0)
        checkpoints = await store.get_checkpoints(created.run_id)
        self.assertFalse(
            any(c.step_id.startswith("compression:") for c in checkpoints)
        )

    async def test_no_compressible_items_skips_dispatch(self):
        compression = CompressionModelAdapter()
        business = BusinessModelAdapter()
        provider = DeterministicContextProvider(
            items=(_item("prot-1", "only protected", PROTECTED_SOURCE),)
        )
        definition = make_compression_definition(
            compression_adapter=compression,
            business_adapter=business,
            provider=provider,
            context_plan=_run_input_plan(),
        )
        runner, store, created, terminal = await _completed_run(definition)
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)
        self.assertEqual(compression.call_count, 0)
        business_request = business.requests[0]
        self.assertEqual(
            [item.item_id for item in business_request.context_items],
            ["prot-1"],
        )

    async def test_compression_uses_its_own_binding_budget(self):
        # 压缩 Model Contract 的窗口独立于业务 Contract：压缩请求自身
        # 也必须通过完整预算检查。
        tiny_compression = CompressionModelAdapter(
            model_contract=_contract_model(
                "compression-tiny", context_window_tokens=8
            )
        )
        business = BusinessModelAdapter()
        definition = make_compression_definition(
            compression_adapter=tiny_compression,
            business_adapter=business,
            provider=_default_provider(),
            context_plan=_run_input_plan(),
        )
        runner, store, created, terminal = await _completed_run(definition)
        # 压缩请求超出自身边界：CONTEXT_BUDGET_EXCEEDED，零业务 dispatch。
        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, ERROR_CONTEXT_BUDGET_EXCEEDED)
        self.assertEqual(business.call_count, 0)


# ---------------------------------------------------------------------------
# 4. No-recursion
# ---------------------------------------------------------------------------


class NoRecursionTests(unittest.IsolatedAsyncioTestCase):
    """AC 4: compression 不递归运行 Pipeline / Tools / 另一层压缩。"""

    async def test_compression_does_not_trigger_model_step_stages(self):
        compression = CompressionModelAdapter()
        tool_call = ToolCall(call_id="call-1", tool_name="echo", arguments="{}")
        business = BusinessModelAdapter(
            model_contract=_contract_model("business-tools", tool_calling=True),
            tool_calls=(tool_call,),
        )
        provider = _default_provider()
        definition = AgentDefinition(
            definition_id="compression-agent-tools",
            version="1.0",
            instructions="Answer using the provided context.",
            model_bindings=_bindings(
                business.model_contract, compression.model_contract
            ),
            model_execution_budget=ModelExecutionBudget(),
            model_adapter=business,
            model_adapters={ModelPurpose.CONTEXT_COMPRESSION: compression},
            context_provider=provider,
            compression_contract=CONTRACT,
            context_plan=_model_step_plan(),
            tools=(_EchoTool(),),
        )
        runner, store, created, terminal = await _completed_run(definition)
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)

        # 业务两步（tool call -> final），compression 恰好一次。
        self.assertEqual(business.call_count, 2)
        self.assertEqual(compression.call_count, 1)

        # MODEL_STEP Scope Stage 只随业务步骤触发（boundary 1、2），
        # 绝不为 compression 步骤额外触发（否则会出现 boundary 0 或
        # 额外计数）。Provider 读取 = RUN_INPUT 1 次 + MODEL_STEP 2 次。
        self.assertEqual(provider.call_count, 3)
        checkpoints = await store.get_checkpoints(created.run_id)
        stage_results = [
            json.loads(c.output)
            for c in checkpoints
            if c.step_type is StepType.CONTEXT
        ]
        model_step_stages = [
            r for r in stage_results if r["scope"] == "MODEL_STEP"
        ]
        self.assertEqual(
            sorted(r["boundary"] for r in model_step_stages), [1, 2]
        )

        # compression checkpoint 只有一个（无第二层压缩）。
        compression_checkpoints = [
            c
            for c in checkpoints
            if c.step_id == compression_step_id(CONTRACT)
        ]
        self.assertEqual(len(compression_checkpoints), 1)

    async def test_compression_requesting_tools_fails_closed(self):
        tool_call = ToolCall(
            call_id="call-1", tool_name="echo", arguments="{}"
        )
        compression = CompressionModelAdapter(
            model_contract=_contract_model("compression-tools"),
            tool_calls=(tool_call,),
        )
        business = BusinessModelAdapter()
        definition = make_compression_definition(
            compression_adapter=compression,
            business_adapter=business,
            provider=_default_provider(),
            context_plan=_run_input_plan(),
        )
        runner, store, created, terminal = await _completed_run(definition)
        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(business.call_count, 0)
        attempts = await store.get_attempts(created.run_id)
        self.assertTrue(
            any(
                a.model_purpose is ModelPurpose.CONTEXT_COMPRESSION
                and a.status.value == "FAILED"
                for a in attempts
            )
        )


# ---------------------------------------------------------------------------
# 5. Fail closed：binding / 能力 / 输出无效 / 压缩后仍超预算
# ---------------------------------------------------------------------------


class FailClosedTests(unittest.IsolatedAsyncioTestCase):
    """AC 6: 无 binding、能力不匹配、输出无效、仍超预算均 fail closed。"""

    def test_registration_rejects_missing_compression_adapter_owner(self):
        compression = CompressionModelAdapter()
        business = BusinessModelAdapter()
        definition = make_compression_definition(
            compression_adapter=compression,
            business_adapter=business,
            provider=_default_provider(),
        )
        ownerless = definition.model_copy(
            update={"model_adapters": {}}
        )
        with self.assertRaises(Exception):
            DefinitionRegistry().register(ownerless)

    def test_registration_rejects_capability_mismatch(self):
        compression = CompressionModelAdapter(
            model_contract=_contract_model("compression-mismatched")
        )
        business = BusinessModelAdapter()
        primary = ModelBinding(
            purpose=ModelPurpose.PRIMARY,
            contract=business.model_contract,
        )
        bindings = ModelBindingSet(
            bindings=(
                primary,
                ModelBinding(
                    purpose=ModelPurpose.CONTEXT_COMPRESSION,
                    contract=compression.model_contract,
                    requirements=ModelRequirements(
                        capabilities=ModelCapabilities(
                            structured_output=StructuredOutputMode.JSON_OBJECT
                        )
                    ),
                ),
                primary.model_copy(
                    update={
                        "purpose": ModelPurpose.OUTPUT_REPAIR,
                        "source_purpose": ModelPurpose.PRIMARY,
                    }
                ),
            )
        )
        with self.assertRaises(Exception):
            DefinitionRegistry().register(
                AgentDefinition(
                    definition_id="mismatch-agent",
                    version="1.0",
                    instructions="Answer.",
                    model_bindings=bindings,
                    model_execution_budget=ModelExecutionBudget(),
                    model_adapter=business,
                    compression_contract=CONTRACT,
                )
            )

    async def test_invalid_compression_output_fails_closed_with_evidence(self):
        compression = CompressionModelAdapter(
            model_contract=_contract_model("compression-invalid"),
            raw_output="not valid json",
        )
        business = BusinessModelAdapter()
        definition = make_compression_definition(
            compression_adapter=compression,
            business_adapter=business,
            provider=_default_provider(),
            context_plan=_run_input_plan(),
        )
        runner, store, created, terminal = await _completed_run(definition)
        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(
            terminal.error_code, "COMPRESSION_CONTRACT_VIOLATION"
        )
        self.assertEqual(business.call_count, 0)

        # 失败证据保留：失败 compression Attempt + FAILED Model Step。
        attempts = await store.get_attempts(created.run_id)
        failed = [
            a
            for a in attempts
            if a.model_purpose is ModelPurpose.CONTEXT_COMPRESSION
            and a.status.value == "FAILED"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(
            failed[0].error_code, "COMPRESSION_CONTRACT_VIOLATION"
        )
        steps = await store.get_steps(created.run_id)
        self.assertTrue(
            any(
                s.step_type is StepType.MODEL
                and s.status.value == "FAILED"
                and s.error_code == "COMPRESSION_CONTRACT_VIOLATION"
                for s in steps
            )
        )
        # 原始 Items 保留在 PROVIDE Stage checkpoint 中。
        checkpoints = await store.get_checkpoints(created.run_id)
        context_checkpoint = next(
            c for c in checkpoints if c.step_type is StepType.CONTEXT
        )
        payload = json.loads(context_checkpoint.output)
        self.assertEqual(len(payload["output_items"]), 3)

    async def test_tampered_provenance_output_fails_closed(self):
        compression = CompressionModelAdapter(
            model_contract=_contract_model("compression-tampered"),
            raw_output=json.dumps(
                {
                    "items": [
                        {
                            "item_id": "s",
                            "content": "summary",
                            "source_item_ids": ["doc-1", "phantom"],
                        }
                    ]
                }
            ),
        )
        business = BusinessModelAdapter()
        definition = make_compression_definition(
            compression_adapter=compression,
            business_adapter=business,
            provider=_default_provider(),
            context_plan=_run_input_plan(),
        )
        runner, store, created, terminal = await _completed_run(definition)
        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(
            terminal.error_code, "COMPRESSION_CONTRACT_VIOLATION"
        )
        self.assertEqual(business.call_count, 0)

    async def test_still_over_budget_after_compression_fails_closed(self):
        # 业务 Contract 窗口极小：即使压缩成功，完整请求仍超预算 ->
        # CONTEXT_BUDGET_EXCEEDED，零业务 dispatch，原始证据保留。
        compression = CompressionModelAdapter(
            model_contract=_contract_model("compression-ok"),
            content="short",
        )
        business = BusinessModelAdapter(
            model_contract=_contract_model(
                "business-tiny", context_window_tokens=24
            )
        )
        definition = make_compression_definition(
            compression_adapter=compression,
            business_adapter=business,
            provider=_default_provider(),
            context_plan=_run_input_plan(),
        )
        runner, store, created, terminal = await _completed_run(definition)
        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(terminal.error_code, ERROR_CONTEXT_BUDGET_EXCEEDED)
        # compression 已执行（显式步骤），业务零 dispatch。
        self.assertEqual(compression.call_count, 1)
        self.assertEqual(business.call_count, 0)
        checkpoints = await store.get_checkpoints(created.run_id)
        self.assertTrue(
            any(
                c.step_id == compression_step_id(CONTRACT)
                for c in checkpoints
            )
        )
        self.assertTrue(
            any(c.step_type is StepType.CONTEXT for c in checkpoints)
        )

    async def test_compression_budget_exhaustion_fails_closed(self):
        compression = CompressionModelAdapter(
            model_contract=_contract_model("compression-budgeted"),
            failure=ModelFailure(
                "TRANSIENT", "provider_unavailable", "down"
            ),
        )
        business = BusinessModelAdapter()
        definition = make_compression_definition(
            compression_adapter=compression,
            business_adapter=business,
            provider=_default_provider(),
            context_plan=_run_input_plan(),
            model_execution_budget=ModelExecutionBudget(
                run_max_attempts=4,
                context_compression_max_attempts=1,
            ),
            retry_policy=RetryPolicy(max_attempts=2),
        )
        runner, store, created, terminal = await _completed_run(definition)
        self.assertIs(terminal.status, RunStatus.FAILED)
        self.assertEqual(
            terminal.error_code, ERROR_MODEL_EXECUTION_BUDGET_EXCEEDED
        )
        self.assertEqual(business.call_count, 0)
        self.assertEqual(compression.call_count, 1)
        # 预算消耗证据：UNCERTAIN/TRANSIENT 失败 Attempt 留痕。
        attempts = await store.get_attempts(created.run_id)
        self.assertEqual(
            len(
                [
                    a
                    for a in attempts
                    if a.model_purpose is ModelPurpose.CONTEXT_COMPRESSION
                ]
            ),
            1,
        )


# ---------------------------------------------------------------------------
# 6. 恢复：completed checkpoint 复用 / crash 窗口
# ---------------------------------------------------------------------------


class CompressionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """AC 5: crash-before/after-dispatch 不造成隐藏重算或预算重置。"""

    async def _crash_then_resume(
        self,
        crash_point: CrashPoint,
        *,
        retry_policy: RetryPolicy | None = RetryPolicy(max_attempts=2),
    ) -> tuple:
        compression = CompressionModelAdapter(
            model_contract=_contract_model("compression-recovery"),
        )
        business = BusinessModelAdapter()
        provider = _default_provider()
        definition = make_compression_definition(
            compression_adapter=compression,
            business_adapter=business,
            provider=provider,
            context_plan=_run_input_plan(),
            retry_policy=retry_policy,
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
        fired: list[CrashPoint] = []

        def crash_hook(point: CrashPoint, run_id: str) -> None:
            # 目标崩溃点首次触发时注入崩溃（恢复 Runner 不带 hook，
            # 不会再次触发）。
            if point is crash_point and point not in fired:
                fired.append(point)
                raise RuntimeError("injected crash")

        runner = Runner(registry=registry, store=store, crash_hook=crash_hook)
        created = await runner.create_run(
            definition.definition_id, definition.version, input="answer"
        )
        with self.assertRaises(RuntimeError):
            await runner.start_run(created.run_id)
        clock.advance(timedelta(seconds=60))
        resumed = await Runner(registry=registry, store=store).resume_run(
            created.run_id
        )
        return (
            resumed,
            store,
            created.run_id,
            compression,
            business,
            provider,
        )

    async def test_crash_after_compression_checkpoint_reuses_result(self):
        # 崩溃点：compression checkpoint 已落盘（第一个 AFTER_MODEL_
        # CHECKPOINT 即 compression checkpoint），业务尚未开始。
        (
            resumed,
            store,
            run_id,
            compression,
            business,
            provider,
        ) = await self._crash_then_resume(CrashPoint.AFTER_MODEL_CHECKPOINT)
        self.assertIs(resumed.status, RunStatus.SUCCEEDED)
        # compression checkpoint 复用：模型零重算，Provider 零重读。
        self.assertEqual(compression.call_count, 1)
        self.assertEqual(business.call_count, 1)
        self.assertEqual(provider.call_count, 1)
        # 业务收到与首跑一致的压缩派生 Items。
        self.assertEqual(
            [item.item_id for item in business.requests[0].context_items],
            ["prot-1", "summary-1"],
        )

    async def test_crash_after_compression_dispatch_replays_at_least_once(self):
        # 崩溃点：compression dispatch 已发生、checkpoint 未提交
        #（第一个 BEFORE_MODEL_CHECKPOINT 即 compression 响应返回后）。
        (
            resumed,
            store,
            run_id,
            compression,
            business,
            provider,
        ) = await self._crash_then_resume(CrashPoint.BEFORE_MODEL_CHECKPOINT)
        self.assertIs(resumed.status, RunStatus.SUCCEEDED)
        # at-least-once 重放：compression 模型第二次调用（新 attempt），
        # Provider 仍只读一次（外部事实不重读）。
        self.assertEqual(compression.call_count, 2)
        self.assertEqual(business.call_count, 1)
        self.assertEqual(provider.call_count, 1)
        attempts = await store.get_attempts(run_id)
        compression_attempts = [
            a
            for a in attempts
            if a.model_purpose is ModelPurpose.CONTEXT_COMPRESSION
        ]
        # 第一个 attempt 归一为 UNCERTAIN 证据，第二个成功 checkpoint。
        self.assertEqual(len(compression_attempts), 2)
        self.assertEqual(
            [a.status.value for a in compression_attempts],
            ["FAILED", "SUCCEEDED"],
        )
        self.assertEqual(
            compression_attempts[0].error_code, "model_checkpoint_unconfirmed"
        )
        # 预算如实消耗（两个 compression attempts），业务照常进行。
        self.assertEqual(business.call_count, 1)

    async def test_crash_after_dispatch_without_policy_fails_closed(self):
        (
            resumed,
            store,
            run_id,
            compression,
            business,
            provider,
        ) = await self._crash_then_resume(
            CrashPoint.BEFORE_MODEL_CHECKPOINT, retry_policy=None
        )
        # 无冻结重试授权：未确认 dispatch fail closed，绝不隐式重放。
        self.assertIs(resumed.status, RunStatus.FAILED)
        self.assertEqual(resumed.error_code, "model_checkpoint_unconfirmed")
        self.assertEqual(compression.call_count, 1)
        self.assertEqual(business.call_count, 0)


# ---------------------------------------------------------------------------
# 7. 保护通道：history / instructions / run input 不被压缩
# ---------------------------------------------------------------------------


class ProtectedChannelCompressionTests(unittest.IsolatedAsyncioTestCase):
    """AC 3: history / instructions / run input 永不进入压缩输入。"""

    async def test_history_and_run_input_never_enter_compression(self):
        compression = CompressionModelAdapter(
            model_contract=_contract_model("compression-protected")
        )
        business = BusinessModelAdapter()
        history = (
            ConversationMessage(
                role=ConversationRole.USER, content="secret history turn"
            ),
            ConversationMessage(
                role=ConversationRole.ASSISTANT, content="prior reply"
            ),
        )
        provider = _default_provider()
        definition = make_compression_definition(
            compression_adapter=compression,
            business_adapter=business,
            provider=provider,
            context_plan=_run_input_plan(),
        )
        registry = DefinitionRegistry()
        registry.register(definition)
        store = InMemoryRunStore(
            payload_codec=PlaintextPayloadCodec(), clock=FakeClock()
        )
        runner = Runner(registry=registry, store=store)
        created = await runner.create_run(
            definition.definition_id,
            definition.version,
            input="run input text",
            history=history,
        )
        terminal = await runner.start_run(created.run_id)
        self.assertIs(terminal.status, RunStatus.SUCCEEDED)

        request = compression.requests[0]
        self.assertEqual(request.history, ())
        self.assertNotIn("run input text", request.input)
        self.assertNotIn("secret history turn", json.dumps(request.model_dump()))

        # 业务请求仍收到完整 history / run input（受保护通道不改写）。
        business_request = business.requests[0]
        self.assertEqual(tuple(business_request.history), history)
        self.assertEqual(business_request.input, "run input text")


if __name__ == "__main__":
    unittest.main()
