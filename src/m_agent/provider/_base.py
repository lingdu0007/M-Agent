"""Live provider Model Adapter 共享基础（Ticket 10 / ADR 0030 / ADR 0038）。

本子包提供访问真实供应商 HTTP API 的 live Model Adapter。它是
:class:`m_agent.ModelAdapter` 的**扩展实现**，与核心
:class:`m_agent.DeterministicModelAdapter`（fake，``deterministic=True``）
在任何场景都不会混淆：本子包的所有 Adapter ``deterministic`` 恒为
False，且只在调用方显式提供凭证后才会发起网络请求。

依赖边界（ADR 0038）：本子包需要 ``httpx``，通过 ``provider`` extra
安装（``pip install ".[provider]"``）。核心 ``m_agent`` 包不依赖
``httpx``，默认安装也不会导入本子包。

凭证边界（ADR 0033 / Ticket 10 AC）：

- 凭证（API Key）只从 embedding environment 读取，**永不写入**
  Definition Snapshot、Run Payload、Checkpoint、Run Update、
  Telemetry、fixture 或错误文本；
- Adapter 不把凭证放入任何异常消息（``ModelFailure`` 只携带稳定
  错误码与 HTTP 状态），错误响应体不进入错误文本；
- 本模块绝不读取或回显 provider 返回的错误 body。

能力声明（ADR 0030）：两个 Adapter 都如实声明 streaming、tool
calling、native structured output 与 usage reporting 支持；Definition
注册时由 :class:`m_agent.DefinitionRegistry` 校验 required capabilities
（不匹配在发出任何网络请求前抛 :class:`m_agent.ModelCapabilityError`）。
Runner 只在声明支持时调用对应路径，本子包不做静默降级。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from m_agent._context import ContextItem
from m_agent._failure import FailureClassification, ModelFailure
from m_agent._model import (
    ModelAdapter,
    ModelContract,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StructuredOutputMode,
)
from m_agent._tools import ToolCall, ToolSpec

try:  # provider extra（ADR 0038）：httpx 缺席时给出可操作的安装提示。
    import httpx
except ImportError as exc:  # pragma: no cover - 依赖缺失路径
    raise ImportError(
        "live provider adapters require the 'provider' extra of this "
        "distribution (pip install \".[provider]\"); the core m_agent "
        "package stays httpx-free"
    ) from exc


# -- 凭证 / 配置解析（只从 embedding environment 读取，绝不持久化） -----

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4.1-mini"


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def resolve_api_key() -> str | None:
    """只从 embedding environment 读取 API key。

    Adapter 不接受调用方传入的凭证值，避免 secret 进入对象构造、fixture
    或任何持久化边界；未配置返回 None，发出请求时才会报错。
    """
    return _first_env("M_AGENT_OPENAI_API_KEY", "OPENAI_API_KEY")


def resolve_base_url(base_url: str | None) -> str:
    value = (
        base_url
        or _first_env(
            "M_AGENT_OPENAI_BASE_URL", "OPENAI_BASE_URL", "AGENT_BASE_URL"
        )
        or _DEFAULT_BASE_URL
    )
    return _strip_userinfo(value)


def _strip_userinfo(url: str) -> str:
    """移除 base URL 的敏感组成，防止进入请求记录或错误文本。

    配置 URL 的 userinfo、query 和 fragment 都可能承载凭证。Adapter
    只接受 scheme、host（含 port）和 path 作为端点基址（ADR 0033）。
    这里不负责验证 URL；保守地删除无法安全持久化的部分即可。
    """
    scheme, separator, rest = url.partition("://")
    if not separator:
        return url.split("?", 1)[0].split("#", 1)[0]
    authority, slash, suffix = rest.partition("/")
    host = authority.rsplit("@", 1)[-1]
    host = host.split("?", 1)[0].split("#", 1)[0]
    path = suffix.split("?", 1)[0].split("#", 1)[0]
    return f"{scheme}://{host}{slash}{path}"


def resolve_model(model: str | None) -> str:
    return (
        model
        or _first_env("M_AGENT_OPENAI_MODEL", "OPENAI_MODEL", "AGENT_MODEL")
        or _DEFAULT_MODEL
    )


def resolve_responses_path(path: str | None) -> str:
    value = path or _first_env(
        "M_AGENT_OPENAI_RESPONSES_PATH",
        "OPENAI_RESPONSES_PATH",
        "AGENT_RESPONSES_PATH",
    ) or "/responses"
    return _strip_endpoint_path(value)


def resolve_chat_completions_path(path: str | None) -> str:
    value = path or _first_env(
        "M_AGENT_OPENAI_CHAT_COMPLETIONS_PATH",
        "OPENAI_CHAT_COMPLETIONS_PATH",
    ) or "/chat/completions"
    return _strip_endpoint_path(value)


def resolve_chat_structured_output_mode(mode: str | None) -> str:
    """Resolve the explicit native JSON mode for a Chat-compatible endpoint.

    ``json_schema`` is the default for OpenAI-compatible endpoints that support
    strict schemas. Providers limited to native JSON-object mode must opt in
    explicitly rather than silently weakening the structured-output contract.
    """
    value = mode or _first_env(
        "M_AGENT_OPENAI_CHAT_STRUCTURED_OUTPUT_MODE",
        "OPENAI_CHAT_STRUCTURED_OUTPUT_MODE",
    ) or "json_schema"
    if value not in {"json_schema", "json_object"}:
        raise ValueError(
            "structured_output_mode must be 'json_schema' or 'json_object'"
        )
    return value


def _strip_endpoint_path(path: str) -> str:
    """保留 endpoint path，丢弃可能承载 token 的 query / fragment。"""
    return path.split("?", 1)[0].split("#", 1)[0]


# -- provider 错误分类（结构化 StepFailure，绝不解析异常文本） -----------


def classify_provider_status(status_code: int) -> tuple[FailureClassification, str]:
    """把 provider HTTP 状态归一为结构化分类（Ticket 06 / ADR 0025）。

    - 429 / 5xx：瞬时（可能恢复），分类 ``TRANSIENT``；
    - 其余 4xx：请求本身被拒绝，分类 ``PERMANENT``（不自动重试）。
    错误消息只含稳定状态码，**不包含 provider 错误 body**，避免任何
    回显凭证或运行时做内容审计。
    """
    if status_code == 429 or 500 <= status_code <= 599:
        return FailureClassification.TRANSIENT, "provider_unavailable"
    return FailureClassification.PERMANENT, "provider_request_failed"


def provider_error(
    status_code: int, *, operation: str
) -> ModelFailure:
    classification, code = classify_provider_status(status_code)
    return ModelFailure(
        classification,
        code,
        f"{operation} failed with HTTP {status_code}",
    )


def transport_error(exc: BaseException, *, operation: str) -> ModelFailure:
    """网络 / 超时 / 传输错误：瞬时分类；消息不含任何请求内容。"""
    return ModelFailure(
        FailureClassification.TRANSIENT,
        "provider_transport_error",
        f"{operation} failed at the transport layer: {type(exc).__name__}",
    )


def invalid_response(operation: str, detail: str) -> ModelFailure:
    return ModelFailure(
        FailureClassification.PERMANENT,
        "provider_response_invalid",
        f"{operation} returned an unparseable response: {detail}",
    )


# -- provider 数据模型转换（不进入核心领域模型） ------------------------


def serialize_context_item_data(item: ContextItem) -> str:
    """保留完整 provenance，把不可信 Context Item 编码为模型数据。"""
    return json.dumps(
        item.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def build_chat_messages(request: ModelRequest) -> list[dict[str, Any]]:
    """把统一 ModelRequest 映射为 Chat Completions ``messages``。

    指令（Agent Instruction，受信）与上下文 / 工具结果（外部数据，
    ADR 0017）严格分层：唯一的 system 消息只承载 Definition
    instructions；Context Items 作为 user data 消息交付，工具结果以标准
    ``tool`` 消息交付。工具调用的参数不在
    :class:`ToolOutcome` 中（M-Agent 只 checkpoint 结果），因此重建
    的 assistant ``tool_calls`` 使用空参数，以 ``call_id`` 关联结果。
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": request.instructions},
        {"role": "user", "content": request.input},
    ]
    for item in request.context_items:
        messages.append(
            {
                "role": "user",
                "content": serialize_context_item_data(item),
            }
        )
    if request.tool_outcomes:
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": outcome.call_id,
                        "type": "function",
                        "function": {
                            "name": outcome.tool_name,
                            "arguments": "{}",
                        },
                    }
                    for outcome in request.tool_outcomes
                ],
            }
        )
        for outcome in request.tool_outcomes:
            content = (
                outcome.result
                if outcome.result is not None
                else f"rejected({outcome.code}): {outcome.message}"
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": outcome.call_id,
                    "content": content,
                }
            )
    return messages


def tool_spec_to_chat_schema(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def tool_spec_to_responses_schema(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
    }


def extract_usage(data: dict[str, Any]) -> ModelUsage | None:
    """从 provider 响应提取用量；缺失时返回 None（显式缺失，不伪造）。

    - Chat Completions 使用 ``prompt_tokens`` / ``completion_tokens``；
    - Responses 使用 ``input_tokens`` / ``output_tokens``（兼容旧版
      prompt/completion 命名）。
    """
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    input_key = (
        "input_tokens" if usage.get("input_tokens") is not None else "prompt_tokens"
    )
    output_key = (
        "output_tokens"
        if usage.get("output_tokens") is not None
        else "completion_tokens"
    )
    input_tokens = usage.get(input_key)
    output_tokens = usage.get(output_key)
    if input_tokens is None and output_tokens is None:
        return None
    mappings = []
    if input_tokens is not None:
        mappings.append(f"{input_key}->input_tokens")
    if output_tokens is not None:
        mappings.append(f"{output_key}->output_tokens")
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        raw_unit="tokens",
        normalization_source="openai-compatible-usage-v1:" + ",".join(mappings),
    )


def parse_chat_tool_calls(message: dict[str, Any]) -> tuple[ToolCall, ...]:
    calls: list[ToolCall] = []
    for index, item in enumerate(message.get("tool_calls") or []):
        function = item.get("function") or {}
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments) if arguments is not None else "{}"
        calls.append(
            ToolCall(
                call_id=item.get("id") or f"call_{index}",
                tool_name=function.get("name") or "",
                arguments=arguments or "{}",
            )
        )
    return tuple(calls)


def parse_responses_output(output: Sequence[dict[str, Any]]) -> tuple[
    str, tuple[ToolCall, ...]
]:
    """解析 Responses API ``output`` 数组为内容与工具调用。"""
    content_parts: list[str] = []
    calls: list[ToolCall] = []
    for item in output:
        item_type = item.get("type")
        if item_type == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text" and part.get("text"):
                    content_parts.append(part["text"])
        elif item_type == "function_call":
            calls.append(
                ToolCall(
                    call_id=item.get("call_id") or item.get("id") or "",
                    tool_name=item.get("name") or "",
                    arguments=item.get("arguments") or "{}",
                )
            )
    return "".join(content_parts), tuple(calls)


def build_structured_output(
    schema: dict[str, Any] | None, *, name: str
) -> dict[str, Any] | None:
    """把可选 JSON Schema 转为 provider 原生 structured output 声明。

    ``structured_output`` 能力声明的是 OpenAI 兼容端点的**原生**
    JSON Schema 模式。未配置 Schema 的普通请求不应附带格式约束，
    否则会把非结构化调用错误地变成 JSON-mode 调用。
    """
    if schema is None:
        return None
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


def build_responses_structured_output(
    schema: dict[str, Any] | None, *, name: str
) -> dict[str, Any] | None:
    if schema is None:
        return None
    return {
        "type": "json_schema",
        "name": name,
        "strict": True,
        "schema": schema,
    }


# -- 共享 live Adapter 基类 --------------------------------------------


class ProviderModelAdapter(ModelAdapter):
    """live provider Adapter 的共享基类（``deterministic`` 恒为 False）。

    构造不发起任何网络请求，也**不要求**凭证已配置；凭证只在真正
    发出请求时由 :meth:`_require_api_key` 检查。``requests`` 记录
    已发出的请求端点（不含凭证），供契约测试证明"注册在发出网络
    请求前拒绝能力不匹配"。
    """

    deterministic: bool = False

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        model_contract: ModelContract | None = None,
        structured_output_schema: dict[str, Any] | None = None,
        structured_output_name: str = "result",
    ) -> None:
        self.model: str = resolve_model(model)
        self.base_url: str = resolve_base_url(base_url)
        self.timeout: float = timeout
        self._model_contract = model_contract
        self._contract_configuration_fingerprint: str | None = None
        self.structured_output_schema: dict[str, Any] | None = (
            structured_output_schema
        )
        self.structured_output_name: str = structured_output_name
        #: 已发出请求的端点记录（不含凭证），供测试断言无请求发生。
        self.requests: list[str] = []
        self._client: httpx.AsyncClient | None = None
        #: 可替换的本地 HTTP transport（测试用，不改变 provider 协议）。
        self._transport: httpx.AsyncBaseTransport | None = None

    @property
    def model_contract(self) -> ModelContract:
        if self._model_contract is None:
            return super().model_contract
        if not self.capabilities.supports(self._model_contract.capabilities):
            raise ValueError(
                "provider instance ModelContract exceeds class capability ceiling"
            )
        declared_structured_output = (
            self._model_contract.capabilities.structured_output
        )
        if (
            declared_structured_output is not StructuredOutputMode.NONE
            and declared_structured_output
            is not self.capabilities.structured_output
        ):
            raise ValueError(
                "provider instance structured-output mode does not match "
                "its ModelContract"
            )
        current = self.definition_contract_fingerprint()
        if self._model_contract.configuration_fingerprint != current:
            raise ValueError(
                "provider instance configuration fingerprint does not match "
                "its ModelContract configuration fingerprint"
            )
        if self._contract_configuration_fingerprint is None:
            self._contract_configuration_fingerprint = current
        elif self._contract_configuration_fingerprint != current:
            raise ValueError(
                "provider instance configuration changed after ModelContract "
                "was frozen"
            )
        return self._model_contract

    def _definition_contract_configuration(self) -> dict[str, Any]:
        """Return semantic provider configuration without credential material.

        The configuration is immediately hashed by
        :meth:`definition_contract_fingerprint`; it is never added to a Run
        Snapshot, telemetry record, request log, or error message.
        """
        return {
            "adapter_type": (
                f"{type(self).__module__}.{type(self).__qualname__}"
            ),
            "capabilities": self.capabilities.model_dump(mode="json"),
            "model": self.model,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "structured_output_schema": self.structured_output_schema,
            "structured_output_name": self.structured_output_name,
        }

    def definition_contract_fingerprint(self) -> str:
        """Hash semantic provider configuration for immutable Run recovery."""
        try:
            encoded = json.dumps(
                self._definition_contract_configuration(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        except TypeError as exc:
            raise ValueError(
                "provider adapter configuration must be JSON-serializable"
            ) from exc
        return hashlib.sha256(encoded).hexdigest()

    # -- 供子类使用的 HTTP 基础设施 -----------------------------------

    def _require_api_key(self) -> str:
        api_key = resolve_api_key()
        if not api_key:
            raise ModelFailure(
                FailureClassification.PERMANENT,
                "provider_credentials_missing",
                f"{type(self).__name__}: no API key configured; set "
                "M_AGENT_OPENAI_API_KEY / OPENAI_API_KEY in the embedding "
                "environment",
            )
        return api_key

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport
            )
        return self._client

    async def _post_json(
        self, url: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """发出一次 POST 并返回 (HTTP 状态, JSON body)；记录请求端点。

        网络异常归一为结构化 ``ModelFailure``（TRANSIENT），凭证不
        出现在任何异常文本中。
        """
        self.requests.append(url)
        api_key = self._require_api_key()
        try:
            response = await self._http_client().post(
                url, json=payload, headers=self._headers(api_key)
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise transport_error(exc, operation=url) from exc
        if 300 <= response.status_code < 400:
            raise provider_error(response.status_code, operation=url)
        if response.status_code >= 400:
            raise provider_error(response.status_code, operation=url)
        try:
            return response.status_code, response.json()
        except ValueError as exc:
            raise invalid_response(
                url, f"expected JSON body ({type(exc).__name__})"
            ) from exc

    async def aclose(self) -> None:
        """关闭底层 HTTP 客户端（幂等；应用在进程退出前调用）。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- 结构化输出 ------------------------------------------------

    @property
    def _structured_output_payload(self) -> dict[str, Any] | None:
        if self.capabilities.structured_output is StructuredOutputMode.NONE:
            return None
        return build_structured_output(
            self.structured_output_schema, name=self.structured_output_name
        )

    @property
    def _structured_output_responses_payload(self) -> dict[str, Any] | None:
        if self.capabilities.structured_output is StructuredOutputMode.NONE:
            return None
        return build_responses_structured_output(
            self.structured_output_schema, name=self.structured_output_name
        )


async def consume_sse_events(
    response: httpx.Response,
) -> AsyncIterator[dict[str, Any]]:
    """逐行消费 SSE 流，产出 JSON 事件；跳过注释与 ``[DONE]``。"""
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except ValueError as exc:
            raise invalid_response(
                "stream", f"invalid SSE JSON ({type(exc).__name__})"
            ) from exc
