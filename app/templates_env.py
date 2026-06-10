"""
Единственный экземпляр Jinja2Templates для всего приложения.

Содержит глобальную функцию nav_counts() — вызывается из base.html
для бейджей навигации.
"""
from pathlib import Path
from fastapi.templating import Jinja2Templates
from app.web.nav import get_nav_counts

_templates_dir = str(Path(__file__).parent / "templates")

templates = Jinja2Templates(directory=_templates_dir)

# Регистрируем функцию как глобальную — доступна в любом шаблоне как nav_counts()
templates.env.globals["get_nav_counts"] = get_nav_counts
