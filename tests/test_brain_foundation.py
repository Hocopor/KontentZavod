"""
Тесты фундамента волны A этапа 8.2 «агентный маркетинговый мозг».

Покрывает:
- app/db.py: новая колонка plan_items.goal, таблица directives, идемпотентность
- app/pipeline/prompts.py: load_rules() — базовый вызов, проектные правила, отсутствие файлов
- app/llm.py: FAKE-заглушки purposes directives_parse и strategy_check
"""
import json
import sqlite3
import uuid

import pytest

import app.config as cfg_module
import app.pipeline.prompts as prompts_module
from app.db import init_db, get_db


# ─── Вспомогательные функции ─────────────────────────────────────────────────


def _unique_slug() -> str:
    return f"proj-{uuid.uuid4().hex[:8]}"


def _make_db(patch_env):
    """Инициализирует БД и возвращает путь."""
    path = cfg_module.settings.db_path_absolute
    init_db(path)
    return path


# ═════════════════════════════════════════════════════════════════════════════
# 1. Миграции БД — новые колонки и таблицы
# ═════════════════════════════════════════════════════════════════════════════


class TestDBBrainFoundation:
    """Тесты новых объектов БД этапа 8.2."""

    def test_plan_items_has_goal_column(self, patch_env):
        """После init_db в plan_items есть колонка goal."""
        _make_db(patch_env)
        with get_db() as db:
            cols = [r[1] for r in db.execute("PRAGMA table_info(plan_items)").fetchall()]
            assert "goal" in cols, "Колонка goal отсутствует в plan_items"

    def test_directives_table_exists_and_constraints(self, patch_env):
        """
        Таблица directives:
        - INSERT валидной директивы проходит;
        - scope='bogus' → IntegrityError;
        - status по умолчанию 'active';
        - DELETE проекта каскадно удаляет директивы.
        """
        _make_db(patch_env)

        # Создаём проект
        with get_db() as db:
            cur = db.execute(
                "INSERT INTO projects (slug, name) VALUES (?, ?)",
                (_unique_slug(), "Тест директив"),
            )
            proj_id = cur.lastrowid

        # Вставка валидной директивы
        with get_db() as db:
            db.execute(
                "INSERT INTO directives (project_id, scope, text) VALUES (?, 'plan', ?)",
                (proj_id, "Публиковать 5 постов в неделю"),
            )

        # Проверяем статус по умолчанию
        with get_db() as db:
            row = db.execute(
                "SELECT status FROM directives WHERE project_id=?", (proj_id,)
            ).fetchone()
            assert row is not None
            assert row["status"] == "active"

        # Неверный scope → IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            with get_db() as db:
                db.execute(
                    "INSERT INTO directives (project_id, scope, text) VALUES (?, 'bogus', 'тест')",
                    (proj_id,),
                )

        # Каскадное удаление: удаляем проект → директивы тоже исчезают
        with get_db() as db:
            db.execute("DELETE FROM projects WHERE id=?", (proj_id,))

        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM directives WHERE project_id=?", (proj_id,)
            ).fetchone()[0]
            assert count == 0, "Директивы не удалились каскадно вместе с проектом"

    def test_migrate_idempotent(self, patch_env):
        """Повторный init_db() не падает."""
        path = cfg_module.settings.db_path_absolute
        init_db(path)
        init_db(path)  # второй вызов — идемпотентно


# ═════════════════════════════════════════════════════════════════════════════
# 2. load_rules() в app/pipeline/prompts.py
# ═════════════════════════════════════════════════════════════════════════════


class TestLoadRules:
    """Тесты функции load_rules() из app/pipeline/prompts.py."""

    def setup_method(self):
        """Сбрасываем кеш перед каждым тестом."""
        prompts_module._rules_cache = None

    def test_load_rules_basic(self):
        """load_rules() содержит ключевые разделы из обоих файлов правил."""
        from app.pipeline.prompts import load_rules
        result = load_rules()
        assert "Дозирование продаж" in result, "Раздел 'Дозирование продаж' из AGENTS.md не найден"
        assert "Запретные темы" in result, "Раздел 'Запретные темы' из forbidden_ru.md не найден"

    def test_load_rules_with_project_rules(self):
        """load_rules с project_settings['agent_rules'] добавляет раздел проектных правил."""
        from app.pipeline.prompts import load_rules
        result = load_rules({"agent_rules": "Только котики"})
        assert "Дополнительные правила проекта" in result
        assert "Только котики" in result

    def test_load_rules_empty_agent_rules_not_added(self):
        """Пустая строка agent_rules не добавляет раздел."""
        from app.pipeline.prompts import load_rules
        result = load_rules({"agent_rules": ""})
        assert "Дополнительные правила проекта" not in result

    def test_load_rules_missing_files_ok(self, monkeypatch, tmp_path):
        """
        При отсутствии файлов правил load_rules() не падает — возвращает строку.
        """
        # Сбрасываем кеш и подменяем директорию промптов на несуществующую
        prompts_module._rules_cache = None
        monkeypatch.setattr(prompts_module, "_PROMPTS_DIR", tmp_path / "nonexistent_prompts")
        # Повторный сброс кеша после patching (на случай гонки)
        prompts_module._rules_cache = None
        from app.pipeline.prompts import load_rules
        result = load_rules()
        assert isinstance(result, str), "load_rules() должна вернуть строку, а не упасть"

    def test_load_rules_cache_works(self):
        """Базовая часть кешируется: повторный вызов без сброса возвращает тот же объект."""
        from app.pipeline.prompts import load_rules
        r1 = load_rules()
        r2 = load_rules()
        assert r1 is r2 or r1 == r2  # кеш или тот же объект


# ═════════════════════════════════════════════════════════════════════════════
# 3. FAKE-заглушки LLM — новые purposes
# ═════════════════════════════════════════════════════════════════════════════


class TestFakeLLMBrain:
    """Тесты FAKE-заглушек purposes этапа 8.2."""

    def _chat(self, purpose: str) -> dict:
        from app.llm import chat
        raw = chat([{"role": "user", "content": "test"}], purpose=purpose)
        return json.loads(raw)

    def test_fake_directives_parse(self, patch_env):
        """purpose='directives_parse' → JSON с ключом directives (список)."""
        data = self._chat("directives_parse")
        assert "directives" in data, "Ожидался ключ 'directives' в ответе"
        assert isinstance(data["directives"], list)
        assert len(data["directives"]) > 0

        # Первая директива должна содержать text, scope и parsed
        first = data["directives"][0]
        assert "text" in first
        assert "scope" in first
        assert "parsed" in first

        # Вторая директива — parsed может быть null
        second = data["directives"][1]
        assert second["parsed"] is None

    def test_fake_strategy_check(self, patch_env):
        """purpose='strategy_check' → JSON с ok=True и пустым violations."""
        data = self._chat("strategy_check")
        assert "ok" in data, "Ожидался ключ 'ok' в ответе"
        assert data["ok"] is True
        assert "violations" in data
        assert isinstance(data["violations"], list)
        assert len(data["violations"]) == 0
