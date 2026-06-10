"""
APScheduler-планировщик КонтентЗавода.

Запускается ТОЛЬКО если ENABLE_SCHEDULER=1 (по умолчанию 0 — тесты/dev не затронуты).

Джобы:
  - process_due: каждую минуту → обрабатывает просроченные записи расписания.
  - process_production: каждую минуту → рендер видео (1 за тик).
  - process_brain: каждые 2 минуты → build стратегий + rolling-покрытие контент-плана (этап 7).
  - process_factory: каждую минуту → генерация контента по одобренным пунктам плана, 1 за тик (этап 7).
  - rotate_media: ежедневно 04:00 UTC → ротация медиафайлов.
  - collect_metrics: ежедневно 03:00 UTC → сбор метрик публикаций (VK/YouTube).
  - analyze_all: еженедельно пн 05:00 UTC → LLM-анализ метрик → learnings.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

import app.config as _cfg
from app.analytics.analyzer import analyze_all
from app.analytics.collector import collect_metrics
from app.pipeline.produce import process_production
from app.services.brain import process_brain
from app.services.cleanup import rotate_media
from app.services.factory import process_factory
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

    # Джоб видеопродакшна: каждую минуту, по одному ролику за тик
    _scheduler.add_job(
        process_production,
        trigger="interval",
        minutes=1,
        id="process_production",
        replace_existing=True,
        max_instances=1,  # рендер тяжёлый — не параллелить
    )

    # Джоб «мозга» (этап 7): build стратегий + rolling контент-план, лёгкий тик
    _scheduler.add_job(
        process_brain,
        trigger="interval",
        minutes=2,
        id="process_brain",
        replace_existing=True,
        max_instances=1,  # LLM-вызовы последовательные
    )

    # Джоб «фабрики» (этап 7): 1 одобренный пункт плана → контент за тик
    _scheduler.add_job(
        process_factory,
        trigger="interval",
        minutes=1,
        id="process_factory",
        replace_existing=True,
        max_instances=1,  # генерация тяжёлая — не параллелить
    )

    # Джоб ротации медиафайлов: ежедневно в 04:00 UTC
    _scheduler.add_job(
        rotate_media,
        trigger="cron",
        hour=4,
        minute=0,
        id="rotate_media",
        replace_existing=True,
        max_instances=1,
    )

    # Джоб сбора метрик: ежедневно в 03:00 UTC (до ротации медиа в 04:00)
    _scheduler.add_job(
        collect_metrics,
        trigger="cron",
        hour=3,
        minute=0,
        id="collect_metrics",
        replace_existing=True,
        max_instances=1,
    )

    # Джоб LLM-анализа метрик: еженедельно, понедельник 05:00 UTC
    _scheduler.add_job(
        analyze_all,
        trigger="cron",
        day_of_week="mon",
        hour=5,
        minute=0,
        id="analyze_all",
        replace_existing=True,
        max_instances=1,
    )

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
