import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Protocol


@dataclass
class GuardrailDecision:
    allowed: bool
    reason: str = ""
    rule: str = ""

    @classmethod
    def allow(cls, rule: str = "") -> "GuardrailDecision":
        return cls(allowed=True, rule=rule)

    @classmethod
    def block(cls, reason: str, rule: str = "") -> "GuardrailDecision":
        return cls(allowed=False, reason=reason, rule=rule)


class Guardrail(Protocol):
    name: str

    def check_tool_call(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> GuardrailDecision:
        ...


class ToolAllowlistGuardrail:
    name = "tool_allowlist"

    def __init__(self, allowed_tools: Iterable[str]) -> None:
        self.allowed_tools = set(allowed_tools)

    def check_tool_call(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> GuardrailDecision:
        if tool_name in self.allowed_tools:
            return GuardrailDecision.allow(self.name)
        return GuardrailDecision.block(
            f"Tool is not allowed: {tool_name}",
            rule=self.name,
        )


class ToolDenylistGuardrail:
    name = "tool_denylist"

    def __init__(self, denied_tools: Iterable[str]) -> None:
        self.denied_tools = set(denied_tools)

    def check_tool_call(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> GuardrailDecision:
        if tool_name in self.denied_tools:
            return GuardrailDecision.block(
                f"Tool is denied: {tool_name}",
                rule=self.name,
            )
        return GuardrailDecision.allow(self.name)


class SensitiveArgumentGuardrail:
    name = "sensitive_arguments"

    def __init__(
        self,
        *,
        blocked_keys: Optional[Iterable[str]] = None,
        blocked_patterns: Optional[Iterable[str]] = None,
    ) -> None:
        self.blocked_keys = {item.lower() for item in (blocked_keys or [])}
        self.blocked_patterns = [
            re.compile(pattern, re.IGNORECASE) for pattern in (blocked_patterns or [])
        ]

    def check_tool_call(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> GuardrailDecision:
        for key, value in _flatten_arguments(arguments):
            if key.lower() in self.blocked_keys:
                return GuardrailDecision.block(
                    f"Argument key is blocked: {key}",
                    rule=self.name,
                )

            text = str(value)
            for pattern in self.blocked_patterns:
                if pattern.search(text):
                    return GuardrailDecision.block(
                        f"Argument value matched blocked pattern: {key}",
                        rule=self.name,
                    )

        return GuardrailDecision.allow(self.name)


def check_guardrails(
    guardrails: Iterable[Guardrail], tool_name: str, arguments: Dict[str, Any]
) -> GuardrailDecision:
    for guardrail in guardrails:
        decision = guardrail.check_tool_call(tool_name, arguments)
        if not decision.allowed:
            return decision
    return GuardrailDecision.allow()


def _flatten_arguments(arguments: Dict[str, Any], prefix: str = "") -> List[tuple]:
    flattened = []
    for key, value in arguments.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flattened.extend(_flatten_arguments(value, prefix=full_key))
        else:
            flattened.append((full_key, value))
    return flattened
