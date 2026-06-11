"""
Тесты фундамента волны A этапа 7 (Завод 2.0).

Покрывает:
- app/catalog.py: хелперы каталога типов контента
- app/db.py: новые таблицы, миграции ALTER TABLE, пересборка content
- app/llm.py: FAKE-заглушки новых purposes
"""
import json
import sqlite3
import uuid

import pytest

import app.config as cfg_module
from app.catalog import allowed_types, default_types, is_valid, type_info, CONTENT_TYPES
from app.db import (
    DEFAULT_PROJECT_SETTINGS,
    get_project_settings,
    init_db,
    get_db,
)


# ─── Вспомогательные функции ─────────────────────────────────────────────────


def _unique_slug() -> str:
    return f"proj-{uuid.uuid4().hex[:8]}"


def _make_db(patch_env):
    """Инициализирует БД и возвращает путь."""
    path = cfg_module.settings.db_path_absolute
    init_db(path)
    return path


# ═════════════════════════════════════════════════════════════════════════════
# 1. Каталог типов контента
# ═════════════════════════════════════════════════════════════════════════════


class TestCatalog:
    """Тесты app/catalog.py."""

    def test_allowed_types_telegram(self):
        """Telegram — только post и video."""
        types = allowed_types("telegram")
        assert set(types.keys()) == {"post", "video"}

    def test_allowed_types_vk(self):
        """VK — post, video, story."""
        types = allowed_types("vk")
        assert set(types.keys()) == {"post", "video", "story"}

    def test_allowed_types_instagram(self):
        """Instagram — post, story, reel."""
        types = allowed_types("instagram")
        assert set(types.keys()) == {"post", "story", "reel"}

    def test_allowed_types_youtube(self):
        """YouTube — только short."""
        types = allowed_types("youtube")
        assert set(types.keys()) == {"short"}

    def test_allowed_types_dzen(self):
        """Дзен — post, article."""
        types = allowed_types("dzen")
        assert set(types.keys()) == {"post", "article"}

    def test_allowed_types_unknown_platform(self):
        """Неизвестная платформа → пустой словарь."""
        assert allowed_types("tiktok") == {}

    def test_default_types_telegram(self):
        """Telegram: оба типа включены по умолчанию."""
        defaults = default_types("telegram")
        assert defaults == {"post": True, "video": True}

    def test_default_types_vk(self):
        """VK: story выключена по умолчанию."""
        defaults = default_types("vk")
        assert defaults["post"] is True
        assert defaults["video"] is True
        assert defaults["story"] is False

    def test_default_types_instagram(self):
        """Instagram: все три включены по умолчанию."""
        defaults = default_types("instagram")
        assert defaults == {"post": True, "story": True, "reel": True}

    def test_default_types_dzen(self):
        """Дзен: article выключена по умолчанию."""
        defaults = default_types("dzen")
        assert defaults["post"] is True
        assert defaults["article"] is False

    def test_default_types_unknown_platform(self):
        """Неизвестная платформа → пустой словарь."""
        assert default_types("myspace") == {}

    def test_is_valid_correct(self):
        """Корректные пары платформа/тип → True."""
        assert is_valid("telegram", "post") is True
        assert is_valid("telegram", "video") is True
        assert is_valid("vk", "story") is True
        assert is_valid("instagram", "reel") is True
        assert is_valid("youtube", "short") is True
        assert is_valid("dzen", "article") is True

    def test_is_valid_wrong_type(self):
        """Тип, не существующий для платформы → False."""
        assert is_valid("telegram", "reel") is False
        assert is_valid("youtube", "post") is False
        assert is_valid("dzen", "video") is False

    def test_is_valid_unknown_platform(self):
        """Неизвестная платформа → False."""
        assert is_valid("tiktok", "post") is False

    def test_type_info_returns_dict(self):
        """type_info возвращает словарь с нужными ключами."""
        info = type_info("telegram", "post")
        assert info is not None
        assert "label" in info
        assert "kind" in info
        assert "publish" in info
        assert "default_on" in info

    def test_type_info_none_for_missing(self):
        """Несуществующий тип → None."""
        assert type_info("telegram", "reel") is None
        assert type_info("unknown", "post") is None

    def test_type_info_publish_values(self):
        """Telegram/VK video — auto; Instagram — manual."""
        assert type_info("telegram", "video")["publish"] == "auto"
        assert type_info("instagram", "post")["publish"] == "manual"
        assert type_info("instagram", "reel")["publish"] == "manual"

    def test_type_info_kind_values(self):
        """kind соответствует ожиданиям."""
        assert type_info("telegram", "post")["kind"] == "text"
        assert type_info("telegram", "video")["kind"] == "video"
        assert type_info("vk", "story")["kind"] == "story"
        assert type_info("instagram", "reel")["kind"] == "video"
        assert type_info("dzen", "article")["kind"] == "text"


# ═════════════════════════════════════════════════════════════════════════════
# 2. Миграции и новые таблицы БД
# ═════════════════════════════════════════════════════════════════════════════


class TestDBMigrations:
    """Тесты миграций db.py."""

    def test_init_db_idempotent(self, patch_env):
        """init_db дважды подряд — не выбрасывает исключений."""
        path = cfg_module.settings.db_path_absolute
        init_db(path)
        init_db(path)  # второй вызов — идемпотентно

    def test_strategies_table_exists(self, patch_env):
        """Таблица strategies создана после init_db."""
        _make_db(patch_env)
        with get_db() as db:
            row = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='strategies'"
            ).fetchone()
            assert row is not None

    def test_plan_items_table_exists(self, patch_env):
        """Таблица plan_items создана после init_db."""
        _make_db(patch_env)
        with get_db() as db:
            row = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='plan_items'"
            ).fetchone()
            assert row is not None

    def test_projects_stage_column_exists(self, patch_env):
        """Колонка projects.stage добавлена миграцией."""
        _make_db(patch_env)
        with get_db() as db:
            cols = [r[1] for r in db.execute("PRAGMA table_info(projects)").fetchall()]
            assert "stage" in cols

    def test_projects_settings_column_exists(self, patch_env):
        """Колонка projects.settings добавлена миграцией."""
        _make_db(patch_env)
        with get_db() as db:
            cols = [r[1] for r in db.execute("PRAGMA table_info(projects)").fetchall()]
            assert "settings" in cols

    def test_project_platforms_content_types_column_exists(self, patch_env):
        """Колонка project_platforms.content_types добавлена миграцией."""
        _make_db(patch_env)
        with get_db() as db:
            cols = [r[1] for r in db.execute("PRAGMA table_info(project_platforms)").fetchall()]
            assert "content_types" in cols

    def test_insert_strategy(self, patch_env):
        """Вставка в strategies работает."""
        _make_db(patch_env)
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO projects (slug, name) VALUES (?, ?)",
                (_unique_slug(), "Тест"),
            )
            proj_id = cur.lastrowid
            db.execute(
                "INSERT INTO strategies (project_id, version, status) VALUES (?, 1, 'generating')",
                (proj_id,),
            )
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM strategies WHERE project_id=?", (proj_id,)
            ).fetchone()
            assert row is not None
            assert row["status"] == "generating"

    def test_strategies_unique_version(self, patch_env):
        """UNIQUE (project_id, version) в strategies работает."""
        _make_db(patch_env)
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO projects (slug, name) VALUES (?, ?)",
                (_unique_slug(), "Тест"),
            )
            proj_id = cur.lastrowid
            db.execute(
                "INSERT INTO strategies (project_id, version) VALUES (?, 1)",
                (proj_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            with get_db() as db:
                db.execute(
                    "INSERT INTO strategies (project_id, version) VALUES (?, 1)",
                    (proj_id,),
                )

    def test_insert_plan_item(self, patch_env):
        """Вставка в plan_items работает."""
        _make_db(patch_env)
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO projects (slug, name) VALUES (?, ?)",
                (_unique_slug(), "Тест"),
            )
            proj_id = cur.lastrowid
            db.execute(
                """INSERT INTO plan_items
                   (project_id, platform, content_type, date, title, status)
                   VALUES (?, 'telegram', 'post', '2026-06-15', 'Тестовый план', 'proposed')""",
                (proj_id,),
            )
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM plan_items WHERE project_id=?", (proj_id,)
            ).fetchone()
            assert row is not None
            assert row["content_type"] == "post"
            assert row["status"] == "proposed"

    def test_content_type_story_allowed(self, patch_env):
        """После миграции можно вставить content с type='story'."""
        _make_db(patch_env)
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO projects (slug, name) VALUES (?, ?)",
                (_unique_slug(), "Тест"),
            )
            proj_id = cur.lastrowid
            db.execute(
                "INSERT INTO content (project_id, type, title) VALUES (?, 'story', 'История')",
                (proj_id,),
            )
        with get_db() as db:
            row = db.execute(
                "SELECT type FROM content WHERE project_id=?", (proj_id,)
            ).fetchone()
            assert row["type"] == "story"

    def test_content_type_article_allowed(self, patch_env):
        """После миграции можно вставить content с type='article'."""
        _make_db(patch_env)
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO projects (slug, name) VALUES (?, ?)",
                (_unique_slug(), "Тест"),
            )
            proj_id = cur.lastrowid
            db.execute(
                "INSERT INTO content (project_id, type, title) VALUES (?, 'article', 'Статья')",
                (proj_id,),
            )
        with get_db() as db:
            row = db.execute(
                "SELECT type FROM content WHERE project_id=?", (proj_id,)
            ).fetchone()
            assert row["type"] == "article"

    def test_content_type_invalid_raises(self, patch_env):
        """Вставка content с мусорным type → IntegrityError."""
        _make_db(patch_env)
        with pytest.raises(sqlite3.IntegrityError):
            with get_db() as db:
                cur = db.execute(
                    "INSERT INTO projects (slug, name) VALUES (?, ?)",
                    (_unique_slug(), "Тест"),
                )
                proj_id = cur.lastrowid
                db.execute(
                    "INSERT INTO content (project_id, type) VALUES (?, 'tweet')",
                    (proj_id,),
                )

    def test_projects_stage_default(self, patch_env):
        """Новый проект получает stage='draft' по умолчанию."""
        _make_db(patch_env)
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO projects (slug, name) VALUES (?, ?)",
                (_unique_slug(), "Тест"),
            )
            proj_id = cur.lastrowid
        with get_db() as db:
            row = db.execute(
                "SELECT stage FROM projects WHERE id=?", (proj_id,)
            ).fetchone()
            assert row["stage"] == "draft"

    def test_get_project_settings_defaults(self):
        """get_project_settings(None) возвращает полный набор дефолтов."""
        s = get_project_settings(None)
        assert s["plan_horizon_days"] == 30
        assert s["gen_lookahead_days"] == 3
        assert s["retention_days"] == 14
        assert s["plan_paused"] == 0
        assert s["gen_paused"] == 0
        assert s["autogen"] == 0

    def test_get_project_settings_merge(self):
        """Сохранённые значения имеют приоритет над дефолтами."""
        saved = json.dumps({"plan_horizon_days": 60, "autogen": 1})
        s = get_project_settings(saved)
        assert s["plan_horizon_days"] == 60
        assert s["autogen"] == 1
        # остальные — из дефолтов
        assert s["gen_lookahead_days"] == 3
        assert s["retention_days"] == 14

    def test_get_project_settings_empty_json(self):
        """Пустой JSON → только дефолты."""
        s = get_project_settings("{}")
        assert s == DEFAULT_PROJECT_SETTINGS

    def test_get_project_settings_invalid_json(self):
        """Битый JSON → только дефолты (не падает)."""
        s = get_project_settings("not-json")
        assert s == DEFAULT_PROJECT_SETTINGS


# ═════════════════════════════════════════════════════════════════════════════
# 3. Миграция content на «старой» БД
# ═════════════════════════════════════════════════════════════════════════════


class TestContentRebuild:
    """Тест пересборки content при миграции с «старой» БД."""

    def test_rebuild_from_old_schema(self, tmp_path):
        """
        Создаём вручную старую таблицу content со старым CHECK
        (без 'story'), добавляем данные + зависимую запись schedule.
        После init_db:
        - старые данные сохранились
        - story вставляется
        - FK живы (schedule.content_id ссылается на content.id)
        """
        import os
        db_file = str(tmp_path / "old_schema.db")
        # Временно подменим DB_PATH
        old_path = os.environ.get("DB_PATH", "")
        os.environ["DB_PATH"] = db_file
        try:
            # Создаём старую схему вручную
            conn = sqlite3.connect(db_file)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript("""
                CREATE TABLE projects (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug       TEXT    NOT NULL UNIQUE,
                    name       TEXT    NOT NULL,
                    status     TEXT    NOT NULL DEFAULT 'active',
                    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE ideas (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    text       TEXT    NOT NULL,
                    source     TEXT    NOT NULL DEFAULT 'generated',
                    status     TEXT    NOT NULL DEFAULT 'new',
                    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE content (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    idea_id       INTEGER REFERENCES ideas(id) ON DELETE SET NULL,
                    type          TEXT    NOT NULL
                                          CHECK (type IN ('post', 'video_footage', 'video_slideshow')),
                    title         TEXT,
                    texts         TEXT,
                    files         TEXT,
                    features      TEXT,
                    status        TEXT    NOT NULL DEFAULT 'draft',
                    reject_reason TEXT,
                    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
                    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE schedule (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_id    INTEGER NOT NULL REFERENCES content(id) ON DELETE CASCADE,
                    platform      TEXT    NOT NULL,
                    planned_at    TEXT    NOT NULL,
                    status        TEXT    NOT NULL DEFAULT 'planned',
                    published_url TEXT,
                    error_text    TEXT,
                    attempts      INTEGER NOT NULL DEFAULT 0,
                    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
                );
            """)
            # Вставляем проект и контент
            conn.execute("INSERT INTO projects (slug, name) VALUES ('old-proj', 'Старый проект')")
            conn.execute(
                "INSERT INTO content (project_id, type, title) VALUES (1, 'post', 'Старый пост')"
            )
            # Зависимая запись в schedule
            conn.execute(
                "INSERT INTO schedule (content_id, platform, planned_at) "
                "VALUES (1, 'telegram', '2026-06-15 09:00')"
            )
            conn.commit()
            conn.close()

            # Запускаем init_db — должна отработать пересборка
            init_db(db_file)

            # Проверяем результаты
            conn2 = sqlite3.connect(db_file)
            conn2.row_factory = sqlite3.Row
            conn2.execute("PRAGMA foreign_keys=ON")

            # Старые данные на месте
            row = conn2.execute(
                "SELECT title FROM content WHERE project_id=1"
            ).fetchone()
            assert row is not None
            assert row["title"] == "Старый пост"

            # schedule.content_id → FK жив
            sched = conn2.execute(
                "SELECT content_id FROM schedule WHERE platform='telegram'"
            ).fetchone()
            assert sched["content_id"] == 1

            # story теперь можно вставить
            conn2.execute(
                "INSERT INTO content (project_id, type, title) "
                "VALUES (1, 'story', 'Новая история')"
            )
            conn2.commit()

            row2 = conn2.execute(
                "SELECT type FROM content WHERE title='Новая история'"
            ).fetchone()
            assert row2["type"] == "story"

            conn2.close()
        finally:
            os.environ["DB_PATH"] = old_path


# ═════════════════════════════════════════════════════════════════════════════
# 4. FAKE-заглушки LLM новых purposes
# ═════════════════════════════════════════════════════════════════════════════


class TestFakeLLM:
    """Тесты FAKE-заглушек в app/llm.py для новых purposes этапа 7."""

    def _chat(self, purpose: str) -> dict:
        from app.llm import chat
        raw = chat([{"role": "user", "content": "test"}], purpose=purpose)
        return json.loads(raw)

    def test_profile_structure(self, patch_env):
        """purpose='profile' → валидный JSON с обязательными ключами."""
        data = self._chat("profile")
        for key in ("audience", "tone", "cta", "themes", "forbidden", "extra"):
            assert key in data, f"Отсутствует ключ '{key}' в profile"
            assert isinstance(data[key], str)
            assert data[key]  # не пустая строка

    def test_strategy_structure(self, patch_env):
        """purpose='strategy' (v2) → JSON с фазами, площадками и корректными типами в mix."""
        data = self._chat("strategy")
        assert "summary" in data
        assert "positioning" in data
        assert "platforms" in data
        assert "phases" in data

        # Фазы v2
        phases = data["phases"]
        assert isinstance(phases, list) and len(phases) >= 1
        for ph in phases:
            assert "weeks" in ph and "goal_share" in ph and "mix" in ph
            assert sum(ph["goal_share"].values()) == 100
            for platform, types in ph["mix"].items():
                for ctype in types:
                    assert is_valid(platform, ctype), (
                        f"phase mix: тип '{ctype}' не в каталоге для '{platform}'"
                    )

        platforms = data["platforms"]
        expected_platforms = {"telegram", "vk", "instagram", "youtube", "dzen"}
        assert set(platforms.keys()) == expected_platforms, (
            f"Ожидались платформы {expected_platforms}, получено {set(platforms.keys())}"
        )

        for platform, info in platforms.items():
            assert "goals" in info
            assert "rubrics" in info
            assert isinstance(info["rubrics"], list)
            # rubrics — объекты {name, goal, description}
            for r in info["rubrics"]:
                assert isinstance(r, dict)
                assert "name" in r and "goal" in r
            assert "best_times" in info
            assert isinstance(info["best_times"], list)
            assert "kpi" in info
            # content_mix в platforms НЕ задаётся (материализует код build_strategy)
            assert "content_mix" not in info

    def test_strategy_revise_structure(self, patch_env):
        """purpose='strategy_revise' (v2) → фазы + площадки + changes_summary."""
        data = self._chat("strategy_revise")
        assert "summary" in data
        assert "platforms" in data
        assert "phases" in data
        assert set(data["platforms"].keys()) == {"telegram", "vk", "instagram", "youtube", "dzen"}
        assert "changes_summary" in data
        assert isinstance(data["changes_summary"], str)
        assert data["changes_summary"]

    def test_strategy_revise_phase_mix_valid(self, patch_env):
        """purpose='strategy_revise' — mix фаз только из каталога."""
        data = self._chat("strategy_revise")
        for ph in data["phases"]:
            for platform, types in ph["mix"].items():
                for ctype in types:
                    assert is_valid(platform, ctype), (
                        f"strategy_revise phase mix: тип '{ctype}' не в каталоге для '{platform}'"
                    )

    def test_plan_structure(self, patch_env):
        """purpose='plan' v2 → JSON items с slot_id/title/brief (без date/type)."""
        data = self._chat("plan")
        assert "items" in data
        items = data["items"]
        assert len(items) >= 5, f"Ожидали ≥5 items, получено {len(items)}"

        slot_ids = set()
        for item in items:
            assert "slot_id" in item
            assert "title" in item
            assert "brief" in item
            slot_ids.add(item["slot_id"])

            brief = item["brief"]
            for key in ("hook", "outline", "cta", "keywords", "rubric"):
                assert key in brief, f"Отсутствует ключ '{key}' в brief"
            assert isinstance(brief["keywords"], list)

        # slot_id уникальны (планнер джойнит по ним)
        assert len(slot_ids) == len(items)

    def test_item_post_structure(self, patch_env):
        """purpose='item_post' → JSON с title, text, hashtags, features."""
        data = self._chat("item_post")
        assert "title" in data
        assert "text" in data
        assert "hashtags" in data
        assert "features" in data

        assert isinstance(data["hashtags"], list)

        features = data["features"]
        for key in ("hook_type", "topic", "length", "format", "ab_variant"):
            assert key in features

    def test_item_story_structure(self, patch_env):
        """purpose='item_story' → JSON с нужными ключами."""
        data = self._chat("item_story")
        assert "title" in data
        assert "image_prompt" in data
        assert "overlay_text" in data
        assert "caption" in data
        assert "features" in data

        # image_prompt должен быть на английском (проверяем наличие латиницы)
        assert any(c.isalpha() and ord(c) < 128 for c in data["image_prompt"]), (
            "image_prompt должен содержать английский текст"
        )

        features = data["features"]
        for key in ("hook_type", "topic", "length", "format", "ab_variant"):
            assert key in features
        assert features["format"] == "story"

    def test_unknown_purpose_returns_string(self, patch_env):
        """Неизвестный purpose → строка 'FAKE_LLM response'."""
        from app.llm import chat
        result = chat([{"role": "user", "content": "x"}], purpose="unknown_xyz")
        assert result == "FAKE_LLM response"
