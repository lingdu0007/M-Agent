from agent_framework import Agent, InMemoryTracer, RuleBasedDemoModel, tool


@tool
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


tracer = InMemoryTracer()
agent = Agent(
    name="TraceAgent",
    instructions="You are a concise assistant. Use tools for arithmetic.",
    model=RuleBasedDemoModel(),
    tools=[add],
    tracer=tracer,
)


if __name__ == "__main__":
    result = agent.run("请计算 10 + 32")
    print(result.output)

    for event in tracer.events():
        print(event.to_dict())
