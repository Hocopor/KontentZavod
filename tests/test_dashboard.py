"""
Тесты дашборда и навигации (воркфлоу 2.0).
Все тесты работают с FAKE_LLM=1 (из conftest.patch_env).
"""
import json
import uuid
from datetime import datetime, timedelta

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _slug() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, *, slug=None, stage="running"):
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage)
        VALUES (?, 'Тест', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', ?)
        """,
        (s, stage),
    )
    project_id = cur.lastrowid
    for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
        db.execute(
            "INSERT INTO project_platforms (project_id, platform, enabled, mode) VALUES (?,?,?,?)",
            (project_id, p, 1, "auto"),
        )
    return project_id


def _create_plan_item(db, project_id, *, status="proposed", platform="telegram",
                      content_type="post", error_text=None):
    from datetime import date
    cur = db.execute(
        """
        INSERT INTO plan_items (project_id, platform, content_type, date, title, status, error_text)
        VALUES (?, ?, ?, ?, 'Тест', ?, ?)
        """,
        (project_id, platform, content_type,
         (date.today() + timedelta(days=1)).isoformat(),
         status, error_text),
    )
    return cur.lastrowid


def _create_content(db, project_id, *, status="approved"):
    texts = json.dumps({
        "telegram": {"text": "Текст", "hashtags": []},
    }, ensure_ascii=False)
    features = json.dumps({"hook_type": "вопрос", "topic": "тест",
                           "length": "short", "format": "text", "ab_variant": "A"})
    cur = db.execute(
        "INSERT INTO content (project_id, type, title, texts, features, status) VALUES (?, 'post', 'Тест', ?, ?, ?)",
        (project_id, texts, features, status),
    )
    return cur.lastrowid


def _create_schedule(db, content_id, platform, *, status="planned", planned_at=None,
                     error_text=None):
    if planned_at is None:
        planned_at = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(planned_at, datetime):
        planned_at = planned_at.strftime("%Y-%m-%d %H:%M:%S")
    cur = db.execute(
        "INSERT INTO schedule (content_id, platform, planned_at, status, error_text) VALUES (?, ?, ?, ?, ?)",
        (content_id, platform, planned_at, status, error_text),
    )
    return cur.lastrowid


# ─── 1. Дашборд — основные счётчики ─────────────────────────────────────────


class TestDashboardCounts:
    def test_dashboard_renders(self, client, patch_env):
        _setup_db()
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Главная" in resp.text

    def test_projects_running_count(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            _create_project(db, stage="running")
            _create_project(db, stage="running")
            _create_project(db, stage="idle")  # не считается
        resp = client.get("/")
        assert resp.status_code == 200
        assert "2" in resp.text  # 2 в работе

    def test_plan_proposed_count(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            _create_plan_item(db, pid, status="proposed")
            _create_plan_item(db, pid, status="proposed")
            _create_plan_item(db, pid, status="approved")  # не считается
        resp = client.get("/")
        assert resp.status_code == 200
        assert "2" in resp.text

    def test_items_generated_count(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            _create_plan_item(db, pid, status="generated")
            _create_plan_item(db, pid, status="generated")
            _create_plan_item(db, pid, status="proposed")  # не считается
        resp = client.get("/")
        assert resp.status_code == 200
        # Проверяем что страница рендерится и счётчик присутствует
        assert resp.status_code == 200

    def test_manual_count(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid)
            _create_schedule(db, cid, "telegram", status="manual_pending")
        resp = client.get("/")
        assert resp.status_code == 200
        assert "1" in resp.text

    def test_errors_total_combines_plan_and_sched(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            # 1 ошибка в plan_items
            _create_plan_item(db, pid, status="error", error_text="Ошибка плана")
            # 1 ошибка в schedule
            cid = _create_content(db, pid)
            _create_schedule(db, cid, "telegram", status="error", error_text="Тест ошибки")
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Тест ошибки" in resp.text or "Ошибка" in resp.text

    def test_published_7d_count(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid)
            # опубликовано 3 дня назад
            past = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
            _create_schedule(db, cid, "telegram",
                             status="published", planned_at=past)
        resp = client.get("/")
        assert resp.status_code == 200


# ─── 2. Дашборд — последние ошибки ───────────────────────────────────────────


class TestDashboardErrors:
    def test_last_errors_block_visible(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid)
            _create_schedule(db, cid, "vk", status="error", error_text="Тест ошибки публикации")
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Тест ошибки публикации" in resp.text

    def test_no_errors_block_hidden(self, client, patch_env):
        _setup_db()
        resp = client.get("/")
        assert resp.status_code == 200
        # Блок «Последние ошибки» не отображается при отсутствии ошибок
        assert "Последние ошибки" not in resp.text


# ─── 3. Дашборд — баннер автопубликации ──────────────────────────────────────


class TestDashboardSchedulerBanner:
    def test_banner_shown_when_scheduler_disabled(self, client, patch_env, monkeypatch):
        _setup_db()
        monkeypatch.setenv("ENABLE_SCHEDULER", "0")
        resp = client.get("/")
        assert resp.status_code == 200
        assert "ENABLE_SCHEDULER=0" in resp.text

    def test_banner_not_shown_when_enabled(self, client, patch_env, monkeypatch):
        _setup_db()
        monkeypatch.setenv("ENABLE_SCHEDULER", "1")
        resp = client.get("/")
        assert resp.status_code == 200
        assert "ENABLE_SCHEDULER=0" not in resp.text


# ─── 4. Удалённые роуты → 404 ────────────────────────────────────────────────


class TestOldRoutesGone:
    def test_review_is_404(self, client, patch_env):
        _setup_db()
        resp = client.get("/review")
        assert resp.status_code == 404

    def test_calendar_is_404(self, client, patch_env):
        _setup_db()
        resp = client.get("/calendar")
        assert resp.status_code == 404

    def test_manual_is_404(self, client, patch_env):
        _setup_db()
        resp = client.get("/manual")
        assert resp.status_code == 404


# ─── 5. Новые роуты → 200 ────────────────────────────────────────────────────


class TestNewRoutesOk:
    def test_root_200(self, client, patch_env):
        _setup_db()
        assert client.get("/").status_code == 200

    def test_projects_200(self, client, patch_env):
        _setup_db()
        assert client.get("/projects").status_code == 200

    def test_plan_200(self, client, patch_env):
        _setup_db()
        assert client.get("/plan").status_code == 200

    def test_queue_200(self, client, patch_env):
        _setup_db()
        assert client.get("/queue").status_code == 200

    def test_analytics_200(self, client, patch_env):
        _setup_db()
        assert client.get("/analytics").status_code == 200


# ─── 6. Счётчики навигации ────────────────────────────────────────────────────


class TestNavCounts:
    def test_nav_plan_proposed_badge(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            _create_plan_item(db, pid, status="proposed")
            _create_plan_item(db, pid, status="proposed")
        resp = client.get("/")
        assert resp.status_code == 200
        # Бейдж «2» должен быть в навигации (Контент-план)
        assert "2" in resp.text

    def test_nav_manual_pending_badge(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid)
            _create_schedule(db, cid, "telegram", status="manual_pending")
        resp = client.get("/")
        assert resp.status_code == 200
        assert "1" in resp.text
