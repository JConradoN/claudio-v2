from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, AsyncIterator, Any

import httpx

if TYPE_CHECKING:
    from claudio.config import Config
    from claudio.core.model_manager import ModelManager
    from claudio.audit import AuditLog

log = logging.getLogger("claudio.executor")


class Executor:
    """
    Envia mensagens ao Ollama via /api/chat.
    Fase 1: sem tools. Fase 2+: tools filtradas por security_profile.
    """

    def __init__(
        self,
        config: "Config",
        model_manager: "ModelManager",
        audit: "AuditLog",
    ) -> None:
        self._config = config
        self._mm = model_manager
        self._audit = audit
        self._base_url = config.ollama_url.rstrip("/")

    async def run(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[str],
        session: Any,
        security_profile: str,
    ) -> str:
        run_id = str(uuid.uuid4())
        model = self._config.default_model
        started = time.monotonic()

        await self._mm.ensure(model)

        messages = self._build_messages(system_prompt, user_message, session)

        self._audit.log("executor.run.start", {
            "run_id": run_id,
            "model": model,
            "msg_preview": user_message[:100],
            "session_id": getattr(session, "id", None),
        })

        try:
            response_text = await self._chat(model, messages)
        except Exception as exc:
            self._audit.log("executor.run.error", {
                "run_id": run_id, "error": str(exc)
            }, level="error")
            raise

        duration_ms = int((time.monotonic() - started) * 1000)
        self._audit.log("executor.run.done", {
            "run_id": run_id,
            "duration_ms": duration_ms,
            "output_len": len(response_text),
        })

        return response_text

    async def run_stream(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[str],
        session: Any,
        security_profile: str,
    ) -> AsyncIterator[str]:
        model = self._config.default_model
        await self._mm.ensure(model)

        messages = self._build_messages(system_prompt, user_message, session)

        async with httpx.AsyncClient(timeout=self._config.ollama_timeout_s) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/api/chat",
                json={"model": model, "messages": messages, "stream": True},
            ) as r:
                r.raise_for_status()
                import json
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            yield content
                        if chunk.get("done"):
                            break
                    except Exception:
                        continue

    def _build_messages(self, system_prompt: str, user_message: str, session: Any) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        # Histórico da sessão
        history = getattr(session, "history", [])
        for turn in history[-20:]:  # últimos 20 turns
            messages.append({"role": turn.role, "content": turn.content})

        messages.append({"role": "user", "content": user_message})
        return messages

    async def _chat(self, model: str, messages: list[dict]) -> str:
        async with httpx.AsyncClient(timeout=self._config.ollama_timeout_s) as client:
            r = await client.post(
                f"{self._base_url}/api/chat",
                json={"model": model, "messages": messages, "stream": False},
            )
            r.raise_for_status()
            data = r.json()
            return data["message"]["content"]
