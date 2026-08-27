"""Deterministic, inspectable Run Policy contracts (ADR 0026)."""

from __future__ import annotations

import enum
import hashlib
import json
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")


class PolicyGate(str, enum.Enum):
    INPUT = "INPUT"
    CONTEXT = "CONTEXT"
    TOOL_REQUEST = "TOOL_REQUEST"
    TOOL_OUTCOME = "TOOL_OUTCOME"
    FINAL_OUTPUT = "FINAL_OUTPUT"


class PolicyAction(str, enum.Enum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"
    REQUIRE_RESOLUTION = "REQUIRE_RESOLUTION"


class PolicyIdentity(BaseModel, frozen=True):
    """Versioned identity of deterministic policy code frozen in a Run."""

    model_config = ConfigDict(extra="forbid")
    policy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)


class PolicyDecision(BaseModel, frozen=True):
    """One deterministic result. Error text is never a policy decision."""

    model_config = ConfigDict(extra="forbid")
    action: PolicyAction
    reason_code: str = Field(min_length=1)

    @field_validator("reason_code")
    @classmethod
    def _validate_reason_code(cls, value: str) -> str:
        if not _REASON_CODE.fullmatch(value):
            raise ValueError("Policy reason_code must be a stable uppercase code")
        return value


class PolicyRequest(BaseModel, frozen=True):
    """Data presented to one policy gate; it never carries executable objects."""

    model_config = ConfigDict(extra="forbid")
    gate: PolicyGate
    run_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PolicyDecisionRecord(BaseModel, frozen=True):
    """Public, non-sensitive evidence of a gate decision."""

    model_config = ConfigDict(extra="forbid")
    run_id: str
    gate: PolicyGate
    action: PolicyAction
    reason_code: str
    policy_id: str
    policy_version: str
    policy_fingerprint: str
    input_summary: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def policy_input_summary(payload: object) -> str:
    """Return a stable digest instead of retaining policy input as metadata."""
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class RunPolicy(ABC):
    """Synchronous deterministic policy port. Model-based review is not a Policy."""

    @property
    @abstractmethod
    def identity(self) -> PolicyIdentity: ...

    @abstractmethod
    def evaluate(self, request: PolicyRequest) -> PolicyDecision: ...


class AllowAllRunPolicy(RunPolicy):
    """Default explicit policy; it exists so all five gates have evidence."""

    @property
    def identity(self) -> PolicyIdentity:
        return PolicyIdentity(
            policy_id="m-agent.allow-all", version="1", fingerprint="allow-all-v1"
        )

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        return PolicyDecision(action=PolicyAction.ALLOW, reason_code="ALLOWED")


class StaticRunPolicy(RunPolicy):
    """Small deterministic policy useful to integrators and contract tests."""

    def __init__(
        self,
        *,
        policy_id: str,
        version: str,
        decisions: dict[PolicyGate, PolicyDecision] | None = None,
    ) -> None:
        self._identity = PolicyIdentity(
            policy_id=policy_id,
            version=version,
            fingerprint=hashlib.sha256(
                json.dumps(
                    {
                        "policy_id": policy_id,
                        "version": version,
                        "decisions": {
                            gate.value: decision.model_dump(mode="json")
                            for gate, decision in sorted(
                                (decisions or {}).items(), key=lambda item: item[0].value
                            )
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )
        self._decisions = dict(decisions or {})

    @property
    def identity(self) -> PolicyIdentity:
        return self._identity

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        return self._decisions.get(
            request.gate,
            PolicyDecision(action=PolicyAction.ALLOW, reason_code="ALLOWED"),
        )
