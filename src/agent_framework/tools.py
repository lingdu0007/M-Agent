import inspect
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional


warnings.warn(
    "agent_framework.tools (Tool, ToolRegistry, and tool) is deprecated in "
    "M-Agent 0.2.x and will be removed in 0.3.0. Migrate to m_agent.Tool "
    "with explicit ToolEffect and AgentDefinition.tools; see "
    "docs/migrating-from-0.1.md.",
    DeprecationWarning,
    stacklevel=2,
)


def _json_type(annotation: Any) -> str:
    if annotation in (int, "int"):
        return "integer"
    if annotation in (float, "float"):
        return "number"
    if annotation in (bool, "bool"):
        return "boolean"
    if annotation in (dict, "dict", Dict):
        return "object"
    if annotation in (list, "list", List):
        return "array"
    return "string"


def _clean_doc(value: Optional[str]) -> str:
    if not value:
        return ""
    return inspect.cleandoc(value).strip()


@dataclass
class Tool:
    name: str
    description: str
    func: Callable[..., Any]

    @classmethod
    def from_function(
        cls,
        func: Callable[..., Any],
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> "Tool":
        return cls(
            name=name or func.__name__,
            description=description or _clean_doc(func.__doc__) or func.__name__,
            func=func,
        )

    def run(self, arguments: Dict[str, Any]) -> Any:
        signature = inspect.signature(self.func)
        bound = signature.bind_partial(**arguments)
        bound.apply_defaults()
        return self.func(*bound.args, **bound.kwargs)

    def to_openai_schema(self) -> Dict[str, Any]:
        signature = inspect.signature(self.func)
        properties: Dict[str, Dict[str, Any]] = {}
        required: List[str] = []

        for param_name, param in signature.parameters.items():
            if param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue

            annotation = param.annotation
            if annotation is inspect.Signature.empty:
                annotation = str

            schema: Dict[str, Any] = {"type": _json_type(annotation)}
            if param.default is not inspect.Signature.empty:
                schema["default"] = param.default
            else:
                required.append(param_name)
            properties[param_name] = schema

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


class ToolRegistry:
    def __init__(self, tools: Optional[Iterable[Tool]] = None) -> None:
        self._tools: Dict[str, Tool] = {}
        for item in tools or []:
            self.add(item)

    def add(self, item: Tool) -> Tool:
        if item.name in self._tools:
            raise ValueError(f"Tool already registered: {item.name}")
        self._tools[item.name] = item
        return item

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def schemas(self) -> List[Dict[str, Any]]:
        return [item.to_openai_schema() for item in self._tools.values()]

    def names(self) -> List[str]:
        return list(self._tools.keys())


def tool(
    func: Optional[Callable[..., Any]] = None,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> Any:
    def decorator(inner: Callable[..., Any]) -> Tool:
        return Tool.from_function(inner, name=name, description=description)

    if func is None:
        return decorator
    return decorator(func)
