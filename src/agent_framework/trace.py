import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Protocol, Union


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TraceEvent:
    name: str
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "name": self.name,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TraceEvent":
        return cls(
            timestamp=str(data.get("timestamp", "")),
            name=str(data.get("name", "")),
            data=dict(data.get("data") or {}),
        )


class Tracer(Protocol):
    def record(self, event: TraceEvent) -> None:
        ...


class NoopTracer:
    def record(self, event: TraceEvent) -> None:
        return None


class InMemoryTracer:
    def __init__(self) -> None:
        self._events: List[TraceEvent] = []

    def record(self, event: TraceEvent) -> None:
        self._events.append(event)

    def events(self) -> List[TraceEvent]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()


class JsonlTracer:
    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)

    def record(self, event: TraceEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def events(self) -> List[TraceEvent]:
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            events.append(TraceEvent.from_dict(json.loads(line)))
        return events

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
