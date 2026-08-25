"""Live provider contract-test preflight .

The preflight is intentionally independent from pytest collection.  Live
contract cases use it from their public ``setUp`` path, so direct unittest
discovery and direct module execution cannot make a request merely because a
provider credential happens to be present in the environment.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Mapping


LIVE_OPT_IN_ENV = "M_AGENT_RUN_LIVE_TESTS"
_CREDENTIAL_ENV_NAMES = ("M_AGENT_OPENAI_API_KEY", "OPENAI_API_KEY")


class LiveContractStatus(str, Enum):
    """Terminal reporting states for credential-gated live contracts."""

    OPTED_OUT = "OPTED_OUT"
    MISSING_CREDENTIALS = "MISSING_CREDENTIALS"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    ASSERTION_FAILURE = "ASSERTION_FAILURE"
    VERIFIED = "VERIFIED"


def live_contract_preflight(
    environ: Mapping[str, str] | None = None,
) -> LiveContractStatus | None:
    """Return a skip status unless a live contract is explicitly authorized.

    ``None`` means the contract may invoke its adapter.  The helper only
    inspects configuration presence; it never returns, logs, or persists a
    credential value.  Pytest additionally requires explicit ``-m live``
    selection through the collection hook.
    """
    environment = os.environ if environ is None else environ
    if environment.get(LIVE_OPT_IN_ENV) != "1":
        return LiveContractStatus.OPTED_OUT
    if not any(name in environment for name in _CREDENTIAL_ENV_NAMES):
        return LiveContractStatus.MISSING_CREDENTIALS
    return None


def live_contract_skip_reason(status: LiveContractStatus) -> str:
    """Provide a stable, credential-free unittest skip reason."""
    if status is LiveContractStatus.OPTED_OUT:
        return (
            "OPTED_OUT: live contract tests require "
            "M_AGENT_RUN_LIVE_TESTS=1; no provider request was made"
        )
    if status is LiveContractStatus.MISSING_CREDENTIALS:
        return (
            "MISSING_CREDENTIALS: set M_AGENT_OPENAI_API_KEY or "
            "OPENAI_API_KEY after explicit live opt-in; no provider request "
            "was made"
        )
    raise ValueError(f"{status.value} is not a skip status")
