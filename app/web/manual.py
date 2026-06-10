"""
Ручная очередь публикаций.

Маршруты:
    GET  /manual                          — список manual_pending
    POST /manual/{schedule_id}/done       — отметить как опубликовано
"""
import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_db
from app.publishers.manual import mark_manual_done
from app.templates_env import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/manual")

PLATFORM_NAMES = {
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "youtube": "YouTube",
    "instagram": "Instagram",
    "dzen": "Яндекс.Дзен",
}


@router.get("", response_class=HTMLResponse)
async def manual_list(request: Request):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT s.id, s.content_id, s.platform, s.planned_at, s.status,
                   c.title, c.type, c.texts, c.files,
                   p.name AS project_name, p.slug AS project_slug
              FROM schedule s
              JOIN content c ON c.id = s.content_id
              JOIN projects p ON p.id = c.project_id
             WHERE s.status = 'manual_pending'
             ORDER BY s.planned_at
            """
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        # Подготавливаем текст для конкретной площадки
        platform = item["platform"]
        try:
            texts = json.loads(item["texts"]) if item.get("texts") else {}
        except (json.JSONDecodeError, TypeError):
            texts = {}

        pdata = texts.get(platform, {})
        if platform == "youtube":
            text_value = pdata.get("description", pdata.get("title", ""))
            extra = {"title": pdata.get("title", ""), "hashtags": pdata.get("hashtags", [])}
        elif platform == "dzen":
            text_value = pdata.get("text", "")
            extra = {"title": pdata.get("title", ""), "hashtags": []}
        elif platform == "instagram":
            text_value = pdata.get("caption", pdata.get("text", ""))
            extra = {"hashtags": pdata.get("hashtags", [])}
        else:
            text_value = pdata.get("text", "")
            extra = {"hashtags": pdata.get("hashtags", [])}

        # Файлы
        try:
            files = json.loads(item["files"]) if item.get("files") else {}
        except (json.JSONDecodeError, TypeError):
            files = {}

        item["platform_name"] = PLATFORM_NAMES.get(platform, platform)
        item["text_value"] = text_value
        item["extra"] = extra
        item["files_parsed"] = files
        item["content_id"] = item["content_id"]
        item["has_video"] = bool(files.get("video_path"))
        # Story: caption из texts
        is_story = item.get("type") == "story"
        item["is_story"] = is_story
        if is_story:
            item["story_caption"] = pdata.get("caption", text_value)
        items.append(item)

    flash = request.query_params.get("flash", "")
    return templates.TemplateResponse(
        request,
        "manual/list.html",
        {"items": items, "flash": flash},
    )


@router.post("/{schedule_id}/done")
async def manual_done(
    request: Request,
    schedule_id: int,
    published_url: str = Form(""),
):
    with get_db() as db:
        row = db.execute(
            "SELECT status FROM schedule WHERE id=?", (schedule_id,)
        ).fetchone()

    if row is None:
        return HTMLResponse("Запись не найдена", status_code=404)

    if row["status"] != "manual_pending":
        return RedirectResponse(
            "/manual?flash=Запись+не+в+состоянии+manual_pending",
            status_code=303,
        )

    url = published_url.strip() or f"manual://done/{schedule_id}"
    mark_manual_done(schedule_id, url)

    return RedirectResponse("/manual?flash=Отмечено+как+опубликовано", status_code=303)
