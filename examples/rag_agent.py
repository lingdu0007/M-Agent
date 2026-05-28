from pathlib import Path

from agent_framework import Agent, KeywordRagIndex, ModelResponse, ToolCall, make_rag_tools


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RagDemoModel:
    def complete(self, messages, tools):
        if messages[-1].role == "tool":
            return ModelResponse(content=f"基于检索结果回答：\n{messages[-1].content}")

        user_prompt = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id="call_search_knowledge",
                    name="search_knowledge",
                    arguments={"query": user_prompt, "top_k": 3},
                )
            ]
        )


index = KeywordRagIndex.from_directory(
    PROJECT_ROOT,
    suffixes={".md", ".py"},
    chunk_size=900,
    overlap=120,
)

agent = Agent(
    name="RagAgent",
    instructions="Use the knowledge search tool before answering project design questions.",
    model=RagDemoModel(),
    tools=make_rag_tools(index),
)


if __name__ == "__main__":
    result = agent.run("SessionMemory 为什么要和 Memory 分开设计？")
    print(result.output)
