"""
Дашборд — главная страница «/».

Показывает сводку: проекты, контент в очереди, ошибки.
В ВОЛНЕ 1 — заглушка с подсчётом только проектов.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.db import get_db

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    with get_db() as db:
        projects_total = db.execute(
            "SELECT COUNT(*) FROM projects WHERE status='active'"
        ).fetchone()[0]
        drafts_total = db.execute(
            "SELECT COUNT(*) FROM content WHERE status IN ('draft','text_review')"
        ).fetchone()[0]
        errors_total = db.execute(
            "SELECT COUNT(*) FROM schedule WHERE status='error'"
        ).fetchone()[0]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "projects_total": projects_total,
            "drafts_total": drafts_total,
            "errors_total": errors_total,
        },
    )
