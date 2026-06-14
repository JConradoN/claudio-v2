from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_id_counter = itertools.count(1)


@dataclass
class Turn:
    role: str          # "user" | "assistant"
    content: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    run_id: str = ""


@dataclass
class Session:
    id: int
    channel: str        # "telegram" | "http" | "mcp"
    channel_id: str
    thread_id: int | None
    project: str | None
    security_profile: str
    history: list[Turn] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_active: datetime = field(default_factory=datetime.utcnow)

    def add_turn(self, role: str, content: str, run_id: str = "") -> None:
        self.history.append(Turn(role=role, content=content, run_id=run_id))
        self.last_active = datetime.utcnow()

    def to_prompt_history(self, max_turns: int = 20) -> list[dict]:
        return [
            {"role": t.role, "content": t.content}
            for t in self.history[-max_turns:]
        ]

    def reset(self) -> None:
        self.history.clear()
        self.project = None
        self.last_active = datetime.utcnow()


class SessionStore:
    """In-memory: channel_id → Session. TTL simples por último acesso."""

    def __init__(self, ttl_seconds: int = 3600 * 5) -> None:
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl_seconds

    def get_or_create(
        self,
        channel: str,
        channel_id: str,
        security_profile: str = "execute",
        thread_id: int | None = None,
    ) -> Session:
        key = f"{channel}:{channel_id}"
        now = time.monotonic()

        if key in self._sessions:
            s = self._sessions[key]
            # Verifica TTL
            age = (datetime.utcnow() - s.last_active).total_seconds()
            if age > self._ttl:
                del self._sessions[key]
            else:
                return s

        session = Session(
            id=next(_id_counter),
            channel=channel,
            channel_id=channel_id,
            thread_id=thread_id,
            project=None,
            security_profile=security_profile,
        )
        self._sessions[key] = session
        return session

    def reset(self, channel: str, channel_id: str) -> None:
        key = f"{channel}:{channel_id}"
        if key in self._sessions:
            self._sessions[key].reset()
