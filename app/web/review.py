"""
Ревью черновиков контента (посты + видео).

Маршруты:
    GET  /review                         — список content со status IN ('text_review','review')
    GET  /review/{content_id}            — страница ревью одного черновика
    POST /review/{content_id}/save       — сохранить правки текстов
    POST /review/{content_id}/approve    — одобрить
                                           • type=post / status=text_review → approved + планирование
                                           • type=video_* / status=text_review → production (без планирования)
                                           • type=video_* / status=review → approved + планирование
    POST /review/{content_id}/reject     — отклонить с причиной
    POST /review/{content_id}/rerender   — сбросить ошибку продакшна (video_*, status production)
"""
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_db
from app.services.scheduling import schedule_content
from app.templates_env import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/review")

# Лимиты символов для каждой площадки
PLATFORM_LIMITS = {
    "telegram": 4096,
    "vk": 15000,
    "youtube": 5000,
    "instagram": 2200,
    "dzen": 40000,
}

PLATFORM_NAMES = {
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "youtube": "YouTube",
    "instagram": "Instagram",
    "dzen": "Яндекс.Дзен",
}

PLATFORMS_ORDER = ["telegram", "vk", "youtube", "instagram", "dzen"]


def _get_enabled_platforms(project_id: int) -> list[str]:
    """Список включённых площадок проекта."""
    with get_db() as db:
        rows = db.execute(
            "SELECT platform FROM project_platforms WHERE project_id=? AND enabled=1",
            (project_id,),
        ).fetchall()
    order = {p: i for i, p in enumerate(PLATFORMS_ORDER)}
    return sorted([r["platform"] for r in rows], key=lambda p: order.get(p, 99))


def _build_content_context(content_id: int) -> dict | None:
    """Собирает контекст для страницы ревью одного черновика."""
    with get_db() as db:
        row = db.execute(
            """
            SELECT c.*, p.name AS project_name, p.slug AS project_slug
              FROM content c
              JOIN projects p ON p.id = c.project_id
             WHERE c.id = ?
            """,
            (content_id,),
        ).fetchone()
    if row is None:
        return None

    c = dict(row)
    # Декодируем texts и features
    try:
        texts = json.loads(c["texts"]) if c.get("texts") else {}
    except (json.JSONDecodeError, TypeError):
        texts = {}

    try:
        features = json.loads(c["features"]) if c.get("features") else {}
    except (json.JSONDecodeError, TypeError):
        features = {}

    try:
        files = json.loads(c["files"]) if c.get("files") else {}
    except (json.JSONDecodeError, TypeError):
        files = {}

    enabled_platforms = _get_enabled_platforms(c["project_id"])

    # ── Для постов: стандартная обработка ─────────────────────────────────────
    is_video = c["type"] in ("video_footage", "video_slideshow")

    if not is_video:
        platform_data = []
        for platform in enabled_platforms:
            pdata = texts.get(platform, {})
            if platform == "youtube":
                text_value = pdata.get("description", pdata.get("title", ""))
            elif platform == "dzen":
                text_value = pdata.get("text", "")
            elif platform == "instagram":
                text_value = pdata.get("caption", "")
            else:
                text_value = pdata.get("text", "")

            platform_data.append({
                "platform": platform,
                "name": PLATFORM_NAMES.get(platform, platform),
                "text": text_value,
                "limit": PLATFORM_LIMITS.get(platform, 5000),
                "raw": pdata,
            })
    else:
        # ── Для видео: площадки без dzen ──────────────────────────────────────
        platform_data = []
        for platform in enabled_platforms:
            if platform == "dzen":
                continue
            pdata = texts.get(platform, {})
            if platform == "youtube":
                text_value = pdata.get("description", pdata.get("title", ""))
            elif platform == "instagram":
                text_value = pdata.get("caption", "")
            else:
                text_value = pdata.get("text", "")

            platform_data.append({
                "platform": platform,
                "name": PLATFORM_NAMES.get(platform, platform),
                "text": text_value,
                "limit": PLATFORM_LIMITS.get(platform, 5000),
                "raw": pdata,
            })

    # Сцены для видео
    video_scenes = []
    if is_video:
        video_block = texts.get("video", {})
        video_scenes = video_block.get("scenes", [])

    return {
        "content": c,
        "texts": texts,
        "features": features,
        "files": files,
        "enabled_platforms": enabled_platforms,
        "platform_data": platform_data,
        "is_video": is_video,
        "video_scenes": video_scenes,
        "produce_error": files.get("produce_error"),
        "produce_attempts": files.get("produce_attempts", 0),
    }


# ─── Список ревью ─────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def review_list(request: Request, project_id: str = ""):
    with get_db() as db:
        projects = db.execute(
            "SELECT id, name, slug FROM projects WHERE status='active' ORDER BY name"
        ).fetchall()

        if project_id:
            rows = db.execute(
                """
                SELECT c.id, c.title, c.type, c.status, c.created_at, c.updated_at,
                       c.files,
                       p.name AS project_name, p.slug AS project_slug, p.id AS pid
                  FROM content c
                  JOIN projects p ON p.id = c.project_id
                 WHERE c.status IN ('text_review', 'review') AND p.id = ?
                 ORDER BY c.updated_at DESC
                """,
                (project_id,),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT c.id, c.title, c.type, c.status, c.created_at, c.updated_at,
                       c.files,
                       p.name AS project_name, p.slug AS project_slug, p.id AS pid
                  FROM content c
                  JOIN projects p ON p.id = c.project_id
                 WHERE c.status IN ('text_review', 'review')
                 ORDER BY c.updated_at DESC
                """
            ).fetchall()

    # Обогащаем: добавляем produce_error из files JSON
    items = []
    for r in rows:
        item = dict(r)
        try:
            files = json.loads(item["files"]) if item.get("files") else {}
        except (json.JSONDecodeError, TypeError):
            files = {}
        item["produce_error"] = files.get("produce_error")
        items.append(item)

    return templates.TemplateResponse(
        request,
        "review/list.html",
        {
            "items": items,
            "projects": [dict(p) for p in projects],
            "selected_project": project_id,
        },
    )


# ─── Страница ревью черновика ──────────────────────────────────────────────────


@router.get("/{content_id}", response_class=HTMLResponse)
async def review_detail(request: Request, content_id: int):
    ctx = _build_content_context(content_id)
    if ctx is None:
        return HTMLResponse("Контент не найден", status_code=404)

    # Дефолтное время планирования — завтра 10:00
    tomorrow = datetime.now() + timedelta(days=1)
    default_dt = tomorrow.strftime("%Y-%m-%dT10:00")
    ctx["default_dt"] = default_dt
    ctx["flash"] = request.query_params.get("flash", "")
    ctx["error"] = request.query_params.get("error", "")

    return templates.TemplateResponse(request, "review/detail.html", ctx)


# ─── Сохранить правки текстов ─────────────────────────────────────────────────


@router.post("/{content_id}/save")
async def review_save(request: Request, content_id: int):
    ctx = _build_content_context(content_id)
    if ctx is None:
        return HTMLResponse("Контент не найден", status_code=404)

    form_data = await request.form()
    texts = ctx["texts"].copy()

    if ctx["is_video"]:
        # Для видео: сцены сохраняются через textarea scene_N
        video_block = texts.get("video", {})
        scenes = video_block.get("scenes", [])
        for i, scene in enumerate(scenes):
            field_name = f"scene_{i}"
            if field_name in form_data:
                scenes[i] = {**scene, "text": form_data[field_name]}
        video_block["scenes"] = scenes
        texts["video"] = video_block
    else:
        for platform in ctx["enabled_platforms"]:
            field_name = f"text_{platform}"
            if field_name in form_data:
                new_text = form_data[field_name]
                if platform not in texts:
                    texts[platform] = {}
                if platform == "youtube":
                    texts[platform]["description"] = new_text
                elif platform == "dzen":
                    texts[platform]["text"] = new_text
                elif platform == "instagram":
                    texts[platform]["caption"] = new_text
                else:
                    texts[platform]["text"] = new_text

    with get_db() as db:
        db.execute(
            "UPDATE content SET texts=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps(texts, ensure_ascii=False), content_id),
        )

    return RedirectResponse(
        f"/review/{content_id}?flash=Правки+сохранены", status_code=303
    )


# ─── Одобрить ──────────────────────────────────────────────────────────────────


@router.post("/{content_id}/approve")
async def review_approve(request: Request, content_id: int):
    ctx = _build_content_context(content_id)
    if ctx is None:
        return HTMLResponse("Контент не найден", status_code=404)

    content = ctx["content"]
    is_video = ctx["is_video"]
    form_data = await request.form()

    # ── Сохраняем редактированные тексты ──────────────────────────────────────
    texts = ctx["texts"].copy()

    if is_video:
        # Сцены из textarea
        video_block = texts.get("video", {})
        scenes = video_block.get("scenes", [])
        for i, scene in enumerate(scenes):
            field_name = f"scene_{i}"
            if field_name in form_data and form_data[field_name].strip():
                scenes[i] = {**scene, "text": form_data[field_name]}
        video_block["scenes"] = scenes
        texts["video"] = video_block
    else:
        for platform in ctx["enabled_platforms"]:
            field_name = f"text_{platform}"
            if field_name in form_data and form_data[field_name].strip():
                if platform not in texts:
                    texts[platform] = {}
                new_text = form_data[field_name]
                if platform == "youtube":
                    texts[platform]["description"] = new_text
                elif platform == "dzen":
                    texts[platform]["text"] = new_text
                elif platform == "instagram":
                    texts[platform]["caption"] = new_text
                else:
                    texts[platform]["text"] = new_text

    # ── Логика approve зависит от типа и статуса ──────────────────────────────
    current_status = content["status"]

    if is_video and current_status == "text_review":
        # Видео после текстового ревью → в продакшн (рендер)
        with get_db() as db:
            db.execute(
                """
                UPDATE content
                   SET status='production', texts=?, updated_at=datetime('now')
                 WHERE id=?
                """,
                (json.dumps(texts, ensure_ascii=False), content_id),
            )

        return RedirectResponse(
            "/review?flash=Видео+отправлено+в+продакшн",
            status_code=303,
        )

    # Посты и видео после рендера (status='review') → approved + планирование
    with get_db() as db:
        db.execute(
            """
            UPDATE content
               SET status='approved', texts=?, updated_at=datetime('now')
             WHERE id=?
            """,
            (json.dumps(texts, ensure_ascii=False), content_id),
        )

    # Планирование
    errors = []
    scheduled_count = 0
    base_time = datetime.now() + timedelta(days=1)
    base_time = base_time.replace(hour=10, minute=0, second=0, microsecond=0)

    # Для видео планируем только по видео-площадкам (без dzen)
    platforms_to_schedule = [
        p for p in ctx["enabled_platforms"]
        if not (is_video and p == "dzen")
    ]

    for i, platform in enumerate(platforms_to_schedule):
        field_name = f"planned_at_{platform}"
        check_field = f"schedule_{platform}"

        if check_field not in form_data:
            continue

        dt_str = form_data.get(field_name, "").strip()
        if dt_str:
            try:
                planned_at = datetime.fromisoformat(dt_str)
            except ValueError:
                errors.append(f"Неверный формат даты для {PLATFORM_NAMES.get(platform, platform)}")
                continue
        else:
            planned_at = base_time + timedelta(minutes=i * 30)

        try:
            schedule_content(content_id, platform, planned_at)
            scheduled_count += 1
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        logger.warning("approve content_id=%d: ошибки планирования: %s", content_id, errors)

    if scheduled_count > 0:
        return RedirectResponse(
            f"/calendar?flash=Контент+одобрен+и+запланирован+({scheduled_count}+площадок)",
            status_code=303,
        )

    return RedirectResponse(
        f"/review/{content_id}?flash=Контент+одобрен.+Запланируйте+публикацию+из+календаря.",
        status_code=303,
    )


# ─── Отклонить ────────────────────────────────────────────────────────────────


@router.post("/{content_id}/reject")
async def review_reject(request: Request, content_id: int):
    ctx = _build_content_context(content_id)
    if ctx is None:
        return HTMLResponse("Контент не найден", status_code=404)

    form_data = await request.form()
    reason = form_data.get("reason", "").strip()

    if not reason:
        return RedirectResponse(
            f"/review/{content_id}?error=Причина+отклонения+обязательна",
            status_code=303,
        )

    project_id = ctx["content"]["project_id"]

    with get_db() as db:
        db.execute(
            """
            UPDATE content
               SET status='rejected', reject_reason=?, updated_at=datetime('now')
             WHERE id=?
            """,
            (reason, content_id),
        )
        insight = f"Отклонено ревьюером: {reason}"
        db.execute(
            """
            INSERT INTO learnings (project_id, insight, source, active)
            VALUES (?, ?, 'reject', 1)
            """,
            (project_id, insight),
        )

    return RedirectResponse("/review?flash=Контент+отклонён", status_code=303)


# ─── Перерендерить видео ──────────────────────────────────────────────────────


@router.post("/{content_id}/rerender")
async def review_rerender(request: Request, content_id: int):
    """
    Сбросить ошибку продакшна и вернуть контент в статус 'production'
    для повторного рендера.
    Применимо только к видеоконтенту.
    """
    with get_db() as db:
        row = db.execute(
            "SELECT id, type, files FROM content WHERE id=?", (content_id,)
        ).fetchone()

    if row is None:
        return HTMLResponse("Контент не найден", status_code=404)

    if row["type"] not in ("video_footage", "video_slideshow"):
        return HTMLResponse("Перерендер доступен только для видеоконтента", status_code=400)

    try:
        files = json.loads(row["files"]) if row["files"] else {}
    except (json.JSONDecodeError, TypeError):
        files = {}

    # Сбрасываем ошибку и счётчик попыток
    files.pop("produce_error", None)
    files.pop("produce_attempts", None)

    with get_db() as db:
        db.execute(
            """
            UPDATE content
               SET status='production',
                   files=?,
                   updated_at=datetime('now')
             WHERE id=?
            """,
            (json.dumps(files, ensure_ascii=False), content_id),
        )

    return RedirectResponse(
        "/review?flash=Контент+поставлен+в+очередь+рендера",
        status_code=303,
    )
