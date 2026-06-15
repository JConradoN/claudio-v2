"""
STT/TTS local.

STT: faster-whisper large-v3, CPU int8 (não ocupa VRAM — qwen3.5:27b permanece carregado).
TTS: edge_tts pt-BR-FranciscaNeural (requer internet, zero custo, zero VRAM).

Modelo whisper é carregado na primeira chamada e mantido em memória (lazy singleton).
Transcrição é bloqueante — roda via asyncio.to_thread().
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import threading
from typing import TYPE_CHECKING

log = logging.getLogger("claudio.stt_tts")

_TTS_VOICE = "pt-BR-FranciscaNeural"
_WHISPER_MODEL_SIZE = "large-v3"
_WHISPER_DEVICE = "cpu"
_WHISPER_COMPUTE = "int8"

_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            log.info("stt: carregando whisper %s (cpu int8)…", _WHISPER_MODEL_SIZE)
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel(
                _WHISPER_MODEL_SIZE,
                device=_WHISPER_DEVICE,
                compute_type=_WHISPER_COMPUTE,
            )
            log.info("stt: whisper pronto")
    return _whisper_model


def _do_transcribe(audio_bytes: bytes) -> str:
    """Bloqueia a thread — chamar via asyncio.to_thread()."""
    import tempfile, os
    model = _get_whisper()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name
    try:
        segments, _info = model.transcribe(
            tmp_path,
            language="pt",
            beam_size=5,
            vad_filter=True,
        )
        return "".join(s.text for s in segments).strip()
    finally:
        os.unlink(tmp_path)


async def transcribe(audio_bytes: bytes) -> str | None:
    """Transcreve áudio para texto usando faster-whisper local (CPU int8)."""
    try:
        text = await asyncio.to_thread(_do_transcribe, audio_bytes)
        return text or None
    except Exception as exc:
        log.warning("stt: transcrição falhou: %s", exc)
        return None


def _strip_markdown(text: str) -> str:
    """Remove marcações básicas de markdown para TTS soar mais natural."""
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,2}([^_]+)_{1,2}", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


async def synthesize(text: str, max_chars: int = 800) -> bytes | None:
    """
    Sintetiza texto para MP3 usando edge_tts (pt-BR-FranciscaNeural).

    Limita a max_chars para evitar áudios excessivamente longos.
    Retorna bytes MP3 ou None em caso de erro.
    """
    import edge_tts

    clean = _strip_markdown(text)
    if not clean:
        return None
    if len(clean) > max_chars:
        clean = clean[:max_chars].rsplit(" ", 1)[0] + "…"

    try:
        buf = io.BytesIO()
        communicate = edge_tts.Communicate(clean, _TTS_VOICE)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        data = buf.getvalue()
        return data if data else None
    except Exception as exc:
        log.warning("tts: síntese falhou: %s", exc)
        return None
