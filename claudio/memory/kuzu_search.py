from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claudio.memory.retrieval import MemoryFragment

log = logging.getLogger("claudio.memory.kuzu")

_DB_PATH = "~/.agent-mesh/research-graph.db"

# Cypher queries estáticas — carregam o "estado da arte" do grafo
_QUERIES: dict[str, str] = {
    "decisions": "MATCH (n:Decision) WHERE n.status = 'active' RETURN n.id, n.title, n.description LIMIT 6",
    "findings": "MATCH (n:Finding) WHERE n.confidence = 'confirmed' RETURN n.id, n.description LIMIT 5",
    "models": "MATCH (n:Model) WHERE n.status IN ['champion', 'active'] RETURN n.id, n.family, n.size_b, n.status, n.notes LIMIT 8",
    "technologies": "MATCH (n:Technology) WHERE n.status = 'active' RETURN n.id, n.name, n.category, n.notes LIMIT 6",
}


def _score_text(text: str, keywords: list[str]) -> float:
    """Pontuação por overlap de keywords (0.0–1.0)."""
    if not keywords:
        return 0.5
    text_lower = text.lower()
    hits = sum(1 for kw in keywords if kw in text_lower)
    return hits / len(keywords)


def _format_model(row: list) -> str:
    node_id, family, size_b, status, notes = row
    size_str = f"{int(size_b)}b" if size_b else "?"
    parts = [f"Model:{node_id} ({family} {size_str}, {status})"]
    if notes:
        parts.append(notes)
    return " — ".join(parts)


def _format_decision(row: list) -> str:
    node_id, title, description = row
    return f"Decisão: {title}: {description}"


def _format_finding(row: list) -> str:
    node_id, description = row
    return f"Finding: {description}"


def _format_technology(row: list) -> str:
    node_id, name, category, notes = row
    base = f"Tech:{name} ({category})"
    return f"{base} — {notes}" if notes else base


class KuzuRetriever:
    """Busca decisões, findings, modelos e tecnologias no grafo Kuzu."""

    def __init__(self, db_path: str = _DB_PATH) -> None:
        self._db_path = Path(db_path).expanduser()

    async def search(
        self,
        query: str,
        limit: int = 5,
        max_tokens: int = 200,
    ) -> list[MemoryFragment]:
        from claudio.memory.retrieval import MemoryFragment

        if not self._db_path.exists():
            log.debug("kuzu: db não encontrado em %s", self._db_path)
            return []

        try:
            candidates = await asyncio.to_thread(self._fetch_all)
        except Exception as exc:
            log.warning("kuzu: falha ao buscar: %s", exc)
            return []

        keywords = [w.lower() for w in query.split() if len(w) > 3]

        scored: list[tuple[float, str, str]] = []
        for source, text in candidates:
            score = _score_text(text, keywords)
            # Sempre inclui modelos champion e decisions com score baseline
            if source == "models" and "champion" in text:
                score = max(score, 0.8)
            elif source in ("decisions", "findings"):
                score = max(score, 0.4)
            scored.append((score, source, text))

        scored.sort(key=lambda x: x[0], reverse=True)

        fragments: list[MemoryFragment] = []
        total_tokens = 0
        for score, source, text in scored[:limit * 2]:
            tokens = len(text) // 4
            if total_tokens + tokens > max_tokens:
                continue
            fragments.append(MemoryFragment(
                fact=text,
                score=score,
                source="kuzu",
                metadata={"kuzu_type": source},
            ))
            total_tokens += tokens
            if len(fragments) >= limit:
                break

        return fragments

    def _fetch_all(self) -> list[tuple[str, str]]:
        import kuzu
        db = kuzu.Database(str(self._db_path))
        conn = kuzu.Connection(db)

        results: list[tuple[str, str]] = []

        # Decisions
        r = conn.execute(_QUERIES["decisions"])
        while r.has_next():
            row = r.get_next()
            results.append(("decisions", _format_decision(row)))

        # Findings
        r = conn.execute(_QUERIES["findings"])
        while r.has_next():
            row = r.get_next()
            results.append(("findings", _format_finding([row[0], row[1]])))

        # Models
        r = conn.execute(_QUERIES["models"])
        while r.has_next():
            row = r.get_next()
            results.append(("models", _format_model(row)))

        # Technologies
        r = conn.execute(_QUERIES["technologies"])
        while r.has_next():
            row = r.get_next()
            results.append(("technologies", _format_technology(row)))

        return results
