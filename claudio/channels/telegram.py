from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import TYPE_CHECKING

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

if TYPE_CHECKING:
    from claudio.audit import AuditLog
    from claudio.audit.runs_db import RunsDB
    from claudio.config import Config
    from claudio.core.classifier import IntentClassifier
    from claudio.core.context import ContextBuilder
    from claudio.core.executor import Executor
    from claudio.core.session import SessionStore
    from claudio.cron.store import CronStore
    from claudio.memory.extraction import SessionExtractor
    from claudio.memory.retrieval import MemoryManager

log = logging.getLogger("claudio.telegram")

_MAX_MSG = 4096


def _split_message(text: str, limit: int = _MAX_MSG) -> list[str]:
    """Quebra texto em chunks respeitando linhas quando possível."""
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        # Tenta cortar na última quebra de linha dentro do limite
        cut = text[:limit].rfind("\n")
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


def _md_to_telegram(text: str) -> tuple[str, str | None]:
    """
    Converte markdown básico para Telegram.
    Retorna (texto_formatado, parse_mode | None).
    Usa MarkdownV2 quando o texto tem formatação; plain text caso contrário.
    """
    # Se o texto já está em MarkdownV2 (LLM gerou diretamente), passthrough
    if re.search(r"\\\(|\\\[|\\\.", text):
        return text, ParseMode.MARKDOWN_V2

    # Detecta se há formatação relevante (Markdown padrão)
    has_format = bool(re.search(r"\*\*|`|```|^#{1,3} ", text, re.M))
    if not has_format:
        return text, None

    # Converte para MarkdownV2
    # 1. Blocos de código (preservar antes de escapar)
    code_blocks: list[str] = []
    def _stash_code(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CODE{len(code_blocks)-1}\x00"

    text = re.sub(r"```[\s\S]*?```", _stash_code, text)
    text = re.sub(r"`[^`]+`", _stash_code, text)

    # 2. Escapa chars especiais do MarkdownV2 (exceto os que vamos usar)
    _SPECIAL = r"\_[]()~>#+=|{}.!"
    text = re.sub(r"([" + re.escape(_SPECIAL) + r"])", r"\\\1", text)

    # 3. Converte **bold** → *bold*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # 4. Converte _italic_ → _italic_ (já escapado corretamente)
    # (não precisam de conversão, mas o escape acima quebrou — restaura)
    text = re.sub(r"\\_(.+?)\\_", r"_\1_", text)

    # 5. Converte # Heading → *Heading*
    text = re.sub(r"^#{1,3}\s+(.+)$", r"*\1*", text, flags=re.M)

    # 6. Restaura code blocks
    def _restore_code(m: re.Match) -> str:
        idx = int(m.group(1))
        orig = code_blocks[idx]
        if orig.startswith("```"):
            lang_match = re.match(r"```(\w*)\n?([\s\S]*?)```", orig)
            if lang_match:
                code = lang_match.group(2).rstrip()
                return f"```\n{code}\n```"
        # inline code
        inner = orig[1:-1]
        # Escapa chars especiais dentro do inline code para MarkdownV2
        inner = inner.replace("\\", "\\\\").replace("`", "\\`")
        return f"`{inner}`"

    text = re.sub(r"\x00CODE(\d+)\x00", _restore_code, text)

    return text, ParseMode.MARKDOWN_V2


class TelegramChannel:
    def __init__(
        self,
        config: "Config",
        classifier: "IntentClassifier",
        context_builder: "ContextBuilder",
        executor: "Executor",
        sessions: "SessionStore",
        audit: "AuditLog",
        extractor: "SessionExtractor | None" = None,
        memory: "MemoryManager | None" = None,
        cron_store: "CronStore | None" = None,
        runs_db: "RunsDB | None" = None,
    ) -> None:
        self._config = config
        self._classifier = classifier
        self._ctx_builder = context_builder
        self._executor = executor
        self._sessions = sessions
        self._audit = audit
        self._extractor = extractor
        self._memory = memory
        self._cron = cron_store
        self._runs_db = runs_db
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
        self._app.add_handler(CommandHandler("memoria", self._cmd_memoria))
        self._app.add_handler(CommandHandler("lembrar", self._cmd_lembrar))
        self._app.add_handler(CommandHandler("tarefas", self._cmd_tarefas))
        self._app.add_handler(CommandHandler("cancelar", self._cmd_cancelar))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )
        self._app.add_handler(
            MessageHandler(filters.PHOTO, self._on_photo)
        )
        self._app.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self._on_voice)
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

        # Intercepta resposta de resolução de conflito pendente
        if hasattr(session, "_pending_conflicts") and session._pending_conflicts:
            choice = text.strip().upper()
            if choice in ("A", "B", "C"):
                await self._resolve_conflict_step(update, session, choice)
                return

        # Indicador de digitação
        await update.message.chat.send_action("typing")

        try:
            intent = await self._classifier.classify(text, session.history)
            system_prompt = await self._ctx_builder.build(intent, session, user_message=text)

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

        await self._send_response(update, response)

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        await update.message.reply_text(
            "Cláudio v2 ativo. Pode falar."
        )

    async def _cmd_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        key = f"telegram:{update.effective_chat.id}"
        session = self._sessions._sessions.get(key)
        # Extrai fatos da sessão atual antes de resetar
        if session and self._extractor and self._memory:
            import asyncio
            asyncio.create_task(self._extract_and_store(session))
        self._sessions.reset("telegram", str(update.effective_chat.id))
        await update.message.reply_text("Sessão reiniciada.")

    async def _extract_and_store(self, session) -> None:
        try:
            facts = await self._extractor.extract(session)
            if facts:
                await self._memory.add(facts, metadata={"source": "session_reset"})
                log.info("extraction: %d fatos gravados após reset", len(facts))
        except Exception as exc:
            log.warning("extraction pós-reset falhou: %s", exc)

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        mm_status = await self._executor._mm.status()
        model = mm_status.get("current") or "nenhum"
        await update.message.reply_text(
            f"Modelo: {model}\nVersão: {self._config.version}"
        )

    async def _cmd_memoria(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        if not self._memory:
            await update.message.reply_text("Sistema de memória não disponível.")
            return

        # Extrai subcomando: /memoria, /memoria check, /memoria salvar X, /memoria esquecer N
        args = (update.message.text or "").split(maxsplit=1)
        sub = args[1].strip().lower() if len(args) > 1 else ""

        if sub == "check":
            await self._memoria_check(update)
        elif sub.startswith("salvar "):
            fact = sub[len("salvar "):].strip()
            if fact:
                await self._memory.add_explicit(fact)
                await update.message.reply_text(f"Gravado: {fact}")
            else:
                await update.message.reply_text("Uso: /memoria salvar <fato>")
        elif sub.startswith("esquecer "):
            ref = sub[len("esquecer "):].strip()
            await self._memoria_esquecer(update, ref)
        else:
            await self._memoria_lista(update)

    async def _memoria_lista(self, update: Update) -> None:
        """Lista os fatos do mem0 com índice para referência."""
        facts = await self._memory.get_all(limit=20)
        if not facts:
            await update.message.reply_text("Memória vazia.")
            return

        session = self._sessions.get_or_create(
            "telegram", str(update.effective_chat.id)
        )
        session._last_memory_list = facts  # guarda para /memoria esquecer N

        lines = []
        for i, f in enumerate(facts, 1):
            source = f.metadata.get("source_type", f.source)
            lines.append(f"{i}. [{source}] {f.fact[:80]}")

        header = f"Memória ({len(facts)} fatos):\n"
        msg = header + "\n".join(lines)
        msg += "\n\n/memoria esquecer <N> — remove o fato N"
        msg += "\n/memoria salvar <texto> — adiciona fato explícito"
        msg += "\n/memoria check — detecta conflitos com kuzu"
        await update.message.reply_text(msg[:4096])

    async def _memoria_esquecer(self, update: Update, ref: str) -> None:
        """Remove fato por índice da última lista ou por texto parcial."""
        session = self._sessions.get_or_create(
            "telegram", str(update.effective_chat.id)
        )
        last_list = getattr(session, "_last_memory_list", [])

        if ref.isdigit():
            idx = int(ref) - 1
            if 0 <= idx < len(last_list):
                frag = last_list[idx]
                memory_id = frag.metadata.get("id", "")
                if memory_id:
                    ok = await self._memory.delete(memory_id)
                    msg = f"Removido: {frag.fact[:80]}" if ok else "Falha ao remover."
                else:
                    msg = "ID não encontrado. Rode /memoria primeiro."
            else:
                msg = f"Índice inválido (use 1–{len(last_list)})."
        else:
            msg = "Use /memoria esquecer <N> com o número da lista."
        await update.message.reply_text(msg)

    async def _memoria_check(self, update: Update) -> None:
        """Detecta conflitos entre mem0 e kuzu, inicia fluxo de resolução."""
        from claudio.memory.conflict import ConflictDetector

        await update.message.reply_text("Analisando inconsistências...")
        session = self._sessions.get_or_create(
            "telegram", str(update.effective_chat.id)
        )

        try:
            detector = ConflictDetector(self._config)
            conflicts = await asyncio.to_thread(detector.detect)
        except Exception as exc:
            await update.message.reply_text(f"Erro na análise: {exc}")
            return

        if not conflicts:
            await update.message.reply_text("Nenhum conflito detectado.")
            return

        session._pending_conflicts = conflicts
        session._conflict_detector = ConflictDetector(self._config)
        session._conflict_index = 0

        await update.message.reply_text(
            f"{len(conflicts)} conflito(s) encontrado(s). Resolvendo um a um..."
        )
        await self._show_current_conflict(update, session)

    async def _show_current_conflict(self, update: Update, session) -> None:
        idx = session._conflict_index
        conflicts = session._pending_conflicts
        if idx >= len(conflicts):
            session._pending_conflicts = []
            await update.message.reply_text("Todos os conflitos resolvidos.")
            return

        c = conflicts[idx]
        msg = (
            f"Conflito {idx + 1}/{len(conflicts)}\n\n"
            f"Mem0 diz:\n  {c.mem0_fact[:180]}\n\n"
            f"Kuzu ({c.kuzu_entity}) diz:\n  {c.kuzu_fact[:180]}\n\n"
            f"A — Manter mem0 (kuzu pode estar desatualizado)\n"
            f"B — Usar kuzu (remove do mem0)\n"
            f"C — Nenhum está correto (remove do mem0)\n\n"
            f"Responda A, B ou C."
        )
        await update.message.reply_text(msg)

    async def _resolve_conflict_step(self, update: Update, session, choice: str) -> None:
        idx = session._conflict_index
        conflicts = session._pending_conflicts
        detector = session._conflict_detector

        c = conflicts[idx]
        result = await asyncio.to_thread(detector.resolve, c, choice)
        await update.message.reply_text(result)

        session._conflict_index = idx + 1
        await self._show_current_conflict(update, session)

    async def _cmd_lembrar(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/lembrar <expressão temporal> <ação> — agenda um lembrete."""
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        if not self._cron:
            await update.message.reply_text("Cron não disponível.")
            return

        args = (update.message.text or "").split(maxsplit=1)
        text = args[1].strip() if len(args) > 1 else ""
        if not text:
            await update.message.reply_text(
                "Uso: /lembrar <quando> <o que>\n"
                "Ex: /lembrar todo dia às 9h verificar backup\n"
                "Ex: /lembrar daqui 30 minutos reunião"
            )
            return

        from claudio.cron.fast_parse import fast_parse
        result = fast_parse(text)
        if result is None:
            await update.message.reply_text(
                "Não entendi quando. Tente:\n"
                "- todo dia às 9h\n- toda segunda às 8h\n"
                "- daqui 30 minutos\n- amanhã às 14h\n- hoje às 18h"
            )
            return

        chat_id = update.effective_chat.id
        user_id = str(update.effective_user.id)
        prompt = result.prompt or text

        job = self._cron.add(
            type=result.type,
            chat_id=chat_id,
            user_id=user_id,
            prompt=prompt,
            cron_expr=result.cron_expr,
            run_at=result.run_at,
        )

        if result.type == "recurring":
            msg = f"Agendado (recorrente, cron: {result.cron_expr})\nAção: {prompt}"
        else:
            msg = f"Agendado para {result.run_at.strftime('%d/%m/%Y %H:%M')}\nAção: {prompt}"
        msg += f"\nID: {job.id[:8]}"
        await update.message.reply_text(msg)

    async def _cmd_tarefas(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/tarefas — lista lembretes ativos."""
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        if not self._cron:
            await update.message.reply_text("Cron não disponível.")
            return

        jobs = self._cron.list(chat_id=update.effective_chat.id)
        if not jobs:
            await update.message.reply_text("Nenhuma tarefa agendada.")
            return

        lines = []
        for j in jobs:
            when = j.cron_expr if j.type == "recurring" else (
                j.run_at.strftime("%d/%m %H:%M") if j.run_at else "?"
            )
            lines.append(f"[{j.id[:8]}] {j.prompt[:60]} — {when}")

        msg = f"Tarefas ativas ({len(jobs)}):\n" + "\n".join(lines)
        msg += "\n\n/cancelar <ID> — cancela a tarefa"
        await update.message.reply_text(msg[:4096])

    async def _cmd_cancelar(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/cancelar <ID> — cancela uma tarefa agendada."""
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return
        if not self._cron:
            await update.message.reply_text("Cron não disponível.")
            return

        args = (update.message.text or "").split()
        if len(args) < 2:
            await update.message.reply_text("Uso: /cancelar <ID>")
            return

        prefix = args[1].strip()
        # Suporta prefixo de 8 chars ou ID completo
        jobs = self._cron.list(chat_id=update.effective_chat.id)
        matches = [j for j in jobs if j.id.startswith(prefix)]

        if not matches:
            await update.message.reply_text(f"Tarefa '{prefix}' não encontrada.")
            return
        if len(matches) > 1:
            await update.message.reply_text(
                f"Ambíguo: {len(matches)} tarefas começam com '{prefix}'. Use mais caracteres."
            )
            return

        job = matches[0]
        ok = self._cron.delete(job.id)
        if ok:
            await update.message.reply_text(f"Cancelado: {job.prompt[:60]}")
        else:
            await update.message.reply_text("Falha ao cancelar.")

    async def _send_response(self, update: Update, text: str) -> None:
        """Envia resposta com formatação MarkdownV2 quando possível, plain text como fallback."""
        formatted, parse_mode = _md_to_telegram(text)
        for chunk in _split_message(formatted):
            try:
                await update.message.reply_text(chunk, parse_mode=parse_mode)
            except Exception:
                # Fallback para plain text se MarkdownV2 falhar
                try:
                    await update.message.reply_text(chunk)
                except Exception as exc:
                    log.error("send_response falhou: %s", exc)

    async def _on_photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Processa foto: baixa, converte para base64, passa ao pipeline com caption."""
        if not update.message or not update.effective_user:
            return
        if not self._is_allowed(update.effective_user.id):
            return

        chat_id = update.effective_chat.id
        thread_id = update.message.message_thread_id
        caption = update.message.caption or "Descreva esta imagem."

        await update.message.chat.send_action("typing")

        try:
            photo = update.message.photo[-1]  # maior resolução disponível
            photo_file = await photo.get_file()
            img_bytes = await photo_file.download_as_bytearray()
            img_b64 = base64.b64encode(img_bytes).decode()
        except Exception as exc:
            await update.message.reply_text(f"Erro ao processar imagem: {exc}")
            return

        session = self._sessions.get_or_create(
            channel="telegram",
            channel_id=str(chat_id),
            security_profile=self._config.default_security_profile,
            thread_id=thread_id,
        )

        try:
            from claudio.core.classifier import Intent
            intent = Intent(type="chat", tools=[])
            system_prompt = await self._ctx_builder.build(intent, session, user_message=caption)
            response = await self._executor.run(
                system_prompt=system_prompt,
                user_message=caption,
                tools=intent.tools,
                session=session,
                security_profile=session.security_profile,
                images=[img_b64],
            )
            session.add_turn("user", f"[imagem] {caption}")
            session.add_turn("assistant", response)
        except Exception as exc:
            log.exception("pipeline error (photo)")
            response = f"Erro ao processar imagem: {exc}"

        await self._send_response(update, response)

    async def _on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Processa mensagem de voz: transcreve via faster-whisper local, passa ao pipeline, responde com texto + áudio TTS."""
        if not update.message or not update.effective_user:
            return
        if not self._is_allowed(update.effective_user.id):
            return

        chat_id = update.effective_chat.id
        thread_id = update.message.message_thread_id

        await update.message.chat.send_action("typing")

        try:
            voice = update.message.voice or update.message.audio
            if not voice:
                await update.message.reply_text("Áudio não reconhecido.")
                return
            voice_file = await voice.get_file()
            audio_bytes = await voice_file.download_as_bytearray()
        except Exception as exc:
            await update.message.reply_text(f"Erro ao baixar áudio: {exc}")
            return

        from claudio.stt_tts import transcribe, synthesize
        transcript = await transcribe(bytes(audio_bytes))
        if not transcript:
            await update.message.reply_text("Não consegui transcrever o áudio.")
            return

        await update.message.reply_text(f"_{transcript[:200]}_", parse_mode="Markdown")

        session = self._sessions.get_or_create(
            channel="telegram",
            channel_id=str(chat_id),
            security_profile=self._config.default_security_profile,
            thread_id=thread_id,
        )

        try:
            intent = await self._classifier.classify(transcript, session.history)
            system_prompt = await self._ctx_builder.build(intent, session, user_message=transcript)
            response = await self._executor.run(
                system_prompt=system_prompt,
                user_message=transcript,
                tools=intent.tools,
                session=session,
                security_profile=session.security_profile,
            )
            session.add_turn("user", transcript)
            session.add_turn("assistant", response)
        except Exception as exc:
            log.exception("pipeline error (voice)")
            response = f"Erro ao processar áudio: {exc}"

        await self._send_response(update, response)

        # Resposta em áudio (TTS)
        try:
            audio_data = await synthesize(response)
            if audio_data:
                import io
                await update.message.reply_voice(
                    voice=io.BytesIO(audio_data),
                    filename="resposta.mp3",
                )
        except Exception as exc:
            log.warning("tts: falha ao enviar áudio: %s", exc)

    async def _cmd_debug(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not self._is_allowed(update.effective_user.id):
            return

        args = (update.message.text or "").split(maxsplit=2)
        sub = args[1].strip().lower() if len(args) > 1 else "last"
        ref = args[2].strip() if len(args) > 2 else ""
        chat_id = str(update.effective_chat.id)

        if sub == "last":
            await self._debug_last(update, chat_id)
        elif sub == "run":
            await self._debug_run(update, ref)
        elif sub == "errors":
            await self._debug_errors(update, chat_id)
        else:
            session = self._sessions.get_or_create("telegram", chat_id)
            await update.message.reply_text(
                f"Sessão: {session.id} | Turns: {len(session.history)}\n"
                f"/debug last — último run\n"
                f"/debug run <id> — run específico\n"
                f"/debug errors — últimos erros"
            )

    async def _debug_last(self, update: Update, chat_id: str) -> None:
        if not self._runs_db:
            await update.message.reply_text("runs_db não disponível.")
            return
        runs = self._runs_db.get_last(chat_id, limit=3)
        if not runs:
            await update.message.reply_text("Nenhum run registrado.")
            return
        lines = []
        for r in runs:
            dur = f"{r.duration_ms}ms" if r.duration_ms else "?"
            ts = r.started_at.strftime("%H:%M:%S")
            lines.append(
                f"[{r.run_id[:8]}] {r.status} | {r.model} | {dur} | tools={r.tool_calls_count} | {ts}"
            )
        await update.message.reply_text("Últimos runs:\n" + "\n".join(lines))

    async def _debug_run(self, update: Update, run_prefix: str) -> None:
        if not run_prefix:
            await update.message.reply_text("Uso: /debug run <run_id>")
            return
        if not self._runs_db:
            await update.message.reply_text("runs_db não disponível.")
            return
        r = self._runs_db.get_run(run_prefix)
        if not r:
            await update.message.reply_text(f"Run '{run_prefix}' não encontrado.")
            return
        dur = f"{r.duration_ms}ms" if r.duration_ms else "?"
        msg = (
            f"Run: {r.run_id}\n"
            f"Status: {r.status}\n"
            f"Modelo: {r.model}\n"
            f"Canal: {r.channel}\n"
            f"Duração: {dur}\n"
            f"Tool calls: {r.tool_calls_count}\n"
            f"Início: {r.started_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        if r.error:
            msg += f"Erro: {r.error[:200]}"
        await update.message.reply_text(msg)

    async def _debug_errors(self, update: Update, chat_id: str) -> None:
        if not self._runs_db:
            await update.message.reply_text("runs_db não disponível.")
            return
        runs = self._runs_db.get_errors(chat_id, limit=5)
        if not runs:
            await update.message.reply_text("Nenhum erro registrado.")
            return
        lines = []
        for r in runs:
            ts = r.started_at.strftime("%d/%m %H:%M")
            err = (r.error or "sem detalhes")[:60]
            lines.append(f"[{r.run_id[:8]}] {ts} — {err}")
        await update.message.reply_text("Últimos erros:\n" + "\n".join(lines))
