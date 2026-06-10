"""
Сервис планирования и обработки публикаций.

schedule_content()  — создать запись в calendar (schedule).
process_due()       — один тик планировщика: обработать все просроченные записи.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.db import get_db
from app.publishers.base import PublishDeferred, PublishError, publish

logger = logging.getLogger(__name__)

# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # SQLite хранит без tzinfo


# ─── Планирование ─────────────────────────────────────────────────────────────


def schedule_content(content_id: int, platform: str, planned_at: datetime) -> int:
    """
    Создать запись расписания для content_id на указанную площадку и время.

    Валидация:
    - Контент должен иметь status='approved' (или 'text_review' — допускается на этапе
      планирования, т.к. approval может прийти позже).
    - Площадка должна быть включена у проекта контента.

    Возвращает id новой записи schedule.
    Поднимает ValueError при ошибке валидации.
    """
    with get_db() as db:
        # Проверяем контент
        row_c = db.execute(
            "SELECT id, project_id, status FROM content WHERE id = ?", (content_id,)
        ).fetchone()
        if row_c is None:
            raise ValueError(f"Контент id={content_id} не найден")

        allowed_statuses = {"approved", "text_review"}
        if row_c["status"] not in allowed_statuses:
            raise ValueError(
                f"Контент id={content_id} имеет статус '{row_c['status']}', "
                f"ожидается один из: {allowed_statuses}"
            )

        project_id = row_c["project_id"]

        # Проверяем площадку
        row_pp = db.execute(
            "SELECT enabled FROM project_platforms WHERE project_id = ? AND platform = ?",
            (project_id, platform),
        ).fetchone()
        if row_pp is None or not row_pp["enabled"]:
            raise ValueError(
                f"Площадка '{platform}' не включена для проекта id={project_id}"
            )

        # Вставляем запись
        cur = db.execute(
            """
            INSERT INTO schedule (content_id, platform, planned_at, status)
            VALUES (?, ?, ?, 'planned')
            """,
            (content_id, platform, planned_at.strftime("%Y-%m-%d %H:%M:%S")),
        )
        schedule_id = cur.lastrowid

    logger.info(
        "Запланировано: schedule_id=%s content_id=%s platform=%s at=%s",
        schedule_id,
        content_id,
        platform,
        planned_at,
    )
    return schedule_id


# ─── Обработка тика планировщика ──────────────────────────────────────────────


def process_due(now: datetime | None = None) -> None:
    """
    Один тик: обработать все просроченные записи schedule со status='planned'.

    - planned_at <= now → попытка публикации.
    - Успех → status='published', published_url.
    - PublishError, attempts < 3 → status='planned', attempts+1, planned_at += 5 мин.
    - PublishError, attempts >= 3 → status='error', error_text.
    - manual_pending не считается ошибкой.

    Вызывается из APScheduler каждую минуту И напрямую из тестов.
    """
    if now is None:
        now = _now_utc()

    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, attempts
              FROM schedule
             WHERE status = 'planned'
               AND planned_at <= ?
            """,
            (now_str,),
        ).fetchall()

    for row in rows:
        schedule_id: int = row["id"]
        attempts: int = row["attempts"]

        # Помечаем как 'publishing' (атомарно)
        with get_db() as db:
            db.execute(
                """
                UPDATE schedule
                   SET status     = 'publishing',
                       updated_at = datetime('now')
                 WHERE id = ? AND status = 'planned'
                """,
                (schedule_id,),
            )

        try:
            published_url = publish(schedule_id)

            # Ручные площадки: publish() сам выставил manual_pending — не трогаем
            if published_url.startswith("manual://"):
                logger.info(
                    "schedule_id=%s → manual_pending (ручная публикация)", schedule_id
                )
                continue

            # Успешная публикация
            with get_db() as db:
                db.execute(
                    """
                    UPDATE schedule
                       SET status        = 'published',
                           published_url = ?,
                           updated_at    = datetime('now')
                     WHERE id = ?
                    """,
                    (published_url, schedule_id),
                )
            logger.info("Опубликовано: schedule_id=%s url=%s", schedule_id, published_url)

        except PublishDeferred as exc:
            # Отложено (квота и т.п.) — переносим без увеличения attempts
            retry_str = exc.retry_at.strftime("%Y-%m-%d %H:%M:%S")
            with get_db() as db:
                db.execute(
                    """
                    UPDATE schedule
                       SET status     = 'planned',
                           planned_at = ?,
                           updated_at = datetime('now')
                     WHERE id = ?
                    """,
                    (retry_str, schedule_id),
                )
            logger.info(
                "Публикация отложена schedule_id=%s до %s: %s",
                schedule_id,
                retry_str,
                exc,
            )

        except PublishError as exc:
            new_attempts = attempts + 1
            if new_attempts < 3:
                # Повторная попытка через 5 минут
                new_planned_at = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
                with get_db() as db:
                    db.execute(
                        """
                        UPDATE schedule
                           SET status     = 'planned',
                               attempts   = ?,
                               planned_at = ?,
                               updated_at = datetime('now')
                         WHERE id = ?
                        """,
                        (new_attempts, new_planned_at, schedule_id),
                    )
                logger.warning(
                    "Ошибка публикации schedule_id=%s (попытка %s/3): %s. "
                    "Следующая попытка в %s",
                    schedule_id,
                    new_attempts,
                    exc,
                    new_planned_at,
                )
            else:
                # Финальная ошибка
                with get_db() as db:
                    db.execute(
                        """
                        UPDATE schedule
                           SET status     = 'error',
                               error_text = ?,
                               attempts   = ?,
                               updated_at = datetime('now')
                         WHERE id = ?
                        """,
                        (str(exc), new_attempts, schedule_id),
                    )
                logger.error(
                    "Публикация окончательно провалилась schedule_id=%s (3 попытки): %s",
                    schedule_id,
                    exc,
                )

        except Exception as exc:
            # Неожиданная ошибка — трактуем как PublishError
            new_attempts = attempts + 1
            error_text = f"Неожиданная ошибка: {exc}"
            if new_attempts < 3:
                new_planned_at = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
                with get_db() as db:
                    db.execute(
                        """
                        UPDATE schedule
                           SET status     = 'planned',
                               attempts   = ?,
                               planned_at = ?,
                               updated_at = datetime('now')
                         WHERE id = ?
                        """,
                        (new_attempts, new_planned_at, schedule_id),
                    )
                logger.exception(
                    "Неожиданная ошибка schedule_id=%s (попытка %s/3)",
                    schedule_id,
                    new_attempts,
                )
            else:
                with get_db() as db:
                    db.execute(
                        """
                        UPDATE schedule
                           SET status     = 'error',
                               error_text = ?,
                               attempts   = ?,
                               updated_at = datetime('now')
                         WHERE id = ?
                        """,
                        (error_text, new_attempts, schedule_id),
                    )
                logger.exception(
                    "Неожиданная ошибка schedule_id=%s (3 попытки) — финальная ошибка",
                    schedule_id,
                )
