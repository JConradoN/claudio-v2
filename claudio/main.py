from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

log = logging.getLogger("claudio.main")


async def run() -> None:
    from claudio.audit import AuditLog
    from claudio.audit.runs_db import RunsDB
    from claudio.channels.chatapi import ChatApiChannel
    from claudio.channels.mcp_server import McpChannel
    from claudio.channels.telegram import TelegramChannel
    from claudio.config import Config
    from claudio.core.classifier import IntentClassifier
    from claudio.core.context import ContextBuilder
    from claudio.core.executor import Executor
    from claudio.core.model_manager import ModelManager
    from claudio.core.session import SessionStore
    from claudio.cron.scheduler import CronScheduler
    from claudio.cron.store import CronStore
    from claudio.memory.extraction import SessionExtractor
    from claudio.memory.retrieval import MemoryManager

    # 1. Config
    try:
        config = Config.load()
        config.validate()
    except Exception as exc:
        print(f"ERRO: configuração inválida — {exc}", file=sys.stderr)
        sys.exit(1)

    config.ensure_dirs()

    # 2. Audit
    audit = AuditLog(config)

    # 3. ModelManager
    model_manager = ModelManager(config)
    await model_manager.probe()

    # 4. DI wiring
    classifier = IntentClassifier(config)
    memory = MemoryManager(config)
    ctx_builder = ContextBuilder(config, memory=memory)
    extractor = SessionExtractor(config)
    runs_db = RunsDB(config)
    executor = Executor(config, model_manager, audit, runs_db=runs_db)
    sessions = SessionStore()
    cron_store = CronStore(config)

    # 5. Channels
    telegram = TelegramChannel(
        config=config,
        classifier=classifier,
        context_builder=ctx_builder,
        executor=executor,
        sessions=sessions,
        audit=audit,
        extractor=extractor,
        memory=memory,
        cron_store=cron_store,
        runs_db=runs_db,
    )

    mcp_channel = McpChannel(
        config=config,
        ctx_builder=ctx_builder,
        executor=executor,
        sessions=sessions,
    )

    chatapi = ChatApiChannel(
        config=config,
        ctx_builder=ctx_builder,
        executor=executor,
        sessions=sessions,
        mcp_channel=mcp_channel,
    )

    # 6. Cron scheduler (precisa de send_message via Telegram)
    async def _send_telegram(chat_id: int, text: str) -> None:
        if telegram._app:
            bot = telegram._app.bot
            # Quebra mensagens longas
            for chunk in [text[i:i+4096] for i in range(0, len(text), 4096)]:
                await bot.send_message(chat_id=chat_id, text=chunk)

    scheduler = CronScheduler(
        config=config,
        store=cron_store,
        executor=executor,
        ctx_builder=ctx_builder,
        sessions=sessions,
        send_message_fn=_send_telegram,
    )

    # 7. Warmup
    if config.model_warmup_on_startup:
        log.info("warmup: carregando %s", config.default_model)
        try:
            await model_manager.ensure(config.default_model)
        except Exception as exc:
            log.warning("warmup falhou: %s (continuando)", exc)

    audit.log("service.start", {
        "version": config.version,
        "model": config.default_model,
    })

    _sd_notify("READY=1")

    # 8. Start channels + scheduler
    await telegram.start()
    await chatapi.start()
    await mcp_channel.start()
    scheduler.start()

    log.info("Cláudio v2 iniciado (telegram + chatapi:%d + mcp)", config.chatapi_port)

    stop_event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    await stop_event.wait()

    # 9. Shutdown
    log.info("shutdown iniciado")
    audit.log("service.stop", {"reason": "signal"})
    _sd_notify("STOPPING=1")

    await scheduler.stop()
    await chatapi.stop()
    await mcp_channel.stop()
    await telegram.stop()
    audit.close()

    log.info("shutdown completo")


def _sd_notify(msg: str) -> None:
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return
    import socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(notify_socket)
            sock.sendall(msg.encode())
    except Exception:
        pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
