import json
from typing import Iterable, List, Optional

from .memory import Memory
from .models import ModelClient
from .tools import Tool, ToolRegistry
from .trace import NoopTracer, TraceEvent, Tracer
from .types import AgentResult, Message, ModelResponse


class Agent:
    def __init__(
        self,
        *,
        name: str,
        instructions: str,
        model: ModelClient,
        tools: Optional[Iterable[Tool]] = None,
        memory: Optional[Memory] = None,
        tracer: Optional[Tracer] = None,
        max_steps: int = 6,
    ) -> None:
        self.name = name
        self.instructions = instructions
        self.model = model
        self.tools = ToolRegistry(tools)
        self.memory = memory
        self.tracer = tracer or NoopTracer()
        self.max_steps = max_steps

    def run(self, prompt: str, history: Optional[List[Message]] = None) -> AgentResult:
        self._trace("agent.run.start", prompt_chars=len(prompt))
        messages: List[Message] = []
        if self.instructions:
            messages.append(Message(role="system", content=self.instructions))
        messages.extend(self._load_history(history))
        messages.append(Message(role="user", content=prompt))

        for step in range(1, self.max_steps + 1):
            tool_schemas = self.tools.schemas()
            self._trace(
                "model.call.start",
                step=step,
                message_count=len(messages),
                tool_count=len(tool_schemas),
            )
            try:
                response = self.model.complete(messages, tool_schemas)
            except Exception as exc:
                self._trace(
                    "model.call.error",
                    step=step,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                raise
            self._trace(
                "model.call.end",
                step=step,
                content_chars=len(response.content),
                tool_calls=[call.name for call in response.tool_calls],
            )
            self._append_assistant_response(messages, response)

            if not response.tool_calls:
                self._save_history(messages)
                self._trace(
                    "agent.run.end",
                    stop_reason="final",
                    steps=step,
                    output_chars=len(response.content),
                )
                return AgentResult(output=response.content, messages=messages, steps=step)

            for call in response.tool_calls:
                tool_output = self._run_tool(call.name, call.arguments)
                messages.append(
                    Message(
                        role="tool",
                        name=call.name,
                        tool_call_id=call.id,
                        content=tool_output,
                    )
                )

        self._save_history(messages)
        self._trace(
            "agent.run.end",
            stop_reason="max_steps",
            steps=self.max_steps,
            output_chars=0,
        )
        return AgentResult(
            output=f"{self.name} stopped after reaching max_steps={self.max_steps}.",
            messages=messages,
            steps=self.max_steps,
        )

    def clear_memory(self) -> None:
        if self.memory:
            self.memory.clear()

    def _load_history(self, history: Optional[List[Message]]) -> List[Message]:
        if history is not None:
            loaded = list(history)
            self._trace("memory.load", source="argument", message_count=len(loaded))
            return loaded
        if not self.memory:
            self._trace("memory.load", source="none", message_count=0)
            return []
        loaded = self.memory.load()
        self._trace(
            "memory.load",
            source=type(self.memory).__name__,
            message_count=len(loaded),
        )
        return loaded

    def _save_history(self, messages: List[Message]) -> None:
        if not self.memory:
            self._trace("memory.save", target="none", message_count=0)
            return
        saved = [message for message in messages if message.role != "system"]
        self.memory.save(saved)
        self._trace(
            "memory.save",
            target=type(self.memory).__name__,
            message_count=len(saved),
        )

    def _append_assistant_response(
        self, messages: List[Message], response: ModelResponse
    ) -> None:
        messages.append(
            Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls or None,
            )
        )

    def _run_tool(self, name: str, arguments: dict) -> str:
        self._trace(
            "tool.call.start",
            tool=name,
            argument_keys=sorted(arguments.keys()),
        )
        selected = self.tools.get(name)
        if selected is None:
            output = f"Tool not found: {name}. Available tools: {', '.join(self.tools.names())}"
            self._trace(
                "tool.call.error",
                tool=name,
                error_type="ToolNotFound",
                output_chars=len(output),
            )
            return output

        try:
            value = selected.run(arguments)
        except Exception as exc:
            output = f"Tool error: {type(exc).__name__}: {exc}"
            self._trace(
                "tool.call.error",
                tool=name,
                error_type=type(exc).__name__,
                output_chars=len(output),
            )
            return output

        if isinstance(value, str):
            output = value
        else:
            output = json.dumps(value, ensure_ascii=False, default=str)
        self._trace("tool.call.end", tool=name, output_chars=len(output))
        return output

    def _trace(self, name: str, **data: object) -> None:
        self.tracer.record(TraceEvent(name=name, data={"agent": self.name, **data}))
