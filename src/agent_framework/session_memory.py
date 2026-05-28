import json
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Union
from urllib.parse import quote, unquote

from .types import Message


class SessionMemory(Protocol):
    def load(self, session_id: str) -> List[Message]:
        ...

    def save(self, session_id: str, messages: List[Message]) -> None:
        ...

    def clear(self, session_id: str) -> None:
        ...

    def list_sessions(self) -> List[str]:
        ...

    def delete_session(self, session_id: str) -> None:
        ...


class InMemorySessionMemory:
    def __init__(self, sessions: Optional[Dict[str, List[Message]]] = None) -> None:
        self._sessions: Dict[str, List[Message]] = {
            session_id: list(messages) for session_id, messages in (sessions or {}).items()
        }

    def load(self, session_id: str) -> List[Message]:
        return list(self._sessions.get(session_id, []))

    def save(self, session_id: str, messages: List[Message]) -> None:
        self._sessions[session_id] = list(messages)

    def clear(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def list_sessions(self) -> List[str]:
        return sorted(self._sessions.keys())

    def delete_session(self, session_id: str) -> None:
        self.clear(session_id)


class JsonDirectorySessionMemory:
    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root)

    def load(self, session_id: str) -> List[Message]:
        path = self._session_path(session_id)
        if not path.exists():
            return []

        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"Session file must contain a list: {path}")
        return [Message.from_dict(item) for item in data]

    def save(self, session_id: str, messages: List[Message]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._session_path(session_id)
        data = [message.to_dict() for message in messages]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def clear(self, session_id: str) -> None:
        path = self._session_path(session_id)
        if path.exists():
            path.unlink()

    def list_sessions(self) -> List[str]:
        if not self.root.exists():
            return []
        session_ids = []
        for path in sorted(self.root.glob("*.json")):
            session_ids.append(unquote(path.stem))
        return session_ids

    def delete_session(self, session_id: str) -> None:
        self.clear(session_id)

    def _session_path(self, session_id: str) -> Path:
        filename = quote(session_id, safe="") + ".json"
        return self.root / filename
