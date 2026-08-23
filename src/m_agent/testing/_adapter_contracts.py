"""Reusable public contract kit for Model Adapter implementers.

This module deliberately imports only Runtime public contract types.  An
application or third-party package can run it without depending on official
provider adapters or their private HTTP implementation.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..runtime import ModelAdapter, ModelRequest, ModelResponse


class ModelAdapterContractReport(BaseModel, frozen=True):
    """Minimal observable result from one deterministic adapter contract probe."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    contract_id: str
    contract_fingerprint: str
    response_content: str | None


async def run_model_adapter_contract(
    adapter: ModelAdapter, request: ModelRequest
) -> ModelAdapterContractReport:
    """Exercise identity, immutable contract and response normalization.

    It is intentionally a small kit, not a provider certification.  Callers
    supply their own deterministic request/fixture and can compose this with
    their endpoint-specific negative cases.
    """
    contract = adapter.model_contract
    if not contract.fingerprint or not adapter.definition_contract_fingerprint():
        raise AssertionError("Model Adapter must expose stable public fingerprints")
    if contract.configuration_fingerprint != adapter.definition_contract_fingerprint():
        raise AssertionError("Model Adapter contract/configuration fingerprints differ")
    response = await adapter.generate(request)
    if not isinstance(response, ModelResponse):
        raise AssertionError("Model Adapter did not return ModelResponse")
    normalized = adapter.validate_response(request, response)
    if not isinstance(normalized, ModelResponse):
        raise AssertionError("Model Adapter validate_response did not return ModelResponse")
    return ModelAdapterContractReport(
        passed=True,
        contract_id=contract.contract_id,
        contract_fingerprint=contract.fingerprint,
        response_content=normalized.content,
    )
