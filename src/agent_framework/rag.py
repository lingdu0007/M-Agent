import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .tools import Tool, tool


DEFAULT_RAG_IGNORES = {
    ".env",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "data",
    "dist",
}

DEFAULT_TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".txt",
    ".toml",
    ".json",
    ".yaml",
    ".yml",
}


@dataclass
class DocumentChunk:
    id: str
    source: str
    text: str
    start_line: int = 1
    end_line: int = 1
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class RetrievedChunk:
    chunk: DocumentChunk
    score: float


class KeywordRagIndex:
    def __init__(self, *, chunk_size: int = 1200, overlap: int = 160) -> None:
        self.chunk_size = chunk_size
        self.overlap = overlap
        self._chunks: Dict[str, DocumentChunk] = {}
        self._token_counts: Dict[str, Counter] = {}

    @classmethod
    def from_directory(
        cls,
        root: Union[str, Path],
        *,
        suffixes: Optional[Iterable[str]] = None,
        ignores: Iterable[str] = DEFAULT_RAG_IGNORES,
        max_file_size: int = 1_000_000,
        chunk_size: int = 1200,
        overlap: int = 160,
    ) -> "KeywordRagIndex":
        index = cls(chunk_size=chunk_size, overlap=overlap)
        base = Path(root).resolve()
        allowed_suffixes = set(suffixes or DEFAULT_TEXT_SUFFIXES)
        ignored = set(ignores)

        for path in sorted(base.rglob("*")):
            if any(part in ignored or part.endswith(".egg-info") for part in path.parts):
                continue
            if not path.is_file() or path.suffix not in allowed_suffixes:
                continue
            if path.stat().st_size > max_file_size:
                continue

            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            source = path.relative_to(base).as_posix()
            index.add_text(source=source, text=text)

        return index

    def add_text(
        self, *, source: str, text: str, metadata: Optional[Dict[str, str]] = None
    ) -> None:
        for index, (chunk_text, start_line, end_line) in enumerate(
            _chunk_text(text, chunk_size=self.chunk_size, overlap=self.overlap),
            start=1,
        ):
            chunk_id = f"{source}#{index}"
            chunk = DocumentChunk(
                id=chunk_id,
                source=source,
                text=chunk_text,
                start_line=start_line,
                end_line=end_line,
                metadata=dict(metadata or {}),
            )
            self._chunks[chunk_id] = chunk
            self._token_counts[chunk_id] = Counter(_tokenize(f"{source}\n{chunk_text}"))

    def get(self, chunk_id: str) -> Optional[DocumentChunk]:
        return self._chunks.get(chunk_id)

    def search(self, query: str, *, top_k: int = 5) -> List[RetrievedChunk]:
        query_terms = _tokenize(query)
        if not query_terms:
            return []

        query_counts = Counter(query_terms)
        scored = []
        for chunk_id, token_counts in self._token_counts.items():
            score = _score(token_counts, query_counts)
            if score <= 0:
                continue
            scored.append(RetrievedChunk(chunk=self._chunks[chunk_id], score=score))

        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[: max(1, int(top_k))]

    def __len__(self) -> int:
        return len(self._chunks)


def make_rag_tools(index: KeywordRagIndex) -> List[Tool]:
    @tool(description="Search the local knowledge index and return relevant chunks.")
    def search_knowledge(query: str, top_k: int = 5) -> str:
        results = index.search(query, top_k=top_k)
        if not results:
            return "No relevant chunks."

        blocks = []
        for item in results:
            chunk = item.chunk
            blocks.append(
                "\n".join(
                    [
                        f"chunk_id: {chunk.id}",
                        f"source: {chunk.source}:{chunk.start_line}-{chunk.end_line}",
                        f"score: {item.score:.2f}",
                        "text:",
                        _truncate(chunk.text, 900),
                    ]
                )
            )
        return "\n\n---\n\n".join(blocks)

    @tool(description="Read one chunk from the local knowledge index by chunk_id.")
    def read_knowledge_chunk(chunk_id: str) -> str:
        chunk = index.get(chunk_id)
        if chunk is None:
            return f"Chunk not found: {chunk_id}"
        return "\n".join(
            [
                f"chunk_id: {chunk.id}",
                f"source: {chunk.source}:{chunk.start_line}-{chunk.end_line}",
                "text:",
                chunk.text,
            ]
        )

    return [search_knowledge, read_knowledge_chunk]


def _chunk_text(text: str, *, chunk_size: int, overlap: int) -> Sequence[Tuple[str, int, int]]:
    lines = text.splitlines() or [""]
    chunks = []
    buffer: List[str] = []
    buffer_len = 0
    start_line = 1

    for line_number, line in enumerate(lines, start=1):
        projected = buffer_len + len(line) + 1
        if buffer and projected > chunk_size:
            chunks.append(("\n".join(buffer), start_line, line_number - 1))
            buffer, start_line = _overlap_buffer(buffer, line_number, overlap)
            buffer_len = sum(len(item) + 1 for item in buffer)

        buffer.append(line)
        buffer_len += len(line) + 1

    if buffer:
        chunks.append(("\n".join(buffer), start_line, len(lines)))
    return chunks


def _overlap_buffer(buffer: List[str], next_line_number: int, overlap: int) -> Tuple[List[str], int]:
    if overlap <= 0:
        return [], next_line_number

    selected: List[str] = []
    selected_len = 0
    for line in reversed(buffer):
        projected = selected_len + len(line) + 1
        if selected and projected > overlap:
            break
        selected.insert(0, line)
        selected_len = projected

    start_line = max(1, next_line_number - len(selected))
    return selected, start_line


def _tokenize(text: str) -> List[str]:
    return [item.lower() for item in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", text)]


def _score(token_counts: Counter, query_counts: Counter) -> float:
    score = 0.0
    for term, query_count in query_counts.items():
        score += min(token_counts.get(term, 0), query_count) * 2.0
        if token_counts.get(term, 0) > query_count:
            score += 0.2
    return score


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n... truncated ..."
