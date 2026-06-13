"""
Тесты /queue — шахматка, модалка, действия, ручная публикация, «вне плана».

Покрывает:
- GET /queue: 200, пустое состояние, индикаторы, фильтр месяца, empty-state-подсказка
- GET /queue/items/{id}/modal: post/video/story, статусы schedule, 404
- POST /queue/items/{id}/cancel: planned → удалён; published/manual_pending → 422
- POST /queue/items/{id}/regen: любой статус → approved, content удалён, файлы удалены
- POST /queue/items/{id}/delete: → rejected, content удалён
- POST /queue/items/{id}/retry: error → approved (обратная совместимость)
- POST /queue/schedules/{id}/manual-done: manual_pending → manual_done с URL
- POST /queue/orphans/{id}/delete: orphan content удалён
- Секция ручной публикации: manual_pending виден
- Секция «вне плана»: осиротевший контент виден и удаляется
- ENABLE_SCHEDULER=0 → предупреждение вверху страницы
"""
import json
import uuid
from pathlib import Path

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _slug() -> str:
    return f"queue-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, *, slug=None, stage="running", platforms=("telegram",)):
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage)
        VALUES (?, 'Тест Очередь', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', ?)
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
    return s, project_id


def _create_content(
    db, project_id, *, ctype="post", title="Тестовый контент",
    status="approved", texts=None, files=None
):
    t = texts or json.dumps({"telegram": {"text": "Текст поста для Telegram"}}, ensure_ascii=False)
    f = files or json.dumps({}, ensure_ascii=False)
    cur = db.execute(
        "INSERT INTO content (project_id, type, title, texts, files, status) VALUES (?,?,?,?,?,?)",
        (project_id, ctype, title, t, f, status),
    )
    return cur.lastrowid


def _create_plan_item(
    db, project_id, *,
    platform="telegram", content_type="post",
    item_date="2026-06-15", time_slot="10:00",
    title="Тестовый пункт", status="generated",
    content_id=None, error_text=None,
):
    cur = db.execute(
        """
        INSERT INTO plan_items
            (project_id, platform, content_type, date, time_slot, title,
             status, content_id, error_text)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (project_id, platform, content_type, item_date, time_slot,
         title, status, content_id, error_text),
    )
    return cur.lastrowid


def _create_schedule(
    db, content_id, *,
    platform="telegram",
    planned_at="2026-06-15 10:00:00",
    status="planned",
    published_url=None,
    error_text=None,
):
    cur = db.execute(
        """
        INSERT INTO schedule
            (content_id, platform, planned_at, status, published_url, error_text)
        VALUES (?,?,?,?,?,?)
        """,
        (content_id, platform, planned_at, status, published_url, error_text),
    )
    return cur.lastrowid


# ─── 1. Страница шахматки ─────────────────────────────────────────────────────


class TestQueueBoard:

    def test_queue_200_empty_month(self, client, patch_env):
        """GET /queue без пунктов: 200, пустое состояние со ссылкой на /plan."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        body = resp.text
        # Пустое состояние должно содержать подсказку и ссылку на /plan
        assert "Публикация" in body or "Очередь" in body
        assert "/plan" in body

    def test_queue_200_no_projects(self, client, patch_env):
        """GET /queue без проектов: 200, пустое состояние."""
        _setup_db()
        resp = client.get("/queue")
        assert resp.status_code == 200

    def test_queue_404_unknown_project(self, client, patch_env):
        """GET /queue?project=unknown → 404."""
        _setup_db()
        resp = client.get("/queue?project=unknown-slug-xyz")
        assert resp.status_code == 404

    def test_queue_shows_generating_dot(self, client, patch_env):
        """Пункт со status=generating: точка с классом dot-generating."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            _create_plan_item(db, pid, status="generating", title="Генерируется")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "dot-generating" in resp.text or "generating" in resp.text

    def test_queue_shows_error_dot(self, client, patch_env):
        """Пункт со status=error: точка dot-error."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            _create_plan_item(db, pid, status="error", error_text="LLM timeout")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "dot-error" in resp.text or "error" in resp.text

    def test_queue_generated_without_schedule(self, client, patch_env):
        """generated без schedule: dot-generated."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "dot-generated" in resp.text or "generated" in resp.text

    def test_queue_generated_with_planned(self, client, patch_env):
        """generated с schedule planned: dot-planned."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            _create_plan_item(db, pid, status="generated", content_id=cid)
            _create_schedule(db, cid, status="planned")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "dot-planned" in resp.text or "planned" in resp.text

    def test_queue_generated_with_published(self, client, patch_env):
        """generated с schedule published: dot-published."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            _create_plan_item(db, pid, status="generated", content_id=cid)
            _create_schedule(db, cid, status="published", published_url="https://t.me/x/1")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "dot-published" in resp.text or "published" in resp.text

    def test_queue_generated_with_manual_pending(self, client, patch_env):
        """generated с schedule manual_pending: dot-manual."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            _create_plan_item(db, pid, status="generated", content_id=cid)
            _create_schedule(db, cid, status="manual_pending")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "dot-manual" in resp.text or "manual" in resp.text

    def test_queue_generated_with_error_schedule(self, client, patch_env):
        """generated с schedule error: dot-error."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            _create_plan_item(db, pid, status="generated", content_id=cid)
            _create_schedule(db, cid, status="error", error_text="Publish failed")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "dot-error" in resp.text or "error" in resp.text

    def test_queue_month_filter(self, client, patch_env):
        """Пункты другого месяца не попадают в шахматку."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid1 = _create_content(db, pid, title="Июньский")
            cid2 = _create_content(db, pid, title="Июльский")
            _create_plan_item(db, pid, item_date="2026-06-15", status="generated",
                              content_id=cid1, title="Июньский пост")
            _create_plan_item(db, pid, item_date="2026-07-15", status="generated",
                              content_id=cid2, title="Июльский пост")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "Июньский пост" in resp.text
        assert "Июльский пост" not in resp.text

    def test_queue_only_generating_generated_error(self, client, patch_env):
        """В шахматке только generating/generated/error, не proposed/approved."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            _create_plan_item(db, pid, status="proposed", title="Предложенный")
            _create_plan_item(db, pid, status="approved", title="Одобренный")
            _create_plan_item(db, pid, status="generated", content_id=cid, title="Готовый")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "Готовый" in resp.text
        assert "Предложенный" not in resp.text
        assert "Одобренный" not in resp.text

    def test_queue_navigation_links(self, client, patch_env):
        """Ссылки переключения месяцев присутствуют."""
        _setup_db()
        with get_db() as db:
            s, _ = _create_project(db)

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "2026-05" in resp.text or "2026-07" in resp.text

    def test_queue_nav_link_present(self, client, patch_env):
        """Ссылка /queue присутствует в навигации."""
        _setup_db()
        resp = client.get("/")
        assert resp.status_code == 200
        assert "/queue" in resp.text

    def test_queue_scheduler_warning_when_disabled(self, client, patch_env, monkeypatch):
        """ENABLE_SCHEDULER=0 → предупреждение об автопубликации."""
        monkeypatch.setenv("ENABLE_SCHEDULER", "0")
        _setup_db()
        with get_db() as db:
            s, _ = _create_project(db)

        resp = client.get(f"/queue?project={s}")
        assert resp.status_code == 200
        assert "Автопубликация выключена" in resp.text or "ENABLE_SCHEDULER" in resp.text

    def test_queue_legend_present(self, client, patch_env):
        """Легенда с подписями присутствует, когда есть данные."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "Генерируется" in resp.text
        assert "Запланировано" in resp.text

    def test_queue_empty_state_hint_links_to_plan(self, client, patch_env):
        """Пустое состояние содержит ссылку на /plan."""
        _setup_db()
        with get_db() as db:
            s, _ = _create_project(db)

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "/plan" in resp.text


# ─── 2. Модалка ───────────────────────────────────────────────────────────────


class TestQueueModal:

    def test_modal_post_shows_text(self, client, patch_env):
        """Модалка post: текст поста виден."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            texts = json.dumps({"telegram": {"text": "Текст тестового поста"}}, ensure_ascii=False)
            cur = db.execute(
                "INSERT INTO content (project_id, type, title, texts, status) VALUES (?,?,?,?,?)",
                (pid, "post", "Тестовый пост", texts, "approved"),
            )
            cid = cur.lastrowid
            iid = _create_plan_item(db, pid, status="generated", content_id=cid, title="Тестовый пост")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Тестовый пост" in resp.text
        assert "Telegram" in resp.text or "telegram" in resp.text

    def test_modal_shows_platform_type_date(self, client, patch_env):
        """Модалка: платформа, тип, дата."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, item_date="2026-06-20", time_slot="14:00",
                                    status="generated", content_id=cid)

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Telegram" in resp.text
        assert "2026-06-20" in resp.text

    def test_modal_video_shows_video_tag(self, client, patch_env):
        """Модалка video_footage: тег <video> и ссылка на mp4."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cur = db.execute(
                "INSERT INTO content (project_id, type, title, status) VALUES (?,?,?,?)",
                (pid, "video_footage", "Видео", "approved"),
            )
            cid = cur.lastrowid
            iid = _create_plan_item(db, pid, content_type="video",
                                    status="generated", content_id=cid)

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "<video" in resp.text
        assert f"/files/videos/{cid}.mp4" in resp.text

    def test_modal_video_shows_download(self, client, patch_env):
        """Модалка video: ссылка «Скачать mp4»."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cur = db.execute(
                "INSERT INTO content (project_id, type, title, status) VALUES (?,?,?,?)",
                (pid, "video_footage", "Видео", "approved"),
            )
            cid = cur.lastrowid
            iid = _create_plan_item(db, pid, content_type="video",
                                    status="generated", content_id=cid)

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Скачать" in resp.text or "mp4" in resp.text

    def test_modal_story_shows_img(self, client, patch_env):
        """Модалка story: тег <img> и ссылка на jpg."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("instagram",))
            texts = json.dumps({"instagram": {"caption": "Красивая история"}}, ensure_ascii=False)
            cur = db.execute(
                "INSERT INTO content (project_id, type, title, texts, status) VALUES (?,?,?,?,?)",
                (pid, "story", "История", texts, "approved"),
            )
            cid = cur.lastrowid
            iid = _create_plan_item(db, pid, platform="instagram", content_type="story",
                                    status="generated", content_id=cid)

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "<img" in resp.text
        assert f"/files/images/{cid}.jpg" in resp.text

    def test_modal_story_shows_caption(self, client, patch_env):
        """Модалка story: caption виден."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("instagram",))
            texts = json.dumps({"instagram": {"caption": "Мой красивый caption"}}, ensure_ascii=False)
            cur = db.execute(
                "INSERT INTO content (project_id, type, title, texts, status) VALUES (?,?,?,?,?)",
                (pid, "story", "История", texts, "approved"),
            )
            cid = cur.lastrowid
            iid = _create_plan_item(db, pid, platform="instagram", content_type="story",
                                    status="generated", content_id=cid)

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Мой красивый caption" in resp.text

    def test_modal_shows_planned_status(self, client, patch_env):
        """Модалка: schedule planned показывает статус."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            _create_schedule(db, cid, status="planned")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Запланировано" in resp.text or "📅" in resp.text

    def test_modal_shows_published_url(self, client, patch_env):
        """Модалка: published schedule показывает URL."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            _create_schedule(db, cid, status="published", published_url="https://t.me/test/42")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "https://t.me/test/42" in resp.text

    def test_modal_shows_schedule_error_text(self, client, patch_env):
        """Модалка: error schedule показывает текст ошибки."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            _create_schedule(db, cid, status="error", error_text="Connection timeout")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Connection timeout" in resp.text

    def test_modal_error_item_shows_error_text(self, client, patch_env):
        """Модалка plan_item со status=error показывает error_text."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="error", error_text="Pipeline crash")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Pipeline crash" in resp.text

    def test_modal_generating_shows_status(self, client, patch_env):
        """Модалка generating: статус виден."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="generating", title="В процессе")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Генерируется" in resp.text or "generating" in resp.text

    def test_modal_404_nonexistent(self, client, patch_env):
        """GET /queue/items/99999/modal → 404."""
        _setup_db()
        resp = client.get("/queue/items/99999/modal")
        assert resp.status_code == 404

    def test_modal_has_regen_button_for_generated(self, client, patch_env):
        """Модалка generated: кнопка «Перегенерировать»."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Перегенерировать" in resp.text

    def test_modal_has_delete_button_for_generated(self, client, patch_env):
        """Модалка generated: кнопка «Удалить»."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Удалить" in resp.text

    def test_modal_has_cancel_button_for_planned(self, client, patch_env):
        """Модалка с planned schedule: кнопка «Отменить публикацию»."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            _create_schedule(db, cid, status="planned")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Отменить публикацию" in resp.text


# ─── 3. Cancel ────────────────────────────────────────────────────────────────


class TestQueueCancel:

    def test_cancel_planned_removes_schedule(self, client, patch_env):
        """cancel: planned schedule удаляется из БД."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            sch_id = _create_schedule(db, cid, status="planned")

        resp = client.post(f"/queue/items/{iid}/cancel")
        assert resp.status_code in (200, 303)

        with get_db() as db:
            row = db.execute("SELECT id FROM schedule WHERE id=?", (sch_id,)).fetchone()
        assert row is None

    def test_cancel_published_returns_error(self, client, patch_env):
        """cancel: published schedule — 422, запись остаётся."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            sch_id = _create_schedule(db, cid, status="published", published_url="https://t.me/1")

        resp = client.post(f"/queue/items/{iid}/cancel")
        assert resp.status_code == 422

        with get_db() as db:
            row = db.execute("SELECT id FROM schedule WHERE id=?", (sch_id,)).fetchone()
        assert row is not None

    def test_cancel_manual_pending_returns_error(self, client, patch_env):
        """cancel: manual_pending — 422, запись остаётся."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            sch_id = _create_schedule(db, cid, status="manual_pending")

        resp = client.post(f"/queue/items/{iid}/cancel")
        assert resp.status_code == 422

        with get_db() as db:
            row = db.execute("SELECT id FROM schedule WHERE id=?", (sch_id,)).fetchone()
        assert row is not None

    def test_cancel_no_schedule_returns_422(self, client, patch_env):
        """cancel без schedule → 422."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.post(f"/queue/items/{iid}/cancel")
        assert resp.status_code == 422

    def test_cancel_404_nonexistent(self, client, patch_env):
        """cancel на несуществующий plan_item → 404."""
        _setup_db()
        resp = client.post("/queue/items/99999/cancel")
        assert resp.status_code == 404


# ─── 4. Regen (перегенерация) ─────────────────────────────────────────────────


class TestQueueRegen:

    def test_regen_sets_approved_clears_content(self, client, patch_env):
        """regen: plan_item → approved, content_id=NULL, content удалён из БД."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            sch_id = _create_schedule(db, cid, status="planned")
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.post(f"/queue/items/{iid}/regen")
        assert resp.status_code == 200

        with get_db() as db:
            pi = db.execute("SELECT status, content_id, error_text FROM plan_items WHERE id=?", (iid,)).fetchone()
            content_row = db.execute("SELECT id FROM content WHERE id=?", (cid,)).fetchone()
            sch_row = db.execute("SELECT id FROM schedule WHERE id=?", (sch_id,)).fetchone()

        assert pi["status"] == "approved"
        assert pi["content_id"] is None
        assert pi["error_text"] is None
        assert content_row is None, "content должен быть удалён"
        assert sch_row is None, "schedule должен быть удалён"

    def test_regen_from_error_status(self, client, patch_env):
        """regen из error: plan_item → approved."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="error", error_text="LLM failed")

        resp = client.post(f"/queue/items/{iid}/regen")
        assert resp.status_code == 200

        with get_db() as db:
            pi = db.execute("SELECT status, error_text FROM plan_items WHERE id=?", (iid,)).fetchone()
        assert pi["status"] == "approved"
        assert pi["error_text"] is None

    def test_regen_deletes_files(self, client, patch_env, tmp_path):
        """regen удаляет файлы media/{id}/ и videos/{id}.mp4."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)

        # Создать фиктивные файлы
        media_dir = tmp_path / "media" / str(cid)
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / "voice.mp3").write_text("fake")
        vid = tmp_path / "videos" / f"{cid}.mp4"
        vid.parent.mkdir(parents=True, exist_ok=True)
        vid.write_text("fake")

        resp = client.post(f"/queue/items/{iid}/regen")
        assert resp.status_code == 200
        assert not media_dir.exists(), "media dir должен быть удалён"
        assert not vid.exists(), "mp4 должен быть удалён"

    def test_regen_404_nonexistent(self, client, patch_env):
        """regen на несуществующий plan_item → 404."""
        _setup_db()
        resp = client.post("/queue/items/99999/regen")
        assert resp.status_code == 404


# ─── 5. Delete (удаление) ─────────────────────────────────────────────────────


class TestQueueDelete:

    def test_delete_sets_rejected_removes_content(self, client, patch_env):
        """delete: plan_item → rejected, content удалён, schedule удалён."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            sch_id = _create_schedule(db, cid, status="planned")
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.post(f"/queue/items/{iid}/delete")
        assert resp.status_code == 200

        with get_db() as db:
            pi = db.execute("SELECT status, content_id FROM plan_items WHERE id=?", (iid,)).fetchone()
            content_row = db.execute("SELECT id FROM content WHERE id=?", (cid,)).fetchone()
            sch_row = db.execute("SELECT id FROM schedule WHERE id=?", (sch_id,)).fetchone()

        assert pi["status"] == "rejected"
        assert pi["content_id"] is None
        assert content_row is None
        assert sch_row is None

    def test_delete_error_item_sets_rejected(self, client, patch_env):
        """delete error plan_item → rejected."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="error", error_text="crash")

        resp = client.post(f"/queue/items/{iid}/delete")
        assert resp.status_code == 200

        with get_db() as db:
            pi = db.execute("SELECT status FROM plan_items WHERE id=?", (iid,)).fetchone()
        assert pi["status"] == "rejected"

    def test_delete_404_nonexistent(self, client, patch_env):
        """delete несуществующего plan_item → 404."""
        _setup_db()
        resp = client.post("/queue/items/99999/delete")
        assert resp.status_code == 404


# ─── 6. Retry (обратная совместимость) ────────────────────────────────────────


class TestQueueRetry:

    def test_retry_error_sets_approved(self, client, patch_env):
        """retry: error → approved, error_text=NULL, content_id=NULL."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="error",
                                    content_id=cid, error_text="LLM failed")

        resp = client.post(f"/queue/items/{iid}/retry")
        assert resp.status_code in (200, 303)

        with get_db() as db:
            row = db.execute(
                "SELECT status, error_text, content_id FROM plan_items WHERE id=?", (iid,)
            ).fetchone()
        assert row["status"] == "approved"
        assert row["error_text"] is None
        assert row["content_id"] is None

    def test_retry_generated_returns_422(self, client, patch_env):
        """retry: generated → 422."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.post(f"/queue/items/{iid}/retry")
        assert resp.status_code == 422

    def test_retry_generating_returns_422(self, client, patch_env):
        """retry: generating → 422."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="generating")

        resp = client.post(f"/queue/items/{iid}/retry")
        assert resp.status_code == 422

    def test_retry_404_nonexistent(self, client, patch_env):
        """retry несуществующего plan_item → 404."""
        _setup_db()
        resp = client.post("/queue/items/99999/retry")
        assert resp.status_code == 404


# ─── 7. Ручная публикация (manual-done) ───────────────────────────────────────


class TestQueueManualDone:

    def test_manual_done_updates_status(self, client, patch_env):
        """manual-done: schedule manual_pending → manual_done с URL."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            sch_id = _create_schedule(db, cid, status="manual_pending")

        resp = client.post(
            f"/queue/schedules/{sch_id}/manual-done",
            data={"published_url": "https://www.instagram.com/p/ABC123/"},
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute(
                "SELECT status, published_url FROM schedule WHERE id=?", (sch_id,)
            ).fetchone()
        assert row["status"] == "manual_done"
        assert row["published_url"] == "https://www.instagram.com/p/ABC123/"

    def test_manual_done_without_url(self, client, patch_env):
        """manual-done: URL не обязателен."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            sch_id = _create_schedule(db, cid, status="manual_pending")

        resp = client.post(f"/queue/schedules/{sch_id}/manual-done", data={})
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT status FROM schedule WHERE id=?", (sch_id,)).fetchone()
        assert row["status"] == "manual_done"

    def test_manual_done_wrong_status_returns_422(self, client, patch_env):
        """manual-done на planned schedule → 422."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            sch_id = _create_schedule(db, cid, status="planned")

        resp = client.post(f"/queue/schedules/{sch_id}/manual-done", data={})
        assert resp.status_code == 422

    def test_manual_done_404_nonexistent(self, client, patch_env):
        """manual-done несуществующего schedule → 404."""
        _setup_db()
        resp = client.post("/queue/schedules/99999/manual-done", data={})
        assert resp.status_code == 404

    def test_manual_section_shows_pending_items(self, client, patch_env):
        """Страница /queue: секция ручной публикации показывает manual_pending."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("instagram",))
            texts = json.dumps(
                {"instagram": {"caption": "Пост для инстаграм"}}, ensure_ascii=False
            )
            cid = _create_content(db, pid, ctype="post", texts=texts)
            _create_schedule(db, cid, platform="instagram", status="manual_pending")
            # plan_item нужен чтобы контент не попал в «вне плана»
            _create_plan_item(db, pid, platform="instagram", content_type="post",
                              status="generated", content_id=cid)

        resp = client.get(f"/queue?project={s}")
        assert resp.status_code == 200
        assert "Ручная публикация" in resp.text or "manual" in resp.text.lower()
        assert "Пост для инстаграм" in resp.text or "Instagram" in resp.text

    def test_manual_section_copy_text_instagram_uses_caption(self, client, patch_env):
        """Instagram: для копирования берём caption."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("instagram",))
            texts = json.dumps(
                {"instagram": {"caption": "Мой красивый caption для Instagram"}},
                ensure_ascii=False,
            )
            cid = _create_content(db, pid, ctype="post", texts=texts)
            _create_schedule(db, cid, platform="instagram", status="manual_pending")
            _create_plan_item(db, pid, platform="instagram", content_type="post",
                              status="generated", content_id=cid)

        resp = client.get(f"/queue?project={s}")
        assert resp.status_code == 200
        assert "Мой красивый caption для Instagram" in resp.text


# ─── 8. Reschedule (перенос даты/времени) ─────────────────────────────────────


class TestReschedule:

    def test_reschedule_planned_updates_datetime(self, client, patch_env):
        """reschedule: planned schedule обновляет planned_at."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            sch_id = _create_schedule(db, cid, status="planned", planned_at="2026-06-15 10:00:00")

        resp = client.post(
            f"/queue/items/{iid}/reschedule",
            data={"new_date": "2026-07-15", "new_time": "09:30"}
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT planned_at FROM schedule WHERE id=?", (sch_id,)).fetchone()
        assert row["planned_at"] == "2026-07-15 09:30:00"

    def test_reschedule_manual_pending_updates_datetime(self, client, patch_env):
        """reschedule: manual_pending schedule обновляет planned_at."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            sch_id = _create_schedule(db, cid, status="manual_pending", planned_at="2026-06-15 14:00:00")

        resp = client.post(
            f"/queue/items/{iid}/reschedule",
            data={"new_date": "2026-07-20", "new_time": "16:45"}
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT planned_at FROM schedule WHERE id=?", (sch_id,)).fetchone()
        assert row["planned_at"] == "2026-07-20 16:45:00"

    def test_reschedule_published_returns_422(self, client, patch_env):
        """reschedule: published schedule → 422."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            sch_id = _create_schedule(db, cid, status="published", planned_at="2026-06-15 10:00:00",
                                    published_url="https://t.me/x/1")

        resp = client.post(
            f"/queue/items/{iid}/reschedule",
            data={"new_date": "2026-07-15", "new_time": "09:30"}
        )
        assert resp.status_code == 422
        # Проверим, что planned_at не изменился
        with get_db() as db:
            row = db.execute("SELECT planned_at FROM schedule WHERE id=?", (sch_id,)).fetchone()
        assert row["planned_at"] == "2026-06-15 10:00:00"

    def test_reschedule_invalid_date_returns_422(self, client, patch_env):
        """reschedule: невалидная дата → 422."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            sch_id = _create_schedule(db, cid, status="planned")

        resp = client.post(
            f"/queue/items/{iid}/reschedule",
            data={"new_date": "не-дата", "new_time": "09:30"}
        )
        assert resp.status_code == 422

    def test_reschedule_no_content_returns_422(self, client, patch_env):
        """reschedule: нет content_id → 422."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            iid = _create_plan_item(db, pid, status="generated", content_id=None)

        resp = client.post(
            f"/queue/items/{iid}/reschedule",
            data={"new_date": "2026-07-15", "new_time": "09:30"}
        )
        assert resp.status_code == 422

    def test_reschedule_no_schedule_returns_422(self, client, patch_env):
        """reschedule: нет schedule → 422."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.post(
            f"/queue/items/{iid}/reschedule",
            data={"new_date": "2026-07-15", "new_time": "09:30"}
        )
        assert resp.status_code == 422

    def test_reschedule_404_nonexistent_item(self, client, patch_env):
        """reschedule: несуществующий plan_item → 404."""
        _setup_db()
        resp = client.post(
            "/queue/items/99999/reschedule",
            data={"new_date": "2026-07-15", "new_time": "09:30"}
        )
        assert resp.status_code == 404


# ─── 9. «Вне плана» (orphan content) ─────────────────────────────────────────


class TestQueueOrphans:

    def test_orphan_content_visible_in_section(self, client, patch_env):
        """Осиротевший контент (без plan_item) виден в секции «Вне плана»."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            # Контент без plan_item
            cid = _create_content(db, pid, title="Осиротевший пост")

        resp = client.get(f"/queue?project={s}")
        assert resp.status_code == 200
        assert "Вне плана" in resp.text or "orphan" in resp.text.lower() or str(cid) in resp.text

    def test_orphan_content_with_plan_item_not_in_section(self, client, patch_env):
        """Контент, связанный с plan_item, НЕ попадает в «Вне плана»."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid, title="Нормальный контент")
            _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.get(f"/queue?project={s}")
        assert resp.status_code == 200
        # Секция «Вне плана» не должна содержать этот контент (раздел может не отображаться)
        # Проверяем, что если секция есть, в ней нет нашего content_id
        body = resp.text
        if "orphan-row" in body:
            assert f"orphan-row-{cid}" not in body

    def test_orphan_delete_removes_content(self, client, patch_env):
        """DELETE orphan: контент удалён из БД."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid, title="Старый контент")
            sch_id = _create_schedule(db, cid, status="published", published_url="https://t.me/x")

        resp = client.post(f"/queue/orphans/{cid}/delete")
        assert resp.status_code == 200

        with get_db() as db:
            content_row = db.execute("SELECT id FROM content WHERE id=?", (cid,)).fetchone()
            sch_row = db.execute("SELECT id FROM schedule WHERE id=?", (sch_id,)).fetchone()
        assert content_row is None
        assert sch_row is None

    def test_orphan_delete_404_nonexistent(self, client, patch_env):
        """DELETE orphan несуществующего content_id → 404."""
        _setup_db()
        resp = client.post("/queue/orphans/99999/delete")
        assert resp.status_code == 404

    def test_orphan_delete_removes_files(self, client, patch_env, tmp_path):
        """DELETE orphan удаляет медиафайлы."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db)
            cid = _create_content(db, pid, title="Видеоконтент", ctype="video_footage")

        # Создать фиктивный файл
        vid = tmp_path / "videos" / f"{cid}.mp4"
        vid.parent.mkdir(parents=True, exist_ok=True)
        vid.write_text("fake")

        resp = client.post(f"/queue/orphans/{cid}/delete")
        assert resp.status_code == 200
        assert not vid.exists()
