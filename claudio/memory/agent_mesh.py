from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claudio.memory.retrieval import MemoryFragment

log = logging.getLogger("claudio.memory.agent_mesh")

_ALWAYS_LOAD = ("session:claude-latest", "infra:ecc:status")
_MAX_VALUE_LEN = 250


def _summarize_value(raw: str) -> str:
    """Extrai texto útil de um value que pode ser JSON ou texto puro."""
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            # Campos preferenciais para summary
            for field in ("summary", "status", "description", "message", "data"):
                if field in obj and isinstance(obj[field], str):
                    return obj[field][:_MAX_VALUE_LEN]
            # Fallback: primeira string do dict
            for v in obj.values():
                if isinstance(v, str) and len(v) > 10:
                    return v[:_MAX_VALUE_LEN]
        return str(obj)[:_MAX_VALUE_LEN]
    except (json.JSONDecodeError, TypeError):
        return raw[:_MAX_VALUE_LEN]


class AgentMeshRetriever:
    """Consulta o shared_memory do agent-mesh por texto + sempre carrega sessão recente."""

    def __init__(self, db_path: str) -> None:
        self._db_path = Path(db_path).expanduser()

    async def search(
        self,
        query: str,
        limit: int = 3,
        max_tokens: int = 150,
    ) -> list[MemoryFragment]:
        from claudio.memory.retrieval import MemoryFragment

        if not self._db_path.exists():
            log.debug("agent_mesh: db não encontrado em %s", self._db_path)
            return []

        try:
            rows = await asyncio.to_thread(self._query, query, limit)
        except Exception as exc:
            log.warning("agent_mesh: falha na query: %s", exc)
            return []

        fragments: list[MemoryFragment] = []
        total_tokens = 0
        seen_keys: set[str] = set()

        for key, value, updated_at in rows:
            if key in seen_keys:
                continue
            seen_keys.add(key)

            summary = _summarize_value(value)
            if not summary.strip():
                continue

            fact = f"[mesh:{key}] {summary}"
            tokens = len(fact) // 4
            if total_tokens + tokens > max_tokens:
                break

            fragments.append(MemoryFragment(
                fact=fact,
                score=0.75,
                source="agent_mesh",
                metadata={"key": key, "updated_at": updated_at},
            ))
            total_tokens += tokens

        return fragments

    def _query(self, query: str, limit: int) -> list[tuple]:
        conn = sqlite3.connect(self._db_path, timeout=5)
        try:
            results: list[tuple] = []

            # Sempre carrega sessão e status de infra
            placeholders = ",".join("?" for _ in _ALWAYS_LOAD)
            always = conn.execute(
                f"SELECT key, value, updated_at FROM shared_memory WHERE key IN ({placeholders}) ORDER BY updated_at DESC",
                _ALWAYS_LOAD,
            ).fetchall()
            results.extend(always)

            # Keyword search para complementar
            keywords = [w.lower() for w in query.split() if len(w) > 3]
            if keywords:
                always_keys = set(r[0] for r in always)
                like_clauses = " OR ".join(
                    "(LOWER(key) LIKE ? OR LOWER(value) LIKE ?)" for _ in keywords
                )
                params: list = []
                for kw in keywords:
                    params.extend([f"%{kw}%", f"%{kw}%"])
                params.append(limit - len(results))
                rows = conn.execute(
                    f"SELECT key, value, updated_at FROM shared_memory WHERE ({like_clauses}) ORDER BY updated_at DESC LIMIT ?",
                    params,
                ).fetchall()
                for row in rows:
                    if row[0] not in always_keys:
                        results.append(row)

            return results[:limit]
        finally:
            conn.close()
