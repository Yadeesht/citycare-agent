"""Session storage. History here is a text-only transcript shared by every worker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Session:
    session_id: str
    history: list[dict[str, Any]] = field(default_factory=list)   # {"role", "content": str}
    state: dict[str, Any] = field(default_factory=dict)           # becomes RunContext.state
    turn: int = 0


class SessionStore(Protocol):
    def get(self, session_id: str) -> Session: ...

    def lock(self, session_id: str) -> asyncio.Lock:
        """One lock per session, so two messages in the same chat never run at once."""
        ...


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, session_id: str) -> Session:
        return self._sessions.setdefault(session_id, Session(session_id))

    def lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())
