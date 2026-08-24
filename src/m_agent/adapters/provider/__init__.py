"""Official live provider Adapters at their 0.3 semantic namespace."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType


def _load_legacy_source(name: str) -> ModuleType:
    """Load implementation source under this package without reviving its API."""
    source = Path(__file__).parents[2] / "provider" / f"{name}.py"
    qualified_name = f"{__name__}.{name}"
    spec = importlib.util.spec_from_file_location(qualified_name, source)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load official provider Adapter {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified_name] = module
    spec.loader.exec_module(module)
    return module


_base = _load_legacy_source("_base")
_chat = _load_legacy_source("_chat_completions")
_responses = _load_legacy_source("_responses")
_live_contract = _load_legacy_source("_live_contract")
_fixtures = _load_legacy_source("fixtures")

ProviderModelAdapter = _base.ProviderModelAdapter
extract_usage = _base.extract_usage
invalid_response = _base.invalid_response
provider_error = _base.provider_error
transport_error = _base.transport_error
CHAT_COMPLETIONS_CAPABILITIES = _chat.CHAT_COMPLETIONS_CAPABILITIES
ChatCompletionsModelAdapter = _chat.ChatCompletionsModelAdapter
RESPONSES_CAPABILITIES = _responses.RESPONSES_CAPABILITIES
ResponsesModelAdapter = _responses.ResponsesModelAdapter
LIVE_OPT_IN_ENV = _live_contract.LIVE_OPT_IN_ENV
LiveContractStatus = _live_contract.LiveContractStatus
live_contract_preflight = _live_contract.live_contract_preflight
live_contract_skip_reason = _live_contract.live_contract_skip_reason
chat_completion_fixture = _fixtures.chat_completion_fixture
chat_stream_fixture = _fixtures.chat_stream_fixture
malformed_response_fixture = _fixtures.malformed_response_fixture
provider_error_fixture = _fixtures.provider_error_fixture
responses_fixture = _fixtures.responses_fixture
responses_stream_fixture = _fixtures.responses_stream_fixture

__all__ = [
    "CHAT_COMPLETIONS_CAPABILITIES",
    "ChatCompletionsModelAdapter",
    "LIVE_OPT_IN_ENV",
    "LiveContractStatus",
    "ProviderModelAdapter",
    "RESPONSES_CAPABILITIES",
    "ResponsesModelAdapter",
    "extract_usage",
    "invalid_response",
    "live_contract_preflight",
    "live_contract_skip_reason",
    "provider_error",
    "transport_error",
    "chat_completion_fixture",
    "chat_stream_fixture",
    "malformed_response_fixture",
    "provider_error_fixture",
    "responses_fixture",
    "responses_stream_fixture",
]
