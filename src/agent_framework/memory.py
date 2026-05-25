import json
from pathlib import Path
from typing import List, Optional, Protocol, Union

from .types import Message


class Memory(Protocol):
    def load(self) -> List[Message]:
        ...

    def save(self, messages: List[Message]) -> None:
        ...

    def clear(self) -> None:
        ...


class InMemoryMemory:
    def __init__(self, messages: Optional[List[Message]] = None) -> None:
        self._messages = list(messages or [])

    def load(self) -> List[Message]:
        return list(self._messages)

    def save(self, messages: List[Message]) -> None:
        self._messages = list(messages)

    def clear(self) -> None:
        self._messages.clear()


class JsonFileMemory:
    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)

    def load(self) -> List[Message]:
        if not self.path.exists():
            return []

        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"Memory file must contain a list: {self.path}")
        return [Message.from_dict(item) for item in data]

    def save(self, messages: List[Message]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [message.to_dict() for message in messages]
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
