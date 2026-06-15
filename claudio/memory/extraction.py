from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from claudio.config import Config

log = logging.getLogger("claudio.memory.extraction")

_PROMPT = """Você é um extrator de memória. Analise a conversa e extraia APENAS fatos duradouros sobre o usuário Conrado ou sobre o fox-server que seriam úteis em conversas futuras.

Regras:
- Somente fatos verificados, não inferências
- Ignore saudações, perguntas genéricas e informações temporárias
- Máximo 5 fatos por sessão
- Retorne JSON array de strings, sem texto adicional

Conversa:
{conversation}

JSON:"""


class SessionExtractor:
    """Extrai fatos de uma sessão e os armazena no mem0."""

    def __init__(self, config: "Config") -> None:
        self._config = config
        self._base_url = config.ollama_url.rstrip("/")

    async def extract(self, session: Any) -> list[str]:
        history = getattr(session, "history", [])
        if len(history) < self._config.memory_extraction_min_turns:
            return []

        turns = [
            t for t in history[-20:]
            if getattr(t, "role", "") in ("user", "assistant")
            and getattr(t, "content", "").strip()
        ]
        if len(turns) < 2:
            return []

        conversation = "\n".join(
            f"{t.role.upper()}: {t.content[:400]}" for t in turns
        )

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    f"{self._base_url}/api/chat",
                    json={
                        "model": self._config.default_model,
                        "messages": [{"role": "user", "content": _PROMPT.format(conversation=conversation)}],
                        "stream": False,
                        "think": False,
                        "keep_alive": "4h",
                        "options": {"temperature": 0.1, "num_ctx": 4096},
                    },
                )
                r.raise_for_status()
                content = r.json().get("message", {}).get("content", "").strip()
                # Extrai JSON mesmo se o modelo adicionar texto antes/depois
                start = content.find("[")
                end = content.rfind("]") + 1
                if start == -1 or end == 0:
                    return []
                facts = json.loads(content[start:end])
                if isinstance(facts, list):
                    return [str(f).strip() for f in facts if str(f).strip()][:5]
        except Exception as exc:
            log.warning("extraction.extract falhou: %s", exc)
        return []
