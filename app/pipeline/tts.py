"""
Озвучка текста сцен через edge-tts (Microsoft Neural TTS).

Публичный API:
    synthesize_scenes(scene_texts, out_dir, voice="dmitry") -> list[SceneAudio]

Режим FAKE_TTS=1: без сети — тишина через ffmpeg + синтетические тайминги.
"""
import asyncio
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# ─── Константы ────────────────────────────────────────────────────────────────

_VOICE_MAP: dict[str, str] = {
    "dmitry":   "ru-RU-DmitryNeural",
    "svetlana": "ru-RU-SvetlanaNeural",
}

# Дефолтные параметры темпа и тона edge-tts
_DEFAULT_RATE  = "+0%"
_DEFAULT_PITCH = "+0Hz"

# Регулярные выражения для валидации rate/pitch
_RATE_RE  = re.compile(r"^[+-]\d{1,3}%$")
_PITCH_RE = re.compile(r"^[+-]\d{1,3}Hz$")


def _valid_rate(rate: str) -> str:
    """Валидировать строку темпа. Невалидное значение → _DEFAULT_RATE."""
    return rate if isinstance(rate, str) and _RATE_RE.match(rate) else _DEFAULT_RATE


def _valid_pitch(pitch: str) -> str:
    """Валидировать строку тона. Невалидное значение → _DEFAULT_PITCH."""
    return pitch if isinstance(pitch, str) and _PITCH_RE.match(pitch) else _DEFAULT_PITCH


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
    proxy: Optional[str] = None,
    rate: str = _DEFAULT_RATE,
    pitch: str = _DEFAULT_PITCH,
) -> list[tuple[str, float, float]]:
    """
    Синтезировать одну сцену через edge-tts.

    Args:
        proxy: опциональный HTTP-прокси (http://user:pass@host:port).
               В edge-tts >= 7.2 передаётся напрямую в Communicate(proxy=...).
        rate:  темп речи в формате "+N%" или "-N%" (например, "+10%", "-10%").
        pitch: тон голоса в формате "+NHz" или "-NHz" (например, "+8Hz", "-8Hz").

    Returns:
        Список (слово, start_сек, end_сек) — тайминги относительно начала сцены.
    """
    import edge_tts  # noqa: PLC0415 — ленивый импорт: не нужен в FAKE_TTS

    words: list[tuple[str, float, float]] = []
    audio_chunks: list[bytes] = []

    # boundary='WordBoundary' обязателен с edge-tts >= 7.2
    # (в 7.2 умолчание изменилось на SentenceBoundary).
    communicate = edge_tts.Communicate(
        text,
        voice_id,
        boundary="WordBoundary",
        proxy=proxy,
        rate=rate,
        pitch=pitch,
    )
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

    out_path.write_bytes(b"".join(audio_chunks))
    return words


def _get_http_proxies() -> list[dict]:
    """
    Возвращает активные HTTP-прокси из БД (socks5 пропускаются намеренно).

    edge-tts использует aiohttp, который не поддерживает socks5-прокси,
    поэтому socks5 здесь не используется — только http/https.
    При отсутствии таблицы proxies — возвращает [].
    """
    try:
        from app.db import get_db  # noqa: PLC0415
        from app.services.proxies import list_active_proxies  # noqa: PLC0415

        with get_db() as db:
            all_proxies = list_active_proxies(db)
        return [p for p in all_proxies if not p["url"].lower().startswith("socks5://")]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Не удалось загрузить прокси из БД: %s", exc)
        return []


def _synthesize_one_with_retry(
    text: str,
    voice_id: str,
    out_path: Path,
    rate: str = _DEFAULT_RATE,
    pitch: str = _DEFAULT_PITCH,
) -> list[tuple[str, float, float]]:
    """
    Синтез с фейловером через прокси при 403.

    Алгоритм:
        1. Попытка без прокси.
        2. При любой ошибке — последовательно пробуем активные HTTP-прокси из БД.
        3. Если все попытки провалились — TTSError с подсказкой по прокси.
    """
    # Попытка 1: напрямую
    try:
        return asyncio.run(_synthesize_one(text, voice_id, out_path, proxy=None, rate=rate, pitch=pitch))
    except Exception as exc:  # noqa: BLE001
        logger.warning("edge-tts: прямая попытка провалилась (%s), пробую прокси…", exc)
        last_exc: Exception = exc

    # Попытки через прокси
    for proxy in _get_http_proxies():
        proxy_url = proxy["url"]
        try:
            result = asyncio.run(_synthesize_one(text, voice_id, out_path, proxy=proxy_url, rate=rate, pitch=pitch))
            logger.info("edge-tts: синтез успешен через прокси %s", proxy_url)
            return result
        except Exception as exc2:  # noqa: BLE001
            logger.warning("edge-tts: прокси %s тоже не помог: %s", proxy_url, exc2)
            last_exc = exc2

    raise TTSError(
        f"edge-tts не смог синтезировать текст: {last_exc} — "
        "edge-tts заблокирован для IP сервера — добавьте рабочий HTTP-прокси на /proxies"
    ) from last_exc


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


# ─── Пунктуация субтитров ────────────────────────────────────────────────────


def _reattach_punctuation(
    text: str,
    raw_timings: list[tuple[str, float, float]],
) -> list[tuple[str, float, float]]:
    """
    Переналожить оригинальную пунктуацию на тайминги слов.

    edge-tts отдаёт слова без знаков препинания. Если число whitespace-токенов
    исходного текста совпадает с числом таймингов — заменяем слово каждого тайминга
    на соответствующий оригинальный токен (с пунктуацией, кавычками, регистром).
    При расхождении длин — возвращаем тайминги как есть (graceful).
    """
    tokens = text.split()
    if len(tokens) == len(raw_timings):
        return [(tokens[i], s, e) for i, (_w, s, e) in enumerate(raw_timings)]
    return raw_timings


# ─── Основная публичная функция ───────────────────────────────────────────────


def synthesize_scenes(
    scene_texts: list[str],
    out_dir: Path,
    voice: str = "dmitry",
    rate: str = _DEFAULT_RATE,
    pitch: str = _DEFAULT_PITCH,
) -> list[SceneAudio]:
    """
    Озвучить список сцен, вернуть SceneAudio с глобальными таймингами слов.

    Args:
        scene_texts: список текстов (по одному на сцену).
        out_dir:     директория для сохранения mp3-файлов (создаётся автоматически).
        voice:       "dmitry" (по умолчанию) | "svetlana".
        rate:        темп речи в формате "+N%" или "-N%" (например, "+10%", "-10%").
                     По умолчанию "+0%" (обычный темп). Невалидные значения
                     заменяются дефолтом автоматически.
        pitch:       тон голоса в формате "+NHz" или "-NHz" (например, "+8Hz", "-8Hz").
                     По умолчанию "+0Hz" (обычный тон). Невалидные значения
                     заменяются дефолтом автоматически.

    Returns:
        Список SceneAudio в том же порядке, что scene_texts.
        WordTiming.start / .end — секунды от начала ВСЕГО ролика (глобальная шкала).

    Raises:
        TTSError: неизвестный голос, сетевая ошибка после ретрая, ошибка ffmpeg/ffprobe.
    """
    # Нормализация: невалидные значения заменяются дефолтами
    rate  = _valid_rate(rate)
    pitch = _valid_pitch(pitch)

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
            raw_timings = _synthesize_one_with_retry(text, voice_id, out_path, rate=rate, pitch=pitch)

        # Переналожить оригинальную пунктуацию (edge-tts отдаёт слова без знаков)
        raw_timings = _reattach_punctuation(text, raw_timings)

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
