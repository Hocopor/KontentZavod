"""
Предпросмотр голоса TTS.

Роут:
    GET /tts/preview/{voice}  — синтез короткой фразы и отдача mp3

Логика:
  - Допустимые голоса: svetlana, dmitry.
  - Результат кешируется в data/tts_preview/{voice}_{rate_slug}_{pitch_slug}.mp3.
  - Повторный запрос — отдаётся кеш без синтеза.
  - FAKE_TTS=1 → тихий mp3 (через существующий механизм tts.py).
  - Неизвестный голос → 404.
"""
import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

from app.config import settings
from app.pipeline.tts import synthesize_scenes, TTSError, _valid_rate, _valid_pitch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tts")

# Фраза для предпросмотра
_PREVIEW_TEXT = "Привет! Так будет звучать озвучка ваших видео."

# Допустимые голоса
_ALLOWED_VOICES = ("svetlana", "dmitry")


def _slug(s: str) -> str:
    """Сделать безопасный суффикс из строки rate/pitch для имени файла кеша."""
    return s.replace("+", "p").replace("-", "m").replace("%", "pct").replace("Hz", "hz")


@router.get("/preview/{voice}")
async def tts_preview(voice: str, rate: str = "+0%", pitch: str = "+0Hz"):
    """
    Синтез короткой фразы для предпросмотра голоса.

    Args:
        voice: "svetlana" или "dmitry".
        rate:  темп речи в формате "+N%" или "-N%" (опционально, дефолт "+0%").
        pitch: тон голоса в формате "+NHz" или "-NHz" (опционально, дефолт "+0Hz").

    Returns:
        FileResponse с audio/mpeg.
        404 если голос неизвестен.
    """
    if voice not in _ALLOWED_VOICES:
        return HTMLResponse(f"Неизвестный голос: {voice!r}", status_code=404)

    # Валидация rate/pitch (невалидные → дефолт)
    rate  = _valid_rate(rate)
    pitch = _valid_pitch(pitch)

    # Кеш-директория
    cache_dir = settings.data_dir_absolute / "tts_preview"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Имя кеш-файла включает rate/pitch — разные настройки дают разный кеш
    cache_path = cache_dir / f"{voice}_{_slug(rate)}_{_slug(pitch)}.mp3"

    # Если кеш уже есть — отдать без синтеза
    if cache_path.exists() and cache_path.stat().st_size > 0:
        logger.debug("tts_preview: кеш найден для голоса %s rate=%s pitch=%s", voice, rate, pitch)
        return FileResponse(
            path=str(cache_path),
            media_type="audio/mpeg",
            filename=f"preview_{voice}.mp3",
        )

    # Синтез (или заглушка при FAKE_TTS=1)
    try:
        logger.info("tts_preview: синтез для голоса %s rate=%s pitch=%s", voice, rate, pitch)
        synthesize_scenes(
            scene_texts=[_PREVIEW_TEXT],
            out_dir=cache_dir,
            voice=voice,
            rate=rate,
            pitch=pitch,
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
