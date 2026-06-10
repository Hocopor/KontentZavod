"""
Слой доступа к SQLite.

Принципы:
- Без ORM: чистый sqlite3, Row-фабрика для dict-like доступа.
- WAL-журнал для конкурентных чтений (APScheduler + веб).
- FOREIGN KEYS принудительно включены (SQLite по умолчанию их не проверяет).
- init_db() создаёт все таблицы через executescript(SCHEMA) и затем
  вызывает _migrate(conn) для ALTER TABLE и пересборок — идемпотентно.

Дефолтные настройки запуска проекта (хранятся в projects.settings JSON):
  plan_horizon_days — горизонт контент-плана в днях (дефолт 30)
  gen_lookahead_days — сколько дней вперёд генерировать контент (дефолт 3)
  retention_days     — хранить финальные ролики N дней после публикации (дефолт 14)
  plan_paused        — пауза контент-плана (0/1, дефолт 0)
  gen_paused         — пауза генерации (0/1, дефолт 0)
  autogen            — автоматическая генерация без ревью (0/1, дефолт 0)
"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from app.config import settings

# Дефолтные настройки запуска проекта (merge в get_project_settings)
DEFAULT_PROJECT_SETTINGS: dict = {
    "plan_horizon_days": 30,
    "gen_lookahead_days": 3,
    "retention_days": 14,
    "plan_paused": 0,
    "gen_paused": 0,
    "autogen": 0,
}

# ─── Инициализация ────────────────────────────────────────────────────────────

SCHEMA = """
-- Проекты пользователя
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    description TEXT,          -- что за продукт / услуга
    audience    TEXT,          -- целевая аудитория
    tone        TEXT,          -- тон голоса (деловой, дружелюбный, экспертный…)
    goals       TEXT,          -- цель продвижения / конверсии
    cta         TEXT,          -- целевое действие (подписка, заявка, покупка…)
    links       TEXT,          -- JSON: {"site": "...", "tg": "...", ...}
    themes      TEXT,          -- тематики для генерации идей (произвольный текст)
    forbidden   TEXT,          -- стоп-темы / чего избегать
    extra       TEXT,          -- доп. контекст для LLM
    status      TEXT    NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'archived')),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Площадки каждого проекта (5 фиксированных платформ per project)
CREATE TABLE IF NOT EXISTS project_platforms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    platform    TEXT    NOT NULL
                        CHECK (platform IN ('telegram','vk','youtube','instagram','dzen')),
    enabled     INTEGER NOT NULL DEFAULT 0,
    mode        TEXT    NOT NULL DEFAULT 'auto'
                        CHECK (mode IN ('auto', 'manual')),
    config      TEXT,          -- JSON: channel_id / group_id / имя канала и т.п.
    credentials TEXT,          -- Fernet-зашифрованный JSON с токенами (NULL = не заданы)
    UNIQUE (project_id, platform)
);

-- Идеи для контента
CREATE TABLE IF NOT EXISTS ideas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    text        TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'generated'
                        CHECK (source IN ('generated', 'manual')),
    status      TEXT    NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new', 'used', 'rejected')),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Контент (посты и видео)
CREATE TABLE IF NOT EXISTS content (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    idea_id       INTEGER REFERENCES ideas(id) ON DELETE SET NULL,
    type          TEXT    NOT NULL
                          CHECK (type IN ('post', 'video_footage', 'video_slideshow')),
    title         TEXT,
    -- texts: JSON per-платформа. Структура описана в llm.py docstring.
    texts         TEXT,
    -- files: JSON {"tts_path": "...", "video_path": "...", "preview_path": "..."}
    files         TEXT,
    -- features: JSON {"hook_type": "вопрос|факт|история|провокация",
    --                  "topic": "...", "length": "short|medium|long",
    --                  "format": "text|carousel|reel", "ab_variant": "A|B|null"}
    features      TEXT,
    status        TEXT    NOT NULL DEFAULT 'draft'
                          CHECK (status IN (
                              'draft','text_review','production',
                              'review','approved','rejected'
                          )),
    reject_reason TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Календарь публикаций
CREATE TABLE IF NOT EXISTS schedule (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id    INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
    platform      TEXT    NOT NULL,
    planned_at    TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'planned'
                          CHECK (status IN (
                              'planned','publishing','published','error',
                              'manual_pending','manual_done'
                          )),
    published_url TEXT,
    error_text    TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Метрики публикаций
CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES schedule(id) ON DELETE CASCADE,
    date        TEXT    NOT NULL,
    views       INTEGER NOT NULL DEFAULT 0,
    likes       INTEGER NOT NULL DEFAULT 0,
    comments    INTEGER NOT NULL DEFAULT 0,
    shares      INTEGER NOT NULL DEFAULT 0,
    watch_pct   REAL    NOT NULL DEFAULT 0.0
);

-- Аналитические инсайты (обучение системы)
CREATE TABLE IF NOT EXISTS learnings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    insight    TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'analyzer'
               CHECK (source IN ('analyzer', 'reject')),
    weight     REAL    NOT NULL DEFAULT 1.0,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Маркетинговые стратегии (версионируются, активна максимум одна)
CREATE TABLE IF NOT EXISTS strategies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'generating'
                 CHECK (status IN ('generating','active','archived','error')),
    strategy     TEXT,   -- JSON: {summary, positioning, platforms: {<platform>: {...}}}
    inputs       TEXT,   -- JSON: входные данные анализа (профиль, learnings, метрики)
    user_comment TEXT,   -- комментарий пользователя при пересмотре
    error_text   TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_id, version)
);

-- Контент-план: ячейки шахматки согласования
CREATE TABLE IF NOT EXISTS plan_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    strategy_id  INTEGER REFERENCES strategies(id) ON DELETE SET NULL,
    platform     TEXT NOT NULL,
    content_type TEXT NOT NULL,   -- ключ из app/catalog.py
    date         TEXT NOT NULL,   -- YYYY-MM-DD
    time_slot    TEXT,            -- HH:MM (рекомендованное время публикации)
    title        TEXT NOT NULL,
    brief        TEXT,            -- JSON: {hook, outline, cta, keywords[], rubric}
    status       TEXT NOT NULL DEFAULT 'proposed'
                 CHECK (status IN ('proposed','approved','rejected','generating','generated','error')),
    content_id   INTEGER REFERENCES content(id) ON DELETE SET NULL,
    error_text   TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Индексы для частых запросов
CREATE INDEX IF NOT EXISTS idx_projects_status      ON projects(status);
CREATE INDEX IF NOT EXISTS idx_ideas_project        ON ideas(project_id, status);
CREATE INDEX IF NOT EXISTS idx_content_project      ON content(project_id, status);
CREATE INDEX IF NOT EXISTS idx_content_updated      ON content(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_schedule_planned     ON schedule(planned_at, status);
CREATE INDEX IF NOT EXISTS idx_learnings_project    ON learnings(project_id, active);
-- Один замер метрик на публикацию в день (collector делает INSERT OR REPLACE)
CREATE UNIQUE INDEX IF NOT EXISTS idx_metrics_schedule_date ON metrics(schedule_id, date);
-- Индексы новых таблиц этапа 7
CREATE INDEX IF NOT EXISTS idx_strategies_project   ON strategies(project_id, status);
CREATE INDEX IF NOT EXISTS idx_plan_items_project_date ON plan_items(project_id, date);
CREATE INDEX IF NOT EXISTS idx_plan_items_status    ON plan_items(status, date);
"""

# ─── Миграции существующих таблиц ────────────────────────────────────────────


def _migrate(conn: sqlite3.Connection) -> None:
    """
    Идемпотентные миграции существующих таблиц.

    Вызывается из init_db() после executescript(SCHEMA).
    Каждая операция защищена от повторного выполнения (try/except или проверка).

    Миграции:
    1. projects + stage (draft|running|paused)
    2. projects + settings (JSON настроек запуска)
    3. project_platforms + content_types (JSON {type: bool})
    4. Пересборка content: расширяем CHECK type до ('post','story','article',
       'video_footage','video_slideshow') — SQLite не умеет менять CHECK,
       поэтому безопасная пересборка через content_new.
    """
    # 1. projects.stage
    try:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN stage TEXT NOT NULL DEFAULT 'draft'"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # колонка уже существует

    # 2. projects.settings
    try:
        conn.execute("ALTER TABLE projects ADD COLUMN settings TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # колонка уже существует

    # 3. project_platforms.content_types
    try:
        conn.execute(
            "ALTER TABLE project_platforms ADD COLUMN content_types TEXT"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass  # колонка уже существует

    # 4. Пересборка content: расширяем CHECK type
    # Проверяем, нужна ли пересборка (нет 'story' в определении таблицы)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='content'"
    ).fetchone()
    if row and "'story'" not in row[0]:
        # Отключаем FK для безопасного ребилда (на этом же соединении)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.commit()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS content_new (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    idea_id       INTEGER REFERENCES ideas(id) ON DELETE SET NULL,
                    type          TEXT    NOT NULL
                                          CHECK (type IN (
                                              'post','story','article',
                                              'video_footage','video_slideshow'
                                          )),
                    title         TEXT,
                    texts         TEXT,
                    files         TEXT,
                    features      TEXT,
                    status        TEXT    NOT NULL DEFAULT 'draft'
                                          CHECK (status IN (
                                              'draft','text_review','production',
                                              'review','approved','rejected'
                                          )),
                    reject_reason TEXT,
                    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO content_new SELECT * FROM content;
                DROP TABLE content;
                ALTER TABLE content_new RENAME TO content;
                CREATE INDEX IF NOT EXISTS idx_content_project ON content(project_id, status);
                CREATE INDEX IF NOT EXISTS idx_content_updated ON content(updated_at DESC);
            """)
            # Проверка целостности внешних ключей
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"Нарушение FK после пересборки content: {violations}"
                )
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.commit()


# ─── Соединение ───────────────────────────────────────────────────────────────


def _make_connection(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """
    Контекстный менеджер соединения с БД.

    Использование:
        with get_db() as db:
            rows = db.execute("SELECT * FROM projects").fetchall()
    """
    conn = _make_connection(settings.db_path_absolute)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── init_db ──────────────────────────────────────────────────────────────────


def init_db(db_path: str | Path | None = None) -> None:
    """
    Создаёт все таблицы и индексы (идемпотентно — IF NOT EXISTS),
    затем применяет _migrate() для ALTER TABLE и пересборок.
    Вызывается один раз при старте приложения (lifespan).
    """
    path = db_path or settings.db_path_absolute
    conn = _make_connection(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
    finally:
        conn.close()


# ─── Helpers ──────────────────────────────────────────────────────────────────


def query(
    sql: str,
    params: tuple | list | dict = (),
    db_path: str | Path | None = None,
) -> list[sqlite3.Row]:
    """Выполнить SELECT и вернуть список строк."""
    with get_db() as conn:
        return conn.execute(sql, params).fetchall()


def execute(
    sql: str,
    params: tuple | list | dict = (),
    db_path: str | Path | None = None,
) -> int:
    """
    Выполнить INSERT/UPDATE/DELETE.
    Возвращает lastrowid (для INSERT) или rowcount.
    """
    with get_db() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid or cur.rowcount


def get_project_settings(row_settings_json: str | None) -> dict:
    """
    Вернуть настройки запуска проекта, объединив дефолты с сохранёнными.

    Сохранённые значения из projects.settings (JSON) имеют приоритет;
    отсутствующие ключи берутся из DEFAULT_PROJECT_SETTINGS.

    Args:
        row_settings_json: значение поля projects.settings (может быть None)

    Returns:
        Словарь с полным набором ключей настроек.
    """
    saved: dict = {}
    if row_settings_json:
        try:
            saved = json.loads(row_settings_json)
        except (json.JSONDecodeError, TypeError):
            pass
    return {**DEFAULT_PROJECT_SETTINGS, **saved}
