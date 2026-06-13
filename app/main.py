"""
Точка входа FastAPI-приложения «КонтентЗавод».

Запуск: uvicorn app.main:app --reload
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings


def _configure_logging() -> None:
    """Включает логи нашего кода (логгер 'app' и его дети) на уровне settings.LOG_LEVEL.
    uvicorn-логгеры не трогаем."""
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s: %(message)s"))
        app_logger.addHandler(handler)
    app_logger.propagate = False
from app.db import init_db
from app.scheduler import start_scheduler, stop_scheduler
from app.web.dashboard import router as dashboard_router
from app.web.projects import router as projects_router
from app.web.files import router as files_router
from app.web.analytics import router as analytics_router
from app.web.launch import router as launch_router
from app.web.strategy import router as strategy_router
from app.web.plan import router as plan_router
from app.web.queue import router as queue_router
from app.web.proxies import router as proxies_router
from app.web.tts_preview import router as tts_preview_router
from app.web.partials import router as partials_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Создать каталог data/ если нет
    settings.data_dir_absolute.mkdir(parents=True, exist_ok=True)
    # Инициализировать схему БД
    init_db()
    # Запустить планировщик (только если ENABLE_SCHEDULER=1)
    start_scheduler()
    yield
    # Остановить планировщик при завершении
    stop_scheduler()


def create_app() -> FastAPI:
    _configure_logging()
    app = FastAPI(
        title="КонтентЗавод",
        description="Генерация и публикация контента для нескольких проектов",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )

    # Статика
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Роутеры
    app.include_router(dashboard_router)
    app.include_router(projects_router)
    app.include_router(files_router)
    app.include_router(analytics_router)
    app.include_router(launch_router)
    app.include_router(strategy_router)
    app.include_router(plan_router)
    app.include_router(queue_router)
    app.include_router(proxies_router)
    app.include_router(tts_preview_router)
    app.include_router(partials_router)

    return app


app = create_app()
