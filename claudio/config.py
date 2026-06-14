from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # Telegram
    telegram_bot_token: str
    telegram_allowed_user_ids: list[int]
    telegram_allowed_group_ids: list[int] = field(default_factory=list)

    # Ollama
    ollama_url: str = "http://localhost:11434"
    default_model: str = "qwen3.5:27b"
    ollama_timeout_s: int = 900
    model_warmup_on_startup: bool = True

    # HTTP API
    chatapi_port: int = 18790
    chatapi_host: str = "127.0.0.1"

    # Memória
    mem0_collection: str = "claudio"
    embed_model: str = "nomic-embed-text"
    memory_extraction_min_turns: int = 3

    # Caminhos
    config_dir: str = "~/.claudio"
    logs_dir: str = "~/.claudio/logs"
    cron_db: str = "~/.claudio/cron.db"
    runs_db: str = "~/.claudio/runs.db"
    agent_mesh_db: str = "~/.agent-mesh/state.db"

    # Segurança
    default_security_profile: str = "execute"

    # Logging
    log_retention_days: int = 30
    runlog_max_bytes: int = 50 * 1024 * 1024

    # AgentForge
    agentforge_path: str = "~/repos/estudo/agents-framework"
    agentforge_agents_dir: str = "~/repos/estudo/agents-framework/agents"

    # Versão
    version: str = "2.0.0"

    @classmethod
    def load(cls) -> "Config":
        # Env vars têm precedência sobre config.json
        env_token = os.environ.get("CLAUDIO_TELEGRAM_TOKEN")
        env_users = os.environ.get("CLAUDIO_ALLOWED_USERS")

        path = Path("~/.claudio/config.json").expanduser()

        data: dict = {}
        if path.exists():
            data = json.loads(path.read_text())

        # Override com env vars
        if env_token:
            data["telegram_bot_token"] = env_token
        if env_users:
            data["telegram_allowed_user_ids"] = [
                int(u.strip()) for u in env_users.split(",") if u.strip()
            ]

        # Override modelo
        env_model = os.environ.get("CLAUDIO_MODEL")
        if env_model:
            data["default_model"] = env_model

        env_ollama = os.environ.get("CLAUDIO_OLLAMA_URL")
        if env_ollama:
            data["ollama_url"] = env_ollama

        # Só passa campos reconhecidos pelo dataclass
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}

        return cls(**filtered)

    def validate(self) -> None:
        if not self.telegram_bot_token:
            raise ValueError("telegram_bot_token é obrigatório")
        if not self.telegram_allowed_user_ids:
            raise ValueError("telegram_allowed_user_ids não pode ser vazio")
        if self.default_security_profile not in ("chat", "read", "execute", "privileged"):
            raise ValueError(f"Perfil de segurança inválido: {self.default_security_profile}")
        if self.ollama_timeout_s < 60:
            raise ValueError("ollama_timeout_s deve ser >= 60")

    def expand(self, path: str) -> Path:
        return Path(path).expanduser()

    def ensure_dirs(self) -> None:
        self.expand(self.config_dir).mkdir(parents=True, exist_ok=True)
        self.expand(self.logs_dir).mkdir(parents=True, exist_ok=True)
