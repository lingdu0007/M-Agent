"""M-Agent scoped Session 与显式 Context 压缩示例（Ticket 16 / 0.4）。

展示 Runtime Integrator 如何组合 0.4 的两个新能力：

1. **scoped Session 会话**：公开 :class:`SessionRunner` +
   :class:`SQLiteSessionStore` 把一条用户消息推进为一次 Agent Run，
   claim → run → commit 全程持久化，历史按 session 版本化保存；
2. **显式语义压缩**：Definition 声明 :class:`CompressionContract` 与
   RUN_INPUT :class:`ContextPlan`，运行时先执行 PROVIDE Stage，再用
   ``ModelPurpose.CONTEXT_COMPRESSION`` 的独立模型调用把可压缩文档
   （``docs:articles``）蒸馏为携带 provenance 的派生 Item，受保护来源
   与会话历史永不进入压缩请求，最后业务模型基于压缩后的上下文作答。

全程确定性、离线（无网络、无凭据），存储使用本仓库临时目录。

运行（仓库根目录）：

    python examples/m_agent_session_context.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from m_agent.adapters import (
    DeterministicContextProvider,
    DeterministicModelAdapter,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent.companion import SessionRunner, SessionScope, SQLiteSessionStore
from m_agent.runtime import (
    AgentDefinition,
    CompressionContract,
    ContextItem,
    ContextPlan,
    ContextScope,
    ContextStage,
    ContextStageIdentity,
    ContextTransformType,
    DefinitionRegistry,
    ModelBinding,
    ModelBindingSet,
    ModelCapabilities,
    ModelContract,
    ModelLimits,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    RevisionStability,
    Runner,
    UsageReportingMode,
)

DEFINITION_ID = "support-summary-agent"
DEFINITION_VERSION = "1.0"

COMPRESSION_CONTRACT = CompressionContract(
    contract_id="support-summarize",
    version="1",
    instructions="Summarize the allowed knowledge articles, keep key facts.",
    allowed_sources=("docs:articles",),
    retained_categories=("facts", "entities"),
    omitted_categories=("verbatim-text",),
    derived_categories=("summary",),
    max_output_items=1,
)

CONTEXT_PLAN = ContextPlan(
    stages=(
        ContextStage(
            identity=ContextStageIdentity(
                stage_id="support-provide",
                scope=ContextScope.RUN_INPUT,
                transform_type=ContextTransformType.PROVIDE,
            )
        ),
    )
)


class SummaryModel(DeterministicModelAdapter):
    """CONTEXT_COMPRESSION 模型：把请求内的文章蒸馏为一条摘要 Item。"""

    deterministic: bool = True

    def __init__(self, *, model_contract: ModelContract) -> None:
        super().__init__(responses=("",), model_contract=model_contract)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        payload = {
            "items": [
                {
                    "item_id": "knowledge-summary",
                    "content": "condensed support knowledge summary",
                    "source_item_ids": [
                        item.item_id for item in request.context_items
                    ],
                }
            ]
        }
        return ModelResponse(
            content=json.dumps(payload),
            usage=ModelUsage(
                input_tokens=48,
                output_tokens=9,
                raw_unit="tokens",
                normalization_source="deterministic-v1",
            ),
        )


class AnswerModel(DeterministicModelAdapter):
    """业务 PRIMARY 模型：基于压缩后的上下文给出最终回答。"""

    deterministic: bool = True

    def __init__(self, *, model_contract: ModelContract) -> None:
        super().__init__(
            responses=("answer based on the compressed knowledge base",),
            model_contract=model_contract,
        )


def _model_contract(contract_id: str) -> ModelContract:
    return ModelContract(
        contract_id=contract_id,
        version="1",
        revision_stability=RevisionStability.PINNED,
        model_identity=f"deterministic:{contract_id}",
        capabilities=ModelCapabilities(
            usage_reporting=UsageReportingMode.PROVIDER_REPORTED
        ),
        limits=ModelLimits(context_window_tokens=1_000_000, max_output_tokens=100_000),
        input_sizer_id="deterministic-v1",
        serialization_id="deterministic-text-v1",
    )


def _registry() -> DefinitionRegistry:
    compression = SummaryModel(
        model_contract=_model_contract("support-compression")
    )
    business = AnswerModel(model_contract=_model_contract("support-business"))
    primary = ModelBinding(
        purpose=ModelPurpose.PRIMARY,
        contract=business.model_contract,
    )
    definition = AgentDefinition(
        definition_id=DEFINITION_ID,
        version=DEFINITION_VERSION,
        instructions="Answer the customer question from the knowledge base.",
        model_bindings=ModelBindingSet(
            bindings=(
                primary,
                ModelBinding(
                    purpose=ModelPurpose.CONTEXT_COMPRESSION,
                    contract=compression.model_contract,
                ),
                primary.model_copy(
                    update={
                        "purpose": ModelPurpose.OUTPUT_REPAIR,
                        "source_purpose": ModelPurpose.PRIMARY,
                    }
                ),
            )
        ),
        model_adapter=business,
        model_adapters={ModelPurpose.CONTEXT_COMPRESSION: compression},
        context_provider=DeterministicContextProvider(
            items=(
                ContextItem(
                    item_id="article-refund",
                    content="refund policy article " * 16,
                    source="docs:articles",
                ),
                ContextItem(
                    item_id="article-delivery",
                    content="delivery policy article " * 16,
                    source="docs:articles",
                ),
                ContextItem(
                    item_id="internal-runbook",
                    content="internal escalation runbook " * 8,
                    source="internal:protected",
                ),
            )
        ),
        compression_contract=COMPRESSION_CONTRACT,
        context_plan=CONTEXT_PLAN,
    )
    registry = DefinitionRegistry()
    registry.register(definition)
    return registry


async def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run_store = SQLiteRunStore(
            root / "runs.sqlite3", payload_codec=PlaintextPayloadCodec()
        )
        session_store = SQLiteSessionStore(
            root / "sessions.sqlite3", payload_codec=PlaintextPayloadCodec()
        )
        try:
            runner = SessionRunner(
                runner=Runner(_registry(), run_store),
                session_store=session_store,
            )
            scope = SessionScope(token="customer-7")
            await session_store.create_session(scope, "conversation-1")
            result = await runner.submit(
                scope,
                "conversation-1",
                DEFINITION_ID,
                DEFINITION_VERSION,
                "My parcel has not arrived, can I get a refund?",
            )
            print(f"turn commit_status={result.commit_status.value}")
            print(f"turn run_status={result.run.status.value}")
            print(f"turn output={result.turn.assistant_output if result.turn else None}")
            snapshot = await session_store.read_snapshot(scope, "conversation-1")
            print(f"session version={snapshot.version}")
            print(f"session turns={[turn.turn_id for turn in snapshot.turns]}")
            claim = await session_store.get_claim(scope, "conversation-1")
            print(f"residual claim={claim is not None}")
        finally:
            session_store.close()
            run_store.close()


if __name__ == "__main__":
    asyncio.run(main())
