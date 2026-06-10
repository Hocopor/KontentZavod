"""
Дашборд — главная страница «/».

Сводка: проекты, ревью, планирование, ошибки, ручная очередь.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import get_db
from app.templates_env import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    with get_db() as db:
        # Активных проектов
        projects_total = db.execute(
            "SELECT COUNT(*) FROM projects WHERE status='active'"
        ).fetchone()[0]

        # На ревью
        review_count = db.execute(
            "SELECT COUNT(*) FROM content WHERE status='text_review'"
        ).fetchone()[0]

        # Approved без schedule (готово к планированию)
        approved_unscheduled = db.execute(
            """
            SELECT COUNT(*) FROM content c
             WHERE c.status='approved'
               AND NOT EXISTS (SELECT 1 FROM schedule s WHERE s.content_id = c.id)
            """
        ).fetchone()[0]

        # Запланировано на 7 дней
        scheduled_7d = db.execute(
            """
            SELECT COUNT(*) FROM schedule
             WHERE status IN ('planned','publishing')
               AND planned_at BETWEEN datetime('now') AND datetime('now','+7 days')
            """
        ).fetchone()[0]

        # Ошибок публикации
        errors_total = db.execute(
            "SELECT COUNT(*) FROM schedule WHERE status='error'"
        ).fetchone()[0]

        # В ручной очереди
        manual_count = db.execute(
            "SELECT COUNT(*) FROM schedule WHERE status='manual_pending'"
        ).fetchone()[0]

        # Последние ошибки (до 5)
        last_errors = db.execute(
            """
            SELECT s.id, s.platform, s.error_text, s.updated_at,
                   p.name AS project_name
              FROM schedule s
              JOIN content c ON c.id = s.content_id
              JOIN projects p ON p.id = c.project_id
             WHERE s.status = 'error'
             ORDER BY s.updated_at DESC
             LIMIT 5
            """
        ).fetchall()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "projects_total": projects_total,
            "review_count": review_count,
            "approved_unscheduled": approved_unscheduled,
            "scheduled_7d": scheduled_7d,
            "errors_total": errors_total,
            "manual_count": manual_count,
            "last_errors": [dict(e) for e in last_errors],
        },
    )
