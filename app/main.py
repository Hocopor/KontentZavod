"""
Точка входа FastAPI-приложения «КонтентЗавод».

Запуск: uvicorn app.main:app --reload
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import init_db
from app.scheduler import start_scheduler, stop_scheduler
from app.web.dashboard import router as dashboard_router
from app.web.projects import router as projects_router
from app.web.generate import router as generate_router
from app.web.review import router as review_router
from app.web.calendar import router as calendar_router
from app.web.manual import router as manual_router
from app.web.files import router as files_router
from app.web.analytics import router as analytics_router
from app.web.launch import router as launch_router
from app.web.strategy import router as strategy_router
from app.web.plan import router as plan_router
from app.web.queue import router as queue_router


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
    app.include_router(generate_router)
    app.include_router(review_router)
    app.include_router(calendar_router)
    app.include_router(manual_router)
    app.include_router(files_router)
    app.include_router(analytics_router)
    app.include_router(launch_router)
    app.include_router(strategy_router)
    app.include_router(plan_router)
    app.include_router(queue_router)

    return app


app = create_app()
