from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claudio.config import Config
    from claudio.core.context import ContextBuilder
    from claudio.core.executor import Executor
    from claudio.core.session import SessionStore
    from claudio.cron.store import CronJob, CronStore

log = logging.getLogger("claudio.cron.scheduler")

_TICK_S = 30


def _cron_matches(expr: str, dt: datetime) -> bool:
    """Avalia expressão cron de 5 campos contra dt (minuto atual)."""
    try:
        parts = expr.split()
        if len(parts) != 5:
            return False
        minute_f, hour_f, dom_f, month_f, dow_f = parts

        def _match(field: str, value: int, min_v: int = 0, max_v: int = 59) -> bool:
            if field == "*":
                return True
            if field.startswith("*/"):
                step = int(field[2:])
                return value % step == 0
            if "," in field:
                return value in {int(x) for x in field.split(",")}
            if "-" in field:
                lo, hi = field.split("-", 1)
                return int(lo) <= value <= int(hi)
            return int(field) == value

        return (
            _match(minute_f, dt.minute, 0, 59)
            and _match(hour_f, dt.hour, 0, 23)
            and _match(dom_f, dt.day, 1, 31)
            and _match(month_f, dt.month, 1, 12)
            and _match(dow_f, dt.weekday() + 1 if dt.weekday() < 6 else 0, 0, 6)
        )
    except Exception:
        return False


class CronScheduler:
    def __init__(
        self,
        config: "Config",
        store: "CronStore",
        executor: "Executor",
        ctx_builder: "ContextBuilder",
        sessions: "SessionStore",
        send_message_fn: Any,  # async fn(chat_id, text)
    ) -> None:
        self._config = config
        self._store = store
        self._executor = executor
        self._ctx_builder = ctx_builder
        self._sessions = sessions
        self._send = send_message_fn
        self._task: asyncio.Task | None = None
        # guarda quando cada recurring job foi executado por último (job_id → minuto)
        self._last_run: dict[str, str] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="cron-scheduler")
        log.info("cron: scheduler iniciado (tick=%ds)", _TICK_S)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("cron: scheduler parado")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(_TICK_S)
            try:
                await self._tick()
            except Exception as exc:
                log.error("cron.tick erro: %s", exc)

    async def _tick(self) -> None:
        now = datetime.now()
        minute_key = now.strftime("%Y-%m-%dT%H:%M")

        for job in self._store.due_jobs():
            if job.type == "once":
                await self._dispatch(job, now)
            elif job.type == "recurring" and job.cron_expr:
                # Evita dupla execução no mesmo minuto
                if self._last_run.get(job.id) == minute_key:
                    continue
                if _cron_matches(job.cron_expr, now):
                    self._last_run[job.id] = minute_key
                    await self._dispatch(job, now)

    async def _dispatch(self, job: "CronJob", now: datetime) -> None:
        log.info("cron.dispatch: job %s → chat %d | %s", job.id[:8], job.chat_id, job.prompt[:60])
        t0 = time.monotonic()
        error: str | None = None

        try:
            from claudio.core.classifier import Intent
            session = self._sessions.get_or_create(
                channel="cron",
                channel_id=str(job.chat_id),
                security_profile=self._config.default_security_profile,
            )
            intent = Intent(type="chat", tools=[])
            system_prompt = await self._ctx_builder.build(intent, session, user_message=job.prompt)
            response = await self._executor.run(
                system_prompt=system_prompt,
                user_message=job.prompt,
                tools=intent.tools,
                session=session,
                security_profile=session.security_profile,
            )
            session.add_turn("user", f"[cron] {job.prompt}")
            session.add_turn("assistant", response)
            await self._send(job.chat_id, response)
        except Exception as exc:
            log.error("cron.dispatch: job %s falhou: %s", job.id[:8], exc)
            error = str(exc)
            try:
                await self._send(job.chat_id, f"Cron '{job.prompt[:50]}' falhou: {exc}")
            except Exception:
                pass

        duration_ms = int((time.monotonic() - t0) * 1000)
        self._store.record_run(
            job.id,
            status="ok" if error is None else "error",
            error=error,
            duration_ms=duration_ms,
        )
