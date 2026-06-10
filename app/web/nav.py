"""
Вспомогательные функции для навигации.
Инжектируются в Jinja2-контекст через middleware.
"""
from app.db import get_db


def get_nav_counts() -> dict:
    """Возвращает счётчики для бейджей навигации."""
    try:
        with get_db() as db:
            review_count = db.execute(
                "SELECT COUNT(*) FROM content WHERE status='text_review'"
            ).fetchone()[0]
            manual_count = db.execute(
                "SELECT COUNT(*) FROM schedule WHERE status='manual_pending'"
            ).fetchone()[0]
        return {"nav_review_count": review_count, "nav_manual_count": manual_count}
    except Exception:
        return {"nav_review_count": 0, "nav_manual_count": 0}
