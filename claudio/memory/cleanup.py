from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claudio.config import Config

log = logging.getLogger("claudio.memory.cleanup")

# Padrões que identificam lixo de migração
_GARBAGE_PATTERNS = [
    re.compile(r"^#{1,6}\s"),          # markdown headers (## Título)
    re.compile(r"^\|"),                # markdown tables
    re.compile(r"^---\s*$"),           # yaml separators
    re.compile(r"^name:\s+\w"),        # yaml frontmatter
    re.compile(r"^description:\s+"),   # yaml frontmatter
    re.compile(r"^type:\s+\w"),        # yaml frontmatter
    re.compile(r"^```"),               # code blocks
    re.compile(r"^###?\s"),            # sub-headers
]

# Padrões que indicam identidade errada (Aurelia vs Cláudio)
_WRONG_IDENTITY_PATTERNS = [
    re.compile(r"Aurelia Assistente", re.IGNORECASE),
    re.compile(r"deviation from this name is not possible", re.IGNORECASE),
    re.compile(r"Desenvolvedor principal do \*\*Aurelia\*\*", re.IGNORECASE),
    re.compile(r"igormaneschy/aurelia", re.IGNORECASE),
]


@dataclass
class GarbageReport:
    id: str
    fact: str
    reason: str


def _is_garbage(fact: str) -> str | None:
    """Retorna a razão se o fato é lixo, None se é útil."""
    stripped = fact.strip()
    lines = stripped.splitlines()
    first_line = lines[0] if lines else ""

    for pattern in _GARBAGE_PATTERNS:
        if pattern.match(first_line):
            return f"markdown/yaml artifact: {first_line[:40]}"

    for pattern in _WRONG_IDENTITY_PATTERNS:
        if pattern.search(fact):
            return "identidade errada (Aurelia vs Cláudio)"

    # JSON bruto sem contexto claro
    if first_line.startswith("{") and len(fact) > 100:
        return "JSON bruto sem contexto"

    # Documentação embutida: maioria das linhas são markdown estrutural
    if len(lines) >= 3:
        markdown_lines = sum(
            1 for ln in lines
            if ln.strip().startswith(("#", "|", "```", "---", "-", "*"))
        )
        if markdown_lines / len(lines) > 0.6:
            return f"documentação markdown embutida ({markdown_lines}/{len(lines)} linhas estruturais)"

    return None


class MemoryCleanup:
    """Identifica e remove fatos inúteis ou incorretos do mem0."""

    def __init__(self, config: "Config") -> None:
        self._config = config
        self._mem0: Any = None

    def _get_mem0(self) -> Any:
        if self._mem0 is None:
            from mem0 import Memory
            self._mem0 = Memory.from_config({
                "embedder": {"provider": "ollama", "config": {
                    "model": self._config.embed_model,
                    "ollama_base_url": self._config.ollama_url,
                }},
                "llm": {"provider": "ollama", "config": {
                    "model": self._config.default_model,
                    "ollama_base_url": self._config.ollama_url,
                }},
                "vector_store": {"provider": "qdrant", "config": {
                    "host": "localhost", "port": 6333,
                    "collection_name": self._config.mem0_collection,
                }},
            })
        return self._mem0

    def scan(self) -> list[GarbageReport]:
        """Identifica fatos lixo sem deletar."""
        import asyncio
        mem0 = self._get_mem0()
        raw = mem0.get_all(filters={"user_id": "conrado"}, limit=500)
        results = raw.get("results", raw) if isinstance(raw, dict) else raw

        garbage: list[GarbageReport] = []
        for r in results:
            fact = r.get("memory", "").strip()
            reason = _is_garbage(fact)
            if reason:
                garbage.append(GarbageReport(
                    id=r.get("id", ""),
                    fact=fact[:100],
                    reason=reason,
                ))
        return garbage

    def run(self, dry_run: bool = False) -> tuple[int, int]:
        """Remove fatos lixo. Retorna (total_analisado, total_removido)."""
        mem0 = self._get_mem0()
        raw = mem0.get_all(filters={"user_id": "conrado"}, limit=500)
        results = raw.get("results", raw) if isinstance(raw, dict) else raw

        total = len(results)
        removed = 0

        for r in results:
            fact = r.get("memory", "").strip()
            memory_id = r.get("id", "")
            reason = _is_garbage(fact)
            if reason and memory_id:
                log.info("cleanup: %s removendo [%s] '%s...' — %s",
                         "DRY" if dry_run else "DELETE",
                         memory_id[:8], fact[:50], reason)
                if not dry_run:
                    try:
                        mem0.delete(memory_id)
                        removed += 1
                    except Exception as exc:
                        log.warning("cleanup: falha ao deletar %s: %s", memory_id[:8], exc)

        log.info("cleanup: %d/%d fatos %s",
                 removed, total, "marcados (dry)" if dry_run else "removidos")
        return total, removed
