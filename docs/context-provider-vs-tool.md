# 检索集成：Context Provider 还是 Tool？

本文是 Durable Run 的最小检索示例，
说明独立 RAG 项目如何通过 M-Agent 的稳定边界组合，而**不把 RAG 索引
或检索策略变成 Runtime Core 的特权子系统**（ADR 0014）。

## 两种接入方式

| 场景 | 集成方式 | 运行时行为 |
| --- | --- | --- |
| 应用选择检索（确定性、与模型无关） | **Context Provider** | 在依赖它的 Model Step 之前执行，形成 Context Step + Step Attempt，结果 checkpoint，崩溃恢复复用 Items、不重复查询外部数据（ADR 0015）。 |
| 模型选择检索（模型决定何时查什么） | **Tool**（普通工具契约） | 模型在响应中请求工具调用，每次调用形成独立 Tool Step，走工具边界与 Tool Effect 声明（后续版本）。 |

一个检索集成是 **Provider** 还是 **Tool**，取决于由谁做选择：

- 应用在 Definition 里声明 Provider → 确定性注入，不依赖模型工具选择；
- 模型在推理中决定调用检索工具 → 走 Tool Step 边界，运行时不做特权处理。

## 最小示例：确定性检索作为 Context Provider

`KeywordRagIndex`（历史 0.1 示例适配器）只做索引与
`search`，不感知运行时。把它包成一个 Context Provider 即可：

```python
from m_agent import (
    ContextItem,
    ContextProvider,
    ContextRequest,
    DeterministicContextProvider,
)

class KeywordContextProvider(DeterministicContextProvider):
    """把现有 KeywordRagIndex 包成应用选择的 Context Provider。

    这是适配层，不是运行时子系统：索引、chunking、打分逻辑全部
    留在外部项目里，本类只负责把检索结果映射成 Context Item。
    """

    deterministic: bool = True

    def __init__(self, index, top_k: int = 5) -> None:
        super().__init__()
        self._index = index
        self._top_k = top_k

    async def provide(self, request: ContextRequest) -> list[ContextItem]:
        hits = self._index.search(request.input, top_k=self._top_k)
        return [
            ContextItem(
                item_id=f"{hit.chunk.id}",
                content=hit.chunk.text,
                source=hit.chunk.source,
                metadata={
                    "doc": hit.chunk.source,
                    "score": float(hit.score),   # 溯源保留，运行时不解释
                    "lines": f"{hit.chunk.start_line}-{hit.chunk.end_line}",
                },
            )
            for hit in hits
        ]
```

注册进 Definition 后，Runner 会在 Model Step 前确定性调用它，把
Context Items 作为 checkpoint 持久化：

```python
registry.register(
    AgentDefinition.for_adapter(
        definition_id="assistant",
        version="1.0",
        instructions="Answer using the provided context.",
        model_adapter=adapter,
        context_provider=KeywordContextProvider(index=rag_index),
    )
)
```

## 模型选择的检索未来走 Tool

当需要由模型决定"要不要查、查什么"时，检索应暴露为普通 Tool，而不是
给检索特殊待遇。历史示例中的 `make_rag_tools(index)` 已经是
这种形态（把索引包装成 Tool 集合）；将来接入 Durable Run 的 Tool Step
时，它必须声明 Tool Effect（READ_ONLY）、返回结构化 Tool Outcome，
并遵守与任何其他工具相同的重试与恢复规则。运行时不会为 RAG 发明
Retriever、Reranker 或索引子系统。

## 指令边界

无论 Provider 还是 Tool，检索内容都是**不可信外部数据**（ADR 0017）：

- Context Item 只进入 `ModelRequest.context_items` 字段，作为数据交付；
- 它永远不能写入或替换 `instructions`（Agent Instruction）；
- Provider 抛出的异常形成可检查的失败 Step Attempt，不会变成模型
  可见的上下文字符串。

在 provider wire 映射中，完整的 `item_id`、`content`、`source` 和
`metadata` 以 JSON 数据保留：Chat Completions 只有 Agent Definition
instructions 使用 system role，Context Item 使用 user data；Responses 的
`instructions` 精确保留 Definition instructions，Context Item 只进入
`input` data。即使其中含有伪 system 标签、`ignore previous instructions`
或结构化 prompt injection，也不会由运行时写入受信指令通道。

这是一项**数据通道隔离**保证，不是“模型一定不会遵循恶意文本”的保证。
把内容放在 user/data 通道避免运行时主动提升权限，但不能替代集成方的
输出校验、策略控制或人工复核。

## 不做什么

- 不把 `KeywordRagIndex`、向量库、Session Memory 或完整 RAG 项目移入
  `m_agent` runtime core；
- 运行时不解释检索分数、不执行 rerank、不决定引用格式（ADR 0016）；
- 运行时不提供 ingestion、chunking、embedding、索引等 RAG 内部能力。
