from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claudio.config import Config

_log_level_map = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


class RunLog:
    """Arquivo rotativo de logs operacionais (30d, 50MB por arquivo)."""

    def __init__(self, config: "Config") -> None:
        self._config = config
        logs_dir = config.expand(config.logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)

        log_path = logs_dir / "claudio.log"
        level_name = os.environ.get("CLAUDIO_LOG_LEVEL", "INFO").lower()
        level = _log_level_map.get(level_name, logging.INFO)

        self._logger = logging.getLogger("claudio.runlog")
        self._logger.setLevel(level)

        if not self._logger.handlers:
            handler = RotatingFileHandler(
                log_path,
                maxBytes=config.runlog_max_bytes,
                backupCount=config.log_retention_days,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            ))
            self._logger.addHandler(handler)

            # stdout para desenvolvimento/journald
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            console.setLevel(level)
            self._logger.addHandler(console)

    def log(self, event: str, data: dict, level: str = "info") -> None:
        lvl = _log_level_map.get(level.lower(), logging.INFO)
        payload = json.dumps({"event": event, **data}, ensure_ascii=False, default=str)
        self._logger.log(lvl, payload)

    def close(self) -> None:
        for handler in self._logger.handlers[:]:
            handler.close()
            self._logger.removeHandler(handler)
