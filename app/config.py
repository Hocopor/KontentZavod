"""
Настройки приложения из переменных окружения.
Загружается один раз при старте через python-dotenv.

Дизайн для тестируемости:
- При каждом обращении к атрибуту сначала проверяется экземплярный словарь
  _overrides (для monkeypatch.setattr / прямого присвоения), затем os.environ
  (для monkeypatch.setenv), затем встроенный дефолт.
- Это позволяет любому тест-файлу использовать ЛИБО monkeypatch.setenv,
  ЛИБО monkeypatch.setattr — без замены объекта settings и без хаков.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Ищем .env в корне проекта (на уровень выше app/)
_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")

# Схема: имя -> (env_var, default, type)
# type: 'str' | 'bool'
_FIELDS: dict[str, tuple[str, str, str]] = {
    "DB_PATH":             ("DB_PATH",             "data/kontentzavod.db",      "str"),
    "LLM_ROUTER_BASE_URL": ("LLM_ROUTER_BASE_URL", "http://127.0.0.1:8100/v1", "str"),
    "LLM_ROUTER_KEY":      ("LLM_ROUTER_KEY",       "",                         "str"),
    "FERNET_KEY":          ("FERNET_KEY",           "",                         "str"),
    # FAKE_LLM=1 -> llm.py возвращает заглушки без сетевых вызовов (для тестов)
    "FAKE_LLM":            ("FAKE_LLM",             "0",                        "bool"),
    # PUBLISH_DRY_RUN=1 -> не ходить в сеть, сохранять payload в data/outbox/
    # По умолчанию 1 (безопасный режим), явно задать 0 для реальных публикаций
    "PUBLISH_DRY_RUN":     ("PUBLISH_DRY_RUN",      "1",                        "bool"),
    # ENABLE_SCHEDULER=1 -> запустить APScheduler при старте приложения
    # По умолчанию 0 (тесты и dev не запускают фоновый планировщик)
    "ENABLE_SCHEDULER":    ("ENABLE_SCHEDULER",     "0",                        "bool"),
    "DATA_DIR":            ("DATA_DIR",             "data",                     "str"),
}


class Settings:
    """
    Контейнер настроек с двухуровневым lookup:
      1. self._overrides  -- явные присвоения (monkeypatch.setattr / прямой setattr)
      2. os.environ       -- переменные окружения (monkeypatch.setenv работает автоматически)
      3. default          -- встроенное значение

    Оба механизма работают одновременно, поэтому test_projects.py может использовать
    monkeypatch.setenv, а test_publishers.py -- monkeypatch.setattr, без конфликтов.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "_overrides", {})

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_overrides":
            object.__setattr__(self, name, value)
        else:
            object.__getattribute__(self, "_overrides")[name] = value

    def __getattribute__(self, name: str) -> object:
        # Пропускаем служебные атрибуты и методы через стандартный lookup
        if name.startswith("_") or name not in _FIELDS:
            return object.__getattribute__(self, name)
        # 1. _overrides (monkeypatch.setattr / прямое присвоение)
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        # 2. os.environ (monkeypatch.setenv)
        env_var, default, kind = _FIELDS[name]
        raw = os.environ.get(env_var, default)
        return raw == "1" if kind == "bool" else raw

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
