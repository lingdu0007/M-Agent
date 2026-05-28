from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

from .agent import Agent
from .trace import NoopTracer, TraceEvent, Tracer
from .types import AgentResult


MultiAgentContext = Dict[str, object]
PromptBuilder = Callable[[MultiAgentContext], str]
SessionBuilder = Callable[[MultiAgentContext], Optional[str]]


@dataclass
class TeamMember:
    name: str
    role: str
    agent: Agent
    prompt: PromptBuilder
    output_key: Optional[str] = None
    session_id: Optional[SessionBuilder] = None

    def key(self) -> str:
        return self.output_key or self.name


@dataclass
class MultiAgentResult:
    task: str
    final_output: str
    context: MultiAgentContext
    member_outputs: Dict[str, str] = field(default_factory=dict)
    agent_results: Dict[str, AgentResult] = field(default_factory=dict)


class MultiAgentTeam:
    def __init__(
        self,
        members: Iterable[TeamMember],
        *,
        name: str = "team",
        final_output_key: Optional[str] = None,
        tracer: Optional[Tracer] = None,
    ) -> None:
        self.name = name
        self.members = list(members)
        self.final_output_key = final_output_key
        self.tracer = tracer or NoopTracer()
        if not self.members:
            raise ValueError("MultiAgentTeam requires at least one member")

    def run(
        self, task: str, context: Optional[MultiAgentContext] = None
    ) -> MultiAgentResult:
        working_context: MultiAgentContext = dict(context or {})
        working_context["task"] = task
        member_outputs: Dict[str, str] = {}
        agent_results: Dict[str, AgentResult] = {}

        self._trace("multi_agent.run.start", task_chars=len(task), member_count=len(self.members))

        for member in self.members:
            prompt = member.prompt(working_context)
            key = member.key()
            self._trace(
                "multi_agent.member.start",
                member=member.name,
                role=member.role,
                prompt_chars=len(prompt),
            )
            result = member.agent.run(
                prompt,
                session_id=member.session_id(working_context) if member.session_id else None,
            )
            working_context[key] = result.output
            working_context[f"{key}_result"] = result
            member_outputs[key] = result.output
            agent_results[key] = result
            self._trace(
                "multi_agent.member.end",
                member=member.name,
                role=member.role,
                output_key=key,
                output_chars=len(result.output),
            )

        final_key = self.final_output_key or self.members[-1].key()
        final_output = str(working_context.get(final_key, ""))
        self._trace(
            "multi_agent.run.end",
            final_output_key=final_key,
            output_chars=len(final_output),
        )
        return MultiAgentResult(
            task=task,
            final_output=final_output,
            context=working_context,
            member_outputs=member_outputs,
            agent_results=agent_results,
        )

    def _trace(self, name: str, **data: object) -> None:
        self.tracer.record(TraceEvent(name=name, data={"team": self.name, **data}))
