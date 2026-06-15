from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claudio.config import Config

log = logging.getLogger("claudio.cron.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL CHECK(type IN ('once', 'recurring')),
    chat_id     INTEGER NOT NULL,
    user_id     TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    cron_expr   TEXT,
    run_at      TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(
        (type = 'recurring' AND cron_expr IS NOT NULL AND run_at IS NULL) OR
        (type = 'once'      AND run_at IS NOT NULL    AND cron_expr IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS job_history (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    ran_at      TEXT NOT NULL,
    status      TEXT NOT NULL CHECK(status IN ('ok', 'error')),
    error       TEXT,
    duration_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_jobs_active_chat ON jobs(active, chat_id);
"""


@dataclass
class CronJob:
    id: str
    type: str          # "once" | "recurring"
    chat_id: int
    user_id: str
    prompt: str
    cron_expr: str | None
    run_at: datetime | None
    active: bool
    created_at: datetime


class CronStore:
    def __init__(self, config: "Config") -> None:
        self._db_path = Path(config.cron_db).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def add(
        self,
        *,
        type: str,
        chat_id: int,
        user_id: str,
        prompt: str,
        cron_expr: str | None = None,
        run_at: datetime | None = None,
    ) -> CronJob:
        job_id = str(uuid.uuid4())
        run_at_str = run_at.isoformat() if run_at else None
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, type, chat_id, user_id, prompt, cron_expr, run_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, type, chat_id, str(user_id), prompt, cron_expr, run_at_str),
            )
        log.info("cron.add: job %s (%s) para chat %d", job_id[:8], type, chat_id)
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: str) -> CronJob | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def list(self, chat_id: int | None = None, active_only: bool = True) -> list[CronJob]:
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list = []
        if active_only:
            query += " AND active = 1"
        if chat_id is not None:
            query += " AND chat_id = ?"
            params.append(chat_id)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_job(r) for r in rows]

    def delete(self, job_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE jobs SET active = 0 WHERE id = ?", (job_id,)
            )
        return cur.rowcount > 0

    def due_jobs(self) -> list[CronJob]:
        """Retorna jobs que devem ser executados agora."""
        now = datetime.now()
        results: list[CronJob] = []

        with self._connect() as conn:
            # Jobs once: run_at <= agora, ainda não rodou (active=1)
            rows = conn.execute(
                "SELECT * FROM jobs WHERE type='once' AND active=1 AND run_at <= ?",
                (now.isoformat(),),
            ).fetchall()
            results.extend(_row_to_job(r) for r in rows)

            # Jobs recurring: checagem via cron_expr (feita pelo scheduler)
            rows_rec = conn.execute(
                "SELECT * FROM jobs WHERE type='recurring' AND active=1"
            ).fetchall()
            results.extend(_row_to_job(r) for r in rows_rec)

        return results

    def record_run(
        self,
        job_id: str,
        *,
        status: str,
        error: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO job_history (job_id, ran_at, status, error, duration_ms) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, datetime.now().isoformat(), status, error, duration_ms),
            )
            # Jobs once são desativados após execução
            conn.execute(
                "UPDATE jobs SET active = 0 WHERE id = ? AND type = 'once'",
                (job_id,),
            )


def _row_to_job(row: sqlite3.Row) -> CronJob:
    return CronJob(
        id=row["id"],
        type=row["type"],
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        prompt=row["prompt"],
        cron_expr=row["cron_expr"],
        run_at=datetime.fromisoformat(row["run_at"]) if row["run_at"] else None,
        active=bool(row["active"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )
