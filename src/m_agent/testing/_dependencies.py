"""Static dependency direction check for the public Runtime namespace."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


_FORBIDDEN_PREFIXES = (
    "m_agent.adapters",
    "m_agent.companion",
    "m_agent.provider",
    "m_agent.testing",
)


def find_runtime_dependency_violations(
    source_root: Path | None = None,
) -> tuple[str, ...]:
    """Return deterministic forbidden imports from actual Runtime Core source."""
    if source_root is None:
        source_root = Path(__file__).resolve().parents[2]
    package_root = source_root / "m_agent"
    excluded_layers = {"adapters", "companion", "testing", "provider"}
    violations: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root)
        if relative.parts[0] in excluded_layers:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        parts = relative.with_suffix("").parts
        package_parts = parts[:-1]
        package = ".".join(("m_agent", *package_parts))
        importlib_names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name == "importlib"
        }
        import_module_names = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "importlib"
            for alias in node.names
            if alias.name == "import_module"
        }
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    module = (
                        importlib.util.resolve_name(
                            "." * node.level + (node.module or ""), package
                        )
                    )
                elif node.module:
                    module = node.module
                else:
                    module = ""
                if module.startswith(_FORBIDDEN_PREFIXES):
                    modules.append(module)
                else:
                    modules.extend(f"{module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    modules.append(alias.name)
            elif (
                isinstance(node, ast.Call)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and (
                    isinstance(node.func, ast.Name)
                    and node.func.id in {"__import__", *import_module_names}
                    or isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in importlib_names
                    and node.func.attr == "import_module"
                )
            ):
                modules.append(node.args[0].value)
            for module in modules:
                if module.startswith(_FORBIDDEN_PREFIXES):
                    violations.append(f"{path}: import {module}")
    return tuple(violations)
