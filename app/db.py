"""
Слой доступа к SQLite.

Принципы:
- Без ORM: чистый sqlite3, Row-фабрика для dict-like доступа.
- WAL-журнал для конкурентных чтений (APScheduler + веб).
- FOREIGN KEYS принудительно включены (SQLite по умолчанию их не проверяет).
- init_db() создаёт все таблицы сразу («нулевая миграция») — следующие волны
  не должны делать ALTER TABLE.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from app.config import settings

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

-- Индексы для частых запросов
CREATE INDEX IF NOT EXISTS idx_projects_status      ON projects(status);
CREATE INDEX IF NOT EXISTS idx_ideas_project        ON ideas(project_id, status);
CREATE INDEX IF NOT EXISTS idx_content_project      ON content(project_id, status);
CREATE INDEX IF NOT EXISTS idx_content_updated      ON content(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_schedule_planned     ON schedule(planned_at, status);
CREATE INDEX IF NOT EXISTS idx_learnings_project    ON learnings(project_id, active);
"""

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
    Создаёт все таблицы и индексы (идемпотентно — IF NOT EXISTS).
    Вызывается один раз при старте приложения (lifespan).
    """
    path = db_path or settings.db_path_absolute
    conn = _make_connection(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
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
