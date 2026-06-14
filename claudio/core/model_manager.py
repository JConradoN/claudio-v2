from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from claudio.config import Config

log = logging.getLogger("claudio.model_manager")

_KEEP_ALIVE = "4h"   # valor explícito em TODAS as chamadas ao Ollama


def _normalize_model_name(name: str) -> str:
    """Remove sufixos de quantização para comparação — 'qwen3.5:27b-q4_K_M' == 'qwen3.5:27b'."""
    # Mantém apenas 'familia:tag_base' sem sufixos de quantização
    if "-q" in name.lower():
        name = name[:name.lower().index("-q")]
    return name.lower().strip()


class ModelUnavailableError(Exception):
    pass


class ModelManager:
    """
    State machine para gerenciar qual modelo está carregado na VRAM.
    asyncio.Lock garante que apenas uma operação de carga/descarga ocorre por vez.
    """

    def __init__(self, config: "Config") -> None:
        self._config = config
        self._base_url = config.ollama_url.rstrip("/")
        self._timeout = config.ollama_timeout_s
        self._lock = asyncio.Lock()
        self._current: str | None = None

    @property
    def current(self) -> str | None:
        return self._current

    async def probe(self) -> None:
        """Consulta /api/ps e descobre o estado atual do Ollama."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{self._base_url}/api/ps")
                r.raise_for_status()
                models = r.json().get("models", [])
                if models:
                    self._current = models[0]["name"]
                    log.info("model_manager: modelo já carregado: %s", self._current)
                else:
                    self._current = None
                    log.info("model_manager: nenhum modelo carregado")
        except Exception as exc:
            log.warning("model_manager: probe falhou: %s", exc)
            self._current = None

    async def ensure(self, model: str) -> None:
        """Garante que `model` está na VRAM. Verifica estado real no Ollama."""
        async with self._lock:
            # Consulta o que o Ollama tem de fato na VRAM agora
            actual = await self._get_loaded_model()

            if actual is not None and _normalize_model_name(actual) == _normalize_model_name(model):
                # Modelo já carregado — o keep_alive da chamada subsequente /api/chat renova o timer
                self._current = actual
                return

            # Tem outro modelo carregado → descarrega primeiro
            if actual is not None:
                log.info("model_manager: modelo diferente na VRAM (%s), descarregando", actual)
                await self._unload(actual)

            self._current = None
            await self._load(model)

    async def _get_loaded_model(self) -> str | None:
        """Consulta /api/ps e retorna o nome do modelo atualmente na VRAM."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self._base_url}/api/ps")
                r.raise_for_status()
                models = r.json().get("models", [])
                return models[0]["name"] if models else None
        except Exception:
            return self._current  # fallback para estado local se Ollama não responder

    async def unload_all(self) -> None:
        """Descarrega qualquer modelo carregado. Usado no shutdown."""
        async with self._lock:
            if self._current is not None:
                await self._unload(self._current)

    async def status(self) -> dict:
        """Estado atual do Ollama."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{self._base_url}/api/ps")
                r.raise_for_status()
                return {"loaded": r.json().get("models", []), "current": self._current}
        except Exception as exc:
            return {"error": str(exc), "current": self._current}

    async def _refresh_keep_alive(self, model: str) -> None:
        """Renova o keep_alive sem recarregar — evita expiry silencioso."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                await client.post(
                    f"{self._base_url}/api/generate",
                    json={"model": model, "prompt": "", "keep_alive": _KEEP_ALIVE},
                )
        except Exception as exc:
            log.warning("model_manager: refresh keep_alive falhou: %s", exc)

    async def _unload(self, model: str) -> None:
        log.info("model_manager: descarregando %s", model)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                await client.post(
                    f"{self._base_url}/api/generate",
                    json={"model": model, "keep_alive": 0},
                )
        except Exception as exc:
            log.warning("model_manager: falha ao descarregar %s: %s", model, exc)
        self._current = None

    async def _load(self, model: str) -> None:
        log.info("model_manager: carregando %s", model)
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    r = await client.post(
                        f"{self._base_url}/api/generate",
                        json={"model": model, "prompt": "", "keep_alive": _KEEP_ALIVE},
                    )
                    r.raise_for_status()
                self._current = model
                log.info("model_manager: %s carregado (keep_alive=%s)", model, _KEEP_ALIVE)
                return
            except Exception as exc:
                log.warning("model_manager: tentativa %d/3 falhou: %s", attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(5 * (2 ** attempt))
        raise ModelUnavailableError(f"Não foi possível carregar {model} após 3 tentativas")
