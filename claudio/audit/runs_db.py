from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Generator

if TYPE_CHECKING:
    from claudio.config import Config

log = logging.getLogger("claudio.runs_db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    channel         TEXT NOT NULL,
    chat_id         TEXT NOT NULL,
    thread_id       TEXT,
    user_id         TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    duration_ms     INTEGER,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    tool_calls_count INTEGER DEFAULT 0,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    phase       TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT 'info',
    message     TEXT,
    ts          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_chat ON runs(chat_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_events_run ON run_events(run_id);
"""


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    channel: str
    chat_id: str
    thread_id: str | None
    user_id: str
    model: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    input_tokens: int
    output_tokens: int
    tool_calls_count: int
    error: str | None


class RunsDB:
    def __init__(self, config: "Config") -> None:
        self._db_path = Path(config.runs_db).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def start_run(
        self,
        *,
        session_id: str,
        channel: str,
        chat_id: str,
        thread_id: str | None,
        user_id: str,
        model: str,
        run_id: str | None = None,
    ) -> str:
        rid = run_id or str(uuid.uuid4())
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, session_id, channel, chat_id, thread_id, "
                "user_id, model, status, started_at) VALUES (?,?,?,?,?,?,?,'running',?)",
                (rid, session_id, channel, str(chat_id), thread_id, user_id, model, now),
            )
        return rid

    def finish_run(
        self,
        run_id: str,
        *,
        status: str = "completed",
        duration_ms: int | None = None,
        tool_calls_count: int = 0,
        error: str | None = None,
    ) -> None:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status=?, finished_at=?, duration_ms=?, "
                "tool_calls_count=?, error=? WHERE run_id=?",
                (status, now, duration_ms, tool_calls_count, error, run_id),
            )

    def add_event(self, run_id: str, phase: str, message: str, level: str = "info") -> None:
        ts = int(time.time() * 1000)
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO run_events (run_id, phase, level, message, ts) VALUES (?,?,?,?,?)",
                    (run_id, phase, level, message[:500], ts),
                )
        except Exception as exc:
            log.debug("runs_db.add_event falhou: %s", exc)

    def get_last(self, chat_id: str, limit: int = 1) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE chat_id=? ORDER BY started_at DESC LIMIT ?",
                (str(chat_id), limit),
            ).fetchall()
        return [_row_to_run(r) for r in rows]

    def get_run(self, run_id_prefix: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id LIKE ? LIMIT 1",
                (f"{run_id_prefix}%",),
            ).fetchone()
        return _row_to_run(row) if row else None

    def get_errors(self, chat_id: str, limit: int = 5) -> list[RunRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE chat_id=? AND status IN ('failed','error','timeout') "
                "ORDER BY started_at DESC LIMIT ?",
                (str(chat_id), limit),
            ).fetchall()
        return [_row_to_run(r) for r in rows]


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        session_id=row["session_id"],
        channel=row["channel"],
        chat_id=row["chat_id"],
        thread_id=row["thread_id"],
        user_id=row["user_id"],
        model=row["model"],
        status=row["status"],
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        duration_ms=row["duration_ms"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        tool_calls_count=row["tool_calls_count"],
        error=row["error"],
    )
