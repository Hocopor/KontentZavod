"""
Паблишер Telegram.

Credentials JSON: {"bot_token": "..."}
Config JSON:      {"channel_id": "@mychannel" или "-100..."}

Текст берётся из texts["telegram"]["text"] + hashtags.
При наличии files["video_path"] отправляет sendVideo (приоритет над preview_path).
При наличии files["preview_path"] (без видео) отправляет sendPhoto.
Иначе sendMessage.

Все реальные запросы к Bot API идут через request_via_proxy() (app/services/proxies.py),
что обеспечивает автоматический фейловер через HTTP-прокси.
Если прокси не настроены — используется прямой запрос (dev-режим).
"""
import logging
from pathlib import Path

import httpx

from app.publishers.base import PublishError, dry_run_publish
from app.services.proxies import request_via_proxy

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def publish_telegram(
    schedule_id: int,
    credentials: dict | None,
    config: dict,
    texts: dict,
    files: dict,
    dry_run: bool,
) -> str:
    """Опубликовать пост, фото или видео в Telegram-канал."""

    channel_id = config.get("channel_id", "")
    tg_texts = texts.get("telegram", {})
    text_body = tg_texts.get("text", "")
    hashtags = tg_texts.get("hashtags", [])

    # Собираем итоговый текст
    full_text = text_body
    if hashtags:
        full_text = f"{text_body}\n\n{' '.join(hashtags)}"

    # Определяем тип вложения (видео имеет приоритет)
    video_path: str | None = files.get("video_path") or None
    preview_path: str | None = files.get("preview_path")

    has_video = bool(video_path)
    has_image = bool(preview_path and Path(preview_path).exists()) if not has_video else False

    if dry_run:
        payload: dict = {
            "dry_run": True,
            "platform": "telegram",
            "channel_id": channel_id,
            "text": full_text,
            "hashtags": hashtags,
        }
        if has_video:
            payload["method"] = "sendVideo"
            payload["video_path"] = video_path
        elif has_image:
            payload["method"] = "sendPhoto"
            payload["photo_path"] = preview_path
        else:
            payload["method"] = "sendMessage"
        return dry_run_publish(schedule_id, "telegram", payload)

    # Реальная отправка
    bot_token = (credentials or {}).get("bot_token", "")
    if not bot_token:
        raise PublishError("Telegram: bot_token не задан в credentials")
    if not channel_id:
        raise PublishError("Telegram: channel_id не задан в config")

    try:
        if has_video:
            if not Path(video_path).exists():  # type: ignore[arg-type]
                raise PublishError(f"Telegram: видео-файл не найден: {video_path}")
            api_url = TELEGRAM_API.format(token=bot_token, method="sendVideo")
            with open(video_path, "rb") as vf:  # type: ignore[arg-type]
                resp = request_via_proxy(
                    "POST",
                    api_url,
                    data={"chat_id": channel_id, "caption": full_text, "supports_streaming": "1"},
                    files={"video": vf},
                    timeout=120,
                )
        elif has_image:
            api_url = TELEGRAM_API.format(token=bot_token, method="sendPhoto")
            with open(preview_path, "rb") as img:  # type: ignore[arg-type]
                resp = request_via_proxy(
                    "POST",
                    api_url,
                    data={"chat_id": channel_id, "caption": full_text},
                    files={"photo": img},
                    timeout=30,
                )
        else:
            api_url = TELEGRAM_API.format(token=bot_token, method="sendMessage")
            resp = request_via_proxy(
                "POST",
                api_url,
                json={"chat_id": channel_id, "text": full_text, "parse_mode": "HTML"},
                timeout=30,
            )
    except PublishError:
        raise
    except httpx.HTTPError as exc:
        raise PublishError(f"Telegram: сетевая ошибка — {exc}") from exc

    data = resp.json()
    if not data.get("ok"):
        description = data.get("description", "нет описания")
        raise PublishError(f"Telegram API: {description}")

    message_id = data["result"]["message_id"]
    published_url = f"https://t.me/{channel_id.lstrip('@')}/{message_id}"
    logger.info("Telegram опубликовано: %s", published_url)
    return published_url
