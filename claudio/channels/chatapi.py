from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from claudio.channels.mcp_server import McpChannel
    from claudio.config import Config
    from claudio.core.context import ContextBuilder
    from claudio.core.executor import Executor
    from claudio.core.session import SessionStore

log = logging.getLogger("claudio.chatapi")


class ChatRequest(BaseModel):
    text: str
    session_key: str = ""
    images: list[dict] = []


class ChatApiChannel:
    def __init__(
        self,
        config: "Config",
        ctx_builder: "ContextBuilder",
        executor: "Executor",
        sessions: "SessionStore",
        mcp_channel: "McpChannel | None" = None,
    ) -> None:
        self._config = config
        self._ctx_builder = ctx_builder
        self._executor = executor
        self._sessions = sessions
        self._mcp_channel = mcp_channel
        self._app = self._build_app()
        self._server_task: asyncio.Task | None = None

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Cláudio Chat API", version=self._config.version)

        if self._mcp_channel:
            app.mount("/mcp", self._mcp_channel.sse_app())
            log.info("chatapi: MCP montado em /mcp")

        @app.post("/api/chat")
        async def chat(req: ChatRequest, request: Request) -> JSONResponse:
            if not req.text.strip():
                return JSONResponse({"error": "text is required"}, status_code=400)

            session_key = req.session_key or str(uuid.uuid4())
            run_id = str(uuid.uuid4())
            t0 = time.monotonic()

            session = self._sessions.get_or_create(
                channel="http",
                channel_id=session_key,
                security_profile=self._config.default_security_profile,
            )

            try:
                from claudio.core.classifier import Intent
                intent = Intent(type="chat", tools=[])
                system_prompt = await self._ctx_builder.build(
                    intent, session, user_message=req.text
                )
                response = await asyncio.wait_for(
                    self._executor.run(
                        system_prompt=system_prompt,
                        user_message=req.text,
                        tools=intent.tools,
                        session=session,
                        security_profile=session.security_profile,
                    ),
                    timeout=self._config.ollama_timeout_s,
                )
                session.add_turn("user", req.text)
                session.add_turn("assistant", response)
                latency_ms = int((time.monotonic() - t0) * 1000)
                return JSONResponse({
                    "response": response,
                    "latency_ms": latency_ms,
                    "chat_id": hash(session_key) & 0x7FFFFFFF,
                    "run_id": run_id,
                })
            except asyncio.TimeoutError:
                return JSONResponse({"error": "timeout"}, status_code=408)
            except Exception as exc:
                log.exception("chatapi.chat erro")
                return JSONResponse({"error": f"pipeline error: {exc}"}, status_code=500)

        @app.post("/api/chat/stream")
        async def chat_stream(req: ChatRequest) -> StreamingResponse:
            if not req.text.strip():
                return StreamingResponse(
                    _sse_error("text is required"), media_type="text/event-stream"
                )

            session_key = req.session_key or str(uuid.uuid4())
            run_id = str(uuid.uuid4())
            t0 = time.monotonic()

            session = self._sessions.get_or_create(
                channel="http",
                channel_id=session_key,
                security_profile=self._config.default_security_profile,
            )

            async def generate() -> AsyncGenerator[str, None]:
                try:
                    from claudio.core.classifier import Intent
                    import json
                    intent = Intent(type="chat", tools=[])
                    system_prompt = await self._ctx_builder.build(
                        intent, session, user_message=req.text
                    )
                    # Executor não suporta streaming nativo — retorna tudo de uma vez
                    # e simula SSE em chunk único
                    response = await asyncio.wait_for(
                        self._executor.run(
                            system_prompt=system_prompt,
                            user_message=req.text,
                            tools=intent.tools,
                            session=session,
                            security_profile=session.security_profile,
                        ),
                        timeout=self._config.ollama_timeout_s,
                    )
                    session.add_turn("user", req.text)
                    session.add_turn("assistant", response)
                    yield f"data: {json.dumps({'chunk': response})}\n\n"
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    yield f"data: {json.dumps({'done': True, 'run_id': run_id, 'latency_ms': latency_ms})}\n\n"
                except asyncio.TimeoutError:
                    import json
                    yield f"data: {json.dumps({'error': 'timeout'})}\n\n"
                except Exception as exc:
                    import json
                    yield f"data: {json.dumps({'error': str(exc)})}\n\n"

            return StreamingResponse(generate(), media_type="text/event-stream")

        @app.get("/api/health")
        async def health() -> JSONResponse:
            return JSONResponse({
                "status": "ok",
                "model": self._config.default_model,
                "version": self._config.version,
            })

        return app

    async def start(self) -> None:
        import uvicorn
        server_config = uvicorn.Config(
            self._app,
            host=self._config.chatapi_host,
            port=self._config.chatapi_port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(server_config)
        self._server_task = asyncio.create_task(server.serve(), name="chatapi")
        log.info(
            "chatapi: iniciado em %s:%d",
            self._config.chatapi_host,
            self._config.chatapi_port,
        )

    async def stop(self) -> None:
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
        log.info("chatapi: parado")


async def _sse_error(msg: str) -> AsyncGenerator[str, None]:
    import json
    yield f"data: {json.dumps({'error': msg})}\n\n"
