from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claudio.config import Config


class AgentMeshAudit:
    """Escreve no audit_log do agent-mesh. Append-only, nunca UPDATE/DELETE."""

    def __init__(self, config: "Config") -> None:
        self._db_path = config.expand(config.agent_mesh_db)
        self._available = self._db_path.exists()

    def log(self, event: str, data: dict, level: str = "info") -> None:
        if not self._available:
            return
        payload = json.dumps({"level": level, **data}, ensure_ascii=False, default=str)
        try:
            with sqlite3.connect(str(self._db_path), timeout=5) as conn:
                conn.execute(
                    "INSERT INTO audit_log (ts, agent, event, data) VALUES (?, ?, ?, ?)",
                    (datetime.utcnow().isoformat(), "claudio", event, payload),
                )
        except sqlite3.Error:
            # audit_log é best-effort — nunca deve travar o pipeline principal
            pass
