"""
Получение медиа-ассетов для вертикальных видео.

Поддерживаемые шаблоны:
  video_footage  — вертикальные видеофутажи (Pexels Videos → Pixabay Videos → картинка)
  video_slideshow — статичные картинки (без Pollinations, он стал платным)

Картинки для постов и историй:
  fetch_image(keywords, dest) — единая цепочка:
    1. Pexels Photos API
    2. Pixabay Photos API
    3. Openverse (без ключа)
    4. Wikimedia Commons (без ключа)
    5. False

FAKE_ASSETS=1 → плейсхолдеры через ffmpeg lavfi без сети.

Прокси-фейловер (для РФ-серверов):
  _http_get и _download_stream сначала пробуют прямой запрос, при 402/403/429
  или сетевых ошибках — перебирают активные прокси из пула proxies.py (http и socks5).
  Ответ 402 от прокси = прокси сдох (исчерпан тариф провайдера) → следующий прокси.
"""
import json
import logging
import random
import subprocess
from pathlib import Path
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ─── Исключения ────────────────────────────────────────────────────────────────


class AssetsError(Exception):
    """Ошибка получения ассета для сцены."""


# ─── Константы ─────────────────────────────────────────────────────────────────

_PEXELS_VIDEO_URL = "https://api.pexels.com/videos/search"
_PEXELS_PHOTO_URL = "https://api.pexels.com/v1/search"
_PIXABAY_VIDEO_URL = "https://pixabay.com/api/videos/"
_PIXABAY_PHOTO_URL = "https://pixabay.com/api/"
_OPENVERSE_PHOTO_URL = "https://api.openverse.org/v1/images/"
_WIKIMEDIA_API_URL = "https://commons.wikimedia.org/w/api.php"

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


def _get_pool_proxies() -> list[str]:
    """
    Вернуть список URL активных прокси из пула (http и socks5).
    Graceful: возвращает [] при отсутствии таблицы или любой ошибке БД.
    socks5 поддерживается через httpx[socks] (socksio).
    """
    try:
        from app.db import get_db  # noqa: PLC0415
        from app.services.proxies import list_active_proxies  # noqa: PLC0415

        with get_db() as db:
            proxies = list_active_proxies(db)
        return [p["url"] for p in proxies]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Не удалось загрузить прокси для assets: %s", exc)
        return []


def _http_get(url: str, headers: dict | None = None, params: dict | None = None) -> httpx.Response:
    """
    GET с таймаутом, 1 повтором при сетевой ошибке и прокси-фейловером.

    Алгоритм:
      1. Прямой запрос (с 1 ретраем при сетевых ошибках).
      2. При сетевых ошибках (TransportError/TimeoutException) или HTTP 402/403/429
         — перебор прокси пула (http и socks5), по одной попытке.
         Ответ 402 через прокси = прокси сдох (исчерпан тариф) → следующий прокси.
      3. Если всё провалилось — raise последней ошибки.
    """
    _headers = headers or {}
    _params = params or {}
    last_exc: Exception | None = None

    # Попытка 1: напрямую (с 1 ретраем)
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = httpx.get(url, headers=_headers, params=_params, timeout=_TIMEOUT, follow_redirects=True)
            if resp.status_code not in (402, 403, 429):
                return resp
            # HTTP-блокировка — считаем ошибкой для фейловера
            last_exc = httpx.HTTPStatusError(
                f"HTTP {resp.status_code}",
                request=resp.request,
                response=resp,
            )
            logger.warning("GET %s → HTTP %s, пробую прокси…", url, resp.status_code)
            break  # ретраи не помогут при 402/403/429 — сразу к прокси
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
            if attempt >= _MAX_RETRIES:
                logger.warning("GET %s провалился (%s), пробую прокси…", url, exc)
                break
            logger.warning("Повтор GET %s после ошибки: %s", url, exc)

    # Попытки через прокси
    from app.services.proxies import mask_proxy_url  # noqa: PLC0415

    for proxy_url in _get_pool_proxies():
        try:
            with httpx.Client(proxy=proxy_url, timeout=_TIMEOUT) as client:
                resp = client.get(url, headers=_headers, params=_params, follow_redirects=True)
            # 402 через прокси — прокси сдох (исчерпан тариф провайдера)
            if resp.status_code == 402:
                logger.warning(
                    "GET через прокси %s → 402 (тариф прокси исчерпан), следующий прокси",
                    mask_proxy_url(proxy_url),
                )
                last_exc = httpx.HTTPStatusError(
                    "HTTP 402 via proxy",
                    request=resp.request,
                    response=resp,
                )
                continue
            masked = mask_proxy_url(proxy_url)
            logger.info("GET %s скачан через прокси %s", url, masked)
            return resp
        except Exception as exc:  # noqa: BLE001
            logger.warning("Прокси %s не помог для GET %s: %s", mask_proxy_url(proxy_url), url, exc)
            last_exc = exc

    if last_exc is not None:
        raise last_exc
    # Сюда не должны попасть, но на всякий случай
    raise AssetsError(f"Не удалось выполнить GET {url}")


def _download_stream(url: str, dest: Path, headers: dict | None = None) -> None:
    """
    Потоковое скачивание файла по URL в dest с прокси-фейловером.

    Алгоритм:
      1. Прямая попытка (с 1 ретраем при сетевых ошибках / HTTPStatusError).
      2. При сетевых ошибках или HTTPStatusError (вкл. 402/403/429)
         — перебор прокси пула (http и socks5), по одной попытке каждый.
         Ответ 402 через прокси = прокси сдох (исчерпан тариф) → следующий прокси.
      3. Если всё провалилось — raise последней ошибки.
    """
    _headers = headers or {}
    last_exc: Exception | None = None

    # Попытка 1: напрямую (с 1 ретраем)
    for attempt in range(_MAX_RETRIES + 1):
        try:
            with httpx.stream("GET", url, headers=_headers, timeout=_TIMEOUT, follow_redirects=True) as resp:
                resp.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)
            return
        except (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt >= _MAX_RETRIES:
                logger.warning("Скачивание %s провалилось (%s), пробую прокси…", url, exc)
                break
            logger.warning("Повтор скачивания %s после ошибки: %s", url, exc)

    # Попытки через прокси
    from app.services.proxies import mask_proxy_url  # noqa: PLC0415

    for proxy_url in _get_pool_proxies():
        try:
            proxy_ok = False
            with httpx.Client(proxy=proxy_url, timeout=_TIMEOUT) as client:
                with client.stream("GET", url, headers=_headers, follow_redirects=True) as resp:
                    # 402 через прокси = прокси сдох (исчерпан тариф провайдера)
                    if resp.status_code == 402:
                        logger.warning(
                            "Скачивание через прокси %s → 402 (тариф исчерпан), следующий прокси",
                            mask_proxy_url(proxy_url),
                        )
                        last_exc = httpx.HTTPStatusError(
                            "HTTP 402 via proxy",
                            request=resp.request,
                            response=resp,
                        )
                    else:
                        resp.raise_for_status()
                        with dest.open("wb") as f:
                            for chunk in resp.iter_bytes(chunk_size=65536):
                                f.write(chunk)
                        proxy_ok = True
            if proxy_ok:
                masked = mask_proxy_url(proxy_url)
                logger.info("Скачивание %s выполнено через прокси %s", url, masked)
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Прокси %s не помог для скачивания %s: %s",
                mask_proxy_url(proxy_url), url, exc,
            )
            last_exc = exc

    if last_exc is not None:
        raise last_exc
    raise AssetsError(f"Не удалось скачать {url}")


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


# ─── Pexels Photos ────────────────────────────────────────────────────────────


def _fetch_pexels_photo(query: str, dest: Path) -> bool:
    """
    Поиск вертикального фото на Pexels Photos API.

    Порядок предпочтения URL: src.portrait → src.large2x.
    Возвращает True при успехе.
    """
    api_key = settings.PEXELS_API_KEY
    if not api_key:
        logger.warning("_fetch_pexels_photo: PEXELS_API_KEY не задан — пропускаем")
        return False

    try:
        resp = _http_get(
            _PEXELS_PHOTO_URL,
            headers={"Authorization": api_key},
            params={"query": query, "orientation": "portrait", "per_page": 5},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Pexels Photos API ошибка: %s", exc)
        return False

    photos = data.get("photos", [])
    if not photos:
        logger.info("Pexels Photos: нет результатов по запросу '%s'", query)
        return False

    for photo in photos:
        src = photo.get("src") or {}
        url = src.get("portrait") or src.get("large2x")
        if not url:
            continue
        try:
            _download_stream(url, dest)
            logger.info("Pexels Photos: скачано фото '%s' → %s", query, dest.name)
            return True
        except Exception as exc:
            logger.warning("Pexels Photos: ошибка скачивания: %s", exc)
            continue

    return False


# ─── Pixabay Photos ───────────────────────────────────────────────────────────


def _fetch_pixabay_photo(query: str, dest: Path) -> bool:
    """
    Поиск вертикального фото на Pixabay Photos API.

    Использует largeImageURL из ответа API.
    Возвращает True при успехе.
    """
    api_key = settings.PIXABAY_API_KEY
    if not api_key:
        logger.warning("_fetch_pixabay_photo: PIXABAY_API_KEY не задан — пропускаем")
        return False

    try:
        resp = _http_get(
            _PIXABAY_PHOTO_URL,
            params={
                "key": api_key,
                "q": query,
                "orientation": "vertical",
                "image_type": "photo",
                "per_page": 5,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Pixabay Photos API ошибка: %s", exc)
        return False

    hits = data.get("hits", [])
    if not hits:
        logger.info("Pixabay Photos: нет результатов по запросу '%s'", query)
        return False

    for hit in hits:
        url = hit.get("largeImageURL")
        if not url:
            continue
        try:
            _download_stream(url, dest)
            logger.info("Pixabay Photos: скачано фото '%s' → %s", query, dest.name)
            return True
        except Exception as exc:
            logger.warning("Pixabay Photos: ошибка скачивания: %s", exc)
            continue

    return False


# ─── Openverse (без ключа) ────────────────────────────────────────────────────


def _fetch_openverse_photo(query: str, dest: Path) -> bool:
    """
    Поиск фото в Openverse (Creative Commons).

    GET https://api.openverse.org/v1/images/?q=query&page_size=10 — без ключа.
    Предпочитает вертикальные (height > width), фоллбэк — первый результат.
    Возвращает True при успехе.
    """
    try:
        resp = _http_get(
            _OPENVERSE_PHOTO_URL,
            params={"q": query, "page_size": 10},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Openverse API ошибка: %s", exc)
        return False

    results = data.get("results", [])
    if not results:
        logger.info("Openverse: нет результатов по запросу '%s'", query)
        return False

    # Предпочитаем вертикальные (height > width)
    vertical = [r for r in results if r.get("height", 0) > r.get("width", 0)]
    chosen = vertical[0] if vertical else results[0]
    url = chosen.get("url")
    if not url:
        logger.warning("Openverse: нет url в результате для '%s'", query)
        return False

    try:
        _download_stream(url, dest)
        logger.info("Openverse: скачано фото '%s' → %s", query, dest.name)
        return True
    except Exception as exc:
        logger.warning("Openverse: ошибка скачивания: %s", exc)
        return False


# ─── Wikimedia Commons (без ключа) ───────────────────────────────────────────

# Обязательный User-Agent для Wikimedia (403 без осмысленного UA)
_WIKIMEDIA_USER_AGENT = "KontentZavod/1.0 (https://github.com/Hocopor/KontentZavod)"


def _fetch_wikimedia_photo(query: str, dest: Path) -> bool:
    """
    Поиск фото в Wikimedia Commons через MediaWiki API.

    Использует generator=search с filetype:bitmap, prop=imageinfo, iiurlwidth=1080.
    ОБЯЗАТЕЛЕН заголовок User-Agent — Wikimedia отвечает 403 без него.
    Предпочитает вертикальные (height > width), качает thumburl (1080px).
    Возвращает True при успехе.
    """
    try:
        resp = _http_get(
            _WIKIMEDIA_API_URL,
            headers={"User-Agent": _WIKIMEDIA_USER_AGENT},
            params={
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": f"filetype:bitmap {query}",
                "gsrnamespace": "6",
                "gsrlimit": "10",
                "prop": "imageinfo",
                "iiprop": "url|size",
                "iiurlwidth": "1080",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Wikimedia API ошибка: %s", exc)
        return False

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        logger.info("Wikimedia: нет результатов по запросу '%s'", query)
        return False

    # Собираем imageinfo из всех страниц
    candidates = []
    for page in pages.values():
        for info in page.get("imageinfo", []):
            candidates.append(info)

    if not candidates:
        logger.info("Wikimedia: нет imageinfo для запроса '%s'", query)
        return False

    # Предпочитаем вертикальные
    vertical = [c for c in candidates if c.get("height", 0) > c.get("width", 0)]
    chosen = vertical[0] if vertical else candidates[0]

    url = chosen.get("thumburl") or chosen.get("url")
    if not url:
        logger.warning("Wikimedia: нет URL изображения для запроса '%s'", query)
        return False

    try:
        _download_stream(url, dest, headers={"User-Agent": _WIKIMEDIA_USER_AGENT})
        logger.info("Wikimedia: скачано фото '%s' → %s", query, dest.name)
        return True
    except Exception as exc:
        logger.warning("Wikimedia: ошибка скачивания: %s", exc)
        return False


# ─── Единая цепочка получения картинки ───────────────────────────────────────


def fetch_image(keywords: list[str], dest: Path) -> bool:
    """
    Получить картинку для поста или истории по ключевым словам.

    Цепочка попыток:
      1. Pexels Photos (portrait, бесплатно).
      2. Pixabay Photos (vertical, бесплатно).
      3. Openverse (Creative Commons, без ключа).
      4. Wikimedia Commons (без ключа).
      5. False — картинка недоступна.

    При FAKE_ASSETS=1 — создаёт плейсхолдер через ffmpeg и возвращает True.

    Параметры:
        keywords — список англоязычных слов для поиска/промпта.
        dest     — путь к целевому файлу (.jpg).

    Возвращает True при успехе, False если ни один источник не сработал.
    """
    # FAKE-режим
    if settings.FAKE_ASSETS:
        _fake_image(dest)
        return True

    query = " ".join(keywords) if keywords else "abstract background"

    # 1. Pexels Photos
    if _fetch_pexels_photo(query, dest):
        logger.info("fetch_image: картинка получена через Pexels Photos (query='%s')", query)
        return True

    # 2. Pixabay Photos
    if _fetch_pixabay_photo(query, dest):
        logger.info("fetch_image: картинка получена через Pixabay Photos (query='%s')", query)
        return True

    # 3. Openverse (Creative Commons, без ключа)
    if _fetch_openverse_photo(query, dest):
        logger.info("fetch_image: картинка получена через Openverse (query='%s')", query)
        return True

    # 4. Wikimedia Commons (без ключа)
    if _fetch_wikimedia_photo(query, dest):
        logger.info("fetch_image: картинка получена через Wikimedia Commons (query='%s')", query)
        return True

    logger.warning(
        "fetch_image: все источники исчерпаны для query='%s' → картинка недоступна", query
    )
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
            if fetch_image(keywords, dest_jpg):
                result.append(dest_jpg)
                continue

            raise AssetsError(
                f"Не удалось получить ассет для сцены {idx + 1} "
                f"(query='{query}', template='{template}')"
            )

        # ── video_slideshow: картинка через единую цепочку ──────────────────
        elif template == "video_slideshow":
            dest_jpg = assets_dir / _scene_filename(idx, "jpg")

            if fetch_image(keywords, dest_jpg):
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
