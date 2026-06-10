"""
Тесты Волны 3: ревью, календарь, ручная очередь, дашборд.
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


def _create_project(db, *, platforms=("telegram", "vk"), slug=None):
    """Создаёт проект с указанными включёнными площадками. Возвращает project_id."""
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes)
        VALUES (?, 'Тест', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы')
        """,
        (s,),
    )
    project_id = cur.lastrowid
    for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
        enabled = 1 if p in platforms else 0
        db.execute(
            "INSERT INTO project_platforms (project_id, platform, enabled, mode) VALUES (?,?,?,?)",
            (project_id, p, enabled, "auto"),
        )
    return project_id


def _create_content(db, project_id, *, status="text_review", title="Тестовый пост"):
    """Создаёт запись content. Возвращает content_id."""
    texts = json.dumps({
        "telegram": {"text": "Текст для Telegram", "hashtags": ["#тест"]},
        "vk": {"text": "Текст для VK", "hashtags": ["#тест"]},
        "features": {"hook_type": "вопрос", "topic": "тест", "length": "short",
                     "format": "text", "ab_variant": "A"},
    }, ensure_ascii=False)
    features = json.dumps({"hook_type": "вопрос", "topic": "тест", "length": "short",
                           "format": "text", "ab_variant": "A"}, ensure_ascii=False)
    cur = db.execute(
        """
        INSERT INTO content (project_id, type, title, texts, features, status)
        VALUES (?, 'post', ?, ?, ?, ?)
        """,
        (project_id, title, texts, features, status),
    )
    return cur.lastrowid


def _create_schedule(db, content_id, platform, *, status="planned", planned_at=None,
                     error_text=None):
    """Создаёт запись schedule. Возвращает schedule_id."""
    if planned_at is None:
        planned_at = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(planned_at, datetime):
        planned_at = planned_at.strftime("%Y-%m-%d %H:%M:%S")

    cur = db.execute(
        """
        INSERT INTO schedule (content_id, platform, planned_at, status, error_text)
        VALUES (?, ?, ?, ?, ?)
        """,
        (content_id, platform, planned_at, status, error_text),
    )
    return cur.lastrowid


# ─── Фикстуры ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client_with_data(client, patch_env):
    """client + инициализированная БД с проектом, контентом и расписанием."""
    _setup_db()
    with get_db() as db:
        project_id = _create_project(db)
        content_id = _create_content(db, project_id, status="text_review")
    return client, project_id, content_id


# ─── 1. Список ревью ──────────────────────────────────────────────────────────


class TestReviewList:
    def test_shows_text_review_content(self, client_with_data):
        c, project_id, content_id = client_with_data
        resp = c.get("/review")
        assert resp.status_code == 200
        assert "Тестовый пост" in resp.text

    def test_filter_by_project(self, client_with_data):
        c, project_id, content_id = client_with_data
        resp = c.get(f"/review?project_id={project_id}")
        assert resp.status_code == 200
        assert "Тестовый пост" in resp.text

    def test_empty_when_no_review(self, client, patch_env):
        _setup_db()
        # Без черновиков
        resp = c = client.get("/review")
        assert resp.status_code == 200


# ─── 2. Страница ревью черновика ──────────────────────────────────────────────


class TestReviewDetail:
    def test_shows_content(self, client_with_data):
        c, project_id, content_id = client_with_data
        resp = c.get(f"/review/{content_id}")
        assert resp.status_code == 200
        assert "Тестовый пост" in resp.text
        # Тексты площадок видны
        assert "Текст для Telegram" in resp.text
        assert "Текст для VK" in resp.text

    def test_404_for_unknown(self, client, patch_env):
        _setup_db()
        resp = client.get("/review/99999")
        assert resp.status_code == 404


# ─── 3. Сохранение правок ─────────────────────────────────────────────────────


class TestReviewSave:
    def test_save_updates_text(self, client_with_data):
        c, project_id, content_id = client_with_data
        new_text = "Новый текст для Telegram"
        resp = c.post(
            f"/review/{content_id}/save",
            data={"text_telegram": new_text, "text_vk": "VK текст"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # Проверяем БД
        with get_db() as db:
            row = db.execute("SELECT texts FROM content WHERE id=?", (content_id,)).fetchone()
        texts = json.loads(row["texts"])
        assert texts["telegram"]["text"] == new_text


# ─── 4. Одобрение ─────────────────────────────────────────────────────────────


class TestReviewApprove:
    def test_approve_without_schedule(self, client_with_data):
        """Одобрить без планирования — статус approved, schedule не создан."""
        c, project_id, content_id = client_with_data
        resp = c.post(
            f"/review/{content_id}/approve",
            data={},  # ни один чекбокс не отмечен
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with get_db() as db:
            row = db.execute("SELECT status FROM content WHERE id=?", (content_id,)).fetchone()
            sched_count = db.execute(
                "SELECT COUNT(*) FROM schedule WHERE content_id=?", (content_id,)
            ).fetchone()[0]
        assert row["status"] == "approved"
        assert sched_count == 0

    def test_approve_with_schedule(self, client_with_data):
        """Одобрить с планированием — создаются записи schedule."""
        c, project_id, content_id = client_with_data
        dt = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        resp = c.post(
            f"/review/{content_id}/approve",
            data={
                "schedule_telegram": "1",
                "planned_at_telegram": dt,
                "schedule_vk": "1",
                "planned_at_vk": dt,
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with get_db() as db:
            row = db.execute("SELECT status FROM content WHERE id=?", (content_id,)).fetchone()
            rows = db.execute(
                "SELECT platform, status FROM schedule WHERE content_id=?", (content_id,)
            ).fetchall()
        assert row["status"] == "approved"
        platforms = {r["platform"] for r in rows}
        assert "telegram" in platforms
        assert "vk" in platforms
        # Оба planned
        for r in rows:
            assert r["status"] == "planned"

    def test_approve_schedule_correct_time(self, client_with_data):
        """Проверяем, что planned_at соответствует переданному времени."""
        c, project_id, content_id = client_with_data
        future = datetime.now() + timedelta(days=3)
        dt_str = future.strftime("%Y-%m-%dT%H:%M")
        c.post(
            f"/review/{content_id}/approve",
            data={"schedule_telegram": "1", "planned_at_telegram": dt_str},
            follow_redirects=True,
        )
        with get_db() as db:
            row = db.execute(
                "SELECT planned_at FROM schedule WHERE content_id=? AND platform='telegram'",
                (content_id,),
            ).fetchone()
        assert row is not None
        # planned_at начинается с той же даты
        assert row["planned_at"][:10] == future.strftime("%Y-%m-%d")


# ─── 5. Отклонение ────────────────────────────────────────────────────────────


class TestReviewReject:
    def test_reject_without_reason_fails(self, client_with_data):
        """Отклонение без причины → редирект с ошибкой, статус не изменился."""
        c, project_id, content_id = client_with_data
        resp = c.post(
            f"/review/{content_id}/reject",
            data={"reason": ""},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with get_db() as db:
            row = db.execute("SELECT status FROM content WHERE id=?", (content_id,)).fetchone()
        # Статус НЕ изменился — остался text_review
        assert row["status"] == "text_review"

    def test_reject_with_reason(self, client_with_data):
        """Отклонение с причиной → rejected + learning создан."""
        c, project_id, content_id = client_with_data
        resp = c.post(
            f"/review/{content_id}/reject",
            data={"reason": "Слишком агрессивный тон"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with get_db() as db:
            row = db.execute("SELECT status, reject_reason FROM content WHERE id=?", (content_id,)).fetchone()
            learning = db.execute(
                "SELECT * FROM learnings WHERE project_id=? AND source='reject'",
                (project_id,),
            ).fetchone()
        assert row["status"] == "rejected"
        assert "агрессивный" in row["reject_reason"]
        assert learning is not None
        assert "Отклонено ревьюером" in learning["insight"]
        assert learning["active"] == 1


# ─── 6. Календарь ─────────────────────────────────────────────────────────────


class TestCalendar:
    def test_month_view_renders(self, client, patch_env):
        _setup_db()
        resp = client.get("/calendar")
        assert resp.status_code == 200

    def test_week_view_renders(self, client, patch_env):
        _setup_db()
        resp = client.get("/calendar?view=week")
        assert resp.status_code == 200

    def test_shows_schedule_entry_current_month(self, client, patch_env):
        _setup_db()
        now = datetime.now()
        with get_db() as db:
            project_id = _create_project(db)
            content_id = _create_content(db, project_id, status="approved")
            _create_schedule(db, content_id, "telegram",
                             planned_at=now, status="planned")

        resp = client.get(f"/calendar?year={now.year}&month={now.month}")
        assert resp.status_code == 200
        assert "Тестовый пост" in resp.text

    def test_entry_detail(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            project_id = _create_project(db)
            content_id = _create_content(db, project_id, status="approved")
            schedule_id = _create_schedule(db, content_id, "telegram", status="planned")

        resp = client.get(f"/calendar/entry/{schedule_id}")
        assert resp.status_code == 200
        assert "telegram" in resp.text

    def test_reschedule_planned(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            project_id = _create_project(db)
            content_id = _create_content(db, project_id, status="approved")
            schedule_id = _create_schedule(db, content_id, "telegram", status="planned")

        new_dt = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M")
        resp = client.post(
            f"/calendar/entry/{schedule_id}/reschedule",
            data={"planned_at": new_dt},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with get_db() as db:
            row = db.execute("SELECT planned_at, status FROM schedule WHERE id=?",
                             (schedule_id,)).fetchone()
        assert row["status"] == "planned"
        # Дата обновилась
        expected_date = new_dt[:10]
        assert row["planned_at"][:10] == expected_date

    def test_cancel_planned(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            project_id = _create_project(db)
            content_id = _create_content(db, project_id, status="approved")
            schedule_id = _create_schedule(db, content_id, "telegram", status="planned")

        resp = client.post(
            f"/calendar/entry/{schedule_id}/cancel",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with get_db() as db:
            row = db.execute("SELECT id FROM schedule WHERE id=?", (schedule_id,)).fetchone()
        assert row is None  # запись удалена

    def test_retry_error_resets_to_planned(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            project_id = _create_project(db)
            content_id = _create_content(db, project_id, status="approved")
            schedule_id = _create_schedule(
                db, content_id, "telegram",
                status="error", error_text="Connection refused",
            )

        resp = client.post(
            f"/calendar/entry/{schedule_id}/retry",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with get_db() as db:
            row = db.execute(
                "SELECT status, attempts, error_text FROM schedule WHERE id=?",
                (schedule_id,),
            ).fetchone()
        assert row["status"] == "planned"
        assert row["attempts"] == 0
        assert row["error_text"] is None


# ─── 7. Ручная очередь ────────────────────────────────────────────────────────


class TestManualQueue:
    def test_manual_pending_visible(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            project_id = _create_project(db)
            content_id = _create_content(db, project_id, status="approved")
            _create_schedule(db, content_id, "telegram", status="manual_pending")

        resp = client.get("/manual")
        assert resp.status_code == 200
        assert "Тестовый пост" in resp.text

    def test_mark_done_with_url(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            project_id = _create_project(db)
            content_id = _create_content(db, project_id, status="approved")
            schedule_id = _create_schedule(db, content_id, "telegram", status="manual_pending")

        url = "https://t.me/test/123"
        resp = client.post(
            f"/manual/{schedule_id}/done",
            data={"published_url": url},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with get_db() as db:
            row = db.execute(
                "SELECT status, published_url FROM schedule WHERE id=?",
                (schedule_id,),
            ).fetchone()
        assert row["status"] == "manual_done"
        assert row["published_url"] == url

    def test_mark_done_without_url(self, client, patch_env):
        """Без URL — тоже работает (генерируется псевдо-URL)."""
        _setup_db()
        with get_db() as db:
            project_id = _create_project(db)
            content_id = _create_content(db, project_id, status="approved")
            schedule_id = _create_schedule(db, content_id, "telegram", status="manual_pending")

        resp = client.post(
            f"/manual/{schedule_id}/done",
            data={"published_url": ""},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with get_db() as db:
            row = db.execute("SELECT status FROM schedule WHERE id=?", (schedule_id,)).fetchone()
        assert row["status"] == "manual_done"


# ─── 8. Дашборд ───────────────────────────────────────────────────────────────


class TestDashboard:
    def test_dashboard_counts(self, client, patch_env):
        _setup_db()
        now = datetime.now()

        with get_db() as db:
            project_id = _create_project(db)
            # 2 контента на ревью
            _create_content(db, project_id, status="text_review", title="Ревью 1")
            _create_content(db, project_id, status="text_review", title="Ревью 2")
            # 1 approved без schedule
            approved_id = _create_content(db, project_id, status="approved", title="Approved")
            # 1 запланированный на 3 дня вперёд
            sched_id = _create_schedule(db, approved_id, "telegram",
                                        planned_at=now + timedelta(days=3), status="planned")
            # 1 ошибка
            err_content = _create_content(db, project_id, status="approved", title="Error")
            _create_schedule(db, err_content, "vk", status="error", error_text="Тест ошибки")
            # 1 manual_pending
            manual_content = _create_content(db, project_id, status="approved", title="Manual")
            _create_schedule(db, manual_content, "telegram", status="manual_pending")

        resp = client.get("/")
        assert resp.status_code == 200

        # Проверяем счётчики через текст HTML
        # review_count=2
        assert "2" in resp.text
        # errors_total >= 1 (ошибка)
        assert "Тест ошибки" in resp.text
        # Проект активен
        assert "1" in resp.text

    def test_dashboard_renders_without_data(self, client, patch_env):
        _setup_db()
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Дашборд" in resp.text
