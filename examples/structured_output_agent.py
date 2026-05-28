from agent_framework import Agent, ModelResponse, OutputSchema


class StructuredDemoModel:
    def complete(self, messages, tools):
        return ModelResponse(
            content='{"summary": "Agent frameworks separate models, tools, memory, and safety.", "confidence": 0.91}'
        )


summary_schema = OutputSchema(
    name="summary_result",
    description="A concise summary with a confidence score.",
    schema={
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["summary", "confidence"],
    },
)

agent = Agent(
    name="StructuredOutputAgent",
    instructions="Answer with the requested structured object.",
    model=StructuredDemoModel(),
)


if __name__ == "__main__":
    result = agent.run("Summarize the Agent framework design.", output_schema=summary_schema)
    print(result.output)
    print(result.structured_output)
