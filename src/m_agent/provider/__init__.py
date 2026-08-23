"""M-Agent live provider Model Adapters（Ticket 10 / ADR 0038）。

本子包提供访问真实供应商 HTTP API 的 live Model Adapter，属于可选
``provider`` extra（依赖 ``httpx``，核心 ``m_agent`` 包不依赖它）：

- :class:`ChatCompletionsModelAdapter`：OpenAI 兼容
  ``/chat/completions`` 端点；
- :class:`ResponsesModelAdapter`：OpenAI 兼容 Responses 端点。

两个 Adapter 都是 :class:`m_agent.ModelAdapter` 的 live 实现
（``deterministic`` 恒为 False），与核心
:class:`m_agent.DeterministicModelAdapter`（fake，``deterministic``
恒为 True）在任何场景都不会混淆。它们如实声明 streaming、tool
calling、native structured output 与 usage reporting 能力；Definition
注册时由 :class:`m_agent.DefinitionRegistry` 在发出任何网络请求前
校验能力匹配（ADR 0030）。

凭证由嵌入应用配置，绝不进入持久化、日志、fixture、快照或错误文本
（ADR 0033）。未配置凭证时 Adapter 可以构造；注册还必须有准确的
实例 ``ModelContract``，任何网络请求都会以结构化的
:class:`m_agent.ModelFailure` 失败。

契约测试见 ``tests/test_live_model_adapters.py``：离线部分在默认 CI
运行（不触网）；live 部分在 pytest 下同时要求 ``pytest -m live`` 和
``M_AGENT_RUN_LIVE_TESTS=1``，并且调用方环境提供凭证后才会发出真实
请求。
"""

from ._base import (
    ProviderModelAdapter,
    extract_usage,
    invalid_response,
    provider_error,
    transport_error,
)
from ._chat_completions import (
    CHAT_COMPLETIONS_CAPABILITIES,
    ChatCompletionsModelAdapter,
)
from ._live_contract import (
    LIVE_OPT_IN_ENV,
    LiveContractStatus,
    live_contract_preflight,
    live_contract_skip_reason,
)
from ._responses import (
    RESPONSES_CAPABILITIES,
    ResponsesModelAdapter,
)
from .fixtures import (
    OfflineProviderTransport,
    OfflineProviderRequest,
    chat_completion_fixture,
    chat_stream_fixture,
    malformed_response_fixture,
    provider_error_fixture,
    responses_fixture,
    responses_stream_fixture,
)

__all__ = [
    "CHAT_COMPLETIONS_CAPABILITIES",
    "ChatCompletionsModelAdapter",
    "LIVE_OPT_IN_ENV",
    "LiveContractStatus",
    "OfflineProviderTransport",
    "OfflineProviderRequest",
    "ProviderModelAdapter",
    "RESPONSES_CAPABILITIES",
    "ResponsesModelAdapter",
    "extract_usage",
    "chat_completion_fixture",
    "chat_stream_fixture",
    "invalid_response",
    "live_contract_preflight",
    "live_contract_skip_reason",
    "malformed_response_fixture",
    "provider_error",
    "provider_error_fixture",
    "responses_fixture",
    "responses_stream_fixture",
    "transport_error",
]
