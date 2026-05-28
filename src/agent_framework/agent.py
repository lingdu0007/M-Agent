import json
from typing import Iterable, List, Optional

from .guardrails import Guardrail, check_guardrails
from .memory import Memory
from .models import ModelClient
from .session_memory import SessionMemory
from .structured_output import OutputSchema, parse_structured_output
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
        guardrails: Optional[Iterable[Guardrail]] = None,
        memory: Optional[Memory] = None,
        session_memory: Optional[SessionMemory] = None,
        session_id: Optional[str] = None,
        tracer: Optional[Tracer] = None,
        max_steps: int = 6,
    ) -> None:
        self.name = name
        self.instructions = instructions
        self.model = model
        self.tools = ToolRegistry(tools)
        self.guardrails = list(guardrails or [])
        self.memory = memory
        self.session_memory = session_memory
        self.session_id = session_id
        self.tracer = tracer or NoopTracer()
        self.max_steps = max_steps

    def run(
        self,
        prompt: str,
        history: Optional[List[Message]] = None,
        session_id: Optional[str] = None,
        output_schema: Optional[OutputSchema] = None,
    ) -> AgentResult:
        self._trace("agent.run.start", prompt_chars=len(prompt))
        messages: List[Message] = []
        if self.instructions:
            messages.append(
                Message(
                    role="system",
                    content=self._instructions_with_schema(output_schema),
                )
            )
        messages.extend(self._load_history(history, session_id=session_id))
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
                self._save_history(messages, session_id=session_id)
                structured_output = self._parse_output(response.content, output_schema)
                self._trace(
                    "agent.run.end",
                    stop_reason="final",
                    steps=step,
                    output_chars=len(response.content),
                )
                return AgentResult(
                    output=response.content,
                    messages=messages,
                    steps=step,
                    structured_output=structured_output,
                )

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

        self._save_history(messages, session_id=session_id)
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

    def clear_memory(self, session_id: Optional[str] = None) -> None:
        if self.session_memory:
            resolved = self._resolve_session_id(session_id)
            if resolved is None:
                return
            self.session_memory.clear(resolved)
            self._trace("session.clear", session_id=resolved)
            return

        if self.memory:
            self.memory.clear()
            self._trace("memory.clear", target=type(self.memory).__name__)

    def list_sessions(self) -> List[str]:
        if not self.session_memory:
            return []
        sessions = self.session_memory.list_sessions()
        self._trace("session.list", session_count=len(sessions))
        return sessions

    def delete_session(self, session_id: str) -> None:
        if not self.session_memory:
            return
        self.session_memory.delete_session(session_id)
        self._trace("session.delete", session_id=session_id)

    def _load_history(
        self, history: Optional[List[Message]], session_id: Optional[str] = None
    ) -> List[Message]:
        if history is not None:
            loaded = list(history)
            self._trace("memory.load", source="argument", message_count=len(loaded))
            return loaded

        resolved_session_id = self._resolve_session_id(session_id)
        if self.session_memory and resolved_session_id is not None:
            loaded = self.session_memory.load(resolved_session_id)
            self._trace(
                "session.load",
                session_id=resolved_session_id,
                message_count=len(loaded),
            )
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

    def _save_history(
        self, messages: List[Message], session_id: Optional[str] = None
    ) -> None:
        saved = [message for message in messages if message.role != "system"]

        resolved_session_id = self._resolve_session_id(session_id)
        if self.session_memory and resolved_session_id is not None:
            self.session_memory.save(resolved_session_id, saved)
            self._trace(
                "session.save",
                session_id=resolved_session_id,
                message_count=len(saved),
            )
            return

        if not self.memory:
            self._trace("memory.save", target="none", message_count=0)
            return
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

        decision = check_guardrails(self.guardrails, name, arguments)
        if not decision.allowed:
            output = f"Tool blocked by guardrail: {decision.reason}"
            self._trace(
                "guardrail.block",
                tool=name,
                rule=decision.rule,
                reason=decision.reason,
            )
            return output
        self._trace("guardrail.allow", tool=name, rule=decision.rule)

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

    def _resolve_session_id(self, session_id: Optional[str]) -> Optional[str]:
        if not self.session_memory:
            return None
        return session_id or self.session_id or "default"

    def _instructions_with_schema(self, output_schema: Optional[OutputSchema]) -> str:
        if output_schema is None:
            return self.instructions
        return f"{self.instructions}\n\n{output_schema.instructions()}"

    def _parse_output(self, text: str, output_schema: Optional[OutputSchema]) -> object:
        if output_schema is None:
            return None
        try:
            parsed = parse_structured_output(text, output_schema)
        except Exception as exc:
            self._trace(
                "structured_output.error",
                schema=output_schema.name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        self._trace("structured_output.valid", schema=output_schema.name)
        return parsed
