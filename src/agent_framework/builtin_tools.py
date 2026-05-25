from pathlib import Path
from typing import Iterable, List

from .tools import Tool, tool


DEFAULT_IGNORES = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}


def make_file_tools(root: Path, ignores: Iterable[str] = DEFAULT_IGNORES) -> List[Tool]:
    base = root.resolve()
    ignored = set(ignores)

    @tool(description="List files under the allowed project root.")
    def list_files(pattern: str = "", max_results: int = 50) -> str:
        pattern_lower = str(pattern).lower()
        limit = _bounded_int(max_results, default=50, minimum=1, maximum=200)
        results = []

        for path in _iter_files(base, ignored):
            relative = path.relative_to(base).as_posix()
            if pattern_lower and pattern_lower not in relative.lower():
                continue
            results.append(relative)
            if len(results) >= limit:
                break

        return "\n".join(results) if results else "No matching files."

    @tool(description="Read a UTF-8 text file under the allowed project root.")
    def read_text_file(path: str, max_chars: int = 4000) -> str:
        target = _safe_path(base, path)
        if not target.is_file():
            raise ValueError(f"Not a file: {path}")

        limit = _bounded_int(max_chars, default=4000, minimum=1, maximum=20000)
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) <= limit:
            return text
        return f"{text[:limit]}\n... truncated at {limit} chars ..."

    @tool(description="Search text files under the allowed project root.")
    def search_text(query: str, max_results: int = 20) -> str:
        needle = str(query)
        if not needle:
            raise ValueError("query cannot be empty")

        limit = _bounded_int(max_results, default=20, minimum=1, maximum=100)
        matches = []
        for path in _iter_files(base, ignored):
            if path.stat().st_size > 1_000_000:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            for line_number, line in enumerate(lines, start=1):
                if needle.lower() not in line.lower():
                    continue
                relative = path.relative_to(base).as_posix()
                snippet = line.strip()
                if len(snippet) > 160:
                    snippet = f"{snippet[:160]}..."
                matches.append(f"{relative}:{line_number}: {snippet}")
                if len(matches) >= limit:
                    return "\n".join(matches)

        return "\n".join(matches) if matches else "No matches."

    return [list_files, read_text_file, search_text]


def _iter_files(root: Path, ignores: set) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if any(part in ignores or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.is_file():
            yield path


def _safe_path(root: Path, path: str) -> Path:
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path outside project root: {path}") from exc
    return target


def _bounded_int(value: int, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))
