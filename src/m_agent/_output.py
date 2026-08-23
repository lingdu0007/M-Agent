"""Versioned final Output Contract and its deterministic validation."""

from __future__ import annotations

import enum
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._model import StructuredOutputMode


class OutputFallback(str, enum.Enum):
    NONE = "NONE"
    REPAIR = "REPAIR"


class OutputRepairPolicy(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")
    max_attempts: int = Field(ge=1)


class OutputContract(BaseModel, frozen=True):
    """Schema and explicit fallback frozen with a Definition Snapshot."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    contract_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    schema_definition: dict[str, Any] = Field(alias="schema", serialization_alias="schema")
    structured_output: StructuredOutputMode = StructuredOutputMode.NONE
    fallback: OutputFallback = OutputFallback.NONE
    repair: OutputRepairPolicy | None = None

    @model_validator(mode="after")
    def _validate_fallback(self) -> "OutputContract":
        if self.fallback is OutputFallback.REPAIR and self.repair is None:
            raise ValueError("Output REPAIR fallback requires OutputRepairPolicy")
        if self.fallback is OutputFallback.NONE and self.repair is not None:
            raise ValueError("OutputRepairPolicy requires REPAIR fallback")
        return self


def validate_output(contract: OutputContract, output: str) -> tuple[bool, str | None]:
    """Validate the intentionally small, deterministic JSON Schema subset."""
    try:
        value = json.loads(output)
    except (TypeError, ValueError):
        return False, "OUTPUT_NOT_VALID_JSON"
    error = _validate_schema(contract.schema_definition, value, "$" )
    return (error is None, error)


def _validate_schema(schema: dict[str, Any], value: Any, path: str) -> str | None:
    expected = schema.get("type")
    type_ok = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }
    if expected is not None and (expected not in type_ok or not type_ok[expected]()):
        return f"OUTPUT_SCHEMA_TYPE:{path}"
    if "enum" in schema and value not in schema["enum"]:
        return f"OUTPUT_SCHEMA_ENUM:{path}"
    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list):
            return "OUTPUT_SCHEMA_INVALID"
        for key in required:
            if key not in value:
                return f"OUTPUT_SCHEMA_REQUIRED:{path}.{key}"
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return "OUTPUT_SCHEMA_INVALID"
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                return f"OUTPUT_SCHEMA_ADDITIONAL:{path}.{sorted(unknown)[0]}"
        for key, child in properties.items():
            if key in value and isinstance(child, dict):
                error = _validate_schema(child, value[key], f"{path}.{key}")
                if error is not None:
                    return error
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            error = _validate_schema(schema["items"], item, f"{path}[{index}]")
            if error is not None:
                return error
    return None
