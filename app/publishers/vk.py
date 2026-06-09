"""
Паблишер ВКонтакте.

API: wall.post (https://api.vk.com/method/wall.post, v=5.199)
Credentials JSON: {"access_token": "..."}
Config JSON:      {"group_id": 12345678}

Текст: texts["vk"]["text"] + hashtags через пробел.
Результат: https://vk.com/wall-{group_id}_{post_id}
"""
import logging

import httpx

from app.publishers.base import PublishError, dry_run_publish

logger = logging.getLogger(__name__)

VK_API_URL = "https://api.vk.com/method/wall.post"
VK_API_VERSION = "5.199"


def publish_vk(
    schedule_id: int,
    credentials: dict | None,
    config: dict,
    texts: dict,
    dry_run: bool,
) -> str:
    """Опубликовать пост на стене VK-группы."""

    group_id = config.get("group_id", "")
    vk_texts = texts.get("vk", {})
    text_body = vk_texts.get("text", "")
    hashtags = vk_texts.get("hashtags", [])

    full_text = text_body
    if hashtags:
        full_text = f"{text_body}\n\n{' '.join(hashtags)}"

    if dry_run:
        payload: dict = {
            "dry_run": True,
            "platform": "vk",
            "method": "wall.post",
            "url": VK_API_URL,
            "owner_id": f"-{group_id}" if group_id else "",
            "from_group": 1,
            "message": full_text,
            "hashtags": hashtags,
        }
        return dry_run_publish(schedule_id, "vk", payload)

    # Реальная отправка
    access_token = (credentials or {}).get("access_token", "")
    if not access_token:
        raise PublishError("VK: access_token не задан в credentials")
    if not group_id:
        raise PublishError("VK: group_id не задан в config")

    params = {
        "owner_id": f"-{group_id}",
        "from_group": 1,
        "message": full_text,
        "v": VK_API_VERSION,
        "access_token": access_token,
    }

    try:
        resp = httpx.post(VK_API_URL, params=params, timeout=30)
    except httpx.HTTPError as exc:
        raise PublishError(f"VK: сетевая ошибка — {exc}") from exc

    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise PublishError(
            f"VK API ошибка {err.get('error_code')}: {err.get('error_msg', 'нет описания')}"
        )

    post_id = data["response"]["post_id"]
    published_url = f"https://vk.com/wall-{group_id}_{post_id}"
    logger.info("VK опубликовано: %s", published_url)
    return published_url
