"""
Тесты очистки контент-плана (POST /plan/clear/{slug}/{mode}).

Режимы: all | unpublished | unapproved
"""
import json
import uuid
from datetime import date, timedelta

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Утилиты ──────────────────────────────────────────────────────────────────


def _slug() -> str:
    return f"clear-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, *, slug=None, stage="running", platforms=("telegram",)):
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage)
        VALUES (?, 'Тест Clear', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', ?)
        """,
        (s, stage),
    )
    project_id = cur.lastrowid
    for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
        enabled = 1 if p in platforms else 0
        db.execute(
            "INSERT INTO project_platforms (project_id, platform, enabled, mode) VALUES (?,?,?,?)",
            (project_id, p, enabled, "auto"),
        )
    return project_id, s


def _create_content(db, project_id, *, title="Контент", status="approved"):
    cur = db.execute(
        "INSERT INTO content (project_id, type, title, status) VALUES (?,?,?,?)",
        (project_id, "post", title, status),
    )
    return cur.lastrowid


def _create_schedule(db, content_id, *, sched_status="planned"):
    item_date = (date.today() + timedelta(days=5)).isoformat() + " 10:00:00"
    cur = db.execute(
        "INSERT INTO schedule (content_id, platform, planned_at, status) VALUES (?,?,?,?)",
        (content_id, "telegram", item_date, sched_status),
    )
    return cur.lastrowid


def _create_plan_item(
    db,
    project_id,
    *,
    platform="telegram",
    content_type="post",
    status="proposed",
    content_id=None,
    title="Пункт плана",
):
    item_date = (date.today() + timedelta(days=5)).isoformat()
    cur = db.execute(
        """
        INSERT INTO plan_items
            (project_id, platform, content_type, date, time_slot, title, status, content_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, platform, content_type, item_date, "10:00", title, status, content_id),
    )
    return cur.lastrowid


# ─── Тесты ────────────────────────────────────────────────────────────────────


class TestClearAll:

    def test_clear_all_deletes_everything_including_published(self, client, patch_env):
        """mode=all: все пункты удалены, включая с опубликованным контентом."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db)

            # proposed без контента
            _create_plan_item(db, pid, status="proposed", title="Предложен")

            # approved без контента
            _create_plan_item(db, pid, status="approved", title="Одобрен")

            # generated с контентом и published-schedule
            cid = _create_content(db, pid, status="approved")
            _create_schedule(db, cid, sched_status="published")
            _create_plan_item(db, pid, status="generated", content_id=cid, title="Опубликован")

        resp = client.post(f"/plan/clear/{slug}/all")
        assert resp.status_code == 200
        assert "plan-board-area" in resp.text

        with get_db() as db:
            items = db.execute("SELECT id FROM plan_items WHERE project_id=?", (pid,)).fetchall()
            contents = db.execute("SELECT id FROM content WHERE project_id=?", (pid,)).fetchall()
            schedules = db.execute(
                "SELECT s.id FROM schedule s JOIN content c ON c.id=s.content_id WHERE c.project_id=?",
                (pid,),
            ).fetchall()

        assert len(items) == 0
        assert len(contents) == 0
        assert len(schedules) == 0

    def test_clear_all_flash_shows_count(self, client, patch_env):
        """mode=all: флеш содержит число удалённых пунктов."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db)
            _create_plan_item(db, pid, status="proposed")
            _create_plan_item(db, pid, status="approved")

        resp = client.post(f"/plan/clear/{slug}/all")
        assert resp.status_code == 200
        assert "2" in resp.text or "Удалено" in resp.text or "🧹" in resp.text


class TestClearUnpublished:

    def test_clear_unpublished_keeps_published(self, client, patch_env):
        """mode=unpublished: опубликованный пункт остаётся, остальные удалены."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db)

            # published — должен остаться
            cid_pub = _create_content(db, pid, status="approved")
            _create_schedule(db, cid_pub, sched_status="published")
            item_pub = _create_plan_item(
                db, pid, status="generated", content_id=cid_pub, title="Опубликован"
            )

            # proposed без контента — должен удалиться
            item_prop = _create_plan_item(db, pid, status="proposed", title="Предложен")

            # approved с контентом planned — должен удалиться
            cid_planned = _create_content(db, pid, status="approved")
            _create_schedule(db, cid_planned, sched_status="planned")
            item_approved = _create_plan_item(
                db, pid, status="approved", content_id=cid_planned, title="Одобрен с контентом"
            )

        resp = client.post(f"/plan/clear/{slug}/unpublished")
        assert resp.status_code == 200

        with get_db() as db:
            remaining_items = db.execute(
                "SELECT id FROM plan_items WHERE project_id=?", (pid,)
            ).fetchall()
            remaining_ids = [r["id"] for r in remaining_items]

        # Опубликованный остался
        assert item_pub in remaining_ids
        # Предложенный и одобренный удалены
        assert item_prop not in remaining_ids
        assert item_approved not in remaining_ids

    def test_clear_unpublished_manual_done_kept(self, client, patch_env):
        """mode=unpublished: manual_done тоже считается опубликованным."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db)

            cid = _create_content(db, pid)
            _create_schedule(db, cid, sched_status="manual_done")
            item_manual = _create_plan_item(
                db, pid, status="generated", content_id=cid, title="Ручная публикация"
            )
            item_prop = _create_plan_item(db, pid, status="proposed", title="Предложен")

        resp = client.post(f"/plan/clear/{slug}/unpublished")
        assert resp.status_code == 200

        with get_db() as db:
            remaining = [
                r["id"] for r in db.execute(
                    "SELECT id FROM plan_items WHERE project_id=?", (pid,)
                ).fetchall()
            ]

        assert item_manual in remaining
        assert item_prop not in remaining


class TestClearUnapproved:

    def test_clear_unapproved_deletes_only_unapproved(self, client, patch_env):
        """mode=unapproved: proposed/rejected/error удалены; approved/generating/generated остались."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db)

            # Должны удалиться
            item_prop = _create_plan_item(db, pid, status="proposed", title="Предложен")
            item_rej = _create_plan_item(db, pid, status="rejected", title="Отклонён")

            # error с content_id — каскад
            cid_err = _create_content(db, pid, status="approved")
            item_err = _create_plan_item(
                db, pid, status="error", content_id=cid_err, title="Ошибка с контентом"
            )

            # Должны остаться
            item_appr = _create_plan_item(db, pid, status="approved", title="Одобрен")
            item_gen = _create_plan_item(db, pid, status="generating", title="Генерируется")

            cid_generated = _create_content(db, pid)
            item_generated = _create_plan_item(
                db, pid, status="generated", content_id=cid_generated, title="Готово"
            )

        resp = client.post(f"/plan/clear/{slug}/unapproved")
        assert resp.status_code == 200

        with get_db() as db:
            remaining_ids = [
                r["id"] for r in db.execute(
                    "SELECT id FROM plan_items WHERE project_id=?", (pid,)
                ).fetchall()
            ]
            # error-контент удалён каскадом
            err_content = db.execute(
                "SELECT id FROM content WHERE id=?", (cid_err,)
            ).fetchone()

        # Удалённые
        assert item_prop not in remaining_ids
        assert item_rej not in remaining_ids
        assert item_err not in remaining_ids
        assert err_content is None  # каскад сработал

        # Оставшиеся
        assert item_appr in remaining_ids
        assert item_gen in remaining_ids
        assert item_generated in remaining_ids


class TestClearEdgeCases:

    def test_clear_unknown_mode_422(self, client, patch_env):
        """Неизвестный mode → 422."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db)

        resp = client.post(f"/plan/clear/{slug}/badmode")
        assert resp.status_code == 422

    def test_clear_unknown_slug_404(self, client, patch_env):
        """Несуществующий slug → 404."""
        _setup_db()
        resp = client.post("/plan/clear/nonexistent-project-xyz/all")
        assert resp.status_code == 404

    def test_clear_empty_plan_ok(self, client, patch_env):
        """Пустой план → 200, без ошибок."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db)
            # Пункты не создаём

        resp = client.post(f"/plan/clear/{slug}/all")
        assert resp.status_code == 200
        # Флеш «Нечего удалять»
        assert "Нечего" in resp.text or "plan-board-area" in resp.text

    def test_clear_empty_plan_unpublished_ok(self, client, patch_env):
        """Пустой план, mode=unpublished → 200."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db)

        resp = client.post(f"/plan/clear/{slug}/unpublished")
        assert resp.status_code == 200

    def test_clear_empty_plan_unapproved_ok(self, client, patch_env):
        """Пустой план, mode=unapproved → 200."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db)

        resp = client.post(f"/plan/clear/{slug}/unapproved")
        assert resp.status_code == 200
