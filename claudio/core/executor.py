from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, AsyncIterator, Any

import httpx

from claudio.core.model_manager import _KEEP_ALIVE
from claudio.core.tools import TOOL_SCHEMAS, run_bash

if TYPE_CHECKING:
    from claudio.audit import AuditLog
    from claudio.audit.runs_db import RunsDB
    from claudio.config import Config
    from claudio.core.model_manager import ModelManager

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
        runs_db: "RunsDB | None" = None,
    ) -> None:
        self._config = config
        self._mm = model_manager
        self._audit = audit
        self._runs_db = runs_db
        self._base_url = config.ollama_url.rstrip("/")

    async def run(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[str],
        session: Any,
        security_profile: str,
        images: list[str] | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        model = self._config.default_model
        started = time.monotonic()
        channel = getattr(session, "channel", "unknown")
        channel_id = getattr(session, "channel_id", "0")
        session_id = str(getattr(session, "id", "0"))
        thread_id = str(getattr(session, "thread_id", None)) if getattr(session, "thread_id", None) else None

        await self._mm.ensure(model)

        messages = self._build_messages(system_prompt, user_message, session, images=images)
        use_tools = bool(tools)

        if self._runs_db:
            self._runs_db.start_run(
                run_id=run_id,
                session_id=session_id,
                channel=channel,
                chat_id=channel_id,
                thread_id=thread_id,
                user_id=str(getattr(session, "user_id", "")),
                model=model,
            )

        self._audit.log("executor.run.start", {
            "run_id": run_id,
            "model": model,
            "msg_preview": user_message[:100],
            "session_id": session_id,
            "tools": tools,
        })

        tool_calls_count = 0
        try:
            response_text, tool_calls_count = await self._chat_with_tools(
                model, messages, use_tools, security_profile, run_id
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._audit.log("executor.run.error", {
                "run_id": run_id, "error": str(exc)
            }, level="error")
            if self._runs_db:
                self._runs_db.finish_run(
                    run_id,
                    status="failed",
                    duration_ms=duration_ms,
                    error=str(exc)[:500],
                )
            raise

        duration_ms = int((time.monotonic() - started) * 1000)
        self._audit.log("executor.run.done", {
            "run_id": run_id,
            "duration_ms": duration_ms,
            "output_len": len(response_text),
        })
        if self._runs_db:
            self._runs_db.finish_run(
                run_id,
                status="completed",
                duration_ms=duration_ms,
                tool_calls_count=tool_calls_count,
            )

        return response_text

    async def _chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        use_tools: bool,
        security_profile: str,
        run_id: str,
    ) -> tuple[str, int]:
        """Loop: chama o LLM → executa tool calls → repete até resposta final.
        Retorna (resposta, total_tool_calls)."""
        total_tool_calls = 0
        for _round in range(_MAX_TOOL_ROUNDS):
            data = await self._call_ollama(model, messages, use_tools)
            msg = data.get("message", {})
            tool_calls = msg.get("tool_calls", [])

            if not tool_calls:
                return msg.get("content", ""), total_tool_calls

            messages.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                args = fn.get("arguments", {})
                total_tool_calls += 1

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

                messages.append({"role": "tool", "content": result})

        log.warning("executor: atingiu limite de %d rounds de tool calls", _MAX_TOOL_ROUNDS)
        data = await self._call_ollama(model, messages, use_tools=False)
        return data.get("message", {}).get("content", ""), total_tool_calls

    async def _execute_tool(self, tool_name: str, args: dict, security_profile: str) -> str:
        from claudio.security.profiles import check_tool_allowed, is_destructive
        if not check_tool_allowed(tool_name, security_profile):
            return f"[BLOQUEADO] Tool '{tool_name}' não permitida no perfil '{security_profile}'"
        if tool_name == "run_bash":
            command = args.get("command", "")
            if not command:
                return "[ERRO] Comando vazio"
            if is_destructive(command) and security_profile != "privileged":
                return f"[BLOQUEADO] Comando destrutivo requer perfil 'privileged': {command[:80]}"
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
            "keep_alive": _KEEP_ALIVE,
            "options": {"temperature": 0.7, "num_ctx": 16384},
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
                    "keep_alive": _KEEP_ALIVE,
                    "options": {"temperature": 0.7, "num_ctx": 16384},
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

    def _build_messages(
        self,
        system_prompt: str,
        user_message: str,
        session: Any,
        images: list[str] | None = None,
    ) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        history = getattr(session, "history", [])
        for turn in history[-20:]:
            messages.append({"role": turn.role, "content": turn.content})
        user_msg: dict = {"role": "user", "content": user_message}
        if images:
            user_msg["images"] = images
        messages.append(user_msg)
        return messages
