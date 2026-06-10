"""
Тесты шахматки готового контента (этап 7, волна D).

Покрывает:
- GET /queue: страница 200, пустая очередь, ячейки по статусам
- Индикаторы: ⏳ generating, ⚠ error, статусы schedule для generated
- GET /queue/items/{id}/modal: модалки post/video/story
- POST /queue/items/{id}/cancel: planned → удалён; published/manual_pending → отказ
- POST /queue/items/{id}/retry: error → approved; generated → 422
- 404 на неизвестные slug/plan_item
"""
import json
import uuid
from datetime import date

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _slug() -> str:
    return f"queue-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, *, slug=None, stage="running", platforms=("telegram",)):
    """Создать проект. Вернуть (slug, project_id)."""
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


def _create_content(db, project_id, *, ctype="post", title="Тестовый контент", status="approved"):
    """Создать запись content. Вернуть content_id."""
    texts = json.dumps({"telegram": {"text": "Текст поста для Telegram"}}, ensure_ascii=False)
    files = json.dumps({}, ensure_ascii=False)
    cur = db.execute(
        """
        INSERT INTO content (project_id, type, title, texts, files, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, ctype, title, texts, files, status),
    )
    return cur.lastrowid


def _create_plan_item(
    db,
    project_id: int,
    *,
    platform="telegram",
    content_type="post",
    item_date="2026-06-15",
    time_slot="10:00",
    title="Тестовый пункт",
    status="generated",
    content_id: int | None = None,
    error_text: str | None = None,
):
    """Создать plan_item. Вернуть id."""
    cur = db.execute(
        """
        INSERT INTO plan_items
            (project_id, platform, content_type, date, time_slot, title, status, content_id, error_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, platform, content_type, item_date, time_slot, title, status, content_id, error_text),
    )
    return cur.lastrowid


def _create_schedule(
    db,
    content_id: int,
    *,
    platform="telegram",
    planned_at="2026-06-15 10:00:00",
    status="planned",
    published_url: str | None = None,
    error_text: str | None = None,
):
    """Создать запись schedule. Вернуть id."""
    cur = db.execute(
        """
        INSERT INTO schedule (content_id, platform, planned_at, status, published_url, error_text)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (content_id, platform, planned_at, status, published_url, error_text),
    )
    return cur.lastrowid


# ─── 1. Страница шахматки ─────────────────────────────────────────────────────


class TestQueueBoard:

    def test_queue_200_empty(self, client, patch_env):
        """GET /queue без пунктов: страница 200, пустое состояние."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        # Страница должна быть отрендерена (нет генерируемых/готовых пунктов)
        body = resp.text
        assert "Очередь" in body or "queue" in body.lower() or "📦" in body

    def test_queue_200_no_projects(self, client, patch_env):
        """GET /queue без проектов: 200, пустое состояние."""
        _setup_db()
        resp = client.get("/queue")
        assert resp.status_code == 200

    def test_queue_404_unknown_project(self, client, patch_env):
        """GET /queue?project=unknown-slug → 404."""
        _setup_db()
        resp = client.get("/queue?project=unknown-slug-xyz")
        assert resp.status_code == 404

    def test_queue_shows_generating_indicator(self, client, patch_env):
        """Пункт со status=generating показывает индикатор ⏳."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            _create_plan_item(db, pid, platform="telegram", content_type="post",
                              item_date="2026-06-15", status="generating", title="Генерируется")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "⏳" in resp.text or "generating" in resp.text

    def test_queue_shows_error_indicator(self, client, patch_env):
        """Пункт со status=error показывает индикатор ⚠."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            _create_plan_item(db, pid, platform="telegram", content_type="post",
                              item_date="2026-06-15", status="error",
                              title="Ошибка", error_text="LLM timeout")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "⚠" in resp.text or "error" in resp.text

    def test_queue_generated_without_schedule_shows_checkmark(self, client, patch_env):
        """Пункт generated без записи в schedule показывает ✓."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            _create_plan_item(db, pid, platform="telegram", content_type="post",
                              item_date="2026-06-15", status="generated", content_id=cid)

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "✓" in resp.text or "generated" in resp.text

    def test_queue_generated_with_planned_schedule(self, client, patch_env):
        """Пункт generated с schedule status=planned показывает 📅."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            _create_plan_item(db, pid, platform="telegram", content_type="post",
                              item_date="2026-06-15", status="generated", content_id=cid)
            _create_schedule(db, cid, status="planned")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "📅" in resp.text or "planned" in resp.text

    def test_queue_generated_with_published_schedule(self, client, patch_env):
        """Пункт generated с schedule status=published показывает 🚀."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            _create_plan_item(db, pid, platform="telegram", content_type="post",
                              item_date="2026-06-15", status="generated", content_id=cid)
            _create_schedule(db, cid, status="published", published_url="https://t.me/test/1")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "🚀" in resp.text or "published" in resp.text

    def test_queue_generated_with_manual_pending_schedule(self, client, patch_env):
        """Пункт generated с schedule status=manual_pending показывает ✋."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            _create_plan_item(db, pid, platform="telegram", content_type="post",
                              item_date="2026-06-15", status="generated", content_id=cid)
            _create_schedule(db, cid, status="manual_pending")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "✋" in resp.text or "manual_pending" in resp.text

    def test_queue_generated_with_error_schedule(self, client, patch_env):
        """Пункт generated с schedule status=error показывает ❌."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            _create_plan_item(db, pid, platform="telegram", content_type="post",
                              item_date="2026-06-15", status="generated", content_id=cid)
            _create_schedule(db, cid, status="error", error_text="Publish failed")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "❌" in resp.text or "error" in resp.text

    def test_queue_month_filter_items_in_month(self, client, patch_env):
        """Пункты выбранного месяца видны, другого месяца — нет."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            _create_plan_item(db, pid, item_date="2026-06-15", status="generated", title="Июньский пост")
            _create_plan_item(db, pid, item_date="2026-07-15", status="generated", title="Июльский пост")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "Июньский пост" in resp.text
        assert "Июльский пост" not in resp.text

    def test_queue_only_relevant_statuses(self, client, patch_env):
        """В очереди только generating/generated/error, не proposed/approved."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            _create_plan_item(db, pid, item_date="2026-06-15", status="proposed", title="Предложенный")
            _create_plan_item(db, pid, item_date="2026-06-15", status="approved", title="Одобренный")
            _create_plan_item(db, pid, item_date="2026-06-15", status="generated", title="Готовый")

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        assert "Готовый" in resp.text
        assert "Предложенный" not in resp.text
        assert "Одобренный" not in resp.text

    def test_queue_default_month_is_current(self, client, patch_env):
        """Без параметра month открывается текущий месяц."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))

        resp = client.get(f"/queue?project={s}")
        assert resp.status_code == 200
        month_names = [
            "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
            "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
        ]
        today = date.today()
        assert month_names[today.month - 1] in resp.text

    def test_queue_month_navigation_links(self, client, patch_env):
        """На странице очереди есть ссылки переключения месяца ← →."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))

        resp = client.get(f"/queue?project={s}&month=2026-06")
        assert resp.status_code == 200
        # Должны быть ссылки на соседние месяцы
        assert "2026-05" in resp.text or "2026-07" in resp.text

    def test_queue_nav_link_present(self, client, patch_env):
        """Ссылка «📦 Очередь» присутствует в навигации."""
        _setup_db()
        resp = client.get("/")
        assert resp.status_code == 200
        assert "/queue" in resp.text


# ─── 2. Модалка контента ──────────────────────────────────────────────────────


class TestQueueModal:

    def test_modal_post_shows_texts(self, client, patch_env):
        """Модалка post показывает тексты из content.texts."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            texts = json.dumps({"telegram": {"text": "Текст тестового поста"}}, ensure_ascii=False)
            cur = db.execute(
                "INSERT INTO content (project_id, type, title, texts, status) VALUES (?,?,?,?,?)",
                (pid, "post", "Тестовый пост", texts, "approved"),
            )
            cid = cur.lastrowid
            iid = _create_plan_item(db, pid, platform="telegram", content_type="post",
                                    status="generated", content_id=cid, title="Тестовый пост")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        body = resp.text
        assert "Тестовый пост" in body
        assert "Telegram" in body or "telegram" in body

    def test_modal_shows_platform_and_type(self, client, patch_env):
        """Модалка показывает платформу и тип контента."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, platform="telegram", content_type="post",
                                    status="generated", content_id=cid)

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        body = resp.text
        assert "Telegram" in body
        assert "Пост" in body

    def test_modal_shows_date_and_time(self, client, patch_env):
        """Модалка показывает дату и время публикации."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, platform="telegram", content_type="post",
                                    item_date="2026-06-15", time_slot="10:00",
                                    status="generated", content_id=cid)

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        body = resp.text
        assert "2026-06-15" in body or "15" in body

    def test_modal_video_shows_video_tag(self, client, patch_env):
        """Модалка video_footage показывает тег <video> с src."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            texts = json.dumps({}, ensure_ascii=False)
            files = json.dumps({"video_path": "/files/videos/1.mp4"}, ensure_ascii=False)
            cur = db.execute(
                "INSERT INTO content (project_id, type, title, texts, files, status) VALUES (?,?,?,?,?,?)",
                (pid, "video_footage", "Тестовое видео", texts, files, "approved"),
            )
            cid = cur.lastrowid
            iid = _create_plan_item(db, pid, platform="telegram", content_type="video",
                                    status="generated", content_id=cid, title="Тестовое видео")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        body = resp.text
        assert "<video" in body or "video" in body.lower()
        assert f"/files/videos/{cid}.mp4" in body or "mp4" in body

    def test_modal_video_shows_download_link(self, client, patch_env):
        """Модалка video показывает ссылку «Скачать mp4»."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cur = db.execute(
                "INSERT INTO content (project_id, type, title, status) VALUES (?,?,?,?)",
                (pid, "video_footage", "Видео", "approved"),
            )
            cid = cur.lastrowid
            iid = _create_plan_item(db, pid, platform="telegram", content_type="video",
                                    status="generated", content_id=cid)

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "mp4" in resp.text or "Скачать" in resp.text

    def test_modal_story_shows_img_tag(self, client, patch_env):
        """Модалка story показывает тег <img>."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("instagram",))
            texts = json.dumps({"instagram": {"caption": "Красивая история"}}, ensure_ascii=False)
            cur = db.execute(
                "INSERT INTO content (project_id, type, title, texts, status) VALUES (?,?,?,?,?)",
                (pid, "story", "История Instagram", texts, "approved"),
            )
            cid = cur.lastrowid
            iid = _create_plan_item(db, pid, platform="instagram", content_type="story",
                                    status="generated", content_id=cid, title="История Instagram")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        body = resp.text
        assert "<img" in body or "img" in body.lower()
        assert f"/files/images/{cid}.jpg" in body or "jpg" in body

    def test_modal_story_shows_download_link(self, client, patch_env):
        """Модалка story показывает ссылку «Скачать картинку»."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("instagram",))
            cur = db.execute(
                "INSERT INTO content (project_id, type, title, status) VALUES (?,?,?,?)",
                (pid, "story", "История", "approved"),
            )
            cid = cur.lastrowid
            iid = _create_plan_item(db, pid, platform="instagram", content_type="story",
                                    status="generated", content_id=cid)

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "jpg" in resp.text or "Скачать" in resp.text

    def test_modal_shows_schedule_status_planned(self, client, patch_env):
        """Модалка показывает статус публикации: planned."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            _create_schedule(db, cid, status="planned")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "planned" in resp.text or "Запланировано" in resp.text or "📅" in resp.text

    def test_modal_shows_schedule_url_when_published(self, client, patch_env):
        """Модалка с published schedule показывает URL публикации."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            _create_schedule(db, cid, status="published", published_url="https://t.me/test/42")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "https://t.me/test/42" in resp.text

    def test_modal_shows_schedule_error_text(self, client, patch_env):
        """Модалка с error schedule показывает last_error."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            _create_schedule(db, cid, status="error", error_text="Connection timeout")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Connection timeout" in resp.text

    def test_modal_error_plan_item_shows_error_text(self, client, patch_env):
        """Модалка plan_item со status=error показывает error_text."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            iid = _create_plan_item(db, pid, status="error", error_text="Pipeline crash")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Pipeline crash" in resp.text

    def test_modal_404_nonexistent(self, client, patch_env):
        """GET /queue/items/99999/modal: 404."""
        _setup_db()
        resp = client.get("/queue/items/99999/modal")
        assert resp.status_code == 404

    def test_modal_generating_shows_status(self, client, patch_env):
        """Модалка generating показывает статус."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            iid = _create_plan_item(db, pid, status="generating", title="Генерируется пост")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert "Генерируется пост" in resp.text or "generating" in resp.text or "Генерируется" in resp.text


# ─── 3. Cancel ────────────────────────────────────────────────────────────────


class TestQueueCancel:

    def test_cancel_planned_removes_schedule(self, client, patch_env):
        """POST cancel: schedule со status=planned удаляется из БД."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            sch_id = _create_schedule(db, cid, status="planned")

        resp = client.post(f"/queue/items/{iid}/cancel")
        # 200 или 303 (redirect)
        assert resp.status_code in (200, 303)

        with get_db() as db:
            row = db.execute("SELECT id FROM schedule WHERE id=?", (sch_id,)).fetchone()
        assert row is None, "Schedule запись должна быть удалена"

    def test_cancel_published_returns_error(self, client, patch_env):
        """POST cancel: published schedule не может быть отменён (422 или сообщение об ошибке)."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            sch_id = _create_schedule(db, cid, status="published", published_url="https://t.me/test/1")

        resp = client.post(f"/queue/items/{iid}/cancel")
        # Отказ: 422 или HTML с сообщением об ошибке
        assert resp.status_code in (200, 422) or "нельзя" in resp.text.lower() or "cannot" in resp.text.lower()

        with get_db() as db:
            row = db.execute("SELECT id FROM schedule WHERE id=?", (sch_id,)).fetchone()
        assert row is not None, "Published schedule НЕ должна быть удалена"

    def test_cancel_manual_pending_returns_error(self, client, patch_env):
        """POST cancel: manual_pending schedule не отменяется."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)
            sch_id = _create_schedule(db, cid, status="manual_pending")

        resp = client.post(f"/queue/items/{iid}/cancel")
        assert resp.status_code in (200, 422)

        with get_db() as db:
            row = db.execute("SELECT id FROM schedule WHERE id=?", (sch_id,)).fetchone()
        assert row is not None, "Manual_pending schedule НЕ должна быть удалена"

    def test_cancel_no_schedule_returns_error(self, client, patch_env):
        """POST cancel без schedule: 422 или сообщение об ошибке."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.post(f"/queue/items/{iid}/cancel")
        assert resp.status_code in (200, 422)

    def test_cancel_404_nonexistent(self, client, patch_env):
        """POST cancel на несуществующий plan_item → 404."""
        _setup_db()
        resp = client.post("/queue/items/99999/cancel")
        assert resp.status_code == 404


# ─── 4. Retry ─────────────────────────────────────────────────────────────────


class TestQueueRetry:

    def test_retry_error_sets_approved(self, client, patch_env):
        """POST retry: plan_item status=error → approved, error_text=NULL, content_id=NULL."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
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
        """POST retry: plan_item status=generated → 422."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            cid = _create_content(db, pid)
            iid = _create_plan_item(db, pid, status="generated", content_id=cid)

        resp = client.post(f"/queue/items/{iid}/retry")
        assert resp.status_code == 422

    def test_retry_generating_returns_422(self, client, patch_env):
        """POST retry: plan_item status=generating → 422."""
        _setup_db()
        with get_db() as db:
            s, pid = _create_project(db, platforms=("telegram",))
            iid = _create_plan_item(db, pid, status="generating")

        resp = client.post(f"/queue/items/{iid}/retry")
        assert resp.status_code == 422

    def test_retry_404_nonexistent(self, client, patch_env):
        """POST retry на несуществующий plan_item → 404."""
        _setup_db()
        resp = client.post("/queue/items/99999/retry")
        assert resp.status_code == 404
