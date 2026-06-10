"""
Паблишер YouTube Shorts.

Квота YouTube Data API v3: 10 000 юнитов/день.
videos.insert стоит 1600 юнитов → при дефолтном YOUTUBE_DAILY_LIMIT=6 расходуется
9600 юнитов из 10 000, оставляя 400 юнитов на другие вызовы (thumbnails, search и т.п.).

До прохождения OAuth audit-верификации Google принудительно выставляет uploaded видео
в private, даже если запрошено «public» или «unlisted». Заявку на верификацию подаёт
пользователь самостоятельно через Google Cloud Console → OAuth consent screen.

Credentials JSON: {"client_id": "...", "client_secret": "...", "refresh_token": "..."}
Config JSON:      {"privacy": "private" | "public" | "unlisted"}  (default: "private")

Сигналы:
  PublishError    — фатальная ошибка, попытка сгорает.
  PublishDeferred — квота исчерпана, attempts НЕ растёт; process_due перенесёт planned_at.
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

import app.config as _cfg
from app.db import get_db
from app.publishers.base import PublishDeferred, PublishError, dry_run_publish

logger = logging.getLogger(__name__)

_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_UPLOAD_INIT_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)


def _today_published_count() -> int:
    """Количество опубликованных YouTube-записей за сегодня (UTC)."""
    with get_db() as db:
        row = db.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM schedule
             WHERE platform = 'youtube'
               AND status   = 'published'
               AND date(updated_at) = date('now')
            """
        ).fetchone()
    return row["cnt"] if row else 0


def _next_midnight_utc() -> datetime:
    """Возвращает следующую полночь UTC + 5 минут (для переноса при исчерпании квоты)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return tomorrow + timedelta(minutes=5)


def _build_description(yt_texts: dict) -> str:
    """Собирает итоговое описание: description + хэштеги через перенос строки."""
    description = yt_texts.get("description", "")
    hashtags = yt_texts.get("hashtags", [])
    if hashtags:
        description = f"{description}\n{' '.join(hashtags)}"
    return description


def _ensure_shorts_tag(title: str) -> str:
    """Добавляет ' #Shorts' в конец title, если '#shorts' ещё нет (без учёта регистра)."""
    if "#shorts" in title.lower():
        return title
    return f"{title} #Shorts"


def publish_youtube(
    schedule_id: int,
    credentials: dict | None,
    config: dict,
    texts: dict,
    files: dict,
    dry_run: bool,
) -> str:
    """
    Опубликовать видео-шорт на YouTube.

    Возвращает URL вида https://www.youtube.com/shorts/{video_id}
    или dry-run://youtube/{schedule_id} в dry-run режиме.

    Поднимает PublishError при фатальной ошибке.
    Поднимает PublishDeferred при исчерпании квоты (без сжигания попытки).
    """
    # ── Проверяем наличие видеофайла ─────────────────────────────────────────
    video_path: str | None = files.get("video_path")
    if not video_path:
        raise PublishError("YouTube: video_path не задан в files")

    # ── Собираем тексты ───────────────────────────────────────────────────────
    yt_texts = texts.get("youtube", {})
    title = _ensure_shorts_tag(yt_texts.get("title", ""))
    description = _build_description(yt_texts)
    privacy = (config or {}).get("privacy", "private")

    # ── Квота: проверяется и в dry-run, и в реальном режиме ──────────────────
    count = _today_published_count()
    limit = _cfg.settings.YOUTUBE_DAILY_LIMIT
    if count >= limit:
        raise PublishDeferred(
            f"YouTube: дневная квота {limit} исчерпана",
            retry_at=_next_midnight_utc(),
        )

    # ── DRY-RUN ───────────────────────────────────────────────────────────────
    if dry_run:
        payload: dict = {
            "dry_run": True,
            "platform": "youtube",
            "method": "videos.insert",
            "title": title,
            "description": description,
            "privacy": privacy,
            "video_path": video_path,
        }
        return dry_run_publish(schedule_id, "youtube", payload)

    # ── Реальный режим ────────────────────────────────────────────────────────
    # Проверяем файл на диске
    if not Path(video_path).exists():
        raise PublishError(f"YouTube: файл не найден на диске: {video_path}")

    creds = credentials or {}
    client_id = creds.get("client_id", "")
    client_secret = creds.get("client_secret", "")
    refresh_token = creds.get("refresh_token", "")

    if not (client_id and client_secret and refresh_token):
        raise PublishError(
            "YouTube: credentials должны содержать client_id, client_secret, refresh_token"
        )

    # a) Получить access_token через refresh
    try:
        token_resp = httpx.post(
            _OAUTH_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PublishError(f"YouTube: ошибка сети при обновлении токена — {exc}") from exc

    if token_resp.status_code != 200:
        raise PublishError(
            f"YouTube: не удалось обновить токен (HTTP {token_resp.status_code}): "
            f"{token_resp.text}"
        )

    token_data = token_resp.json()
    access_token: str = token_data.get("access_token", "")
    if not access_token:
        raise PublishError(
            f"YouTube: access_token отсутствует в ответе OAuth: {token_resp.text}"
        )

    # b) Resumable upload init
    init_body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    try:
        init_resp = httpx.post(
            _UPLOAD_INIT_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-Upload-Content-Type": "video/mp4",
            },
            json=init_body,
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise PublishError(f"YouTube: ошибка сети при инициализации загрузки — {exc}") from exc

    _check_youtube_response(init_resp)

    upload_url: str = init_resp.headers.get("Location", "")
    if not upload_url:
        raise PublishError(
            f"YouTube: заголовок Location отсутствует в ответе resumable init: "
            f"{init_resp.text}"
        )

    # c) PUT файла на upload URL
    try:
        with open(video_path, "rb") as video_file:
            put_resp = httpx.put(
                upload_url,
                content=video_file.read(),
                headers={"Content-Type": "video/mp4"},
                timeout=300,
            )
    except httpx.HTTPError as exc:
        raise PublishError(f"YouTube: ошибка сети при загрузке файла — {exc}") from exc

    _check_youtube_response(put_resp)

    put_data = put_resp.json()
    video_id: str = put_data.get("id", "")
    if not video_id:
        raise PublishError(
            f"YouTube: поле 'id' отсутствует в ответе после загрузки: {put_resp.text}"
        )

    published_url = f"https://www.youtube.com/shorts/{video_id}"
    logger.info("YouTube опубликовано: %s", published_url)
    return published_url


def _check_youtube_response(resp: httpx.Response) -> None:
    """
    Проверяет ответ YouTube API.

    - HTTP 403 с reason quotaExceeded / uploadLimitExceeded →
      PublishDeferred(retry_at = now + 12 часов).
    - Прочие ошибки → PublishError с телом ответа.
    """
    if resp.is_success:
        return

    if resp.status_code == 403:
        try:
            body = resp.json()
        except Exception:
            body = {}
        errors = body.get("error", {}).get("errors", [])
        quota_reasons = {"quotaExceeded", "uploadLimitExceeded"}
        for err in errors:
            if err.get("reason") in quota_reasons:
                retry_at = (
                    datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=12)
                )
                raise PublishDeferred(
                    f"YouTube: квота API исчерпана (reason={err.get('reason')})",
                    retry_at=retry_at,
                )

    raise PublishError(
        f"YouTube API: HTTP {resp.status_code} — {resp.text}"
    )
