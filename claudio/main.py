from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

log = logging.getLogger("claudio.main")


async def run() -> None:
    # Import aqui para erro de config ser visível antes de tudo
    from claudio.audit import AuditLog
    from claudio.channels.telegram import TelegramChannel
    from claudio.config import Config
    from claudio.core.classifier import IntentClassifier
    from claudio.core.context import ContextBuilder
    from claudio.core.executor import Executor
    from claudio.core.model_manager import ModelManager
    from claudio.core.session import SessionStore

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

    # 3. ModelManager — probe estado atual do Ollama
    model_manager = ModelManager(config)
    await model_manager.probe()

    # 4. DI wiring
    classifier = IntentClassifier(config)
    ctx_builder = ContextBuilder(config)
    executor = Executor(config, model_manager, audit)
    sessions = SessionStore()

    # 5. Channels
    telegram = TelegramChannel(
        config=config,
        classifier=classifier,
        context_builder=ctx_builder,
        executor=executor,
        sessions=sessions,
        audit=audit,
    )

    # 6. Warmup
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

    # Systemd notify (se disponível)
    _sd_notify("READY=1")

    # 7. Start channels
    await telegram.start()

    log.info("Cláudio v2 iniciado")

    # Aguarda SIGTERM/SIGINT
    stop_event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)

    await stop_event.wait()

    # 8. Shutdown
    log.info("shutdown iniciado")
    audit.log("service.stop", {"reason": "signal"})

    _sd_notify("STOPPING=1")

    await telegram.stop()
    # Não descarregar o modelo no shutdown/restart — 27b-only, VRAM não precisa ser liberada.
    # Unload na reinicialização cria race condition com o novo processo (keep_alive: 0 vs 4h).
    audit.close()

    log.info("shutdown completo")


def _sd_notify(msg: str) -> None:
    """Envia notificação ao systemd via socket NOTIFY_SOCKET (best-effort)."""
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
