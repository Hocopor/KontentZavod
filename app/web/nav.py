"""
Вспомогательные функции для навигации.
Инжектируются в Jinja2-контекст через middleware.
"""
from app.db import get_db


def get_nav_counts() -> dict:
    """Возвращает счётчики для бейджей навигации (флоу 2.0)."""
    try:
        with get_db() as db:
            # «Контент-план» — proposed пункты, ожидающие одобрения
            plan_proposed = db.execute(
                "SELECT COUNT(*) FROM plan_items WHERE status='proposed'"
            ).fetchone()[0]
            # «Публикация» — manual_pending в очереди
            manual_count = db.execute(
                "SELECT COUNT(*) FROM schedule WHERE status='manual_pending'"
            ).fetchone()[0]
        return {"nav_plan_proposed": plan_proposed, "nav_manual_count": manual_count}
    except Exception:
        return {"nav_plan_proposed": 0, "nav_manual_count": 0}
