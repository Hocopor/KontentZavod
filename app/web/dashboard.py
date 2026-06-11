"""
Дашборд — главная страница «/».

Сводка по воркфлоу 2.0: проекты → план → фабрика → публикация.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import settings
from app.db import get_db
from app.templates_env import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    with get_db() as db:
        # Проекты в работе (stage='running')
        projects_running = db.execute(
            "SELECT COUNT(*) FROM projects WHERE stage='running'"
        ).fetchone()[0]

        # Ждут одобрения — plan_items proposed
        plan_proposed = db.execute(
            "SELECT COUNT(*) FROM plan_items WHERE status='proposed'"
        ).fetchone()[0]

        # Готово к публикации — plan_items generated
        items_generated = db.execute(
            "SELECT COUNT(*) FROM plan_items WHERE status='generated'"
        ).fetchone()[0]

        # Опубликовано за 7 дней
        published_7d = db.execute(
            """
            SELECT COUNT(*) FROM schedule
             WHERE status='published'
               AND updated_at >= datetime('now', '-7 days')
            """
        ).fetchone()[0]

        # В ручной публикации
        manual_count = db.execute(
            "SELECT COUNT(*) FROM schedule WHERE status='manual_pending'"
        ).fetchone()[0]

        # Ошибки — plan_items error + schedule error
        plan_errors = db.execute(
            "SELECT COUNT(*) FROM plan_items WHERE status='error'"
        ).fetchone()[0]
        sched_errors = db.execute(
            "SELECT COUNT(*) FROM schedule WHERE status='error'"
        ).fetchone()[0]
        errors_total = plan_errors + sched_errors

        # Последние ошибки (до 5) — из schedule
        last_errors_sched = db.execute(
            """
            SELECT s.id, s.platform, s.error_text, s.updated_at,
                   p.name AS project_name, 'schedule' AS source
              FROM schedule s
              JOIN content c ON c.id = s.content_id
              JOIN projects p ON p.id = c.project_id
             WHERE s.status = 'error'
             ORDER BY s.updated_at DESC
             LIMIT 5
            """
        ).fetchall()

        # Последние ошибки из plan_items (до 5 итого с учётом schedule)
        last_errors_plan = db.execute(
            """
            SELECT pi.id, pi.platform, pi.error_text, pi.updated_at,
                   p.name AS project_name, 'plan_item' AS source
              FROM plan_items pi
              JOIN projects p ON p.id = pi.project_id
             WHERE pi.status = 'error'
             ORDER BY pi.updated_at DESC
             LIMIT 5
            """
        ).fetchall()

    # Объединяем и берём до 5 свежих
    all_errors = sorted(
        [dict(e) for e in last_errors_sched] + [dict(e) for e in last_errors_plan],
        key=lambda x: x.get("updated_at") or "",
        reverse=True,
    )[:5]

    scheduler_enabled = bool(getattr(settings, "ENABLE_SCHEDULER", False))

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "projects_running": projects_running,
            "plan_proposed": plan_proposed,
            "items_generated": items_generated,
            "published_7d": published_7d,
            "manual_count": manual_count,
            "errors_total": errors_total,
            "last_errors": all_errors,
            "scheduler_enabled": scheduler_enabled,
        },
    )
