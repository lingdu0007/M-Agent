from agent_framework import Agent, ModelResponse, MultiAgentTeam, TeamMember


class RoleModel:
    def __init__(self, role: str) -> None:
        self.role = role

    def complete(self, messages, tools):
        prompt = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        if self.role == "researcher":
            return ModelResponse(
                content=(
                    "Key facts: Agent frameworks separate model access, tools, "
                    "memory, guardrails, tracing, and workflow orchestration."
                )
            )
        if self.role == "reviewer":
            return ModelResponse(
                content=f"Review: The research is usable. Add why Workflow stays outside Agent. Input: {prompt}"
            )
        return ModelResponse(
            content=(
                "Final: A clean Agent framework keeps the Agent focused on one "
                "reasoning loop, while Workflow and Multi-Agent layers coordinate "
                "larger tasks around it."
            )
        )


researcher = Agent(
    name="Researcher",
    instructions="Find concise technical facts.",
    model=RoleModel("researcher"),
)
reviewer = Agent(
    name="Reviewer",
    instructions="Review research for gaps.",
    model=RoleModel("reviewer"),
)
writer = Agent(
    name="Writer",
    instructions="Write the final answer.",
    model=RoleModel("writer"),
)

team = MultiAgentTeam(
    [
        TeamMember(
            name="researcher",
            role="Research the topic",
            agent=researcher,
            prompt=lambda context: f"Research: {context['task']}",
            output_key="research",
        ),
        TeamMember(
            name="reviewer",
            role="Review the research",
            agent=reviewer,
            prompt=lambda context: f"Review this research: {context['research']}",
            output_key="review",
        ),
        TeamMember(
            name="writer",
            role="Write final response",
            agent=writer,
            prompt=lambda context: (
                f"Task: {context['task']}\nResearch: {context['research']}\nReview: {context['review']}"
            ),
            output_key="final",
        ),
    ],
    name="DesignTeam",
    final_output_key="final",
)


if __name__ == "__main__":
    result = team.run("Explain why Workflow should stay outside Agent.")
    print(result.final_output)
