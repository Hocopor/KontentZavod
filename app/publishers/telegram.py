"""
Паблишер Telegram.

Credentials JSON: {"bot_token": "..."}
Config JSON:      {"channel_id": "@mychannel" или "-100..."}

Текст берётся из texts["telegram"]["text"] + hashtags.
При наличии files["preview_path"] отправляет sendPhoto, иначе sendMessage.
"""
import logging
from pathlib import Path

import httpx

from app.publishers.base import PublishError, dry_run_publish

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
    """Опубликовать пост в Telegram-канал."""

    channel_id = config.get("channel_id", "")
    tg_texts = texts.get("telegram", {})
    text_body = tg_texts.get("text", "")
    hashtags = tg_texts.get("hashtags", [])

    # Собираем итоговый текст
    full_text = text_body
    if hashtags:
        full_text = f"{text_body}\n\n{' '.join(hashtags)}"

    # Файл-картинка (необязательно)
    preview_path: str | None = files.get("preview_path")
    has_image = bool(preview_path and Path(preview_path).exists())

    if dry_run:
        payload: dict = {
            "dry_run": True,
            "platform": "telegram",
            "channel_id": channel_id,
            "text": full_text,
            "hashtags": hashtags,
        }
        if has_image:
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
        if has_image:
            url = TELEGRAM_API.format(token=bot_token, method="sendPhoto")
            with open(preview_path, "rb") as img:  # type: ignore[arg-type]
                resp = httpx.post(
                    url,
                    data={"chat_id": channel_id, "caption": full_text},
                    files={"photo": img},
                    timeout=30,
                )
        else:
            url = TELEGRAM_API.format(token=bot_token, method="sendMessage")
            resp = httpx.post(
                url,
                json={"chat_id": channel_id, "text": full_text, "parse_mode": "HTML"},
                timeout=30,
            )
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
