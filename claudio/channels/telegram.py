from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

if TYPE_CHECKING:
    from claudio.audit import AuditLog
    from claudio.config import Config
    from claudio.core.classifier import IntentClassifier
    from claudio.core.context import ContextBuilder
    from claudio.core.executor import Executor
    from claudio.core.session import SessionStore

log = logging.getLogger("claudio.telegram")

_MAX_MSG = 4096  # limite do Telegram


def _split_message(text: str, limit: int = _MAX_MSG) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        parts.append(text[:limit])
        text = text[limit:]
    return parts


def _escape_md2(text: str) -> str:
    """Escapa caracteres especiais do MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(r"([" + re.escape(special) + r"])", r"\\\1", text)


class TelegramChannel:
    def __init__(
        self,
        config: "Config",
        classifier: "IntentClassifier",
        context_builder: "ContextBuilder",
        executor: "Executor",
        sessions: "SessionStore",
        audit: "AuditLog",
    ) -> None:
        self._config = config
        self._classifier = classifier
        self._ctx_builder = context_builder
        self._executor = executor
        self._sessions = sessions
        self._audit = audit
        self._allowed_users = set(config.telegram_allowed_user_ids)
        self._app: Application | None = None

    def _is_allowed(self, user_id: int) -> bool:
        return user_id in self._allowed_users

    async def start(self) -> None:
        self._app = (
            Application.builder()
            .token(self._config.telegram_bot_token)
            .build()
        )
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("reset", self._cmd_reset))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("debug", self._cmd_debug))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram: polling iniciado")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            log.info("telegram: parado")

    async def _on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return

        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        thread_id = update.message.message_thread_id
        text = update.message.text or ""

        if not self._is_allowed(user_id):
            log.warning("telegram: user_id %d não autorizado", user_id)
            return

        self._audit.log("telegram.message", {
            "user_id": user_id,
            "chat_id": chat_id,
            "text_preview": text[:100],
        })

        session = self._sessions.get_or_create(
            channel="telegram",
            channel_id=str(chat_id),
            security_profile=self._config.default_security_profile,
            thread_id=thread_id,
        )

        # Indicador de digitação
        await update.message.chat.send_action("typing")

        try:
            intent = await self._classifier.classify(text, session.history)
            system_prompt = await self._ctx_builder.build(intent, session)

            # Log do tamanho do system prompt para verificar < 600 tokens
            token_est = len(system_prompt) // 4
            self._audit.log("pipeline.context", {
                "token_estimate": token_est,
                "intent": intent.type,
            })

            response = await self._executor.run(
                system_prompt=system_prompt,
                user_message=text,
                tools=intent.tools,
                session=session,
                security_profile=session.security_profile,
            )

            session.add_turn("user", text)
            session.add_turn("assistant", response)

        except Exception as exc:
            log.exception("pipeline error")
            self._audit.log("pipeline.error", {"error": str(exc)}, level="error")
            response = f"Erro interno: {exc}"

        # Envia resposta em chunks se necessário
        for chunk in _split_message(response):
            await update.message.reply_text(chunk)

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        await update.message.reply_text(
            "Cláudio v2 ativo. Pode falar."
        )

    async def _cmd_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        self._sessions.reset("telegram", str(update.effective_chat.id))
        await update.message.reply_text("Sessão reiniciada.")

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        mm_status = await self._executor._mm.status()
        model = mm_status.get("current") or "nenhum"
        await update.message.reply_text(
            f"Modelo: {model}\nVersão: {self._config.version}"
        )

    async def _cmd_debug(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        # Fase 1: stub básico. Implementação completa na Fase 4.
        session = self._sessions.get_or_create(
            "telegram", str(update.effective_chat.id)
        )
        await update.message.reply_text(
            f"Sessão: {session.id}\nTurns: {len(session.history)}\nProjeto: {session.project or 'nenhum'}"
        )
