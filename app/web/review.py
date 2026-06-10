"""
Ревью черновиков контента.

Маршруты:
    GET  /review                      — список content со status='text_review'
    GET  /review/{content_id}         — страница ревью одного черновика
    POST /review/{content_id}/save    — сохранить правки текстов
    POST /review/{content_id}/approve — одобрить + форма планирования
    POST /review/{content_id}/reject  — отклонить с причиной
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

    enabled_platforms = _get_enabled_platforms(c["project_id"])
    platform_data = []
    for platform in enabled_platforms:
        pdata = texts.get(platform, {})
        # Для youtube текст составной
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

    return {
        "content": c,
        "texts": texts,
        "features": features,
        "enabled_platforms": enabled_platforms,
        "platform_data": platform_data,
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
                SELECT c.id, c.title, c.type, c.created_at, c.updated_at,
                       p.name AS project_name, p.slug AS project_slug, p.id AS pid
                  FROM content c
                  JOIN projects p ON p.id = c.project_id
                 WHERE c.status = 'text_review' AND p.id = ?
                 ORDER BY c.updated_at DESC
                """,
                (project_id,),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT c.id, c.title, c.type, c.created_at, c.updated_at,
                       p.name AS project_name, p.slug AS project_slug, p.id AS pid
                  FROM content c
                  JOIN projects p ON p.id = c.project_id
                 WHERE c.status = 'text_review'
                 ORDER BY c.updated_at DESC
                """
            ).fetchall()

    items = [dict(r) for r in rows]
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

    # Сохраняем редактированные тексты если есть
    form_data = await request.form()
    texts = ctx["texts"].copy()
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

    # Меняем статус на approved
    with get_db() as db:
        db.execute(
            """
            UPDATE content
               SET status='approved', texts=?, updated_at=datetime('now')
             WHERE id=?
            """,
            (json.dumps(texts, ensure_ascii=False), content_id),
        )

    # Планирование: для каждой площадки ищем planned_at_{platform}
    errors = []
    scheduled_count = 0
    base_time = datetime.now() + timedelta(days=1)
    base_time = base_time.replace(hour=10, minute=0, second=0, microsecond=0)

    for i, platform in enumerate(ctx["enabled_platforms"]):
        field_name = f"planned_at_{platform}"
        check_field = f"schedule_{platform}"

        # Если чекбокс не отмечен — пропускаем
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
            # Дефолт: завтра 10:00 + i*30 мин
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
        # Записать в learnings
        insight = f"Отклонено ревьюером: {reason}"
        db.execute(
            """
            INSERT INTO learnings (project_id, insight, source, active)
            VALUES (?, ?, 'reject', 1)
            """,
            (project_id, insight),
        )

    return RedirectResponse("/review?flash=Контент+отклонён", status_code=303)
