from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from claudio.config import Config
    from claudio.core.context import ContextBuilder
    from claudio.core.executor import Executor
    from claudio.core.session import SessionStore

log = logging.getLogger("claudio.mcp")

mcp = FastMCP("claudio")

# Referências globais injetadas por McpChannel.setup()
_executor: "Executor | None" = None
_ctx_builder: "ContextBuilder | None" = None
_sessions: "SessionStore | None" = None
_config: "Config | None" = None


@mcp.tool()
async def ask_claudio(
    prompt: str,
    context: str = "",
    session_key: str = "mcp",
) -> str:
    """Envia um pedido ao Cláudio e retorna a resposta completa."""
    assert _executor and _ctx_builder and _sessions and _config
    full_prompt = f"{context}\n\n{prompt}".strip() if context else prompt
    session = _sessions.get_or_create(
        channel="mcp",
        channel_id=session_key,
        security_profile=_config.default_security_profile,
    )
    from claudio.core.classifier import Intent
    intent = Intent(type="chat", tools=[])
    system_prompt = await _ctx_builder.build(intent, session, user_message=full_prompt)
    response = await _executor.run(
        system_prompt=system_prompt,
        user_message=full_prompt,
        tools=intent.tools,
        session=session,
        security_profile=session.security_profile,
    )
    session.add_turn("user", full_prompt)
    session.add_turn("assistant", response)
    return response


@mcp.tool()
async def run_agent(
    agent_id: str,
    input: str,
    timeout: int = 900,
) -> str:
    """Delega uma tarefa diretamente a um agente AgentForge. Retorna o resultado."""
    import asyncio
    import os
    from pathlib import Path

    assert _config
    agentforge_path = str(Path(_config.agentforge_path).expanduser())
    agents_dir = str(Path(_config.agentforge_agents_dir).expanduser())

    cmd = [
        "python3", "-m", "agentforge.cli.main",
        "run",
        "--agent-dir", f"{agents_dir}/{agent_id}",
        "--input", input,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=agentforge_path,
            env={**os.environ, "PYTHONPATH": f"{agentforge_path}/src"},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:500]
            return f"Agente {agent_id} falhou (rc={proc.returncode}): {err}"
        return stdout.decode(errors="replace")
    except asyncio.TimeoutError:
        return f"Timeout após {timeout}s"
    except Exception as exc:
        return f"Erro ao executar agente {agent_id}: {exc}"


@mcp.tool()
async def claudio_status() -> dict:
    """Retorna o status atual do Cláudio (modelo, sessões ativas)."""
    assert _config and _sessions
    return {
        "status": "ok",
        "model": _config.default_model,
        "version": _config.version,
        "active_sessions": len(_sessions._sessions),
    }


class McpChannel:
    """Monta o MCP SSE server como sub-app do FastAPI (sem porta extra)."""

    def __init__(
        self,
        config: "Config",
        ctx_builder: "ContextBuilder",
        executor: "Executor",
        sessions: "SessionStore",
    ) -> None:
        global _executor, _ctx_builder, _sessions, _config
        _executor = executor
        _ctx_builder = ctx_builder
        _sessions = sessions
        _config = config

    def sse_app(self):
        """Retorna a sub-app SSE para montar em /mcp no FastAPI."""
        return mcp.sse_app()

    async def start(self) -> None:
        log.info("mcp: tools registradas (montado em /mcp via ChatAPI)")

    async def stop(self) -> None:
        log.info("mcp: encerrado")
