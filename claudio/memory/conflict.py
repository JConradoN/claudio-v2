from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claudio.config import Config

log = logging.getLogger("claudio.memory.conflict")

_USER_ID = "conrado"
_KUZU_DB = "~/.agent-mesh/research-graph.db"

# Marcadores que indicam referência histórica — não é conflito, é consistência
_HISTORICAL_MARKERS = [
    "antes do", "antes de", "usava", "era ", "foi ", "anterior",
    "substituído", "migrado", "v1", "versão anterior", "até junho",
    "até 2026", "antigamente",
]


def _is_historical_reference(fact: str) -> bool:
    """Fato menciona entidade no passado — não é conflito com status superseded."""
    fact_lower = fact.lower()
    return any(m in fact_lower for m in _HISTORICAL_MARKERS)


@dataclass
class Conflict:
    mem0_id: str
    mem0_fact: str
    kuzu_entity: str        # ex: "Model:qwen3.5:27b"
    kuzu_fact: str          # o que o kuzu diz
    conflict_type: str      # "stale_model" | "stale_tech" | "stale_decision"
    options: dict = field(default_factory=lambda: {
        "A": "Manter mem0 (o kuzu está desatualizado)",
        "B": "Usar kuzu (deletar do mem0)",
        "C": "Nenhum está correto (deletar mem0, kuzu fica)",
    })


class ConflictDetector:
    """Cruza mem0 com kuzu e detecta inconsistências."""

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

    def _load_kuzu_entities(self) -> dict[str, list[tuple[str, str, str]]]:
        """
        Retorna entidades superseded/rejected do kuzu para cruzar com mem0.
        Formato: {keyword: [(entity_id, entity_type, kuzu_description), ...]}
        """
        db_path = Path(_KUZU_DB).expanduser()
        if not db_path.exists():
            return {}

        import kuzu
        db = kuzu.Database(str(db_path))
        conn = kuzu.Connection(db)
        entities: dict[str, list[tuple[str, str, str]]] = {}

        # Modelos superseded
        r = conn.execute(
            "MATCH (n:Model) WHERE n.status IN ['superseded','rejected','discarded'] "
            "RETURN n.id, n.notes"
        )
        while r.has_next():
            row = r.get_next()
            mid, notes = row[0], row[1] or ""
            keyword = mid.split(":")[0].lower()  # ex: "gemma4" de "gemma4:26b"
            entities.setdefault(mid.lower(), []).append(
                (mid, "Model", f"status=superseded. {notes}")
            )
            if keyword != mid.lower():
                entities.setdefault(keyword, []).append(
                    (mid, "Model", f"status=superseded. {notes}")
                )

        # Decisions superseded
        r = conn.execute(
            "MATCH (n:Decision) WHERE n.status = 'superseded' "
            "RETURN n.id, n.title, n.description"
        )
        while r.has_next():
            row = r.get_next()
            did, title, desc = row
            for keyword in title.lower().split():
                if len(keyword) > 4:
                    entities.setdefault(keyword, []).append(
                        (did, "Decision", f"Decisão superseded: {title}. {desc}")
                    )

        # Technologies superseded
        r = conn.execute(
            "MATCH (n:Technology) WHERE n.status IN ['superseded','rejected','discarded'] "
            "RETURN n.id, n.name, n.notes"
        )
        while r.has_next():
            row = r.get_next()
            tid, name, notes = row
            for keyword in [name.lower(), tid.lower()]:
                entities.setdefault(keyword, []).append(
                    (tid, "Technology", f"Tech superseded: {name}. {notes or ''}")
                )

        return entities

    def detect(self) -> list[Conflict]:
        """Detecta conflitos entre mem0 e kuzu. Não requer LLM."""
        try:
            kuzu_entities = self._load_kuzu_entities()
        except Exception as exc:
            log.warning("conflict: falha ao carregar kuzu: %s", exc)
            return []

        if not kuzu_entities:
            return []

        mem0 = self._get_mem0()
        raw = mem0.get_all(filters={"user_id": _USER_ID}, limit=500)
        results = raw.get("results", raw) if isinstance(raw, dict) else raw

        conflicts: list[Conflict] = []
        seen_mem0_ids: set[str] = set()

        for r in results:
            fact = r.get("memory", "").strip()
            memory_id = r.get("id", "")
            if not fact or not memory_id or memory_id in seen_mem0_ids:
                continue

            fact_lower = fact.lower()

            # Referências históricas não são conflitos
            if _is_historical_reference(fact):
                continue

            for keyword, kuzu_list in kuzu_entities.items():
                if keyword not in fact_lower:
                    continue

                for entity_id, entity_type, kuzu_desc in kuzu_list:
                    seen_mem0_ids.add(memory_id)
                    conflicts.append(Conflict(
                        mem0_id=memory_id,
                        mem0_fact=fact[:200],
                        kuzu_entity=f"{entity_type}:{entity_id}",
                        kuzu_fact=kuzu_desc[:200],
                        conflict_type=f"stale_{entity_type.lower()}",
                    ))
                    break  # um conflito por fato mem0

                if memory_id in seen_mem0_ids:
                    break

        log.info("conflict: %d conflitos detectados", len(conflicts))
        return conflicts

    def resolve(self, conflict: "Conflict", choice: str) -> str:
        """
        Aplica a resolução do usuário.
        choice: "A" = manter mem0 | "B" = deletar mem0 (kuzu vence) | "C" = deletar mem0
        """
        choice = choice.upper().strip()

        if choice == "A":
            return "Memória mantida. Kuzu permanece como está (pode estar desatualizado)."

        if choice in ("B", "C"):
            mem0 = self._get_mem0()
            try:
                mem0.delete(conflict.mem0_id)
                label = "kuzu vence" if choice == "B" else "nenhum correto"
                log.info("conflict.resolve: deletado %s (%s)", conflict.mem0_id[:8], label)
                return "Fato removido do mem0."
            except Exception as exc:
                log.warning("conflict.resolve: falha ao deletar %s: %s", conflict.mem0_id[:8], exc)
                return f"Erro ao remover: {exc}"

        return "Opção inválida. Use A, B ou C."
