"""
Получение медиа-ассетов для вертикальных видео.

Поддерживаемые шаблоны:
  video_footage  — вертикальные видеофутажи (Pexels Videos → Pixabay Videos → картинка)
  video_slideshow — статичные картинки через Pollinations.ai (без ключа)

FAKE_ASSETS=1 → плейсхолдеры через ffmpeg lavfi без сети.
"""
import json
import logging
import random
import subprocess
from pathlib import Path
from urllib.parse import quote

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ─── Исключения ────────────────────────────────────────────────────────────────


class AssetsError(Exception):
    """Ошибка получения ассета для сцены."""


# ─── Константы ─────────────────────────────────────────────────────────────────

_PEXELS_VIDEO_URL = "https://api.pexels.com/videos/search"
_PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"
_POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"

_TIMEOUT = 60
_MAX_RETRIES = 1


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _assets_dir(content_id: int) -> Path:
    """Вернуть (и создать) рабочую директорию ассетов для content_id."""
    p = settings.data_dir_absolute / "media" / str(content_id) / "assets"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _scene_filename(idx: int, ext: str) -> str:
    """scene_01.mp4, scene_02.jpg и т.д."""
    return f"scene_{idx + 1:02d}.{ext}"


def _http_get(url: str, headers: dict | None = None, params: dict | None = None) -> httpx.Response:
    """GET с таймаутом и 1 повтором при сетевой ошибке."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return httpx.get(url, headers=headers or {}, params=params or {}, timeout=_TIMEOUT, follow_redirects=True)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if attempt >= _MAX_RETRIES:
                raise
            logger.warning("Повтор GET %s после ошибки: %s", url, exc)


def _download_stream(url: str, dest: Path, headers: dict | None = None) -> None:
    """Потоковое скачивание файла по URL в dest."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            with httpx.stream("GET", url, headers=headers or {}, timeout=_TIMEOUT, follow_redirects=True) as resp:
                resp.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)
            return
        except (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            if attempt >= _MAX_RETRIES:
                raise
            logger.warning("Повтор скачивания %s после ошибки: %s", url, exc)


# ─── Fake-режим (FAKE_ASSETS=1) ───────────────────────────────────────────────


def _fake_video(dest: Path) -> None:
    """Создать тестовый вертикальный mp4 через ffmpeg lavfi (testsrc2)."""
    cmd = [
        settings.FFMPEG_BIN,
        "-y",
        "-f", "lavfi",
        "-i", "testsrc2=size=1080x1920:rate=30",
        "-t", "2",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise AssetsError(f"ffmpeg не смог создать fake-видео: {result.stderr.decode(errors='replace')}")


def _fake_image(dest: Path) -> None:
    """Создать тестовую вертикальную картинку через ffmpeg lavfi (color)."""
    cmd = [
        settings.FFMPEG_BIN,
        "-y",
        "-f", "lavfi",
        "-i", "color=c=0x336699:size=1080x1920",
        "-frames:v", "1",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise AssetsError(f"ffmpeg не смог создать fake-картинку: {result.stderr.decode(errors='replace')}")


# ─── Pexels Videos ────────────────────────────────────────────────────────────


def _fetch_pexels_video(query: str, dest: Path) -> bool:
    """
    Поиск портретного видео на Pexels.
    Выбирает файл с height≥1280 и width<height, минимально достаточного размера.
    Возвращает True при успехе.
    """
    api_key = settings.PEXELS_API_KEY
    if not api_key:
        return False

    try:
        resp = _http_get(
            _PEXELS_VIDEO_URL,
            headers={"Authorization": api_key},
            params={"query": query, "orientation": "portrait", "per_page": 5},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Pexels API ошибка: %s", exc)
        return False

    videos = data.get("videos", [])
    if not videos:
        return False

    # Ищем подходящий video_file: portrait (height≥1280 и width<height)
    # Приоритет: HD (≈1080p) перед 4K — экономия диска
    for video in videos:
        video_files = video.get("video_files", [])
        # Фильтруем: height≥1280, width<height
        portrait_files = [
            vf for vf in video_files
            if vf.get("height", 0) >= 1280 and vf.get("width", 1) < vf.get("height", 0)
        ]
        if not portrait_files:
            continue

        # Выбираем файл с наименьшим разрешением среди подходящих (экономия CPU/диска)
        portrait_files.sort(key=lambda vf: vf.get("height", 0))
        best = portrait_files[0]
        url = best.get("link") or best.get("url")
        if not url:
            continue

        try:
            _download_stream(url, dest)
            logger.info("Pexels: скачан футаж '%s' → %s", query, dest.name)
            return True
        except Exception as exc:
            logger.warning("Pexels: ошибка скачивания: %s", exc)
            continue

    return False


# ─── Pixabay Videos ───────────────────────────────────────────────────────────


def _fetch_pixabay_video(query: str, dest: Path) -> bool:
    """
    Поиск видео на Pixabay.
    Варианты: medium → small (portrait не фильтруется API, берём medium/small).
    Возвращает True при успехе.
    """
    api_key = settings.PIXABAY_API_KEY
    if not api_key:
        return False

    try:
        resp = _http_get(
            _PIXABAY_VIDEO_URL,
            params={"key": api_key, "q": query, "per_page": 5, "video_type": "all"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Pixabay API ошибка: %s", exc)
        return False

    hits = data.get("hits", [])
    if not hits:
        return False

    for hit in hits:
        videos_dict = hit.get("videos", {})
        # Приоритет: medium → small (не 4K)
        for quality in ("medium", "small"):
            variant = videos_dict.get(quality, {})
            url = variant.get("url")
            if not url:
                continue
            try:
                _download_stream(url, dest)
                logger.info("Pixabay: скачан футаж '%s' (%s) → %s", query, quality, dest.name)
                return True
            except Exception as exc:
                logger.warning("Pixabay: ошибка скачивания (%s): %s", quality, exc)

    return False


# ─── Pollinations.ai (картинка без ключа) ─────────────────────────────────────


def _fetch_pollinations_image(keywords: list[str], dest: Path) -> bool:
    """
    Генерация вертикальной картинки через Pollinations.ai без ключа.
    Промпт: keywords + ", vertical photo, no text".
    Возвращает True при успехе.
    """
    prompt_text = ", ".join(keywords) + ", vertical photo, no text"
    url = _POLLINATIONS_URL.format(prompt=quote(prompt_text))

    try:
        _download_stream(
            url + "?width=1080&height=1920&nologo=true",
            dest,
        )
        logger.info("Pollinations: сгенерирована картинка → %s", dest.name)
        return True
    except Exception as exc:
        logger.warning("Pollinations: ошибка генерации: %s", exc)
        return False


# ─── Основная функция ─────────────────────────────────────────────────────────


def fetch_scene_assets(
    content_id: int,
    scenes: list[dict],
    template: str,
) -> list[Path]:
    """
    Получить по одному ассету на каждую сцену видео.

    Параметры:
        content_id — id контента в БД
        scenes     — список dict {"text": "...", "keywords": ["english", "words"]}
        template   — 'video_footage' или 'video_slideshow'

    Возвращает список Path в порядке сцен.
    Расширения: .mp4 для видео, .jpg для картинок.
    Поднимает AssetsError если ассет для сцены не удалось получить.

    Режим FAKE_ASSETS=1: создаёт валидные плейсхолдеры через ffmpeg lavfi.
    """
    assets_dir = _assets_dir(content_id)
    result: list[Path] = []

    for idx, scene in enumerate(scenes):
        keywords: list[str] = scene.get("keywords", [])
        query = " ".join(keywords) if keywords else "abstract background"

        # ── FAKE_ASSETS режим ────────────────────────────────────────────────
        if settings.FAKE_ASSETS:
            if template == "video_footage":
                dest = assets_dir / _scene_filename(idx, "mp4")
                _fake_video(dest)
            else:
                dest = assets_dir / _scene_filename(idx, "jpg")
                _fake_image(dest)
            result.append(dest)
            continue

        # ── video_footage: Pexels → Pixabay → картинка (Ken Burns fallback) ─
        if template == "video_footage":
            dest_mp4 = assets_dir / _scene_filename(idx, "mp4")

            if _fetch_pexels_video(query, dest_mp4):
                result.append(dest_mp4)
                continue

            if _fetch_pixabay_video(query, dest_mp4):
                result.append(dest_mp4)
                continue

            # Фоллбэк: картинка — рендер сделает Ken Burns
            logger.info(
                "Сцена %d: видеофутаж не найден, фоллбэк на картинку (Ken Burns)", idx + 1
            )
            dest_jpg = assets_dir / _scene_filename(idx, "jpg")
            if _fetch_pollinations_image(keywords, dest_jpg):
                result.append(dest_jpg)
                continue

            raise AssetsError(
                f"Не удалось получить ассет для сцены {idx + 1} "
                f"(query='{query}', template='{template}')"
            )

        # ── video_slideshow: только картинка Pollinations ────────────────────
        elif template == "video_slideshow":
            dest_jpg = assets_dir / _scene_filename(idx, "jpg")

            if _fetch_pollinations_image(keywords, dest_jpg):
                result.append(dest_jpg)
                continue

            raise AssetsError(
                f"Не удалось сгенерировать картинку для сцены {idx + 1} "
                f"(keywords={keywords})"
            )

        else:
            raise AssetsError(f"Неизвестный шаблон: '{template}'")

    return result


# ─── Музыкальная библиотека ────────────────────────────────────────────────────

_MOODS = ("energetic", "calm", "inspiring", "neutral")


def pick_music(mood: str) -> Path | None:
    """
    Выбрать случайный mp3 из библиотеки настроений.

    Порядок поиска:
      1. {DATA_DIR}/music/{mood}/ — папка настроения
      2. {DATA_DIR}/music/        — корень музыкальной библиотеки (не рекурсивно)
      3. None                     — mp3 нет совсем

    Параметры:
        mood — одно из: energetic, calm, inspiring, neutral
    """
    music_root = settings.data_dir_absolute / "music"

    # 1. Папка настроения
    mood_dir = music_root / mood
    if mood_dir.is_dir():
        mp3s = list(mood_dir.glob("*.mp3"))
        if mp3s:
            return random.choice(mp3s)

    # 2. Корень музыкальной библиотеки (не рекурсивно)
    if music_root.is_dir():
        mp3s = list(music_root.glob("*.mp3"))
        if mp3s:
            return random.choice(mp3s)

    return None
