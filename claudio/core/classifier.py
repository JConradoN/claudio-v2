from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claudio.config import Config


@dataclass(frozen=True)
class IntentResult:
    type: str
    tools: list[str] = field(default_factory=list)
    agent: str | None = None
    context_hints: list[str] = field(default_factory=list)
    confidence: float = 1.0


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(c) != "Mn"
    )


# Todas as tools disponíveis — passadas ao modelo por padrão
_ALL_TOOLS = ["run_bash", "read_link", "list_agents", "run_agent", "save_memory"]

# Chat casual: sem tarefa técnica, sem tools necessárias
_CASUAL_RE = re.compile(
    r"^(oi|ola|ola |bom dia|boa tarde|boa noite|e ai|eai|tudo bem|tudo bom|"
    r"obrigado|valeu|flw|tchau|ate mais|certo|entendido|ok|"
    r"haha|kkk+|lol|rs+|😂|👍|blz|beleza)"
    r"[\s!?.,🙂😄]*$",
    re.IGNORECASE,
)

# Padrões de agendamento — tipo cron, sem tools de execução imediata
_CRON_RE = re.compile(
    r"\b(todo dia|toda semana|toda (segunda|terca|quarta|quinta|sexta)|"
    r"todo (sabado|domingo)|daqui a|hoje as \d|amanha as \d|agendar|alarme)\b",
    re.IGNORECASE,
)

# Comandos internos — slash commands exatos
_SLASH_COMMANDS = {"/debug", "/status", "/cron", "/reset", "/imagem", "/audio"}


class IntentClassifier:
    """
    Classificador minimalista: menos harness = melhor desempenho do modelo.

    Lógica:
    1. Slash command → command (sem tools)
    2. Agendamento → cron (sem tools)
    3. Chat casual curto → chat (sem tools)
    4. Tudo o mais → execute com TODAS as tools
    """

    def __init__(self, config: "Config") -> None:
        self._config = config

    async def classify(self, text: str, history: list[Any] = []) -> IntentResult:
        normalized = _strip_accents(text.strip())
        first_word = normalized.split()[0] if normalized.split() else ""

        # 1. Slash commands exatos
        if first_word in _SLASH_COMMANDS:
            return IntentResult("command", [], None, [])

        # 2. Agendamento / cron
        if _CRON_RE.search(normalized):
            return IntentResult("cron", [], None, ["schedule"])

        # 3. Chat casual — mensagem curta e claramente conversacional
        if _CASUAL_RE.match(normalized):
            return IntentResult("chat", [], None, [])

        # 4. Default: passa todas as tools — o modelo decide o que usar
        return IntentResult("execute", _ALL_TOOLS, None, [])
