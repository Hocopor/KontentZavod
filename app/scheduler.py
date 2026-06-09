"""
APScheduler-планировщик КонтентЗавода.

Запускается ТОЛЬКО если ENABLE_SCHEDULER=1 (по умолчанию 0 — тесты/dev не затронуты).

Джобы:
  - process_due: каждую минуту → обрабатывает просроченные записи расписания.
  # TODO: сбор метрик публикаций (views, likes, comments) — добавить в волне 4.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

import app.config as _cfg
from app.services.scheduling import process_due

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    """
    Создать и запустить BackgroundScheduler.
    Вызывается из lifespan при ENABLE_SCHEDULER=1.
    Повторный вызов — no-op (идемпотентно).
    """
    global _scheduler

    if not _cfg.settings.ENABLE_SCHEDULER:
        logger.debug("ENABLE_SCHEDULER=0 — планировщик не запущен")
        return

    if _scheduler is not None and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(timezone="UTC")

    # Джоб публикации: каждую минуту
    _scheduler.add_job(
        process_due,
        trigger="interval",
        minutes=1,
        id="process_due",
        replace_existing=True,
        max_instances=1,  # не запускать параллельно
    )

    # TODO: джоб сбора метрик (волна 4)
    # _scheduler.add_job(collect_metrics, trigger="interval", hours=1, id="collect_metrics")

    _scheduler.start()
    logger.info("Планировщик запущен (process_due каждую минуту)")


def stop_scheduler() -> None:
    """
    Остановить планировщик. Вызывается из lifespan при завершении.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Планировщик остановлен")
    _scheduler = None
