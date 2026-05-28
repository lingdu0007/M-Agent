import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional


class StructuredOutputError(ValueError):
    pass


@dataclass
class OutputSchema:
    name: str
    schema: Dict[str, Any]
    description: str = ""

    def instructions(self) -> str:
        description = f"\nDescription: {self.description}" if self.description else ""
        return (
            "Return the final answer as JSON only. Do not wrap it in markdown."
            f"\nSchema name: {self.name}{description}"
            f"\nJSON schema: {json.dumps(self.schema, ensure_ascii=False)}"
        )


def parse_structured_output(text: str, schema: OutputSchema) -> Any:
    value = _parse_json_value(text)
    _validate_value(value, schema.schema, path="$")
    return value


def _parse_json_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        extracted = _extract_json_object(text)
        if extracted is None:
            raise StructuredOutputError("Final answer is not valid JSON")
        try:
            return json.loads(extracted)
        except json.JSONDecodeError as exc:
            raise StructuredOutputError("Final answer contains invalid JSON") from exc


def _extract_json_object(text: str) -> Optional[str]:
    start_positions = [index for index, char in enumerate(text) if char in "[{"]
    for start in start_positions:
        opening = text[start]
        closing = "}" if opening == "{" else "]"
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
    return None


def _validate_value(value: Any, schema: Dict[str, Any], *, path: str) -> None:
    expected_type = schema.get("type")
    if expected_type:
        _validate_type(value, expected_type, path=path)

    if expected_type == "object":
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        for key in required:
            if key not in value:
                raise StructuredOutputError(f"Missing required field: {path}.{key}")
        for key, child_schema in properties.items():
            if key in value:
                _validate_value(value[key], child_schema, path=f"{path}.{key}")

    if expected_type == "array":
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _validate_value(item, item_schema, path=f"{path}[{index}]")

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        raise StructuredOutputError(f"Value at {path} is not one of {enum}")


def _validate_type(value: Any, expected_type: Any, *, path: str) -> None:
    if isinstance(expected_type, list):
        if any(_matches_type(value, item) for item in expected_type):
            return
        raise StructuredOutputError(f"Value at {path} does not match any type: {expected_type}")

    if not _matches_type(value, expected_type):
        raise StructuredOutputError(f"Value at {path} is not {expected_type}")


def _matches_type(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True
