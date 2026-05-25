from datetime import datetime

from agent_framework import Agent, RuleBasedDemoModel, tool


@tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@tool
def get_current_time() -> str:
    """Return the current local time."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


agent = Agent(
    name="LearningAgent",
    instructions="You are a helpful assistant. Use tools when they are useful.",
    model=RuleBasedDemoModel(),
    tools=[add, get_current_time],
)


if __name__ == "__main__":
    result = agent.run("请帮我计算 3 + 5")
    print(result.output)

    result = agent.run("现在时间是多少？")
    print(result.output)
