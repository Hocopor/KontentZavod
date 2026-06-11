"""
Предпросмотр голоса TTS.

Роут:
    GET /tts/preview/{voice}  — синтез короткой фразы и отдача mp3

Логика:
  - Допустимые голоса: svetlana, dmitry.
  - Результат кешируется в data/tts_preview/{voice}.mp3.
  - Повторный запрос — отдаётся кеш без синтеза.
  - FAKE_TTS=1 → тихий mp3 (через существующий механизм tts.py).
  - Неизвестный голос → 404.
"""
import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

from app.config import settings
from app.pipeline.tts import synthesize_scenes, TTSError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tts")

# Фраза для предпросмотра
_PREVIEW_TEXT = "Привет! Так будет звучать озвучка ваших видео."

# Допустимые голоса
_ALLOWED_VOICES = ("svetlana", "dmitry")


@router.get("/preview/{voice}")
async def tts_preview(voice: str):
    """
    Синтез короткой фразы для предпросмотра голоса.

    Args:
        voice: "svetlana" или "dmitry".

    Returns:
        FileResponse с audio/mpeg.
        404 если голос неизвестен.
    """
    if voice not in _ALLOWED_VOICES:
        return HTMLResponse(f"Неизвестный голос: {voice!r}", status_code=404)

    # Кеш-директория
    cache_dir = settings.data_dir_absolute / "tts_preview"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{voice}.mp3"

    # Если кеш уже есть — отдать без синтеза
    if cache_path.exists() and cache_path.stat().st_size > 0:
        logger.debug("tts_preview: кеш найден для голоса %s", voice)
        return FileResponse(
            path=str(cache_path),
            media_type="audio/mpeg",
            filename=f"preview_{voice}.mp3",
        )

    # Синтез (или заглушка при FAKE_TTS=1)
    try:
        logger.info("tts_preview: синтез для голоса %s", voice)
        synthesize_scenes(
            scene_texts=[_PREVIEW_TEXT],
            out_dir=cache_dir,
            voice=voice,
        )
        # synthesize_scenes создаёт voice_01.mp3 — переименовываем в кеш
        tmp_path = cache_dir / "voice_01.mp3"
        if tmp_path.exists():
            tmp_path.rename(cache_path)
    except TTSError as exc:
        logger.warning("tts_preview: ошибка синтеза %s: %s", voice, exc)
        return HTMLResponse(f"Ошибка синтеза: {exc}", status_code=502)

    if not cache_path.exists():
        return HTMLResponse("Синтез не дал результата", status_code=502)

    return FileResponse(
        path=str(cache_path),
        media_type="audio/mpeg",
        filename=f"preview_{voice}.mp3",
    )
