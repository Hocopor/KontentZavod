"""
Хелпер «текущий проект» — читает cookie current_project и возвращает
активный проект из БД (или None при отсутствии cookie / удалённом / архивном).
"""
from fastapi import Request
from app.db import get_db


def get_current_project(request: Request) -> dict | None:
    """
    Читает cookie current_project (slug), достаёт активный проект из БД.
    Возвращает None если:
      - cookie нет
      - проект не найден
      - проект архивирован (status != 'active')
    """
    slug = request.cookies.get("current_project", "")
    if not slug:
        return None
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM projects WHERE slug=? AND status='active'",
                (slug,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)
    except Exception:
        return None
