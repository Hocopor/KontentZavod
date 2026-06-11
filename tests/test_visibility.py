"""
Тест «ничего не теряется»: все единицы контента видны хотя бы на одной странице.

Проверяем три конфигурации:
  (а) content без plan_item → должен быть виден в /queue (секция «Вне плана»)
  (б) content с plan_item каждого статуса → виден в /plan ИЛИ /queue
  (в) проект архивирован → весь его контент виден в /queue (секция «Вне плана»)

Дополнительно:
  - статус content=rejected виден в «Вне плана» (не фильтруется)
  - статус content=published виден в «Вне плана» (старый флоу v1)

Проверка — наличие id или title контента в HTML ответа соответствующей страницы.
"""
import json
import sqlite3
from pathlib import Path

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _setup():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, name="TestVis", status="active", stage="running"):
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage, status)
        VALUES (?,?,?,'ца','тон','цели','cta','темы',?,?)
        """,
        (name.lower().replace(" ", "-"), name, "Описание", stage, status),
    )
    pid = cur.lastrowid
    for plat in ("telegram", "vk", "youtube", "instagram", "dzen"):
        db.execute(
            "INSERT OR IGNORE INTO project_platforms (project_id, platform, enabled, mode) VALUES (?,?,1,'auto')",
            (pid, plat),
        )
    return pid


def _create_content(db, project_id, status="approved", title=None):
    t = title or f"Content status={status}"
    cur = db.execute(
        "INSERT INTO content (project_id, type, title, status) VALUES (?,?,?,?)",
        (project_id, "post", t, status),
    )
    return cur.lastrowid


def _create_plan_item(db, project_id, content_id=None, item_status="proposed"):
    cur = db.execute(
        """
        INSERT INTO plan_items
            (project_id, platform, content_type, date, title, status, content_id)
        VALUES (?, 'telegram', 'post', '2026-06-15', ?, ?, ?)
        """,
        (project_id, f"Item {item_status}", item_status, content_id),
    )
    return cur.lastrowid


# ─── Тест (а): content без plan_item виден в «Вне плана» ─────────────────────


class TestOrphanVisibility:
    """Content без plan_item — все статусы видны в /queue."""

    # Все возможные статусы content
    _CONTENT_STATUSES = [
        "draft", "text_review", "production", "review",
        "approved", "rejected",
    ]

    def test_all_content_statuses_visible_in_queue_orphans(self, client, patch_env):
        """
        Для каждого статуса content без plan_item: контент виден в /queue
        в секции «Вне плана».
        """
        _setup()

        with get_db() as db:
            pid = _create_project(db, name="OA Active")

        content_ids = {}
        with get_db() as db:
            for st in self._CONTENT_STATUSES:
                cid = _create_content(db, pid, status=st, title=f"Orphan {st}")
                content_ids[st] = cid

        resp = client.get("/queue")
        assert resp.status_code == 200
        body = resp.text

        for st, cid in content_ids.items():
            # Каждый контент должен быть виден — по title или id
            assert str(cid) in body or f"Orphan {st}" in body, (
                f"Контент с status={st!r} id={cid} не найден в /queue"
            )

    def test_rejected_content_visible(self, client, patch_env):
        """Отклонённый контент (rejected) без plan_item виден в /queue — старый v1 баг."""
        _setup()
        with get_db() as db:
            pid = _create_project(db, name="OA Rejected")
            cid = _create_content(db, pid, status="rejected", title="RejectVisible")

        resp = client.get("/queue")
        assert resp.status_code == 200
        assert "RejectVisible" in resp.text or str(cid) in resp.text


# ─── Тест (б): content с plan_item — виден в /plan или /queue ─────────────────


class TestPlanItemVisibility:
    """
    plan_item в каждом статусе с привязанным content → виден в /plan (шахматка)
    или в /queue (шахматка или «Вне плана»).
    """

    # Все статусы plan_items
    _PLAN_STATUSES = ["proposed", "approved", "rejected", "generating", "generated", "error"]

    def test_plan_items_visible(self, client, patch_env):
        """
        Для каждого статуса plan_item: контент виден в /plan или /queue.
        """
        _setup()

        with get_db() as db:
            pid = _create_project(db, name="PB Running")

        item_ids = {}
        with get_db() as db:
            for st in self._PLAN_STATUSES:
                # Для generated/error нужен content_id, для остальных нет
                if st in ("generated", "error"):
                    cid = _create_content(db, pid, status="approved", title=f"Item {st}")
                    item_ids[st] = _create_plan_item(db, pid, content_id=cid, item_status=st)
                else:
                    item_ids[st] = _create_plan_item(db, pid, content_id=None, item_status=st)

        # /plan должна показывать все статусы
        resp_plan = client.get("/plan?project=pb-running&year=2026&month=6")
        assert resp_plan.status_code == 200
        plan_body = resp_plan.text

        # /queue показывает generating/generated/error
        resp_queue = client.get("/queue?project=pb-running&month=2026-06")
        assert resp_queue.status_code == 200
        queue_body = resp_queue.text

        for st, iid in item_ids.items():
            title_marker = f"Item {st}"
            visible_in_plan = title_marker in plan_body or str(iid) in plan_body
            visible_in_queue = title_marker in queue_body or str(iid) in queue_body

            assert visible_in_plan or visible_in_queue, (
                f"plan_item со status={st!r} id={iid} не найден ни в /plan, ни в /queue"
            )


# ─── Тест (в): архивированный проект — контент в «Вне плана» ─────────────────


class TestArchivedProjectVisibility:
    """
    Контент архивированного проекта (даже с plan_item) виден в /queue «Вне плана».
    """

    def test_archived_project_content_visible(self, client, patch_env):
        """
        Создаём проект, наполняем контентом, архивируем →
        контент виден в /queue «Вне плана».
        """
        _setup()

        # Создаём через клиент (проект активный)
        resp = client.post("/projects/new", data={"name": "АрхивПроект"}, follow_redirects=False)
        assert resp.status_code == 303
        slug = resp.headers["location"].rstrip("/").split("/")[-1]

        with get_db() as db:
            project_id = db.execute("SELECT id FROM projects WHERE slug=?", (slug,)).fetchone()["id"]
            # Контент без plan_item
            cid_orphan = _create_content(db, project_id, status="approved", title="ArchOrphan")
            # Контент с plan_item (generated)
            cid_with_pi = _create_content(db, project_id, status="approved", title="ArchWithPlan")
            _create_plan_item(db, project_id, content_id=cid_with_pi, item_status="generated")

        # Архивируем проект
        resp = client.post(f"/projects/{slug}/archive", follow_redirects=False)
        assert resp.status_code == 303

        # Оба контента должны быть видны в /queue «Вне плана»
        resp = client.get("/queue")
        assert resp.status_code == 200
        body = resp.text

        assert "ArchOrphan" in body or str(cid_orphan) in body, (
            "Осиротевший контент архивированного проекта не виден в /queue"
        )
        assert "ArchWithPlan" in body or str(cid_with_pi) in body, (
            "Контент с plan_item архивированного проекта не виден в /queue"
        )


# ─── Тест: delete-all orphans ─────────────────────────────────────────────────


class TestDeleteAllOrphans:
    """POST /queue/orphans/delete-all удаляет весь осиротевший контент."""

    def test_delete_all_removes_all_orphans(self, client, patch_env):
        """После delete-all ни один осиротевший контент не остаётся."""
        _setup()

        with get_db() as db:
            pid = _create_project(db, name="DelAll")
            for st in ("draft", "approved", "rejected"):
                _create_content(db, pid, status=st, title=f"DelAll {st}")

        resp = client.post(
            "/queue/orphans/delete-all",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        # В БД контент исчез
        with get_db() as db:
            count = db.execute("SELECT COUNT(*) FROM content WHERE project_id=?", (pid,)).fetchone()[0]
        assert count == 0

    def test_delete_all_with_files(self, client, patch_env):
        """delete-all удаляет и файлы контента."""
        _setup()

        with get_db() as db:
            pid = _create_project(db, name="DelAllFiles")
            cid = _create_content(db, pid, status="approved", title="WithFile")

        # Создаём временный файл
        data_dir = Path(str(cfg_module.settings.data_dir_absolute))
        videos_dir = data_dir / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        fake_file = videos_dir / f"{cid}.mp4"
        fake_file.write_text("fake")

        resp = client.post(
            "/queue/orphans/delete-all",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert not fake_file.exists(), "Файл не был удалён при delete-all"
