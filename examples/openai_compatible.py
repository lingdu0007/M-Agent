import os

from agent_framework import Agent, OpenAICompatibleClient, tool


@tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


model = OpenAICompatibleClient(
    model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
    base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
)

agent = Agent(
    name="RealModelAgent",
    instructions="You are a concise assistant. Use the add tool for arithmetic.",
    model=model,
    tools=[add],
)


if __name__ == "__main__":
    result = agent.run("用工具计算 128 + 256，然后用一句话回答。")
    print(result.output)
