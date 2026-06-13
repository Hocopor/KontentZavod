"""
HTMX-фрагменты для live-обновления UI (этап 8.8).

Решение: поллинг через HTMX (без SSE/вебсокетов) — стек уже на HTMX,
SQLite+APScheduler не дают pub/sub. Крупные зоны (борды /plan, /queue,
счётчики дашборда) обновляются опросом собственного GET-эндпоинта страницы
через hx-select + idiomorph morph (сохраняет скролл/фокус/открытые details).
Разрозненные счётчики навигации обновляются здесь — через OOB-свопы.

Маршруты:
    GET /partials/nav-badges — OOB-спаны бейджей навигации (plan proposed / manual pending)
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templates_env import templates
from app.web.nav import get_nav_counts

router = APIRouter(prefix="/partials")


@router.get("/nav-badges", response_class=HTMLResponse)
async def nav_badges(request: Request):
    """OOB-фрагмент бейджей навигации (поллинг каждые 30с из base.html)."""
    return templates.TemplateResponse(
        request,
        "partials/_nav_badges.html",
        {"_nav": get_nav_counts()},
    )
