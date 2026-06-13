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
# type: 'str' | 'bool' | 'int'
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
    # ── Видеоконвейер (этап 3) ──
    # FAKE_TTS=1 -> tts.py не ходит в сеть: тишина через ffmpeg + синтетические тайминги
    "FAKE_TTS":            ("FAKE_TTS",             "0",                        "bool"),
    # FAKE_ASSETS=1 -> assets.py не ходит в сеть: плейсхолдеры через ffmpeg lavfi
    "FAKE_ASSETS":         ("FAKE_ASSETS",          "0",                        "bool"),
    "PEXELS_API_KEY":      ("PEXELS_API_KEY",       "",                         "str"),
    "PIXABAY_API_KEY":     ("PIXABAY_API_KEY",      "",                         "str"),
    # Сколько дней хранить финальные ролики после публикации (SSD 30GB!)
    "MEDIA_RETENTION_DAYS": ("MEDIA_RETENTION_DAYS", "14",                      "int"),
    "FFMPEG_BIN":          ("FFMPEG_BIN",           "ffmpeg",                   "str"),
    "FFPROBE_BIN":         ("FFPROBE_BIN",          "ffprobe",                  "str"),
    # YouTube Data API v3: 10 000 юнитов/день, videos.insert = 1600 юнитов → ~6 загрузок
    "YOUTUBE_DAILY_LIMIT": ("YOUTUBE_DAILY_LIMIT",  "6",                        "int"),
    # Шрифт и размер для наложения текста на слайды сторис (этап 8.5).
    # На сервере: "DejaVu Sans" или "Liberation Sans" (apt install fonts-dejavu).
    # Шрифт может содержать пробел — НЕ добавляем в _SINGLE_TOKEN_FIELDS.
    "STORY_FONT":          ("STORY_FONT",           "Arial",                    "str"),
    "STORY_FONT_SIZE":     ("STORY_FONT_SIZE",      "96",                       "int"),
    # Глобальный сдвиг таймингов субтитров в секундах (default 0.0).
    # >0 — субтитры позже, для подстройки систематического лида edge-tts WordBoundary.
    # Настраивается при приёмке без правки кода.
    "SUBTITLE_OFFSET_SEC": ("SUBTITLE_OFFSET_SEC",  "0.0",                      "float"),
    # Интервал авто-сбора метрик в часах (этап 8.7). default 6.
    # Джоб collect_metrics запускается раз в N часов с джиттером (антиблок),
    # а не daily cron. Тип int.
    "METRICS_INTERVAL_HOURS": ("METRICS_INTERVAL_HOURS", "6",                   "int"),
    # Уровень логирования для логгера "app" (DEBUG/INFO/WARNING).
    # INFO — видны успехи скачиваний, пропущенные джобы и т.д.
    "LOG_LEVEL":              ("LOG_LEVEL",              "INFO",                 "str"),
    # Политика egress для медиа-запросов (Pexels/Pixabay/Openverse/Wikimedia).
    # proxy_first — с РФ-IP прямой запрос всегда блокируется, начинаем с прокси.
    # direct_first — сначала прямой запрос, затем прокси (старое поведение).
    "MEDIA_EGRESS":           ("MEDIA_EGRESS",           "proxy_first",          "str"),
    # Перф контент-плана: сколько недельных LLM-вызовов плана делать параллельно (1 = последовательно).
    "PLAN_LLM_CONCURRENCY":   ("PLAN_LLM_CONCURRENCY",   "4",                    "int"),
    # Макс. дней плана, генерируемых за один тик мозга (план наполняется частями, тик короткий).
    "PLAN_TICK_MAX_DAYS":     ("PLAN_TICK_MAX_DAYS",     "14",                   "int"),
}

# Строковые поля, значения которых по своей природе не содержат пробелов
# (URL, API-ключи). Для них берём только первый токен значения: systemd
# (EnvironmentFile= в юните) НЕ удаляет inline-комментарии вида
# «KEY=значение  # пояснение» — без отсечения мусор уезжает в base_url/ключ
# (реальный случай на проде: 404 от LLM-Router). python-dotenv комментарии
# режет сам, поэтому локального запуска это не касается.
# FFMPEG_BIN/FFPROBE_BIN сюда НЕ входят — путь может содержать пробелы.
_SINGLE_TOKEN_FIELDS = {
    "LLM_ROUTER_BASE_URL",
    "LLM_ROUTER_KEY",
    "FERNET_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "LOG_LEVEL",
    "MEDIA_EGRESS",
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
        # bool/int — всегда одиночный токен: отсекаем systemd-хвост «# комментарий»
        if kind == "bool":
            return raw.strip().split()[0] == "1" if raw.strip() else False
        if kind == "int":
            return int(raw.strip().split()[0]) if raw.strip() else int(default)
        if kind == "float":
            return float(raw.strip().split()[0]) if raw.strip() else float(default)
        if name in _SINGLE_TOKEN_FIELDS:
            stripped = raw.strip()
            return stripped.split()[0] if stripped else ""
        return raw

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
