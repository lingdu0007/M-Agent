from agent_framework import (
    Agent,
    EvalCase,
    ExpectedToolCall,
    ModelResponse,
    OutputSchema,
    ToolCall,
    run_agent_evals,
    tool,
)


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


class EvalDemoModel:
    def complete(self, messages, tools):
        if messages[-1].role == "tool":
            return ModelResponse(content=f"answer={messages[-1].content}")

        prompt = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        if "JSON" in prompt:
            return ModelResponse(content='{"answer": "ok", "score": 1}')
        if "calculate" in prompt:
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="add",
                        arguments={"a": 3, "b": 5},
                    )
                ]
            )
        return ModelResponse(
            content="Agent frameworks separate models, tools, memory, tracing, and evals."
        )


answer_schema = OutputSchema(
    name="answer",
    schema={
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "score": {"type": "integer"},
        },
        "required": ["answer", "score"],
    },
)

agent = Agent(
    name="EvalAgent",
    instructions="Answer predictably for evaluation examples.",
    model=EvalDemoModel(),
    tools=[add],
)

cases = [
    EvalCase(
        name="architecture-keywords",
        prompt="Explain the framework architecture.",
        expected_keywords=["models", "tools", "memory", "evals"],
    ),
    EvalCase(
        name="structured-json",
        prompt="Return JSON with an answer and score.",
        expected_structured={"answer": "ok", "score": 1},
        output_schema=answer_schema,
    ),
    EvalCase(
        name="calculator-tool",
        prompt="Please calculate 3 + 5.",
        expected_output="answer=8",
        expected_tool_calls=[ExpectedToolCall("add", {"a": 3, "b": 5})],
    ),
]


if __name__ == "__main__":
    report = run_agent_evals(agent, cases)
    print(report.summary())
    for result in report.results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{status} {result.case_name}")
        for check in result.checks:
            print(f"  {check.status} {check.name}: {check.message}")
