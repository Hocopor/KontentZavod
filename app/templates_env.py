"""
Единственный экземпляр Jinja2Templates для всего приложения.

Содержит глобальную функцию nav_counts() — вызывается из base.html
для бейджей навигации.
"""
import time
from pathlib import Path
from fastapi.templating import Jinja2Templates
from app.web.nav import get_nav_counts

_templates_dir = str(Path(__file__).parent / "templates")

templates = Jinja2Templates(directory=_templates_dir)

# Регистрируем функцию как глобальную — доступна в любом шаблоне как nav_counts()
templates.env.globals["get_nav_counts"] = get_nav_counts

# Epoch-метка серверного времени — тикающие часы в base.html
templates.env.globals["now_epoch"] = time.time


def get_nav_projects() -> list[dict]:
    """
    Список активных проектов для выпадающего меню в шапке.
    Graceful при отсутствии таблицы (как get_nav_counts).
    """
    try:
        from app.db import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT id, slug, name FROM projects WHERE status='active' ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


templates.env.globals["get_nav_projects"] = get_nav_projects
