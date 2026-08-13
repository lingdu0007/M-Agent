from dataclasses import dataclass, field
import warnings
from typing import Any, Dict, List, Optional


warnings.warn(
    "agent_framework.types is deprecated in M-Agent 0.2.x and will be "
    "removed in 0.3.0. Migrate AgentResult to m_agent.RunRecord and "
    "RunInspection; adapt Message, ModelResponse, and ToolCall to their "
    "m_agent counterparts; see docs/migrating-from-0.1.md.",
    DeprecationWarning,
    stacklevel=2,
)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolCall":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            arguments=dict(data.get("arguments") or {}),
        )


@dataclass
class Message:
    role: str
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "name": self.name,
            "tool_call_id": self.tool_call_id,
            "tool_calls": [
                item.to_dict() for item in self.tool_calls or []
            ] or None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        raw_tool_calls = data.get("tool_calls") or []
        return cls(
            role=str(data.get("role", "")),
            content=str(data.get("content", "")),
            name=data.get("name"),
            tool_call_id=data.get("tool_call_id"),
            tool_calls=[ToolCall.from_dict(item) for item in raw_tool_calls] or None,
        )


@dataclass
class ModelResponse:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)


@dataclass
class AgentResult:
    output: str
    messages: List[Message]
    steps: int
    structured_output: Optional[Any] = None
