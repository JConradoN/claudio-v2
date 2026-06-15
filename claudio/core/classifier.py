from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claudio.config import Config

# Portado de aurelia/internal/telegram/commands.go — MatchCommand()


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


def _looks_narrative(text: str, keyword: str) -> bool:
    """Anti-false-positive: 2+ palavras significativas antes da keyword → skip."""
    idx = text.find(keyword)
    if idx < 0:
        return False
    before = text[:idx].strip().split()
    stopwords = {
        "o", "a", "os", "as", "um", "uma", "de", "do", "da", "no", "na", "e", "e",
        # pronomes/interrogativas PT-BR que precedem perguntas
        "como", "qual", "quais", "quando", "quanto", "quantos", "quanta",
        "voce", "vc", "me", "nos", "seu", "sua",
        # verbos auxiliares/cópula frequentes antes do tema
        "esta", "estao", "esta", "estamos", "tem", "ha", "eh", "sao",
        # preposições/conjunções
        "que", "se", "por", "para", "com", "em", "ao", "pelo", "pela",
    }
    significant = [w for w in before if w not in stopwords and len(w) > 2]
    return len(significant) >= 3


# Mapa: keyword → (tipo, tools, agent, context_hints)
# ordem importa: mais específico primeiro
_COMMAND_RULES: list[tuple[str, bool, str, list[str], str | None, list[str]]] = [
    # (keyword, exact_match, type, tools, agent, context_hints)

    # Cron
    ("todo dia", False, "cron", [], None, ["schedule", "recurring"]),
    ("toda semana", False, "cron", [], None, ["schedule", "recurring"]),
    ("toda segunda", False, "cron", [], None, ["schedule", "recurring"]),
    ("toda terca", False, "cron", [], None, ["schedule", "recurring"]),
    ("toda quarta", False, "cron", [], None, ["schedule", "recurring"]),
    ("toda quinta", False, "cron", [], None, ["schedule", "recurring"]),
    ("toda sexta", False, "cron", [], None, ["schedule", "recurring"]),
    ("todo sabado", False, "cron", [], None, ["schedule", "recurring"]),
    ("todo domingo", False, "cron", [], None, ["schedule", "recurring"]),
    ("daqui", False, "cron", [], None, ["schedule", "once"]),
    ("hoje as", False, "cron", [], None, ["schedule", "once"]),
    ("amanha as", False, "cron", [], None, ["schedule", "once"]),
    ("agendar", False, "cron", [], None, ["schedule"]),
    ("lembrar", False, "cron", [], None, ["schedule", "reminder"]),
    ("alarme", False, "cron", [], None, ["schedule", "alarm"]),

    # Debug / observabilidade (exact)
    ("/debug", True, "command", [], None, []),
    ("/status", True, "command", [], None, []),
    ("/cron", True, "command", [], None, []),
    ("/reset", True, "command", [], None, []),

    # Delegate para agente
    ("pesquisa", False, "research", ["run_bash"], None, ["research", "web"]),
    ("procura", False, "research", ["run_bash"], None, ["research", "web"]),

    # Execute (comandos de shell/sistema)
    ("execute", False, "execute", ["run_bash"], None, []),
    ("roda", False, "execute", ["run_bash"], None, []),
    ("quanto espaco", False, "execute", ["run_bash"], None, ["disk", "storage"]),
    ("memoria ram", False, "execute", ["run_bash"], None, ["memory", "system"]),
    ("cpu", False, "execute", ["run_bash"], None, ["system"]),
    ("docker", False, "execute", ["run_bash"], None, ["docker"]),
    ("container", False, "execute", ["run_bash"], None, ["docker"]),
    ("log", False, "execute", ["run_bash"], None, ["logs"]),
    ("servico", False, "execute", ["run_bash"], None, ["service"]),
    ("saude do servidor", False, "execute", ["run_bash"], None, ["system", "health"]),
    ("saude do fox", False, "execute", ["run_bash"], None, ["system", "health"]),
    ("status do servidor", False, "execute", ["run_bash"], None, ["system", "health"]),
    ("servidor esta", False, "execute", ["run_bash"], None, ["system", "health"]),
    ("temperatura", False, "execute", ["run_bash"], None, ["system", "thermal"]),
    ("gpu", False, "execute", ["run_bash"], None, ["system", "gpu"]),
    ("nvidia", False, "execute", ["run_bash"], None, ["system", "gpu"]),
    ("vram", False, "execute", ["run_bash"], None, ["system", "gpu"]),
    ("disco", False, "execute", ["run_bash"], None, ["disk"]),
    ("systemctl", False, "execute", ["run_bash"], None, ["service"]),
]


class IntentClassifier:
    """
    Classificador heurístico (heurística primeiro, LLM 27b como fallback).
    Cobre ~80% dos casos sem LLM.
    """

    def __init__(self, config: "Config") -> None:
        self._config = config

    async def classify(self, text: str, history: list[Any] = []) -> IntentResult:
        normalized = _strip_accents(text.strip())

        # Comandos exact-match (prefixo /)
        first_word = normalized.split()[0] if normalized.split() else ""
        for keyword, exact, intent_type, tools, agent, hints in _COMMAND_RULES:
            if exact:
                if first_word == keyword or normalized == keyword:
                    return IntentResult(intent_type, tools[:3], agent, hints)
            else:
                if keyword in normalized and not _looks_narrative(normalized, keyword):
                    return IntentResult(intent_type, tools[:3], agent, hints)

        # Fallback: chat simples (sem LLM na Fase 1 — adicionado na Fase 2)
        return IntentResult("chat", [], None, [], confidence=1.0)
