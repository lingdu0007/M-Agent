"""Responses 风格 live Model Adapter（Ticket 10）。

适配 OpenAI 兼容的 Responses API 端点（``{base_url}{responses_path}``，
默认 ``https://api.openai.com/v1/responses``；也兼容不带 ``/v1`` 前缀
的兼容端点，此时默认路径为 ``/responses``）。

能力声明（ADR 0030，如实且完整）：

- ``streaming=DELTA``：SSE 事件流（``response.output_text.delta`` /
  ``response.function_call_arguments.delta`` / ``response.completed``）；
- ``tool_calling=NATIVE``：``tools``（function 工具）；
- ``structured_output=NATIVE``：``text.format``（json_schema）；
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
    consume_sse_events,
    extract_usage,
    httpx,
    invalid_response,
    parse_responses_output,
    provider_error,
    resolve_responses_path,
    serialize_context_item_data,
    tool_spec_to_responses_schema,
    transport_error,
)
from m_agent._model import (
    ModelCapabilityCombination,
    ModelCapabilities,
    ModelContract,
    ModelDelta,
    ModelRequest,
    ModelResponse,
    StreamingMode,
    StructuredOutputMode,
    ToolCallingMode,
    UsageReportingMode,
)

#: Responses 兼容端点能力声明：四类语义全部如实支持。
RESPONSES_CAPABILITIES = ModelCapabilities(
    streaming=StreamingMode.DELTA,
    tool_calling=ToolCallingMode.NATIVE,
    structured_output=StructuredOutputMode.NATIVE,
    usage_reporting=UsageReportingMode.PROVIDER_REPORTED,
    supported_combinations=(
        ModelCapabilityCombination(
            streaming=StreamingMode.DELTA,
            tool_calling=ToolCallingMode.NATIVE,
            structured_output=StructuredOutputMode.NATIVE,
            usage_reporting=UsageReportingMode.PROVIDER_REPORTED,
        ),
    ),
)


class ResponsesModelAdapter(ProviderModelAdapter):
    """Live Responses 风格 Model Adapter（OpenAI 兼容）。

    :param model: 模型标识；缺省从 ``M_AGENT_OPENAI_MODEL`` /
        ``OPENAI_MODEL`` / ``AGENT_MODEL`` 读取。
    凭证只由 embedding environment 的 ``M_AGENT_OPENAI_API_KEY`` /
        ``OPENAI_API_KEY`` 提供，且只在发出请求时读取。
    :param base_url: provider 根地址；缺省从
        ``M_AGENT_OPENAI_BASE_URL`` / ``OPENAI_BASE_URL`` /
        ``AGENT_BASE_URL`` 读取，最终默认
        ``https://api.openai.com/v1``。
    :param responses_path: 端点路径，默认 ``/responses``（也读取
        ``M_AGENT_OPENAI_RESPONSES_PATH`` / ``OPENAI_RESPONSES_PATH`` /
        ``AGENT_RESPONSES_PATH``）。
    :param model_contract: 当前 model/deployment 的显式实例 Contract；
        缺失时 Adapter 可构造但不能注册进 DefinitionRegistry。
    :param structured_output_schema: 可选 JSON Schema；声明
        structured_output 时随请求发送（json_schema）。
    """

    capabilities: ModelCapabilities = RESPONSES_CAPABILITIES

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        model_contract: ModelContract | None = None,
        responses_path: str | None = None,
        structured_output_schema: dict[str, Any] | None = None,
        structured_output_name: str = "result",
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            timeout=timeout,
            model_contract=model_contract,
            structured_output_schema=structured_output_schema,
            structured_output_name=structured_output_name,
        )
        self.responses_path: str = resolve_responses_path(responses_path)

    @property
    def endpoint_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.responses_path}"

    def _definition_contract_configuration(self) -> dict[str, Any]:
        config = super()._definition_contract_configuration()
        config["responses_path"] = self.responses_path
        return config

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        messages = _responses_input(request)
        payload: dict[str, Any] = {
            "model": self.model,
            "input": messages["items"],
            "instructions": messages["instructions"],
            "stream": False,
        }
        if request.tools:
            payload["tools"] = [
                tool_spec_to_responses_schema(spec) for spec in request.tools
            ]
        structured = self._structured_output_responses_payload
        if structured is not None:
            payload["text"] = {"format": structured}
        return payload

    async def generate(self, request: ModelRequest) -> ModelResponse:
        url = self.endpoint_url
        _, data = await self._post_json(url, self._build_payload(request))
        output = data.get("output")
        if not isinstance(output, list):
            raise invalid_response(url, "missing field 'output'")
        content, tool_calls = parse_responses_output(output)
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
                async for event in consume_sse_events(response):
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if delta:
                            yield ModelDelta(content=delta)
                    elif event_type == "response.completed":
                        complete = event.get("response")
                        if not isinstance(complete, dict):
                            raise invalid_response(
                                url, "response.completed without 'response'"
                            )
                        output = complete.get("output")
                        if not isinstance(output, list):
                            raise invalid_response(
                                url, "completed response missing 'output'"
                            )
                        content, tool_calls = parse_responses_output(output)
                        yield ModelResponse(
                            content=content or None,
                            tool_calls=tool_calls,
                            usage=extract_usage(complete),
                        )
                        return
                    elif event_type == "response.failed":
                        raise invalid_response(
                            url, "provider stream reported failure"
                        )
                # 流正常结束但未收到 response.completed：视为响应不完整。
                raise invalid_response(
                    url, "stream ended without response.completed"
                )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise transport_error(exc, operation=url) from exc

    async def aclose(self) -> None:
        await super().aclose()


def _responses_input(request: ModelRequest) -> dict[str, Any]:
    """把统一 ModelRequest 映射为 Responses ``input`` 数组。

    指令（Agent Instruction，受信）提升为顶层 ``instructions``；
    Context Items（外部数据，ADR 0017）作为 input data 交付；
    工具结果以 ``function_call_output`` 消息交付（标准 Responses
    协议）。与 Chat Completions 一样，工具调用的参数不在
    :class:`ToolOutcome` 中，因此重建的 ``function_call`` 使用空参数，
    以 ``call_id`` 关联结果。
    """
    items: list[dict[str, Any]] = [{"type": "input_text", "text": request.input}]
    for item in request.context_items:
        items.append(
            {
                "type": "input_text",
                "text": serialize_context_item_data(item),
            }
        )
    for outcome in request.tool_outcomes:
        items.append(
            {
                "type": "function_call",
                "call_id": outcome.call_id,
                "name": outcome.tool_name,
                "arguments": "{}",
            }
        )
        content = (
            outcome.result
            if outcome.result is not None
            else f"rejected({outcome.code}): {outcome.message}"
        )
        items.append(
            {
                "type": "function_call_output",
                "call_id": outcome.call_id,
                "output": content,
            }
        )
    return {"instructions": request.instructions, "items": items}
