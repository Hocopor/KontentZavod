"""
Ежедневный сборщик метрик опубликованного контента.

Поддерживаемые платформы:
  - vk   — wall.getById (пост) или video.get (видео) через VK API v5.199
  - youtube — videos?part=statistics через YouTube Data API v3 (OAuth Bearer)

Telegram-метрики (Telethon) НЕ реализованы — отложено до реальных ключей.
  Для TG нужны api_id/api_hash (новая зависимость telethon); пока TG-метрики
  вводятся вручную через дашборд.

Запускается из APScheduler (джоб добавляет оркестратор в scheduler.py).

Пример:
    from app.analytics.collector import collect_metrics
    result = collect_metrics()
    # {"collected": 3, "skipped": 1, "errors": 0}
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from app.db import get_db
from app.security import decrypt

logger = logging.getLogger(__name__)

# ─── VK константы ─────────────────────────────────────────────────────────────

_VK_API_URL = "https://api.vk.com/method/{method}"
_VK_API_VERSION = "5.199"

# Паттерны URL публикаций
_RE_VK_POST = re.compile(r"vk\.com/wall(-?\d+)_(\d+)")
_RE_VK_VIDEO = re.compile(r"vk\.com/video(-?\d+)_(\d+)")

# ─── YouTube константы ────────────────────────────────────────────────────────

_YT_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_YT_OAUTH_URL = "https://oauth2.googleapis.com/token"
_RE_YT_SHORTS = re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]+)")
_RE_YT_WATCH = re.compile(r"youtube\.com/watch\?v=([A-Za-z0-9_-]+)")


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _decrypt_credentials(raw: str | None) -> dict | None:
    """Расшифровать credentials из project_platforms. None если не заданы/повреждены."""
    if not raw:
        return None
    try:
        return json.loads(decrypt(raw))
    except Exception as exc:
        logger.warning("Не удалось расшифровать credentials: %s", exc)
        return None


def _parse_config(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _refresh_yt_access_token(credentials: dict) -> str:
    """
    Получить access_token через refresh_token (OAuth 2.0).

    Аналог логики из publishers/youtube.py (намеренно продублирован,
    чтобы не импортировать и не менять паблишер).

    Поднимает RuntimeError при ошибке.
    """
    resp = httpx.post(
        _YT_OAUTH_URL,
        data={
            "client_id": credentials.get("client_id", ""),
            "client_secret": credentials.get("client_secret", ""),
            "refresh_token": credentials.get("refresh_token", ""),
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"YouTube: ошибка обновления токена (HTTP {resp.status_code}): {resp.text}"
        )
    token_data = resp.json()
    access_token = token_data.get("access_token", "")
    if not access_token:
        raise RuntimeError(f"YouTube: access_token отсутствует в ответе OAuth: {resp.text}")
    return access_token


# ─── Сборщики по платформам ───────────────────────────────────────────────────


def _collect_vk(
    schedule_id: int,
    published_url: str,
    credentials: dict,
    today: str,
) -> bool:
    """
    Собрать метрики VK-поста или VK-видео и записать в таблицу metrics.

    Возвращает True при успехе, False при ошибке API.
    """
    access_token = credentials.get("access_token", "")
    if not access_token:
        logger.warning("VK schedule_id=%d: access_token отсутствует в credentials", schedule_id)
        return False

    # Определяем тип: видео или пост
    m_video = _RE_VK_VIDEO.search(published_url)
    m_post = _RE_VK_POST.search(published_url)

    views = likes = comments = shares = 0

    if m_video:
        owner_id, video_id = m_video.group(1), m_video.group(2)
        params = {
            "videos": f"{owner_id}_{video_id}",
            "access_token": access_token,
            "v": _VK_API_VERSION,
        }
        resp = httpx.get(
            _VK_API_URL.format(method="video.get"),
            params=params,
            timeout=30,
        )
        data = resp.json()
        if "error" in data:
            err = data["error"]
            logger.warning(
                "VK video.get error %s для schedule_id=%d: %s",
                err.get("error_code"),
                schedule_id,
                err.get("error_msg"),
            )
            return False

        items = data.get("response", {}).get("items", [])
        if items:
            item = items[0]
            views = item.get("views", 0) or 0
            likes = item.get("likes", {}).get("count", 0) or 0
            comments = item.get("comments", {}).get("count", 0) or 0
            # VK видео не возвращает reposts — оставляем 0

    elif m_post:
        group = m_post.group(1)   # может быть "-12345" или "12345"
        post = m_post.group(2)
        post_str = f"{group}_{post}"
        params = {
            "posts": post_str,
            "access_token": access_token,
            "v": _VK_API_VERSION,
        }
        resp = httpx.get(
            _VK_API_URL.format(method="wall.getById"),
            params=params,
            timeout=30,
        )
        data = resp.json()
        if "error" in data:
            err = data["error"]
            logger.warning(
                "VK wall.getById error %s для schedule_id=%d: %s",
                err.get("error_code"),
                schedule_id,
                err.get("error_msg"),
            )
            return False

        items = data.get("response", [])
        # В v5.199 wall.getById возвращает {"response": [post, ...]}
        # но может быть и вложенный items в зависимости от версии
        if isinstance(items, dict):
            items = items.get("items", [])
        if items:
            item = items[0]
            views = item.get("views", {}).get("count", 0) or 0
            likes = item.get("likes", {}).get("count", 0) or 0
            comments = item.get("comments", {}).get("count", 0) or 0
            shares = item.get("reposts", {}).get("count", 0) or 0

    else:
        logger.warning(
            "VK schedule_id=%d: не удалось разобрать published_url=%r",
            schedule_id,
            published_url,
        )
        return False

    with get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO metrics
              (schedule_id, date, views, likes, comments, shares, watch_pct)
            VALUES (?, ?, ?, ?, ?, ?, 0.0)
            """,
            (schedule_id, today, views, likes, comments, shares),
        )

    logger.info(
        "VK метрики schedule_id=%d дата=%s: views=%d likes=%d comments=%d shares=%d",
        schedule_id, today, views, likes, comments, shares,
    )
    return True


def _collect_youtube(
    schedule_id: int,
    published_url: str,
    credentials: dict,
    today: str,
) -> bool:
    """
    Собрать метрики YouTube Shorts и записать в таблицу metrics.

    Возвращает True при успехе, False при ошибке.
    """
    m = _RE_YT_SHORTS.search(published_url) or _RE_YT_WATCH.search(published_url)
    if not m:
        logger.warning(
            "YouTube schedule_id=%d: не удалось извлечь video_id из URL=%r",
            schedule_id,
            published_url,
        )
        return False

    video_id = m.group(1)

    # Получаем access_token через refresh
    try:
        access_token = _refresh_yt_access_token(credentials)
    except Exception as exc:
        logger.warning("YouTube schedule_id=%d: ошибка получения токена: %s", schedule_id, exc)
        return False

    # Запрашиваем статистику
    resp = httpx.get(
        _YT_VIDEOS_URL,
        params={"part": "statistics", "id": video_id},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )

    if not resp.is_success:
        logger.warning(
            "YouTube schedule_id=%d: HTTP %d: %s",
            schedule_id,
            resp.status_code,
            resp.text[:200],
        )
        return False

    data = resp.json()
    items = data.get("items", [])
    if not items:
        logger.warning(
            "YouTube schedule_id=%d: video_id=%r не найден в ответе API",
            schedule_id,
            video_id,
        )
        return False

    stats = items[0].get("statistics", {})
    views = int(stats.get("viewCount", 0) or 0)
    likes = int(stats.get("likeCount", 0) or 0)
    comments = int(stats.get("commentCount", 0) or 0)

    with get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO metrics
              (schedule_id, date, views, likes, comments, shares, watch_pct)
            VALUES (?, ?, ?, ?, ?, 0, 0.0)
            """,
            (schedule_id, today, views, likes, comments),
        )

    logger.info(
        "YouTube метрики schedule_id=%d дата=%s: views=%d likes=%d comments=%d",
        schedule_id, today, views, likes, comments,
    )
    return True


# ─── Главная функция ──────────────────────────────────────────────────────────


def collect_metrics(now: datetime | None = None) -> dict:
    """
    Собрать метрики всех опубликованных записей за последние 30 дней.

    Вызывается из APScheduler (джоб добавляется в scheduler.py).
    При отсутствии credentials для площадки — запись пропускается (skipped);
    метрики в этом случае вводятся вручную через дашборд.

    Telegram-метрики не реализованы: Telethon требует api_id/api_hash (новая
    зависимость); пока TG-метрики вводятся вручную.

    Возвращает:
        {"collected": int, "skipped": int, "errors": int}
    """
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)

    today = now.strftime("%Y-%m-%d")
    cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    collected = 0
    skipped = 0
    errors = 0

    # Выборка published записей за последние 30 дней для vk и youtube
    with get_db() as db:
        rows = db.execute(
            """
            SELECT
                s.id          AS schedule_id,
                s.platform,
                s.published_url,
                s.content_id,
                c.project_id
            FROM schedule s
            JOIN content c ON c.id = s.content_id
            WHERE s.status = 'published'
              AND s.published_url IS NOT NULL
              AND s.platform IN ('vk', 'youtube')
              AND s.updated_at >= ?
            ORDER BY s.id
            """,
            (cutoff,),
        ).fetchall()

    for row in rows:
        schedule_id: int = row["schedule_id"]
        platform: str = row["platform"]
        published_url: str = row["published_url"]
        project_id: int = row["project_id"]

        # Получаем credentials из project_platforms
        with get_db() as db:
            pp = db.execute(
                "SELECT credentials FROM project_platforms WHERE project_id = ? AND platform = ?",
                (project_id, platform),
            ).fetchone()

        if pp is None:
            logger.debug(
                "schedule_id=%d: площадка %s не найдена в project_platforms — пропускаем",
                schedule_id,
                platform,
            )
            skipped += 1
            continue

        credentials = _decrypt_credentials(pp["credentials"])
        if credentials is None:
            logger.debug(
                "schedule_id=%d платформа=%s: credentials не заданы — пропускаем",
                schedule_id,
                platform,
            )
            skipped += 1
            continue

        # Пропускаем dry-run URL (нечего собирать)
        if published_url.startswith("dry-run://") or published_url.startswith("manual://"):
            logger.debug(
                "schedule_id=%d: URL=%r — dry-run/manual, пропускаем",
                schedule_id,
                published_url,
            )
            skipped += 1
            continue

        try:
            if platform == "vk":
                ok = _collect_vk(schedule_id, published_url, credentials, today)
            elif platform == "youtube":
                ok = _collect_youtube(schedule_id, published_url, credentials, today)
            else:
                ok = False

            if ok:
                collected += 1
            else:
                errors += 1

        except httpx.HTTPError as exc:
            logger.warning(
                "Сетевая ошибка при сборе метрик schedule_id=%d платформа=%s: %s",
                schedule_id,
                platform,
                exc,
            )
            errors += 1
        except Exception as exc:
            logger.warning(
                "Ошибка при сборе метрик schedule_id=%d платформа=%s: %s",
                schedule_id,
                platform,
                exc,
            )
            errors += 1

    logger.info(
        "collect_metrics завершён: collected=%d skipped=%d errors=%d",
        collected,
        skipped,
        errors,
    )
    return {"collected": collected, "skipped": skipped, "errors": errors}
