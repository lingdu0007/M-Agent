import json
import os
import re
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Protocol, Tuple

from .types import Message, ModelResponse, ToolCall


class ModelClient(Protocol):
    def complete(self, messages: List[Message], tools: List[Dict[str, Any]]) -> ModelResponse:
        ...


class EchoModel:
    def complete(self, messages: List[Message], tools: List[Dict[str, Any]]) -> ModelResponse:
        last_user = _last_content(messages, "user")
        return ModelResponse(content=f"Echo: {last_user}")


class RuleBasedDemoModel:
    """A local model stub for learning the agent loop without an API key."""

    def complete(self, messages: List[Message], tools: List[Dict[str, Any]]) -> ModelResponse:
        if messages and messages[-1].role == "tool":
            tool_name = messages[-1].name or "tool"
            return ModelResponse(content=f"{tool_name} returned: {messages[-1].content}")

        prompt = _last_content(messages, "user")
        tool_names = {
            item.get("function", {}).get("name")
            for item in tools
            if item.get("type") == "function"
        }

        if "get_current_time" in tool_names and _mentions_time(prompt):
            return ModelResponse(
                tool_calls=[
                    ToolCall(id="call_get_current_time", name="get_current_time", arguments={})
                ]
            )

        if "add" in tool_names:
            numbers = _numbers(prompt)
            if len(numbers) >= 2:
                return ModelResponse(
                    tool_calls=[
                        ToolCall(
                            id="call_add",
                            name="add",
                            arguments={"a": numbers[0], "b": numbers[1]},
                        )
                    ]
                )

        return ModelResponse(content=f"I can answer directly: {prompt}")


class OpenAICompatibleClient:
    """Minimal OpenAI-compatible /v1/chat/completions client using stdlib only."""

    def __init__(
        self,
        model: Optional[str] = None,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.2,
        timeout: int = 60,
    ) -> None:
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.temperature = temperature
        self.timeout = timeout

    def complete(self, messages: List[Message], tools: List[Dict[str, Any]]) -> ModelResponse:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAICompatibleClient")

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [_message_to_openai(item) for item in messages],
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Model request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Model request failed: {exc.reason}") from exc

        data = json.loads(raw)
        message = data["choices"][0]["message"]
        calls = []
        for index, item in enumerate(message.get("tool_calls") or []):
            function = item.get("function", {})
            calls.append(
                ToolCall(
                    id=item.get("id") or f"call_{index}",
                    name=function.get("name", ""),
                    arguments=_parse_arguments(function.get("arguments", "{}")),
                )
            )

        return ModelResponse(content=message.get("content") or "", tool_calls=calls)


class OpenAIResponsesClient:
    """Minimal OpenAI Responses API client using stdlib only."""

    def __init__(
        self,
        model: Optional[str] = None,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        endpoint_path: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: int = 60,
    ) -> None:
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.reasoning_effort = reasoning_effort or os.getenv("OPENAI_REASONING_EFFORT")
        self.endpoint_path = endpoint_path or os.getenv("OPENAI_RESPONSES_PATH", "/responses")
        self.temperature = temperature
        self.timeout = timeout

    def complete(self, messages: List[Message], tools: List[Dict[str, Any]]) -> ModelResponse:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAIResponsesClient")

        instructions, input_items = _messages_to_responses(messages)
        payload: Dict[str, Any] = {
            "model": self.model,
            "input": input_items,
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = [_tool_to_responses_schema(item) for item in tools]
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        request = urllib.request.Request(
            _join_api_path(self.base_url, self.endpoint_path),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Model request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Model request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Model request timed out after {self.timeout}s") from exc
        except socket.timeout as exc:
            raise RuntimeError(f"Model request timed out after {self.timeout}s") from exc

        return _response_to_model_response(json.loads(raw))


def _last_content(messages: List[Message], role: str) -> str:
    for item in reversed(messages):
        if item.role == role:
            return item.content
    return ""


def _mentions_time(prompt: str) -> bool:
    lower = prompt.lower()
    return any(word in lower for word in ["time", "date", "today", "now"]) or any(
        word in prompt for word in ["时间", "日期", "今天", "现在"]
    )


def _numbers(prompt: str) -> List[float]:
    return [float(item) for item in re.findall(r"-?\d+(?:\.\d+)?", prompt)]


def _parse_arguments(raw: str) -> Dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_raw": raw}
    if isinstance(value, dict):
        return value
    return {"value": value}


def _message_to_openai(message: Message) -> Dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }

    result: Dict[str, Any] = {"role": message.role, "content": message.content}
    if message.name:
        result["name"] = message.name
    if message.tool_calls:
        result["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    return result


def _messages_to_responses(messages: List[Message]) -> Tuple[str, List[Dict[str, Any]]]:
    instructions = []
    input_items: List[Dict[str, Any]] = []

    for message in messages:
        if message.role == "system":
            instructions.append(message.content)
            continue

        if message.role == "tool":
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
            continue

        if message.tool_calls:
            for call in message.tool_calls:
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    }
                )

        if message.content:
            input_items.append({"role": message.role, "content": message.content})

    return "\n\n".join(instructions), input_items


def _tool_to_responses_schema(tool_schema: Dict[str, Any]) -> Dict[str, Any]:
    function = tool_schema.get("function", {})
    return {
        "type": "function",
        "name": function.get("name", ""),
        "description": function.get("description", ""),
        "parameters": function.get("parameters", {"type": "object", "properties": {}}),
    }


def _response_to_model_response(data: Dict[str, Any]) -> ModelResponse:
    calls: List[ToolCall] = []
    text_parts: List[str] = []

    for index, item in enumerate(data.get("output") or []):
        item_type = item.get("type")
        if item_type == "function_call":
            calls.append(
                ToolCall(
                    id=item.get("call_id") or item.get("id") or f"call_{index}",
                    name=item.get("name", ""),
                    arguments=_parse_arguments(item.get("arguments", "{}")),
                )
            )
            continue

        if item_type == "message":
            for content in item.get("content") or []:
                if isinstance(content, str):
                    text_parts.append(content)
                    continue
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if text and content.get("type") in ("output_text", "text"):
                    text_parts.append(text)

    content = data.get("output_text") or "\n".join(text_parts)
    return ModelResponse(content=content, tool_calls=calls)


def _join_api_path(base_url: str, endpoint_path: str) -> str:
    if not endpoint_path.startswith("/"):
        endpoint_path = f"/{endpoint_path}"
    return f"{base_url.rstrip('/')}{endpoint_path}"
