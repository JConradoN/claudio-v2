from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claudio.config import Config

log = logging.getLogger("claudio.memory")

_USER_ID = "conrado"


@dataclass
class MemoryFragment:
    fact: str
    score: float
    source: str = "mem0"
    metadata: dict = field(default_factory=dict)


class MemoryManager:
    """Agrega 3 fontes dinâmicas: mem0, agent_mesh e kuzu."""

    def __init__(self, config: "Config") -> None:
        self._config = config
        self._mem0: Any = None
        self._mesh: Any = None   # AgentMeshRetriever (lazy)
        self._kuzu: Any = None   # KuzuRetriever (lazy)

    def _get_mesh(self) -> Any:
        if self._mesh is None:
            from claudio.memory.agent_mesh import AgentMeshRetriever
            self._mesh = AgentMeshRetriever(self._config.agent_mesh_db)
        return self._mesh

    def _get_kuzu(self) -> Any:
        if self._kuzu is None:
            from claudio.memory.kuzu_search import KuzuRetriever
            self._kuzu = KuzuRetriever()
        return self._kuzu

    def _get_mem0(self) -> Any:
        if self._mem0 is None:
            from mem0 import Memory
            self._mem0 = Memory.from_config({
                "embedder": {
                    "provider": "ollama",
                    "config": {
                        "model": self._config.embed_model,
                        "ollama_base_url": self._config.ollama_url,
                    },
                },
                "llm": {
                    "provider": "ollama",
                    "config": {
                        "model": self._config.default_model,
                        "ollama_base_url": self._config.ollama_url,
                    },
                },
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "host": "localhost",
                        "port": 6333,
                        "collection_name": self._config.mem0_collection,
                    },
                },
            })
        return self._mem0

    async def search(
        self,
        query: str,
        context_hints: list[str] | None = None,
        limit: int = 10,
        max_tokens: int = 2000,
    ) -> list[MemoryFragment]:
        """Busca em mem0 + agent_mesh em paralelo. Retorna fragmentos ordenados por score."""
        if not query.strip():
            return []
        full_query = query
        if context_hints:
            full_query = f"{query} {' '.join(context_hints)}"

        # Divide budget: 60% mem0, 40% agent_mesh
        mem0_budget = int(max_tokens * 0.6)
        mesh_budget = max_tokens - mem0_budget

        mem0_task = asyncio.create_task(self._search_mem0(full_query, limit, mem0_budget))
        mesh_task = asyncio.create_task(self._get_mesh().search(full_query, limit=2, max_tokens=mesh_budget))

        mem0_results, mesh_results = await asyncio.gather(mem0_task, mesh_task, return_exceptions=True)

        fragments: list[MemoryFragment] = []
        if isinstance(mem0_results, list):
            fragments.extend(mem0_results)
        else:
            log.warning("mem0.search falhou: %s", mem0_results)

        if isinstance(mesh_results, list):
            fragments.extend(mesh_results)
        else:
            log.warning("agent_mesh.search falhou: %s", mesh_results)

        log.debug(
            "memory.search: %d fragmentos (mem0=%d mesh=%d) para '%s'",
            len(fragments),
            len(mem0_results) if isinstance(mem0_results, list) else 0,
            len(mesh_results) if isinstance(mesh_results, list) else 0,
            query[:50],
        )
        return fragments

    async def _search_mem0(self, query: str, limit: int, max_tokens: int) -> list[MemoryFragment]:
        try:
            mem0 = self._get_mem0()
            raw = await asyncio.to_thread(
                mem0.search,
                query,
                filters={"user_id": _USER_ID},
                top_k=limit,
                threshold=0.3,
            )
            results = raw.get("results", raw) if isinstance(raw, dict) else raw
            fragments: list[MemoryFragment] = []
            total_tokens = 0
            for r in results:
                fact = r.get("memory", "").strip()
                if not fact:
                    continue
                score = float(r.get("score", 0.0))
                tokens = len(fact) // 4
                if total_tokens + tokens > max_tokens:
                    break
                fragments.append(MemoryFragment(
                    fact=fact,
                    score=score,
                    source="mem0",
                    metadata=r.get("metadata", {}),
                ))
                total_tokens += tokens
            return fragments
        except Exception as exc:
            log.warning("mem0.search falhou: %s", exc)
            return []

    async def search_kuzu(
        self,
        query: str,
        limit: int = 8,
        max_tokens: int = 1500,
    ) -> list[MemoryFragment]:
        """Busca separada no Kuzu (grafo de decisões/modelos). Chamada pelo ContextBuilder."""
        try:
            return await self._get_kuzu().search(query, limit=limit, max_tokens=max_tokens)
        except Exception as exc:
            log.warning("kuzu.search falhou: %s", exc)
            return []

    async def add(self, facts: list[str], metadata: dict | None = None) -> None:
        if not facts:
            return
        try:
            mem0 = self._get_mem0()
            messages = [{"role": "user", "content": f} for f in facts]
            # infer=False: não usa LLM, armazena diretamente
            await asyncio.to_thread(
                mem0.add,
                messages,
                user_id=_USER_ID,
                metadata=metadata or {},
                infer=False,
            )
            log.info("memory.add: %d fatos gravados", len(facts))
        except Exception as exc:
            log.warning("memory.add falhou: %s", exc)

    async def get_all(self, limit: int = 50) -> list[MemoryFragment]:
        try:
            mem0 = self._get_mem0()
            raw = await asyncio.to_thread(
                mem0.get_all,
                filters={"user_id": _USER_ID},
                limit=limit,
            )
            results = raw.get("results", raw) if isinstance(raw, dict) else raw
            return [
                MemoryFragment(
                    fact=r.get("memory", ""),
                    score=1.0,
                    source="mem0",
                    metadata={"id": r.get("id", ""), **(r.get("metadata") or {})},
                )
                for r in results
                if r.get("memory")
            ]
        except Exception as exc:
            log.warning("memory.get_all falhou: %s", exc)
            return []

    async def delete(self, memory_id: str) -> bool:
        """Remove um fato do mem0 por ID. Retorna True se bem-sucedido."""
        try:
            mem0 = self._get_mem0()
            await asyncio.to_thread(mem0.delete, memory_id)
            log.info("memory.delete: %s removido", memory_id[:8])
            return True
        except Exception as exc:
            log.warning("memory.delete falhou para %s: %s", memory_id[:8], exc)
            return False

    async def add_explicit(self, fact: str) -> None:
        """Adiciona fato com metadado source_type=explicit (maior prioridade)."""
        await self.add([fact], metadata={"source_type": "explicit"})
