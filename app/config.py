"""
Настройки приложения из переменных окружения.
Загружается один раз при старте через python-dotenv.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Ищем .env в корне проекта (на уровень выше app/)
_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")


class Settings:
    # --- База данных ---
    DB_PATH: str = os.getenv("DB_PATH", "data/kontentzavod.db")

    # --- LLM-Router ---
    LLM_ROUTER_BASE_URL: str = os.getenv("LLM_ROUTER_BASE_URL", "http://127.0.0.1:8100/v1")
    LLM_ROUTER_KEY: str = os.getenv("LLM_ROUTER_KEY", "")

    # --- Безопасность ---
    FERNET_KEY: str = os.getenv("FERNET_KEY", "")

    # --- Режим разработки ---
    # FAKE_LLM=1 → llm.py возвращает заглушки без сетевых вызовов (для тестов)
    FAKE_LLM: bool = os.getenv("FAKE_LLM", "0") == "1"

    # --- Публикация ---
    # PUBLISH_DRY_RUN=1 → не ходить в сеть, сохранять payload в data/outbox/
    # По умолчанию 1 (безопасный режим), явно задать 0 для реальных публикаций
    PUBLISH_DRY_RUN: bool = os.getenv("PUBLISH_DRY_RUN", "1") == "1"

    # --- Планировщик ---
    # ENABLE_SCHEDULER=1 → запустить APScheduler при старте приложения
    # По умолчанию 0 (тесты и dev не запускают фоновый планировщик)
    ENABLE_SCHEDULER: bool = os.getenv("ENABLE_SCHEDULER", "0") == "1"

    # --- Хранилище файлов ---
    DATA_DIR: str = os.getenv("DATA_DIR", "data")

    @property
    def db_path_absolute(self) -> Path:
        p = Path(self.DB_PATH)
        if not p.is_absolute():
            p = _BASE_DIR / p
        return p

    @property
    def data_dir_absolute(self) -> Path:
        p = Path(self.DATA_DIR)
        if not p.is_absolute():
            p = _BASE_DIR / p
        return p


settings = Settings()
