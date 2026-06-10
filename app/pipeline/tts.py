"""
Озвучка текста сцен через edge-tts (Microsoft Neural TTS).

Публичный API:
    synthesize_scenes(scene_texts, out_dir, voice="dmitry") -> list[SceneAudio]

Режим FAKE_TTS=1: без сети — тишина через ffmpeg + синтетические тайминги.
"""
import asyncio
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# ─── Константы ────────────────────────────────────────────────────────────────

_VOICE_MAP: dict[str, str] = {
    "dmitry":   "ru-RU-DmitryNeural",
    "svetlana": "ru-RU-SvetlanaNeural",
}

# edge-tts отдаёт тайминги в 100-наносекундных единицах
_TICKS_PER_SEC: float = 1e7

# Длительность одного слова в режиме FAKE_TTS (секунды)
_FAKE_WORD_DUR: float = 0.4
_FAKE_MIN_DUR:  float = 1.0


# ─── Исключения ───────────────────────────────────────────────────────────────


class TTSError(Exception):
    """Ошибка синтеза речи (сеть, таймаут, неизвестный голос)."""


# ─── Данные ───────────────────────────────────────────────────────────────────


@dataclass
class WordTiming:
    """Тайминг одного слова на глобальной шкале ролика (в секундах)."""

    word:  str
    start: float   # секунды от начала всего ролика
    end:   float   # секунды от начала всего ролика


@dataclass
class SceneAudio:
    """Результат озвучки одной сцены."""

    index:    int               # 0-based
    path:     Path              # путь к mp3-файлу
    duration: float             # длительность в секундах (из ffprobe)
    words:    list[WordTiming] = field(default_factory=list)


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _ffprobe_duration(mp3_path: Path) -> float:
    """Получить длительность аудиофайла через ffprobe."""
    cmd = [
        settings.FFPROBE_BIN,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(mp3_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw = result.stdout.strip()
        if result.returncode != 0 or not raw:
            raise TTSError(
                f"ffprobe вернул ошибку для {mp3_path}: {result.stderr}"
            )
        return float(raw)
    except subprocess.TimeoutExpired as exc:
        raise TTSError(f"ffprobe завис на {mp3_path}") from exc
    except ValueError as exc:
        raise TTSError(
            f"ffprobe вернул нечитаемую длительность для {mp3_path}: {raw!r}"
        ) from exc


def _ffmpeg_silence(out_path: Path, duration: float) -> None:
    """Сгенерировать файл тишины нужной длины через ffmpeg (для FAKE_TTS)."""
    cmd = [
        settings.FFMPEG_BIN,
        "-y",
        "-f", "lavfi",
        "-i", f"anullsrc=r=24000:cl=mono",
        "-t", str(duration),
        "-q:a", "9",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    if result.returncode != 0:
        raise TTSError(
            f"ffmpeg не смог создать тишину: {result.stderr.decode(errors='replace')}"
        )


# ─── Синтез одной сцены: реальный ────────────────────────────────────────────


async def _synthesize_one(
    text: str,
    voice_id: str,
    out_path: Path,
) -> list[tuple[str, float, float]]:
    """
    Синтезировать одну сцену через edge-tts.

    Returns:
        Список (слово, start_сек, end_сек) — тайминги относительно начала сцены.
    """
    import edge_tts  # noqa: PLC0415 — ленивый импорт: не нужен в FAKE_TTS

    words: list[tuple[str, float, float]] = []
    audio_chunks: list[bytes] = []
    last_word_offset: int = 0   # в тиках, для расчёта end у последнего слова

    communicate = edge_tts.Communicate(text, voice_id)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            offset   = chunk["offset"]          # начало слова, тики
            duration = chunk["duration"]         # длительность, тики
            word     = chunk["text"]
            start_s  = offset / _TICKS_PER_SEC
            end_s    = (offset + duration) / _TICKS_PER_SEC
            words.append((word, start_s, end_s))
            last_word_offset = offset + duration

    out_path.write_bytes(b"".join(audio_chunks))
    return words


def _synthesize_one_with_retry(
    text: str,
    voice_id: str,
    out_path: Path,
) -> list[tuple[str, float, float]]:
    """Обёртка с одним повтором при сетевой ошибке."""
    for attempt in range(2):
        try:
            return asyncio.run(_synthesize_one(text, voice_id, out_path))
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                logger.warning(
                    "edge-tts: ошибка на попытке 1, повторяю: %s", exc
                )
            else:
                raise TTSError(
                    f"edge-tts не смог синтезировать текст после 2 попыток: {exc}"
                ) from exc
    return []  # unreachable


# ─── Синтез одной сцены: заглушка ────────────────────────────────────────────


def _synthesize_fake(
    text: str,
    out_path: Path,
) -> list[tuple[str, float, float]]:
    """
    Заглушка (FAKE_TTS=1): тишина + синтетические тайминги.

    Тайминги: каждое слово по 0.4 с, стартуют с 0.0.
    Длительность аудио: 0.4 × N слов, минимум 1.0 с.
    """
    word_list = text.split()
    n = len(word_list)
    duration = max(_FAKE_MIN_DUR, _FAKE_WORD_DUR * n)
    _ffmpeg_silence(out_path, duration)

    timings: list[tuple[str, float, float]] = []
    for i, word in enumerate(word_list):
        start = i * _FAKE_WORD_DUR
        end   = start + _FAKE_WORD_DUR
        timings.append((word, start, end))
    return timings


# ─── Основная публичная функция ───────────────────────────────────────────────


def synthesize_scenes(
    scene_texts: list[str],
    out_dir: Path,
    voice: str = "dmitry",
) -> list[SceneAudio]:
    """
    Озвучить список сцен, вернуть SceneAudio с глобальными таймингами слов.

    Args:
        scene_texts: список текстов (по одному на сцену).
        out_dir:     директория для сохранения mp3-файлов (создаётся автоматически).
        voice:       "dmitry" (по умолчанию) | "svetlana".

    Returns:
        Список SceneAudio в том же порядке, что scene_texts.
        WordTiming.start / .end — секунды от начала ВСЕГО ролика (глобальная шкала).

    Raises:
        TTSError: неизвестный голос, сетевая ошибка после ретрая, ошибка ffmpeg/ffprobe.
    """
    if voice not in _VOICE_MAP:
        raise TTSError(
            f"Неизвестный голос: {voice!r}. Доступные: {list(_VOICE_MAP)}"
        )
    voice_id = _VOICE_MAP[voice]

    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[SceneAudio] = []
    global_offset: float = 0.0   # сумма длительностей предыдущих сцен

    for idx, text in enumerate(scene_texts):
        filename = f"voice_{idx + 1:02d}.mp3"
        out_path = out_dir / filename

        # Синтез или заглушка
        if settings.FAKE_TTS:
            raw_timings = _synthesize_fake(text, out_path)
        else:
            raw_timings = _synthesize_one_with_retry(text, voice_id, out_path)

        # Длительность — из ffprobe (точнее, чем последний WordBoundary)
        duration = _ffprobe_duration(out_path)

        # Переводим локальные тайминги в глобальные
        word_timings = [
            WordTiming(
                word=w,
                start=start + global_offset,
                end=end + global_offset,
            )
            for w, start, end in raw_timings
        ]

        results.append(
            SceneAudio(
                index=idx,
                path=out_path,
                duration=duration,
                words=word_timings,
            )
        )
        global_offset += duration

        logger.debug(
            "Сцена %d озвучена: %s, %.2f с, %d слов",
            idx, out_path.name, duration, len(word_timings),
        )

    return results
