from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, AsyncIterator, Any

import httpx

from claudio.core.tools import TOOL_SCHEMAS, run_bash

if TYPE_CHECKING:
    from claudio.config import Config
    from claudio.core.model_manager import ModelManager
    from claudio.audit import AuditLog

log = logging.getLogger("claudio.executor")

_MAX_TOOL_ROUNDS = 5   # evita loop infinito de tool calls


class Executor:
    """
    Envia mensagens ao Ollama via /api/chat com loop de tool calling.
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
        use_tools = bool(tools)

        self._audit.log("executor.run.start", {
            "run_id": run_id,
            "model": model,
            "msg_preview": user_message[:100],
            "session_id": getattr(session, "id", None),
            "tools": tools,
        })

        try:
            response_text = await self._chat_with_tools(
                model, messages, use_tools, security_profile, run_id
            )
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

    async def _chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        use_tools: bool,
        security_profile: str,
        run_id: str,
    ) -> str:
        """Loop: chama o LLM → executa tool calls → repete até resposta final."""
        for round_num in range(_MAX_TOOL_ROUNDS):
            data = await self._call_ollama(model, messages, use_tools)
            msg = data.get("message", {})
            tool_calls = msg.get("tool_calls", [])

            if not tool_calls:
                # Resposta final — sem mais tool calls
                return msg.get("content", "")

            # Adiciona a mensagem do assistente com as tool calls
            messages.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": tool_calls,
            })

            # Executa cada tool call e adiciona o resultado
            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                args = fn.get("arguments", {})

                self._audit.log("tool.call", {
                    "run_id": run_id,
                    "tool": tool_name,
                    "args": str(args)[:200],
                })

                result = await self._execute_tool(tool_name, args, security_profile)

                self._audit.log("tool.result", {
                    "run_id": run_id,
                    "tool": tool_name,
                    "result_len": len(result),
                })

                messages.append({
                    "role": "tool",
                    "content": result,
                })

        # Atingiu limite de rounds — pede resposta final sem tools
        log.warning("executor: atingiu limite de %d rounds de tool calls", _MAX_TOOL_ROUNDS)
        data = await self._call_ollama(model, messages, use_tools=False)
        return data.get("message", {}).get("content", "")

    async def _execute_tool(self, tool_name: str, args: dict, security_profile: str) -> str:
        if tool_name == "run_bash":
            command = args.get("command", "")
            if not command:
                return "[ERRO] Comando vazio"
            return run_bash(command, security_profile)
        return f"[ERRO] Tool desconhecida: {tool_name}"

    async def _call_ollama(
        self, model: str, messages: list[dict], use_tools: bool
    ) -> dict:
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"temperature": 0.7, "num_ctx": 8192},
        }
        if use_tools:
            payload["tools"] = TOOL_SCHEMAS

        async with httpx.AsyncClient(timeout=self._config.ollama_timeout_s) as client:
            r = await client.post(f"{self._base_url}/api/chat", json=payload)
            r.raise_for_status()
            return r.json()

    async def run_stream(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[str],
        session: Any,
        security_profile: str,
    ) -> AsyncIterator[str]:
        """Streaming sem tool calling (para respostas longas sem execução)."""
        model = self._config.default_model
        await self._mm.ensure(model)
        messages = self._build_messages(system_prompt, user_message, session)

        async with httpx.AsyncClient(timeout=self._config.ollama_timeout_s) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "think": False,
                    "options": {"temperature": 0.7, "num_ctx": 8192},
                },
            ) as r:
                r.raise_for_status()
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
        history = getattr(session, "history", [])
        for turn in history[-20:]:
            messages.append({"role": turn.role, "content": turn.content})
        messages.append({"role": "user", "content": user_message})
        return messages
