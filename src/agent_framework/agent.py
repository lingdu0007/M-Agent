"""The bounded synchronous part of the 0.1 Agent compatibility shim."""

from __future__ import annotations

from typing import Iterable, List, Optional
import warnings

from . import LegacyMigrationError
from .models import ModelClient
from .tools import Tool, ToolRegistry
from .types import AgentResult, Message, ModelResponse


warnings.warn(
    "agent_framework.agent.Agent is deprecated in M-Agent 0.2.x and will be "
    "removed in 0.3.0. Migrate to m_agent.SyncRunner plus AgentDefinition; "
    "see docs/migrating-from-0.1.md.",
    DeprecationWarning,
    stacklevel=2,
)


def _unsupported_legacy_option(names: Iterable[str]) -> LegacyMigrationError:
    rendered = ", ".join(sorted(names))
    return LegacyMigrationError(
        f"agent_framework.Agent option(s) {rendered} have no "
        "semantics-preserving M-Agent 0.2 mapping. Supply context, "
        "guardrails, tracing, and output validation in the embedding "
        "application; see docs/migrating-from-0.1.md."
    )


class Agent:
    """Compatibility loop for the accurate synchronous Agent subset.

    New integrations should use ``m_agent.Runner`` or ``m_agent.SyncRunner``.
    Memory, sessions, guardrails, tracing, structured output, workflows, and
    multi-agent orchestration deliberately fail rather than silently preserve
    a legacy semantic contract.
    """

    def __init__(
        self,
        *,
        name: str,
        instructions: str,
        model: ModelClient,
        tools: Optional[Iterable[Tool]] = None,
        max_steps: int = 6,
        **legacy_options: object,
    ) -> None:
        if legacy_options:
            raise _unsupported_legacy_option(legacy_options)
        self.name = name
        self.instructions = instructions
        self.model = model
        self.tools = ToolRegistry(tools)
        self.max_steps = max_steps

    def run(
        self,
        prompt: str,
        history: Optional[List[Message]] = None,
        **legacy_options: object,
    ) -> AgentResult:
        if legacy_options:
            raise _unsupported_legacy_option(legacy_options)

        messages: List[Message] = []
        if self.instructions:
            messages.append(Message(role="system", content=self.instructions))
        if history is not None:
            messages.extend(history)
        messages.append(Message(role="user", content=prompt))

        for step in range(1, self.max_steps + 1):
            response = self.model.complete(messages, self.tools.schemas())
            if not isinstance(response, ModelResponse):
                raise TypeError("legacy ModelClient.complete must return ModelResponse")
            messages.append(
                Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls or None,
                )
            )
            if not response.tool_calls:
                return AgentResult(output=response.content, messages=messages, steps=step)

            for call in response.tool_calls:
                selected = self.tools.get(call.name)
                if selected is None:
                    output = (
                        f"Tool not found: {call.name}. "
                        f"Available tools: {', '.join(self.tools.names())}"
                    )
                else:
                    try:
                        value = selected.run(call.arguments)
                    except Exception as exc:
                        output = f"Tool error: {type(exc).__name__}: {exc}"
                    else:
                        output = value if isinstance(value, str) else str(value)
                messages.append(
                    Message(
                        role="tool",
                        name=call.name,
                        tool_call_id=call.id,
                        content=output,
                    )
                )

        return AgentResult(
            output=f"{self.name} stopped after reaching max_steps={self.max_steps}.",
            messages=messages,
            steps=self.max_steps,
        )
