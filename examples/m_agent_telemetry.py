"""M-Agent 本地 JSONL Telemetry 示例（Ticket 09 / ADR 0035）。

展示 Runtime Integrator 如何把官方 :class:`JsonlTelemetrySink` 附加到
公开 :class:`Runner`：一次确定性 Run（Context Step + Model Step +
Tool Step）产生可关联 Run / Step / Attempt 生命周期、耗时、状态与
usage 的 JSONL 文件，无需 observability 服务器或 Dashboard 即可
逐行检查。

运行（仓库根目录）：

    python examples/m_agent_telemetry.py

脚本会把 telemetry 写入当前目录的 ``telemetry-example.jsonl`` 并打印。
"""

from __future__ import annotations

import json
from pathlib import Path

from m_agent.runtime import (
    AgentDefinition,
    ContextItem,
    DefinitionRegistry,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    Runner,
    ToolCall,
    ToolEffect,
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
)
from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
    Runner,
)
from m_agent.runtime import ModelRequirements, ToolCallingMode

OUTPUT_PATH = Path(__file__).with_name("telemetry-example.jsonl")


class LookupModel(DeterministicModelAdapter):
    """确定性模型：第一次请求 lookup 工具，收到结果后给出最终响应。"""

    def __init__(self) -> None:
        super().__init__(
            capabilities=ModelCapabilities(
                tool_calling=ToolCallingMode.NATIVE
            )
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        if not request.tool_outcomes:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="call-lookup",
                        tool_name="lookup_order",
                        arguments='{"order_id": "A-1024"}',
                    ),
                )
            )
        return ModelResponse(
            content="order A-1024 is shipped",
            usage=None,  # 确定性示例不声明 usage_reporting
        )


class LookupOrderTool(DeterministicTool):
    """READ_ONLY 工具：返回订单状态（无外部副作用）。"""

    def __init__(self) -> None:
        super().__init__(name="lookup_order", effect=ToolEffect.READ_ONLY)

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        return ToolOutcome.success(
            request.call_id, self.name, result="status=shipped"
        )


def main() -> None:
    import asyncio

    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id="telemetry_demo",
            version="1.0",
            instructions="Answer deterministically.",
            model_requirements=ModelRequirements(
                capabilities=ModelCapabilities(
                    tool_calling=ToolCallingMode.NATIVE
                )
            ),
            model_adapter=LookupModel(),
            context_provider=DeterministicContextProvider(
                [
                    ContextItem(
                        item_id="policy-1",
                        content="order lookup is read-only",
                        source="demo-policy",
                    )
                ]
            ),
            tools=(LookupOrderTool(),),
        )
    )
    store = InMemoryRunStore(payload_codec=PlaintextPayloadCodec())
    async def run_demo() -> None:
        created = await runner.create_run(
            "telemetry_demo", "1.0", input="check order A-1024"
        )
        terminal = await runner.start_run(created.run_id)
        print(f"run {created.run_id} -> {terminal.status.value}")
        print(f"telemetry written to {OUTPUT_PATH}\n")

    # JsonlTelemetrySink owns a local file descriptor. The context guarantees
    # a flush/close even if the Run raises, so the file can be read below.
    with JsonlTelemetrySink(OUTPUT_PATH) as sink:
        runner = Runner(registry=registry, store=store, telemetry_sink=sink)
        asyncio.run(run_demo())
    for line in OUTPUT_PATH.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        summary = {
            "event": event["event_type"],
            "run_status": event.get("run_status"),
            "step_type": event.get("step_type"),
            "duration_ms": event.get("duration_ms"),
        }
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
