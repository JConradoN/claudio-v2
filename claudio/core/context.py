from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claudio.config import Config
    from claudio.core.classifier import IntentResult

IDENTITY_BLOCK = """Você é Cláudio, assistente pessoal do Conrado rodando localmente no fox-server.

Perfil do Conrado:
- Desenvolvedor e pesquisador em IA, agentes, Python e infraestrutura
- Nível avançado — não explique conceitos básicos sem ser pedido
- Prefere respostas diretas, técnicas, em PT-BR, sem emojis, sem rodeios

Você tem acesso ao fox-server (Ubuntu 26.04, Xeon E5-2696 v3, 2×RTX 3060, 128GB RAM).
Serviços ativos: n8n, qdrant, ollama, open-webui, aurelia, forte.jus, portainer.

Regras invioláveis:
- Forte.jus e fox-vault: zero APIs externas, apenas Ollama local
- Ações destrutivas (rm, docker stop, systemctl stop): peça confirmação explícita
- Nunca inventar outputs de comandos não executados"""

_INTENT_HINTS: dict[str, str] = {
    "chat": "",
    "command": "\nModo: comando interno. Responda de forma concisa.",
    "execute": "\nModo: execução. Use as tools disponíveis para completar a tarefa.",
    "research": "\nModo: pesquisa. Busque informações e sintetize.",
    "delegate": "\nModo: delegação. Coordene o agente apropriado.",
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
    """
    Monta o system prompt final.
    Fase 1: identity + intent hints (mem0 e kuzu são stubs).
    """

    def __init__(self, config: "Config") -> None:
        self._config = config

    async def build(
        self,
        intent: "IntentResult",
        session: Any,
        max_tokens: int = 600,
    ) -> str:
        blocks: list[ContextBlock] = []

        identity = ContextBlock(IDENTITY_BLOCK, _estimate_tokens(IDENTITY_BLOCK), "identity")
        blocks.append(identity)

        # Intent instructions
        hint = _INTENT_HINTS.get(intent.type, "")
        if hint:
            blocks.append(ContextBlock(hint, _estimate_tokens(hint), "intent"))

        # Projeto ativo (se session tiver)
        project = getattr(session, "project", None)
        if project:
            proj_block = f"\nProjeto ativo: {project}"
            blocks.append(ContextBlock(proj_block, _estimate_tokens(proj_block), "project"))

        # Stub: mem0 e kuzu retornam vazio na Fase 1
        # Será substituído na Fase 2 por retrieval real

        # Monta e verifica total
        result = "\n".join(b.content for b in blocks)
        total_tokens = sum(b.token_estimate for b in blocks)

        if total_tokens > max_tokens:
            # Truncamento de emergência: mantém identity + intent, trunca o resto
            result = "\n".join(
                b.content for b in blocks if b.source in ("identity", "intent")
            )

        return result.strip()
