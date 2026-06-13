"""
Паблишер ВКонтакте.

API: wall.post (https://api.vk.com/method/wall.post, v=5.199)
Credentials JSON: {"access_token": "...", "user_token": "..."}
  access_token — групповой токен сообщества (wall, photos).
  user_token   — пользовательский токен АДМИНА сообщества с правами video+wall+offline.
                 Обязателен для video.save (групповой токен получает ошибку 27 VK API).
                 Опционален: если не задан, текстовые посты и фото работают в штатном режиме.
Config JSON:      {"group_id": 12345678}

Текст: texts["vk"]["text"] + hashtags через пробел.
Markdown-разметка убирается через md_to_plain (VK не поддерживает разметку).
Результат: https://vk.com/wall-{group_id}_{post_id}

Видео-режим (если files["video_path"] задан):
  Требует user_token (пользовательский токен с правами video+wall).
  Используется video.save + wallpost=1 для публикации видео в стену группы.
  Метод shortVideo.create — партнёрский (недоступен обычным токенам сообществ),
  поэтому видео-клипы НЕ реализуются; видео идёт через wallpost=1.
  ВАЖНО: video.save с групповым access_token возвращает ошибку 27 («Group authorization
  failed»); вызывать его необходимо с пользовательским user_token, group_id при этом
  остаётся положительным.

Фото-режим (если files["image_path"] задан и нет video_path):
  photos.getWallUploadServer → POST файла → photos.saveWallPhoto → wall.post с attachment.
  Всё через групповой access_token. Ошибка загрузки фото → публикация без фото (warning).

Результат видео: https://vk.com/video{owner_id}_{video_id}
"""
import logging
from pathlib import Path

import httpx

from app.publishers.base import PublishError, dry_run_publish
from app.services.textfmt import md_to_plain

logger = logging.getLogger(__name__)

VK_API_URL = "https://api.vk.com/method/wall.post"
VK_VIDEO_SAVE_URL = "https://api.vk.com/method/video.save"
VK_PHOTO_UPLOAD_SERVER_URL = "https://api.vk.com/method/photos.getWallUploadServer"
VK_PHOTO_SAVE_URL = "https://api.vk.com/method/photos.saveWallPhoto"
VK_STORIES_GET_UPLOAD_SERVER_URL = "https://api.vk.com/method/stories.getPhotoUploadServer"
VK_STORIES_SAVE_URL = "https://api.vk.com/method/stories.save"
VK_API_VERSION = "5.199"


def publish_vk(
    schedule_id: int,
    credentials: dict | None,
    config: dict,
    texts: dict,
    files: dict,
    dry_run: bool,
) -> str:
    """Опубликовать пост или видео на стене VK-группы."""

    group_id = config.get("group_id", "")
    vk_texts = texts.get("vk", {})
    text_body = vk_texts.get("text", "")
    hashtags = vk_texts.get("hashtags", [])

    # Убираем markdown-разметку (VK не поддерживает никакую разметку)
    raw_text = text_body
    if hashtags:
        raw_text = f"{text_body}\n\n{' '.join(hashtags)}"
    full_text = md_to_plain(raw_text)

    video_path: str | None = files.get("video_path") or None
    image_path: str | None = files.get("image_path") or None

    # ─── Сторис (slides) — ВСЕГДА раньше video/image ─────────────────────────
    slides = files.get("slides") or []
    if isinstance(slides, list) and slides:
        return _publish_story(
            schedule_id=schedule_id,
            credentials=credentials,
            group_id=group_id,
            slides=slides,
            dry_run=dry_run,
        )

    # ─── Видео-путь ───────────────────────────────────────────────────────────
    if video_path:
        return _publish_video(
            schedule_id=schedule_id,
            credentials=credentials,
            group_id=group_id,
            full_text=full_text,
            hashtags=hashtags,
            video_path=video_path,
            dry_run=dry_run,
        )

    # ─── Пост с фото (image_path, без видео) ─────────────────────────────────
    if image_path:
        return _publish_post_with_photo(
            schedule_id=schedule_id,
            credentials=credentials,
            group_id=group_id,
            full_text=full_text,
            hashtags=hashtags,
            image_path=image_path,
            dry_run=dry_run,
        )

    # ─── Текстовый пост ───────────────────────────────────────────────────────
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


# ─── Вспомогательная функция для публикации видео ─────────────────────────────


def _publish_video(
    schedule_id: int,
    credentials: dict | None,
    group_id: int | str,
    full_text: str,
    hashtags: list,
    video_path: str,
    dry_run: bool,
) -> str:
    """Загрузить видео через video.save и опубликовать на стене (wallpost=1)."""

    # Название: первые 100 символов текста или «Видео»
    text_for_name = full_text.strip()
    video_name = text_for_name[:100] if text_for_name else "Видео"

    if dry_run:
        payload: dict = {
            "dry_run": True,
            "platform": "vk",
            "method": "video.save",
            "wallpost": 1,
            "group_id": group_id,
            "name": video_name,
            "description": full_text,
            "video_path": video_path,
        }
        return dry_run_publish(schedule_id, "vk", payload)

    # Реальная загрузка
    if not Path(video_path).exists():
        raise PublishError(f"VK: видео-файл не найден: {video_path}")

    user_token = (credentials or {}).get("user_token", "")
    if not user_token:
        raise PublishError(
            "VK: для публикации видео нужен пользовательский токен (user_token) с правами "
            "video+wall+offline — групповой токен video.save не поддерживает (ошибка 27 VK API). "
            "Добавьте его в настройках площадки VK."
        )
    if not group_id:
        raise PublishError("VK: group_id не задан в config")

    # 1. Получаем upload_url через video.save (только с user_token!)
    params = {
        "access_token": user_token,
        "group_id": int(group_id),  # положительный group_id
        "name": video_name,
        "description": full_text,
        "wallpost": 1,
        "v": VK_API_VERSION,
    }

    try:
        resp = httpx.post(VK_VIDEO_SAVE_URL, params=params, timeout=30)
    except httpx.HTTPError as exc:
        raise PublishError(f"VK video.save: сетевая ошибка — {exc}") from exc

    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise PublishError(
            f"VK API video.save ошибка {err.get('error_code')}: {err.get('error_msg', 'нет описания')}"
        )

    vk_resp = data.get("response", {})
    upload_url = vk_resp.get("upload_url")
    owner_id = vk_resp.get("owner_id")
    video_id = vk_resp.get("video_id")

    if not upload_url:
        raise PublishError("VK video.save: upload_url не получен")

    # 2. Загружаем файл на upload_url
    try:
        with open(video_path, "rb") as vf:
            upload_resp = httpx.post(
                upload_url,
                files={"video_file": vf},
                timeout=120,
            )
    except httpx.HTTPError as exc:
        raise PublishError(f"VK upload: сетевая ошибка при загрузке видео — {exc}") from exc

    upload_data = upload_resp.json()
    if "error" in upload_data:
        raise PublishError(f"VK upload: ошибка ответа — {upload_data['error']}")

    # Финальные owner_id/video_id могут быть в ответе аплоада
    final_owner_id = upload_data.get("owner_id", owner_id)
    final_video_id = upload_data.get("video_id", video_id)

    published_url = f"https://vk.com/video{final_owner_id}_{final_video_id}"
    logger.info("VK видео опубликовано: %s", published_url)
    return published_url


# ─── Публикация поста с фотографией ──────────────────────────────────────────


def _publish_post_with_photo(
    schedule_id: int,
    credentials: dict | None,
    group_id: int | str,
    full_text: str,
    hashtags: list,
    image_path: str,
    dry_run: bool,
) -> str:
    """
    Загрузить фото через photos.getWallUploadServer → saveWallPhoto,
    затем опубликовать wall.post с attachment.

    При любой ошибке загрузки фото → публикует текстовый пост без фото (warning).
    """
    if dry_run:
        payload: dict = {
            "dry_run": True,
            "platform": "vk",
            "method": "wall.post+photo",
            "owner_id": f"-{group_id}" if group_id else "",
            "from_group": 1,
            "message": full_text,
            "hashtags": hashtags,
            "image_path": image_path,
        }
        return dry_run_publish(schedule_id, "vk", payload)

    access_token = (credentials or {}).get("access_token", "")
    if not access_token:
        raise PublishError("VK: access_token не задан в credentials")
    if not group_id:
        raise PublishError("VK: group_id не задан в config")

    attachment: str | None = None

    # Пробуем загрузить фото (ошибка → пост без фото, не валим публикацию)
    if Path(image_path).exists():
        try:
            # 1. Получаем upload URL
            save_params = {
                "access_token": access_token,
                "group_id": int(group_id),
                "v": VK_API_VERSION,
            }
            resp_server = httpx.post(VK_PHOTO_UPLOAD_SERVER_URL, params=save_params, timeout=30)
            server_data = resp_server.json()
            if "error" in server_data:
                raise RuntimeError(f"getWallUploadServer: {server_data['error']}")
            upload_url = server_data["response"]["upload_url"]

            # 2. Загружаем файл
            with open(image_path, "rb") as img:
                upload_resp = httpx.post(
                    upload_url,
                    files={"photo": img},
                    timeout=60,
                )
            upload_data = upload_resp.json()

            # 3. Сохраняем фото
            save_photo_params = {
                "access_token": access_token,
                "group_id": int(group_id),
                "v": VK_API_VERSION,
                "photo": upload_data.get("photo", ""),
                "server": upload_data.get("server", ""),
                "hash": upload_data.get("hash", ""),
            }
            resp_save = httpx.post(VK_PHOTO_SAVE_URL, params=save_photo_params, timeout=30)
            save_data = resp_save.json()
            if "error" in save_data:
                raise RuntimeError(f"saveWallPhoto: {save_data['error']}")
            photo_info = save_data["response"][0]
            owner_id = photo_info["owner_id"]
            photo_id = photo_info["id"]
            attachment = f"photo{owner_id}_{photo_id}"
            logger.debug("VK фото загружено: %s", attachment)
        except Exception as exc:  # noqa: BLE001
            logger.warning("VK: не удалось загрузить фото (%s) — публикую без фото", exc)
            attachment = None
    else:
        logger.warning("VK: файл изображения не найден (%s) — публикую без фото", image_path)

    # 4. Публикуем пост
    params: dict = {
        "owner_id": f"-{group_id}",
        "from_group": 1,
        "message": full_text,
        "v": VK_API_VERSION,
        "access_token": access_token,
    }
    if attachment:
        params["attachments"] = attachment

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
    logger.info("VK опубликовано с фото: %s", published_url)
    return published_url


# ─── Публикация историй (stories) ────────────────────────────────────────────


def _publish_story(
    schedule_id: int,
    credentials: dict | None,
    group_id: int | str,
    slides: list,
    dry_run: bool,
) -> str:
    """
    Опубликовать историю VK из слайдов-картинок через VK Stories API.

    Алгоритм для каждого слайда:
      1. stories.getPhotoUploadServer — получить upload_url
      2. POST файла на upload_url — получить upload_result
      3. stories.save — сохранить историю, получить owner_id и story id

    Текстовое описание в историю НЕ передаётся — истории VK не имеют caption.
    Требует пользовательского user_token с правами stories+offline.
    """
    if dry_run:
        payload: dict = {
            "dry_run": True,
            "platform": "vk",
            "method": "stories.save",
            "group_id": group_id,
            "slides": list(slides),
            "count": len(slides),
        }
        return dry_run_publish(schedule_id, "vk", payload)

    # Реальный режим
    user_token = (credentials or {}).get("user_token", "")
    if not user_token:
        raise PublishError(
            "VK: для публикации истории нужен пользовательский токен (user_token) с правами "
            "stories+offline — добавьте его в настройках площадки VK."
        )
    if not group_id:
        raise PublishError("VK: group_id не задан в config")

    first_owner_id: int | None = None
    first_story_id: int | None = None

    for slide in slides:
        if not Path(slide).exists():
            raise PublishError(f"VK: слайд не найден: {slide}")

        try:
            # 1. Получаем upload_url
            resp_server = httpx.post(
                VK_STORIES_GET_UPLOAD_SERVER_URL,
                params={
                    "access_token": user_token,
                    "add_to_news": 1,
                    "group_id": int(group_id),
                    "v": VK_API_VERSION,
                },
                timeout=30,
            )
            server_data = resp_server.json()
            if "error" in server_data:
                err = server_data["error"]
                raise PublishError(
                    f"VK stories.getPhotoUploadServer ошибка "
                    f"{err.get('error_code')}: {err.get('error_msg', 'нет описания')}"
                )
            upload_url = server_data["response"]["upload_url"]

            # 2. Загружаем файл
            with open(slide, "rb") as f:
                up = httpx.post(upload_url, files={"file": f}, timeout=120)
            up_data = up.json()
            upload_result = up_data.get("response", {}).get("upload_result")
            if not upload_result:
                raise PublishError("VK stories: upload_result не получен")

            # 3. Сохраняем историю
            resp_save = httpx.post(
                VK_STORIES_SAVE_URL,
                params={
                    "access_token": user_token,
                    "upload_results": upload_result,
                    "v": VK_API_VERSION,
                },
                timeout=30,
            )
            save_data = resp_save.json()
            if "error" in save_data:
                err = save_data["error"]
                raise PublishError(
                    f"VK stories.save ошибка "
                    f"{err.get('error_code')}: {err.get('error_msg', 'нет описания')}"
                )

            story_item = save_data["response"]["items"][0]
            owner_id = story_item["owner_id"]
            story_id = story_item["id"]

            if first_owner_id is None:
                first_owner_id = owner_id
                first_story_id = story_id

        except httpx.HTTPError as exc:
            raise PublishError(f"VK stories: сетевая ошибка — {exc}") from exc

    published_url = f"https://vk.com/story{first_owner_id}_{first_story_id}"
    logger.info("VK история опубликована (%d слайд(ов)): %s", len(slides), published_url)
    return published_url
