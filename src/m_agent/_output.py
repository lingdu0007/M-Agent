"""Versioned final Output Contract and its deterministic validation."""

from __future__ import annotations

import enum
import json
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._model import StructuredOutputMode


class _FrozenSchemaDict(dict[str, Any]):
    """A JSON-compatible mapping that rejects in-place schema mutation."""

    def __init__(self, values: dict[str, Any]) -> None:
        dict.__init__(self)
        for key, value in values.items():
            dict.__setitem__(self, key, value)

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("Output Contract schema is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __copy__(self) -> "_FrozenSchemaDict":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "_FrozenSchemaDict":
        del memo
        return self


class _FrozenSchemaList(list[Any]):
    """A JSON-compatible sequence that rejects in-place schema mutation."""

    def __init__(self, values: list[Any]) -> None:
        list.__init__(self, values)

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("Output Contract schema is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable

    def __copy__(self) -> "_FrozenSchemaList":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "_FrozenSchemaList":
        del memo
        return self


def _freeze_schema_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenSchemaDict(
            {key: _freeze_schema_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return _FrozenSchemaList([_freeze_schema_value(item) for item in value])
    return value


def _canonical_frozen_schema(value: dict[str, Any]) -> dict[str, Any]:
    """Copy a JSON Schema through its canonical JSON representation."""
    try:
        canonical = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Output Contract schema must be JSON-serializable") from exc
    if not isinstance(canonical, dict):  # Defensive: the field type is dict.
        raise ValueError("Output Contract schema must be a JSON object")
    return _freeze_schema_value(canonical)


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

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> "OutputContract":
        """Copy through validation so a public update cannot thaw the schema."""
        del deep
        values = self.model_dump()
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)

    @model_validator(mode="after")
    def _validate_fallback(self) -> "OutputContract":
        if self.fallback is OutputFallback.REPAIR and self.repair is None:
            raise ValueError("Output REPAIR fallback requires OutputRepairPolicy")
        if self.fallback is OutputFallback.NONE and self.repair is not None:
            raise ValueError("OutputRepairPolicy requires REPAIR fallback")
        object.__setattr__(
            self,
            "schema_definition",
            _canonical_frozen_schema(self.schema_definition),
        )
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
