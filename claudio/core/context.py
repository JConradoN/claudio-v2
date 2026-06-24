from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claudio.config import Config
    from claudio.core.classifier import IntentResult
    from claudio.memory.retrieval import MemoryManager

IDENTITY_BLOCK = """Você é Cláudio, assistente pessoal do Conrado rodando localmente no fox-server.

Perfil do Conrado:
- Desenvolvedor e pesquisador em IA, agentes, Python e infraestrutura
- Nível avançado — não explique conceitos básicos sem ser pedido
- Prefere respostas diretas, técnicas, em PT-BR, sem rodeios
- Em relatórios e conteúdo estruturado: use emojis como ícones de seção (🖥 📊 🐳 🎯 ⚠️) e bullets (•)

Você tem acesso ao fox-server (Ubuntu 26.04, Xeon E5-2696 v3, 2×RTX 3060, 128GB RAM).
Stack oficial: TurboQuant/Qwen3.6 (porta 8082), AgentForge, memória 4 níveis (agent-mesh/Kuzu/mem0).
Serviços ativos: n8n, qdrant, ollama (só embed), open-webui, forte.jus, portainer, claudio-api.

Você tem memória persistente em 4 níveis injetada no topo deste prompt:
- Memória: fatos do mem0 (semântico) + agent_mesh (SQLite compartilhado com outros agentes)
- Conhecimento: grafo Kuzu com decisões, modelos e tecnologias do laboratório

Tools disponíveis:
- run_bash: executa comandos no fox-server
- read_link: lê e analisa URLs via browser autenticado
- list_agents: lista os agentes disponíveis no AgentForge
- run_agent: aciona um agente AgentForge com uma tarefa
- save_memory: salva um fato na memória persistente (mem0 + agent-mesh)

Formato de resposta: livre. Use o formato que melhor comunicar a informação.

Regras invioláveis:
- Forte.jus e fox-vault: zero APIs externas, apenas Ollama local
- Ações destrutivas (rm, docker stop, systemctl stop): peça confirmação explícita
- Nunca inventar outputs de comandos não executados
- Se não souber algo com certeza (histórico, versões anteriores, datas, configurações), diga explicitamente que não sabe — nunca invente
- Não faça git commit, git push ou qualquer operação de versionamento — você não tem acesso a repositórios git
- Nunca invente outputs de ações que não executou com uma tool real"""

_INTENT_HINTS: dict[str, str] = {
    "chat": "",
    "command": "\nModo: comando interno. Responda de forma concisa.",
    "execute": "\nUse as tools necessárias para completar a tarefa. Após obter os dados, escreva a resposta final.",
    "cron": "\nModo: agendamento. Confirme os detalhes do job com o usuário.",
}


@dataclass(frozen=True)
class ContextBlock:
    content: str
    token_estimate: int
    source: str


def _estimate_tokens(text: str) -> int:
    return len(text) // 4


class ContextBuilder:
    def __init__(self, config: "Config", memory: "MemoryManager | None" = None) -> None:
        self._config = config
        self._memory = memory

    async def build(
        self,
        intent: "IntentResult",
        session: Any,
        max_tokens: int = 8000,
        user_message: str = "",
    ) -> str:
        blocks: list[ContextBlock] = []

        # 1. Identity (nunca truncar)
        blocks.append(ContextBlock(IDENTITY_BLOCK, _estimate_tokens(IDENTITY_BLOCK), "identity"))

        # 2. Intent instructions
        hint = _INTENT_HINTS.get(intent.type, "")
        if hint:
            blocks.append(ContextBlock(hint, _estimate_tokens(hint), "intent"))

        # 3. Projeto ativo
        project = getattr(session, "project", None)
        if project:
            proj_block = f"\nProjeto ativo: {project}"
            blocks.append(ContextBlock(proj_block, _estimate_tokens(proj_block), "project"))

        if self._memory and user_message:
            query = user_message
            hints = getattr(intent, "context_hints", [])
            used = sum(b.token_estimate for b in blocks)
            remaining = max_tokens - used - 50  # margem de 50 tokens

            # Budget: 55% para mem0+agent_mesh, 35% para kuzu, 10% reserva
            mem_budget = int(remaining * 0.55)
            kuzu_budget = int(remaining * 0.35)

            # 4. Memória episódica: mem0 + agent_mesh (paralelo)
            mem_kuzu = await asyncio.gather(
                self._memory.search(query=query, context_hints=hints, limit=10, max_tokens=mem_budget),
                self._memory.search_kuzu(query=query, limit=8, max_tokens=kuzu_budget),
                return_exceptions=True,
            )
            mem_fragments, kuzu_fragments = mem_kuzu

            if isinstance(mem_fragments, list) and mem_fragments:
                mem_lines = "\n".join(
                    f"- [{f.source}] {f.fact}" for f in mem_fragments
                )
                mem_block = f"\nMemória:\n{mem_lines}"
                blocks.append(ContextBlock(mem_block, _estimate_tokens(mem_block), "memory"))

            # 5. Conhecimento estruturado: kuzu (decisões, modelos, tecnologias)
            if isinstance(kuzu_fragments, list) and kuzu_fragments:
                kuzu_lines = "\n".join(
                    f"- [kuzu:{f.metadata.get('kuzu_type', 'graph')}] {f.fact}"
                    for f in kuzu_fragments
                )
                kuzu_block = f"\nConhecimento:\n{kuzu_lines}"
                blocks.append(ContextBlock(kuzu_block, _estimate_tokens(kuzu_block), "kuzu"))

        # Monta e verifica total
        total_tokens = sum(b.token_estimate for b in blocks)
        if total_tokens <= max_tokens:
            return "\n".join(b.content for b in blocks).strip()

        # Truncamento: kuzu primeiro, depois memória, depois projeto; nunca identity/intent
        for source_to_drop in ("kuzu", "memory", "project"):
            blocks = [b for b in blocks if b.source != source_to_drop]
            total_tokens = sum(b.token_estimate for b in blocks)
            if total_tokens <= max_tokens:
                break

        return "\n".join(b.content for b in blocks).strip()
