"""
Паблишер для ручных площадок (manual mode, instagram, dzen).

publish_manual() → выставляет schedule.status = 'manual_pending'.
  Публикации НЕ происходит — контент берётся UI ручной очереди (волна 3).

mark_manual_done(schedule_id, published_url) → 'manual_done'.
"""
import logging

from app.db import get_db

logger = logging.getLogger(__name__)


def publish_manual(schedule_id: int, platform: str, texts: dict) -> str:
    """
    Пометить запись как ожидающую ручной публикации.
    Возвращает pseudo-URL вида manual://{platform}/{schedule_id}.
    """
    with get_db() as db:
        db.execute(
            """
            UPDATE schedule
               SET status     = 'manual_pending',
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (schedule_id,),
        )
    url = f"manual://{platform}/{schedule_id}"
    logger.info(
        "Ручная публикация: schedule_id=%s platform=%s → manual_pending, url=%s",
        schedule_id,
        platform,
        url,
    )
    return url


def mark_manual_done(schedule_id: int, published_url: str) -> None:
    """
    Отметить ручную публикацию как выполненную (вызывается из UI ручной очереди).
    """
    with get_db() as db:
        db.execute(
            """
            UPDATE schedule
               SET status        = 'manual_done',
                   published_url = ?,
                   updated_at    = datetime('now')
             WHERE id = ?
            """,
            (published_url, schedule_id),
        )
    logger.info(
        "Ручная публикация завершена: schedule_id=%s url=%s",
        schedule_id,
        published_url,
    )
