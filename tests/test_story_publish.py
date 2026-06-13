"""
Тесты волны 8.5B: раздача слайдов сторис по HTTP + ручная очередь.

Покрывает:
- GET /files/stories/{id}/{n}.jpg: 200 (файл есть) / 404 (файл отсутствует)
- GET /files/images/{id}.jpg: отдаёт slide_1.jpg как первый слайд
- _get_manual_pending: slide_indexes и copy_text для story
- GET /queue/items/{id}/modal: слайды показываются для story со slides
"""
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Вспомогательные функции (аналог test_queue.py) ──────────────────────────


def _slug() -> str:
    return f"story-pub-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, *, slug=None, platforms=("vk",)):
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage)
        VALUES (?, 'Story Project', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', 'running')
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
    return s, project_id


def _create_content(db, project_id, *, ctype="story", title="Сторис",
                    status="approved", texts=None, files=None):
    t = texts or json.dumps({}, ensure_ascii=False)
    f = files or json.dumps({}, ensure_ascii=False)
    cur = db.execute(
        "INSERT INTO content (project_id, type, title, texts, files, status) VALUES (?,?,?,?,?,?)",
        (project_id, ctype, title, t, f, status),
    )
    return cur.lastrowid


def _create_plan_item(db, project_id, *, platform="vk", content_type="story",
                      item_date="2026-06-15", time_slot="12:00",
                      title="Story пункт", status="generated", content_id=None):
    cur = db.execute(
        """
        INSERT INTO plan_items
            (project_id, platform, content_type, date, time_slot, title,
             status, content_id)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (project_id, platform, content_type, item_date, time_slot,
         title, status, content_id),
    )
    return cur.lastrowid


def _create_schedule(db, content_id, *, platform="vk",
                     planned_at="2026-06-15 12:00:00", status="manual_pending"):
    cur = db.execute(
        """
        INSERT INTO schedule
            (content_id, platform, planned_at, status)
        VALUES (?,?,?,?)
        """,
        (content_id, platform, planned_at, status),
    )
    return cur.lastrowid


def _make_slide(content_id: int, idx: int) -> Path:
    """Создать фиктивный файл слайда в data_dir и вернуть путь."""
    media = cfg_module.settings.data_dir_absolute / "media" / str(content_id)
    media.mkdir(parents=True, exist_ok=True)
    p = media / f"slide_{idx}.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)  # минимальный JPEG-заголовок
    return p


# ─── 1. Раздача слайда по HTTP ───────────────────────────────────────────────


class TestServeStorySlide:

    def test_serve_story_slide_200(self, client, patch_env):
        """GET /files/stories/777/1.jpg → 200 image/jpeg (файл создан)."""
        _setup_db()
        cid = 777
        p = _make_slide(cid, 1)
        try:
            resp = client.get(f"/files/stories/{cid}/1.jpg")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("image/jpeg")
        finally:
            shutil.rmtree(
                cfg_module.settings.data_dir_absolute / "media" / str(cid),
                ignore_errors=True,
            )

    def test_serve_story_slide_404(self, client, patch_env):
        """GET /files/stories/99999/5.jpg → 404 (файла нет)."""
        _setup_db()
        resp = client.get("/files/stories/99999/5.jpg")
        assert resp.status_code == 404

    def test_serve_story_image_uses_slide1(self, client, patch_env):
        """GET /files/images/{id}.jpg отдаёт slide_1.jpg как первый слайд."""
        _setup_db()
        cid = 888
        p = _make_slide(cid, 1)
        try:
            resp = client.get(f"/files/images/{cid}.jpg")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("image/jpeg")
        finally:
            shutil.rmtree(
                cfg_module.settings.data_dir_absolute / "media" / str(cid),
                ignore_errors=True,
            )


# ─── 2. _get_manual_pending: slide_indexes и copy_text ───────────────────────


class TestManualPendingStory:

    def test_manual_pending_story_slide_indexes(self, patch_env):
        """_get_manual_pending возвращает slide_indexes=[1,2,3] и copy_text='подпись истории'."""
        _setup_db()
        files_json = json.dumps(
            {"slides": ["a", "b", "c"], "image_path": "a"}, ensure_ascii=False
        )
        texts_json = json.dumps(
            {"vk": {"caption": "подпись истории", "story_type": "carousel"}},
            ensure_ascii=False,
        )
        with get_db() as db:
            _, pid = _create_project(db, platforms=("vk",))
            cid = _create_content(
                db, pid,
                files=files_json,
                texts=texts_json,
                status="approved",
            )
            _create_plan_item(db, pid, content_id=cid, status="generated")
            _create_schedule(db, cid, platform="vk", status="manual_pending")

        from app.web.queue import _get_manual_pending
        with get_db() as db:
            items = _get_manual_pending(db, pid)

        assert len(items) == 1
        item = items[0]
        assert item["slide_indexes"] == [1, 2, 3]
        assert item["copy_text"] == "подпись истории"

    def test_manual_pending_story_no_slides(self, patch_env):
        """Story без slides в files → slide_indexes=[]."""
        _setup_db()
        texts_json = json.dumps(
            {"vk": {"caption": "простая история"}}, ensure_ascii=False
        )
        with get_db() as db:
            _, pid = _create_project(db, platforms=("vk",))
            cid = _create_content(
                db, pid,
                files=json.dumps({"image_path": "/some/path.jpg"}, ensure_ascii=False),
                texts=texts_json,
                status="approved",
            )
            _create_plan_item(db, pid, content_id=cid, status="generated")
            _create_schedule(db, cid, platform="vk", status="manual_pending")

        from app.web.queue import _get_manual_pending
        with get_db() as db:
            items = _get_manual_pending(db, pid)

        assert len(items) == 1
        assert items[0]["slide_indexes"] == []
        assert items[0]["copy_text"] == "простая история"


# ─── 3. Модалка: слайды для story ────────────────────────────────────────────


class TestModalStorySlides:

    def test_modal_story_shows_slides(self, client, patch_env):
        """Модалка story со slides показывает /files/stories/{id}/1.jpg и /files/stories/{id}/2.jpg."""
        _setup_db()
        files_json = json.dumps(
            {"slides": ["path/slide_1.jpg", "path/slide_2.jpg"], "image_path": "path/slide_1.jpg"},
            ensure_ascii=False,
        )
        texts_json = json.dumps(
            {"vk": {"caption": "карусель из 2 слайдов", "story_type": "carousel"}},
            ensure_ascii=False,
        )
        with get_db() as db:
            _, pid = _create_project(db, platforms=("vk",))
            cid = _create_content(
                db, pid,
                files=files_json,
                texts=texts_json,
                status="approved",
            )
            iid = _create_plan_item(db, pid, content_id=cid, status="generated")

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert f"/files/stories/{cid}/1.jpg" in resp.text
        assert f"/files/stories/{cid}/2.jpg" in resp.text

    def test_modal_story_fallback_no_slides(self, client, patch_env):
        """Модалка story без slides показывает /files/images/{id}.jpg (легаси-фоллбэк)."""
        _setup_db()
        texts_json = json.dumps(
            {"instagram": {"caption": "легаси история"}}, ensure_ascii=False
        )
        with get_db() as db:
            _, pid = _create_project(db, platforms=("instagram",))
            cid = _create_content(
                db, pid,
                texts=texts_json,
                status="approved",
            )
            iid = _create_plan_item(
                db, pid, platform="instagram", content_id=cid, status="generated"
            )

        resp = client.get(f"/queue/items/{iid}/modal")
        assert resp.status_code == 200
        assert f"/files/images/{cid}.jpg" in resp.text
