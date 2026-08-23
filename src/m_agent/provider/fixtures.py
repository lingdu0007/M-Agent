"""Public offline HTTP fixtures for official provider adapter contracts.

They use ``httpx.MockTransport`` only after a caller explicitly injects the
returned transport into a provider Adapter.  Nothing in this module reads the
network, environment credentials or provider endpoints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ._base import httpx


@dataclass(frozen=True)
class OfflineProviderRequest:
    """Safe fixture observation of a request dispatch.

    The fixture deliberately retains no headers, URL, or body.  All of those
    can contain a credential, endpoint secret, or protected prompt payload.
    Tests can still assert the number and HTTP method of offline dispatches.
    """

    method: str


class OfflineProviderTransport:
    """Finite responses with a credential- and payload-safe request log."""

    def __init__(self, responses: tuple[httpx.Response, ...]) -> None:
        if not responses:
            raise ValueError("offline provider fixture needs at least one response")
        self._responses = iter(responses)
        self.requests: list[OfflineProviderRequest] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(OfflineProviderRequest(method=request.method))
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise AssertionError("offline provider fixture received an unexpected request") from exc


def chat_completion_fixture(
    *, content: str = "offline chat response", usage: dict[str, Any] | None = None
) -> OfflineProviderTransport:
    """One successful non-streaming Chat Completions response."""
    body: dict[str, Any] = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return OfflineProviderTransport((httpx.Response(200, json=body),))


def responses_fixture(
    *, content: str = "offline responses response", usage: dict[str, Any] | None = None
) -> OfflineProviderTransport:
    """One successful non-streaming Responses response."""
    body: dict[str, Any] = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": content}],
            }
        ]
    }
    if usage is not None:
        body["usage"] = usage
    return OfflineProviderTransport((httpx.Response(200, json=body),))


def provider_error_fixture(status_code: int = 429) -> OfflineProviderTransport:
    """One sanitized provider error response for negative contract cases."""
    return OfflineProviderTransport((httpx.Response(status_code, json={"error": "fixture"}),))


def malformed_response_fixture() -> OfflineProviderTransport:
    """A successful HTTP response missing required provider protocol fields."""
    return OfflineProviderTransport((httpx.Response(200, json={"fixture": "missing-fields"}),))


def chat_stream_fixture(*events: dict[str, Any]) -> OfflineProviderTransport:
    """A Chat SSE fixture; callers provide the exact protocol events."""
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    return OfflineProviderTransport(
        (httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}),)
    )


def responses_stream_fixture(*events: dict[str, Any]) -> OfflineProviderTransport:
    """A Responses SSE fixture; callers provide the exact protocol events."""
    body = "".join(f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n" for event in events)
    return OfflineProviderTransport(
        (httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}),)
    )
