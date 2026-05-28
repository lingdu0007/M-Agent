from agent_framework import Agent, AgentStep, FunctionStep, ModelResponse, Workflow


class DraftModel:
    def complete(self, messages, tools):
        user_prompt = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        if "Workflow" in user_prompt:
            return ModelResponse(
                content=(
                    "Draft: Workflow is the layer that coordinates multiple steps, "
                    "while each Agent stays focused on one reasoning loop."
                )
            )
        return ModelResponse(content=f"Draft: {user_prompt}")


agent = Agent(
    name="DraftAgent",
    instructions="Write concise drafts.",
    model=DraftModel(),
)

workflow = Workflow(
    [
        FunctionStep(
            "prepare",
            lambda context: {
                "topic": context.get("topic", "Agent framework"),
                "audience": context.get("audience", "beginner"),
            },
        ),
        AgentStep(
            "draft",
            agent,
            lambda context: (
                f"Explain {context['topic']} to a {context['audience']} in one sentence."
            ),
            output_key="draft",
        ),
        FunctionStep(
            "finalize",
            lambda context: {
                "final": context["draft"].replace("Draft: ", "").strip(),
            },
        ),
    ],
    start="prepare",
)


if __name__ == "__main__":
    result = workflow.run({"topic": "Workflow", "audience": "new Agent developer"})
    print(result["final"])
