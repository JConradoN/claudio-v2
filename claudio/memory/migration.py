from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claudio.memory.retrieval import MemoryManager

log = logging.getLogger("claudio.memory.migration")

_AURELIA_DIR = Path.home() / ".aurelia" / "memory"
_SKIP = {"MEMORY.md", "conversation_log.md"}  # logs de conversa, não fatos


async def migrate_aurelia(memory: "MemoryManager") -> int:
    """Importa arquivos ~/.aurelia/memory/*.md para o mem0. Idempotente via check de existência."""
    if not _AURELIA_DIR.exists():
        log.info("migration: %s não encontrado, pulando", _AURELIA_DIR)
        return 0

    total = 0
    for md_file in sorted(_AURELIA_DIR.glob("*.md")):
        if md_file.name in _SKIP:
            continue
        try:
            content = md_file.read_text(encoding="utf-8").strip()
            if not content:
                continue
            # Divide em parágrafos não-vazios de ao menos 30 chars
            paragraphs = [p.strip() for p in content.split("\n\n") if len(p.strip()) >= 30]
            if not paragraphs:
                continue
            # Limita a 10 fatos por arquivo para não poluir a memória
            facts = paragraphs[:10]
            await memory.add(facts, metadata={"source": "aurelia_migration", "file": md_file.name})
            total += len(facts)
            log.info("migration: %s → %d fatos", md_file.name, len(facts))
        except Exception as exc:
            log.warning("migration: falha em %s: %s", md_file.name, exc)

    log.info("migration: total %d fatos importados", total)
    return total
