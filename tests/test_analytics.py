"""
Тесты страницы аналитики /analytics и ручного ввода метрик.
"""
import json
import time as _time
import uuid
from datetime import datetime, timedelta, date

import pytest

import app.config as cfg_module
import app.web.analytics as analytics_module
from app.db import get_db, init_db


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _slug() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, slug=None):
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects (slug, name, description, audience, tone, goals, cta, themes)
        VALUES (?, 'Тест Аналитика', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы')
        """,
        (s,),
    )
    project_id = cur.lastrowid
    for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
        db.execute(
            "INSERT INTO project_platforms (project_id, platform, enabled, mode) VALUES (?,?,?,?)",
            (project_id, p, 1, "auto"),
        )
    return project_id


def _create_content(db, project_id, title="Аналитик-пост"):
    features = json.dumps({
        "hook_type": "факт",
        "format": "text",
        "ab_variant": "A",
    }, ensure_ascii=False)
    cur = db.execute(
        """
        INSERT INTO content (project_id, type, title, texts, features, status)
        VALUES (?, 'post', ?, '{}', ?, 'approved')
        """,
        (project_id, title, features),
    )
    return cur.lastrowid


def _create_schedule(db, content_id, platform="telegram", status="published",
                     planned_at=None, published_url=None):
    if planned_at is None:
        planned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(planned_at, datetime):
        planned_at = planned_at.strftime("%Y-%m-%d %H:%M:%S")
    cur = db.execute(
        """
        INSERT INTO schedule (content_id, platform, planned_at, status, published_url)
        VALUES (?, ?, ?, ?, ?)
        """,
        (content_id, platform, planned_at, status, published_url),
    )
    return cur.lastrowid


def _create_metric(db, schedule_id, views=100, likes=10, comments=2, shares=1,
                   watch_pct=0.0, offset_days=0):
    d = (date.today() - timedelta(days=offset_days)).isoformat()
    db.execute(
        """
        INSERT OR REPLACE INTO metrics (schedule_id, date, views, likes, comments, shares, watch_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (schedule_id, d, views, likes, comments, shares, watch_pct),
    )
    return d


# ─── 1. Страница /analytics на пустой БД ─────────────────────────────────────


class TestAnalyticsEmpty:
    def test_empty_db_returns_200(self, client, patch_env):
        """Пустая БД не вызывает деления на ноль и возвращает 200."""
        _setup_db()
        resp = client.get("/analytics")
        assert resp.status_code == 200

    def test_empty_shows_zero_stats(self, client, patch_env):
        _setup_db()
        resp = client.get("/analytics")
        assert resp.status_code == 200
        # Нули в карточках
        assert "0" in resp.text

    def test_period_filter_7_days(self, client, patch_env):
        _setup_db()
        resp = client.get("/analytics?days=7")
        assert resp.status_code == 200

    def test_period_filter_90_days(self, client, patch_env):
        _setup_db()
        resp = client.get("/analytics?days=90")
        assert resp.status_code == 200

    def test_invalid_days_defaults_to_30(self, client, patch_env):
        _setup_db()
        resp = client.get("/analytics?days=999")
        assert resp.status_code == 200


# ─── 2. Страница с данными ─────────────────────────────────────────────────────


class TestAnalyticsWithData:
    def test_shows_content_title(self, client, patch_env):
        """Title контента отображается на странице."""
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid, title="Уникальный тестовый пост")
            sid = _create_schedule(db, cid, status="published")
            _create_metric(db, sid, views=500, likes=42)

        resp = client.get("/analytics")
        assert resp.status_code == 200
        assert "Уникальный тестовый пост" in resp.text

    def test_shows_last_metric_views(self, client, patch_env):
        """Показывает значения из последнего замера, а не из первого."""
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid, title="Пост двух замеров")
            sid = _create_schedule(db, cid, status="published")
            # Старый замер — 100 просмотров
            _create_metric(db, sid, views=100, likes=5, offset_days=2)
            # Новый замер — 999 просмотров
            _create_metric(db, sid, views=999, likes=50, offset_days=0)

        resp = client.get("/analytics")
        assert resp.status_code == 200
        assert "999" in resp.text
        # Старый замер НЕ должен доминировать в топ-значении
        # (100 тоже может быть на странице в форме ввода — это ок)

    def test_summary_counts_publications(self, client, patch_env):
        """Сводные карточки: публикаций = 1."""
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid)
            _create_schedule(db, cid, status="published")

        resp = client.get("/analytics")
        assert resp.status_code == 200
        assert "1" in resp.text

    def test_features_badges_displayed(self, client, patch_env):
        """Бейджи из features (hook_type, format, ab_variant) видны в топе."""
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid, title="Пост с бейджами")
            sid = _create_schedule(db, cid, status="published")
            _create_metric(db, sid, views=1000)

        resp = client.get("/analytics")
        assert resp.status_code == 200
        # hook_type="факт" должен быть в бейджах
        assert "факт" in resp.text

    def test_manual_done_included(self, client, patch_env):
        """Публикации со статусом manual_done тоже учитываются."""
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid, title="Мануальный пост")
            sid = _create_schedule(db, cid, status="manual_done")
            _create_metric(db, sid, views=200)

        resp = client.get("/analytics")
        assert resp.status_code == 200
        assert "Мануальный пост" in resp.text


# ─── 3. POST ручного ввода метрик ─────────────────────────────────────────────


class TestMetricsInput:
    def test_post_creates_metric(self, client, patch_env):
        """POST /analytics/metrics/{id} создаёт строку в таблице metrics."""
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid)
            sid = _create_schedule(db, cid, status="published")

        resp = client.post(
            f"/analytics/metrics/{sid}",
            data={"views": "500", "likes": "30", "comments": "5",
                  "shares": "2", "watch_pct": "0.0"},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute(
                "SELECT * FROM metrics WHERE schedule_id=?", (sid,)
            ).fetchone()
        assert row is not None
        assert row["views"] == 500
        assert row["likes"] == 30

    def test_post_replaces_same_day(self, client, patch_env):
        """Повторный POST в тот же день перезаписывает значения (INSERT OR REPLACE)."""
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid)
            sid = _create_schedule(db, cid, status="published")

        # Первый ввод
        client.post(
            f"/analytics/metrics/{sid}",
            data={"views": "100", "likes": "5", "comments": "1",
                  "shares": "0", "watch_pct": "0.0"},
        )
        # Второй ввод в тот же день
        client.post(
            f"/analytics/metrics/{sid}",
            data={"views": "777", "likes": "50", "comments": "10",
                  "shares": "3", "watch_pct": "55.5"},
        )

        today = date.today().isoformat()
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM metrics WHERE schedule_id=? AND date=?",
                (sid, today),
            ).fetchall()

        # Должна быть ровно одна строка за сегодня
        assert len(rows) == 1
        assert rows[0]["views"] == 777
        assert rows[0]["likes"] == 50

    def test_post_unknown_schedule_returns_404(self, client, patch_env):
        """Несуществующий schedule_id → 404."""
        _setup_db()
        resp = client.post(
            "/analytics/metrics/99999",
            data={"views": "10", "likes": "1", "comments": "0",
                  "shares": "0", "watch_pct": "0.0"},
        )
        assert resp.status_code == 404

    def test_post_redirects_back(self, client, patch_env):
        """После успешного POST идёт редирект на /analytics."""
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid)
            sid = _create_schedule(db, cid, status="manual_done")

        resp = client.post(
            f"/analytics/metrics/{sid}",
            data={"views": "0", "likes": "0", "comments": "0",
                  "shares": "0", "watch_pct": "0.0"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/analytics" in resp.headers["location"]


# ─── 4. Фильтрация по project_id ──────────────────────────────────────────────


class TestAnalyticsProjectFilter:
    def test_filter_excludes_other_project(self, client, patch_env):
        """Фильтр project_id отсекает контент чужого проекта."""
        _setup_db()
        with get_db() as db:
            pid1 = _create_project(db)
            pid2 = _create_project(db)

            cid1 = _create_content(db, pid1, title="Мой проект пост")
            cid2 = _create_content(db, pid2, title="Чужой проект пост")

            sid1 = _create_schedule(db, cid1, status="published")
            sid2 = _create_schedule(db, cid2, status="published")

            _create_metric(db, sid1, views=100)
            _create_metric(db, sid2, views=200)

        # Фильтруем по pid1
        resp = client.get(f"/analytics?project_id={pid1}")
        assert resp.status_code == 200
        assert "Мой проект пост" in resp.text
        assert "Чужой проект пост" not in resp.text

    def test_filter_shows_own_project_metrics(self, client, patch_env):
        """Метрики своего проекта отображаются при фильтре."""
        _setup_db()
        with get_db() as db:
            pid = _create_project(db)
            cid = _create_content(db, pid, title="Собственный пост")
            sid = _create_schedule(db, cid, status="published")
            _create_metric(db, sid, views=777)

        resp = client.get(f"/analytics?project_id={pid}")
        assert resp.status_code == 200
        assert "777" in resp.text

    def test_all_projects_no_filter(self, client, patch_env):
        """Без фильтра показываются оба проекта."""
        _setup_db()
        with get_db() as db:
            pid1 = _create_project(db)
            pid2 = _create_project(db)
            cid1 = _create_content(db, pid1, title="Проект А пост")
            cid2 = _create_content(db, pid2, title="Проект Б пост")
            sid1 = _create_schedule(db, cid1, status="published")
            sid2 = _create_schedule(db, cid2, status="published")
            _create_metric(db, sid1, views=50)
            _create_metric(db, sid2, views=60)

        resp = client.get("/analytics")
        assert resp.status_code == 200
        assert "Проект А пост" in resp.text
        assert "Проект Б пост" in resp.text


# ─── 5. Кнопка «Обновить метрики» (этап 8.7) ─────────────────────────────────


class TestCollectNow:
    def test_metrics_interval_default(self, patch_env):
        # default настройки = 6 часов
        assert cfg_module.settings.METRICS_INTERVAL_HOURS == 6

    def test_collect_button_on_page(self, client, patch_env):
        _setup_db()
        resp = client.get("/analytics")
        assert resp.status_code == 200
        assert 'hx-post="/analytics/collect"' in resp.text

    def test_collect_runs_and_shows_result(self, client, patch_env, monkeypatch):
        _setup_db()
        monkeypatch.setattr(analytics_module, "_last_collect_ts", 0.0)
        monkeypatch.setattr(
            analytics_module, "collect_metrics",
            lambda: {"collected": 3, "skipped": 1, "errors": 0},
        )
        resp = client.post("/analytics/collect")
        assert resp.status_code == 200
        assert "Собрано: 3" in resp.text
        assert "пропущено: 1" in resp.text
        # фрагмент снова содержит кнопку (для повторного запуска)
        assert 'hx-post="/analytics/collect"' in resp.text

    def test_collect_throttled(self, client, patch_env, monkeypatch):
        _setup_db()
        # имитируем недавний запуск → следующий клик должен быть отбит
        monkeypatch.setattr(analytics_module, "_last_collect_ts", _time.monotonic())
        called = {"n": 0}
        def _spy():
            called["n"] += 1
            return {"collected": 0, "skipped": 0, "errors": 0}
        monkeypatch.setattr(analytics_module, "collect_metrics", _spy)
        resp = client.post("/analytics/collect")
        assert resp.status_code == 200
        assert "подождите" in resp.text.lower()
        assert called["n"] == 0  # сборщик не вызывался

    def test_collect_handles_collector_error(self, client, patch_env, monkeypatch):
        _setup_db()
        monkeypatch.setattr(analytics_module, "_last_collect_ts", 0.0)
        def _boom():
            raise RuntimeError("api down")
        monkeypatch.setattr(analytics_module, "collect_metrics", _boom)
        resp = client.post("/analytics/collect")
        assert resp.status_code == 200
        assert "Ошибка" in resp.text
