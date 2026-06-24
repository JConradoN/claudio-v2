from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, AsyncIterator, Any

import httpx

from claudio.core.model_manager import _KEEP_ALIVE
from claudio.core.tools import TOOL_SCHEMAS, run_bash, read_link

if TYPE_CHECKING:
    from claudio.audit import AuditLog
    from claudio.audit.runs_db import RunsDB
    from claudio.config import Config
    from claudio.core.model_manager import ModelManager

log = logging.getLogger("claudio.executor")

_MAX_TOOL_ROUNDS = 8   # evita loop infinito de tool calls

# Regex para parsear tool calls XML emitidos pelo Qwen3.6 sem thinking:
# <tool_call>\n<function=name>\n<parameter=p>v</parameter>\n</function>\n</tool_call>
_XML_TOOL_RE = re.compile(
    r"<tool_call>\s*<function=([^>]+)>(.*?)</function>\s*</tool_call>",
    re.DOTALL,
)
_XML_PARAM_RE = re.compile(r"<parameter=([^>]+)>(.*?)</parameter>", re.DOTALL)


def _parse_xml_tool_calls(content: str) -> tuple[list[dict], str]:
    """Extrai tool calls em formato XML do content e retorna (tool_calls, content_limpo)."""
    tool_calls = []
    for i, m in enumerate(_XML_TOOL_RE.finditer(content)):
        fn_name = m.group(1).strip()
        fn_body = m.group(2)
        args = {p.group(1).strip(): p.group(2).strip() for p in _XML_PARAM_RE.finditer(fn_body)}
        tool_calls.append({
            "id": f"xml_{i}",
            "type": "function",
            "function": {"name": fn_name, "arguments": json.dumps(args)},
        })
    clean = _XML_TOOL_RE.sub("", content).strip()
    if tool_calls:
        log.debug("_parse_xml_tool_calls: %d tool calls extraídos do content XML", len(tool_calls))
    return tool_calls, clean


class Executor:
    """
    Envia mensagens ao LLM via /api/chat (Ollama) ou /v1/chat/completions (OpenAI-compat).
    Seleciona o provider em runtime via config.use_llamacpp.
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
        self._llamacpp_url = config.llamacpp_url.rstrip("/")

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

        if not self._config.use_llamacpp:
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
        call_fn = self._call_openai_compat if self._config.use_llamacpp else self._call_ollama
        try:
            response_text, tool_calls_count = await self._chat_with_tools(
                model, messages, use_tools, security_profile, run_id, call_fn
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
        call_fn,
    ) -> tuple[str, int]:
        """Loop: chama o LLM → executa tool calls → repete até resposta final.
        Retorna (resposta, total_tool_calls). call_fn é _call_ollama ou _call_openai_compat."""
        total_tool_calls = 0
        for _round in range(_MAX_TOOL_ROUNDS):
            data = await call_fn(model, messages, use_tools)
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
                # OpenAI-compat entrega arguments como string JSON; normaliza para dict
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
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

                # Ollama usa role "tool"; OpenAI-compat precisa de tool_call_id
                tool_msg: dict = {"role": "tool", "content": result}
                if "id" in tc:
                    tool_msg["tool_call_id"] = tc["id"]
                messages.append(tool_msg)

        log.warning("executor: atingiu limite de %d rounds de tool calls", _MAX_TOOL_ROUNDS)
        messages.append({
            "role": "user",
            "content": f"Você já executou {total_tool_calls} ferramentas. Escreva sua resposta final agora com base nos resultados obtidos. Não chame mais ferramentas.",
        })
        data = await call_fn(model, messages, use_tools=False)
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
        if tool_name == "read_link":
            url = args.get("url", "")
            if not url:
                return "[ERRO] Parâmetro 'url' obrigatório"
            return read_link(url)
        if tool_name == "list_agents":
            from claudio.core.tools import list_agents
            return list_agents()
        if tool_name == "save_memory":
            from claudio.core.tools import save_memory
            fact = args.get("fact", "")
            key = args.get("key")
            if not fact:
                return "[ERRO] Parâmetro 'fact' obrigatório"
            return await asyncio.to_thread(save_memory, fact, key)
        if tool_name == "run_agent":
            from claudio.core.tools import run_agent
            agent_id = args.get("agent_id", "")
            input_text = args.get("input", "")
            if not agent_id or not input_text:
                return "[ERRO] Parâmetros 'agent_id' e 'input' são obrigatórios"
            return run_agent(agent_id, input_text)
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

    async def _call_openai_compat(
        self, model: str, messages: list[dict], use_tools: bool
    ) -> dict:
        """Chama /v1/chat/completions (llama.cpp / TurboQuant) e normaliza para o
        formato interno usado pelo executor (mesmo layout do Ollama /api/chat)."""
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": 0.7,
        }
        budget = self._config.llamacpp_thinking_budget
        # Tool calls precisam de thinking para emitir JSON estruturado corretamente
        effective_budget = budget if budget > 0 else (600 if use_tools else 0)
        if effective_budget > 0:
            payload["chat_template_kwargs"] = {"enable_thinking": True}
            payload["max_tokens"] = effective_budget + 8192
        else:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            payload["max_tokens"] = 8192
        if use_tools:
            payload["tools"] = TOOL_SCHEMAS

        async with httpx.AsyncClient(timeout=self._config.ollama_timeout_s) as client:
            r = await client.post(
                f"{self._llamacpp_url}/v1/chat/completions", json=payload
            )
            r.raise_for_status()
            raw = r.json()

        # Normaliza OpenAI → formato interno (igual ao Ollama)
        choice = raw.get("choices", [{}])[0]
        oai_msg = choice.get("message", {})
        tool_calls = oai_msg.get("tool_calls") or []
        content = oai_msg.get("content") or ""

        log.info("_call_openai_compat: finish_reason=%s use_tools=%s tool_calls=%d content_preview=%r",
                 choice.get("finish_reason"), use_tools, len(tool_calls), content[:120])

        # Fallback: Qwen3.6 sem thinking às vezes emite tool calls como XML no content
        # em vez de JSON estruturado. Parseia e extrai se tool_calls estiver vazio.
        if not tool_calls and use_tools and "<tool_call>" in content:
            tool_calls, content = _parse_xml_tool_calls(content)

        return {"message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}

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
        messages = self._build_messages(system_prompt, user_message, session)

        if self._config.use_llamacpp:
            async for chunk in self._stream_openai_compat(model, messages):
                yield chunk
        else:
            await self._mm.ensure(model)
            async for chunk in self._stream_ollama(model, messages):
                yield chunk

    async def _stream_ollama(self, model: str, messages: list[dict]) -> AsyncIterator[str]:
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

    async def _stream_openai_compat(self, model: str, messages: list[dict]) -> AsyncIterator[str]:
        budget = self._config.llamacpp_thinking_budget
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": 0.7,
            "max_tokens": (budget + 8192) if budget > 0 else 8192,
        }
        if budget > 0:
            payload["chat_template_kwargs"] = {"enable_thinking": True}
        else:
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        async with httpx.AsyncClient(timeout=self._config.ollama_timeout_s) as client:
            async with client.stream(
                "POST",
                f"{self._llamacpp_url}/v1/chat/completions",
                json=payload,
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line or line == "data: [DONE]":
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    try:
                        chunk = json.loads(line)
                        content = chunk.get("choices", [{}])[0].get("delta", {}).get("content") or ""
                        if content:
                            yield content
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
