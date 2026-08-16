"""Chat Completions 风格 live Model Adapter（Ticket 10）。

适配 OpenAI 兼容的 ``POST {base_url}/chat/completions`` 端点
（``{base_url}/chat/completions``，base_url 语义与官方
``https://api.openai.com/v1`` 一致；也兼容不带 ``/v1`` 前缀的
兼容端点，此时路径为 ``/chat/completions``）。

能力声明（ADR 0030，如实且完整）：

- ``streaming=DELTA``：SSE 流式（``stream`` + ``stream_options.include_usage``）；
- ``tool_calling=NATIVE``：``tools`` + ``tool_choice: "auto"``；
- ``structured_output=NATIVE``：``response_format``（json_schema /
  json_object）；
- ``usage_reporting=PROVIDER_REPORTED``：usage 从 provider 返回时映射为
  :class:`ModelUsage`，缺失时显式为 None（不伪造）。

本 Adapter 是 live 实现（``deterministic=False``），构造不触网、不
要求凭证；凭证只在发出请求时从 embedding environment 读取（ADR 0033），
绝不进入持久化、日志或错误文本。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ._base import (
    ProviderModelAdapter,
    build_chat_messages,
    consume_sse_events,
    extract_usage,
    httpx,
    invalid_response,
    parse_chat_tool_calls,
    provider_error,
    resolve_chat_completions_path,
    resolve_chat_structured_output_mode,
    tool_spec_to_chat_schema,
    transport_error,
)
from m_agent._model import (
    ModelCapabilities,
    ModelContract,
    ModelDelta,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StreamingMode,
    StructuredOutputMode,
    ToolCallingMode,
    UsageReportingMode,
)
from m_agent._tools import ToolCall

#: Chat Completions 兼容端点能力声明：四类语义全部如实支持。
CHAT_COMPLETIONS_CAPABILITIES = ModelCapabilities(
    streaming=StreamingMode.DELTA,
    tool_calling=ToolCallingMode.NATIVE,
    structured_output=StructuredOutputMode.NATIVE,
    usage_reporting=UsageReportingMode.PROVIDER_REPORTED,
)


class ChatCompletionsModelAdapter(ProviderModelAdapter):
    """Live Chat Completions 风格 Model Adapter（OpenAI 兼容）。

    :param model: 模型标识；缺省从 ``M_AGENT_OPENAI_MODEL`` /
        ``OPENAI_MODEL`` / ``AGENT_MODEL`` 读取。
    凭证只由 embedding environment 的 ``M_AGENT_OPENAI_API_KEY`` /
        ``OPENAI_API_KEY`` 提供，且只在发出请求时读取。
    :param base_url: provider 根地址；缺省从
        ``M_AGENT_OPENAI_BASE_URL`` / ``OPENAI_BASE_URL`` /
        ``AGENT_BASE_URL`` 读取，最终默认
        ``https://api.openai.com/v1``。
    :param chat_completions_path: 端点路径，默认 ``/chat/completions``。
    :param model_contract: 当前 model/deployment 的显式实例 Contract；
        缺失时 Adapter 可构造但不能注册进 DefinitionRegistry。
    :param structured_output_schema: 可选 JSON Schema；声明
    structured_output 时随请求发送（json_schema / json_object）。
    :param structured_output_mode: 原生结构化输出模式，缺省为
        ``json_schema``；仅支持 JSON object 的兼容端点必须显式设为
        ``json_object``（也可通过 embedding environment 的
        ``M_AGENT_OPENAI_CHAT_STRUCTURED_OUTPUT_MODE`` 配置）。
    """

    capabilities: ModelCapabilities = CHAT_COMPLETIONS_CAPABILITIES

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        model_contract: ModelContract | None = None,
        chat_completions_path: str | None = None,
        structured_output_schema: dict[str, Any] | None = None,
        structured_output_name: str = "result",
        structured_output_mode: str | None = None,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            timeout=timeout,
            model_contract=model_contract,
            structured_output_schema=structured_output_schema,
            structured_output_name=structured_output_name,
        )
        self.chat_completions_path: str = resolve_chat_completions_path(
            chat_completions_path
        )
        self.structured_output_mode: str = resolve_chat_structured_output_mode(
            structured_output_mode
        )

    @property
    def endpoint_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.chat_completions_path}"

    def _definition_contract_configuration(self) -> dict[str, Any]:
        config = super()._definition_contract_configuration()
        config["chat_completions_path"] = self.chat_completions_path
        config["structured_output_mode"] = self.structured_output_mode
        return config

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": build_chat_messages(request),
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [
                tool_spec_to_chat_schema(spec) for spec in request.tools
            ]
            payload["tool_choice"] = "auto"
        structured = self._structured_output_payload
        if structured is not None:
            if self.structured_output_mode == "json_object":
                structured = {"type": "json_object"}
            payload["response_format"] = structured
        return payload

    async def generate(self, request: ModelRequest) -> ModelResponse:
        url = self.endpoint_url
        _, data = await self._post_json(url, self._build_payload(request))
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise _missing_field(url, "choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise _missing_field(url, "choices[0].message")
        content = message.get("content")
        tool_calls = parse_chat_tool_calls(message)
        return ModelResponse(
            content=content or None,
            tool_calls=tool_calls,
            usage=extract_usage(data),
        )

    async def stream(
        self, request: ModelRequest,
    ) -> AsyncIterator[ModelDelta | ModelResponse]:
        url = self.endpoint_url
        payload = self._build_payload(request)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        self.requests.append(url)
        api_key = self._require_api_key()
        try:
            async with self._http_client().stream(
                "POST", url, json=payload, headers=self._headers(api_key)
            ) as response:
                # 错误路径不读取响应体：错误消息只含稳定状态码，
                # 且避免对保持连接的 chunked 响应阻塞等待 EOF。
                if (
                    300 <= response.status_code < 400
                    or response.status_code >= 400
                ):
                    raise provider_error(response.status_code, operation=url)
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    raise invalid_response(
                        url, "unexpected SSE content type"
                    )
                content_parts: list[str] = []
                tool_calls: dict[int, dict[str, Any]] = {}
                usage: ModelUsage | None = None
                async for event in consume_sse_events(response):
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            content_parts.append(text)
                            yield ModelDelta(content=text)
                        for call in delta.get("tool_calls") or []:
                            index = call.get("index", 0)
                            entry = tool_calls.setdefault(
                                index, {"id": None, "name": None, "arguments": ""}
                            )
                            if call.get("id"):
                                entry["id"] = call["id"]
                            function = call.get("function") or {}
                            if function.get("name"):
                                entry["name"] = function["name"]
                            if function.get("arguments"):
                                entry["arguments"] += function["arguments"]
                    usage = usage or extract_usage(event)
                calls = tuple(
                    ToolCall(
                        call_id=entry["id"] or f"call_{index}",
                        tool_name=entry["name"] or "",
                        arguments=entry["arguments"] or "{}",
                    )
                    for index, entry in sorted(tool_calls.items())
                )
                yield ModelResponse(
                    content="".join(content_parts) or None,
                    tool_calls=calls,
                    usage=usage,
                )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise transport_error(exc, operation=url) from exc

    async def aclose(self) -> None:
        await super().aclose()


def _missing_field(url: str, field: str):
    return invalid_response(url, f"missing field {field!r}")
