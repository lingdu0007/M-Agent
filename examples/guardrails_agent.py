from agent_framework import (
    Agent,
    InMemoryTracer,
    ModelResponse,
    SensitiveArgumentGuardrail,
    ToolCall,
    tool,
)


@tool
def echo_secret(secret: str) -> str:
    """Echo a secret value."""
    return f"secret={secret}"


class SecretRequestModel:
    def complete(self, messages, tools):
        if messages[-1].role == "tool":
            return ModelResponse(content=f"Tool result: {messages[-1].content}")
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id="call_echo_secret",
                    name="echo_secret",
                    arguments={"secret": "sk-demo-value"},
                )
            ]
        )


tracer = InMemoryTracer()
agent = Agent(
    name="GuardrailsAgent",
    instructions="Use tools when needed, but respect guardrail results.",
    model=SecretRequestModel(),
    tools=[echo_secret],
    guardrails=[SensitiveArgumentGuardrail(blocked_keys=["secret"])],
    tracer=tracer,
)


if __name__ == "__main__":
    result = agent.run("请调用工具处理 secret")
    print(result.output)

    for event in tracer.events():
        print(event.to_dict())
