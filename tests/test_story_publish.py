"""
Тесты волны 8.5B + 9.1B: раздача слайдов сторис по HTTP + ручная очередь
+ автопубликация VK-сторис через Stories API.

Покрывает:
- GET /files/stories/{id}/{n}.jpg: 200 (файл есть) / 404 (файл отсутствует)
- GET /files/images/{id}.jpg: отдаёт slide_1.jpg как первый слайд
- _get_manual_pending: slide_indexes и copy_text для story
- GET /queue/items/{id}/modal: слайды показываются для story со slides
- publish_vk: dry-run сторис → outbox с method=stories.save, count, без caption
- publish_vk: реальный режим с моком httpx → URL story{owner}_{id}
- publish_vk: реальный режим без user_token → PublishError
- publish_vk: несуществующий слайд → PublishError
- catalog: vk.story publish==auto; instagram.story publish==manual
- factory: vk-story schedule → planned (не manual_pending)
"""
import json
import os
import shutil
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch, call

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


# ─── 4. Каталог: vk.story==auto, instagram.story==manual ─────────────────────


class TestCatalogStoryPublish:

    def test_vk_story_publish_auto(self):
        """catalog.type_info('vk','story')['publish'] == 'auto' после флипа 9.1B."""
        from app.catalog import type_info
        info = type_info("vk", "story")
        assert info is not None
        assert info["publish"] == "auto", (
            f"vk.story должна быть auto, но сейчас: {info['publish']}"
        )

    def test_instagram_story_publish_manual(self):
        """catalog.type_info('instagram','story')['publish'] == 'manual' (не трогали)."""
        from app.catalog import type_info
        info = type_info("instagram", "story")
        assert info is not None
        assert info["publish"] == "manual", (
            f"instagram.story должна остаться manual, но сейчас: {info['publish']}"
        )


# ─── 5. publish_vk: dry-run сторис ───────────────────────────────────────────


class TestPublishVkStoryDryRun:

    def test_dry_run_stories_save_method(self, patch_env, tmp_path):
        """dry_run=True с slides → outbox method=stories.save, count==2, нет caption/message."""
        from app.publishers.vk import publish_vk

        slide1 = tmp_path / "slide_1.jpg"
        slide2 = tmp_path / "slide_2.jpg"
        slide1.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)
        slide2.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)

        files = {
            "slides": [str(slide1), str(slide2)],
            "image_path": str(slide1),
        }
        texts = {"vk": {"text": "Карусель", "hashtags": ["#тест"]}}

        url = publish_vk(
            schedule_id=5001,
            credentials=None,
            config={"group_id": 123},
            texts=texts,
            files=files,
            dry_run=True,
        )

        assert url == "dry-run://vk/5001"

        outbox = cfg_module.settings.data_dir_absolute / "outbox" / "5001_vk.json"
        assert outbox.exists(), "outbox-файл не создан"
        payload = json.loads(outbox.read_text(encoding="utf-8"))

        assert payload["method"] == "stories.save"
        assert payload["count"] == 2
        assert payload["dry_run"] is True
        # Текстовое описание НЕ должно попасть в payload истории
        for bad_key in ("message", "caption", "description", "text"):
            assert bad_key not in payload, (
                f"В payload сторис не должно быть ключа '{bad_key}'"
            )

    def test_dry_run_stories_slides_list(self, patch_env, tmp_path):
        """Payload содержит slides — список переданных путей."""
        from app.publishers.vk import publish_vk

        slide1 = tmp_path / "s1.jpg"
        slide1.write_bytes(b"\xff\xd8")

        files = {"slides": [str(slide1)], "image_path": str(slide1)}
        url = publish_vk(
            schedule_id=5002,
            credentials=None,
            config={"group_id": 456},
            texts={},
            files=files,
            dry_run=True,
        )
        outbox = cfg_module.settings.data_dir_absolute / "outbox" / "5002_vk.json"
        payload = json.loads(outbox.read_text(encoding="utf-8"))
        assert payload["slides"] == [str(slide1)]


# ─── 6. publish_vk: реальный режим, моки httpx ───────────────────────────────


class TestPublishVkStoryReal:

    def _make_mock_response(self, json_data: dict) -> MagicMock:
        m = MagicMock()
        m.json.return_value = json_data
        return m

    def test_real_story_two_slides_returns_url(self, patch_env, tmp_path):
        """
        Реальный режим: 2 слайда → последовательность getPhotoUploadServer / upload / save
        на каждый → возвращает URL первого story.
        """
        from app.publishers.vk import publish_vk
        from app.publishers.base import PublishError

        slide1 = tmp_path / "slide_1.jpg"
        slide2 = tmp_path / "slide_2.jpg"
        slide1.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)
        slide2.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)

        # httpx.post вызывается 3 раза на слайд = 6 раз всего
        side_effects = [
            # слайд 1
            self._make_mock_response({"response": {"upload_url": "http://up1"}}),
            self._make_mock_response({"response": {"upload_result": "RES1"}}),
            self._make_mock_response({"response": {"items": [{"owner_id": -123, "id": 777}]}}),
            # слайд 2
            self._make_mock_response({"response": {"upload_url": "http://up2"}}),
            self._make_mock_response({"response": {"upload_result": "RES2"}}),
            self._make_mock_response({"response": {"items": [{"owner_id": -123, "id": 778}]}}),
        ]

        with patch("app.publishers.vk.httpx.post", side_effect=side_effects) as mock_post:
            url = publish_vk(
                schedule_id=5010,
                credentials={"access_token": "AT", "user_token": "UT"},
                config={"group_id": 123},
                texts={"vk": {"text": "История", "hashtags": []}},
                files={
                    "slides": [str(slide1), str(slide2)],
                    "image_path": str(slide1),
                },
                dry_run=False,
            )

        assert url == "https://vk.com/story-123_777"
        assert mock_post.call_count == 6

        # Проверяем что в stories.save НЕТ message/caption
        save_calls = [mock_post.call_args_list[2], mock_post.call_args_list[5]]
        for c in save_calls:
            params = c.kwargs.get("params", c.args[0] if c.args else {})
            if not params and c.args:
                params = {}
            # params передаётся как keyword arg
            kwargs_params = c.kwargs.get("params", {})
            for bad_key in ("message", "caption", "description"):
                assert bad_key not in kwargs_params, (
                    f"В stories.save не должно быть '{bad_key}', params={kwargs_params}"
                )

    def test_real_story_no_user_token_raises(self, patch_env, tmp_path):
        """Реальный режим без user_token → PublishError с упоминанием user_token."""
        from app.publishers.vk import publish_vk
        from app.publishers.base import PublishError

        slide = tmp_path / "s.jpg"
        slide.write_bytes(b"\xff\xd8")

        with pytest.raises(PublishError, match="user_token"):
            publish_vk(
                schedule_id=5020,
                credentials={"access_token": "AT"},  # нет user_token
                config={"group_id": 123},
                texts={},
                files={"slides": [str(slide)], "image_path": str(slide)},
                dry_run=False,
            )

    def test_real_story_none_credentials_raises(self, patch_env, tmp_path):
        """credentials=None → PublishError с упоминанием user_token."""
        from app.publishers.vk import publish_vk
        from app.publishers.base import PublishError

        slide = tmp_path / "s.jpg"
        slide.write_bytes(b"\xff\xd8")

        with pytest.raises(PublishError, match="user_token"):
            publish_vk(
                schedule_id=5021,
                credentials=None,
                config={"group_id": 123},
                texts={},
                files={"slides": [str(slide)], "image_path": str(slide)},
                dry_run=False,
            )

    def test_real_story_missing_slide_raises(self, patch_env, tmp_path):
        """Несуществующий слайд-файл → PublishError с именем файла."""
        from app.publishers.vk import publish_vk
        from app.publishers.base import PublishError

        nonexistent = str(tmp_path / "ghost.jpg")

        with pytest.raises(PublishError, match="ghost.jpg"):
            publish_vk(
                schedule_id=5030,
                credentials={"user_token": "UT"},
                config={"group_id": 123},
                texts={},
                files={"slides": [nonexistent], "image_path": nonexistent},
                dry_run=False,
            )


# ─── 7. Factory: vk-story → schedule planned ──────────────────────────────────


class TestFactoryVkStoryPlanned:

    def test_vk_story_schedule_is_planned(self, patch_env, tmp_path, monkeypatch):
        """
        Фабрика для VK-story создаёт schedule со status='planned' (не 'manual_pending').
        После флипа vk.story→auto catalog возвращает publish='auto'.
        """
        import app.config as cfg_module
        import app.db as db_module
        from app.db import init_db, get_db
        from app.services.factory import _create_schedule

        init_db(cfg_module.settings.db_path_absolute)

        with get_db() as db:
            cur = db.execute(
                """INSERT INTO projects
                   (slug, name, description, audience, tone, goals, cta, themes, stage)
                   VALUES (?, 'VK Story Test', 'Desc', 'ЦА', 'тон', 'цели', 'CTA', 'темы', 'running')""",
                (f"vk-story-{uuid.uuid4().hex[:8]}",),
            )
            project_id = cur.lastrowid
            for p in ("vk",):
                db.execute(
                    "INSERT INTO project_platforms (project_id, platform, enabled, mode) VALUES (?,?,?,?)",
                    (project_id, p, 1, "auto"),
                )
            cur2 = db.execute(
                "INSERT INTO content (project_id, type, title, texts, files, status) "
                "VALUES (?,?,?,?,?,?)",
                (project_id, "story", "VK Story", "{}", "{}", "approved"),
            )
            content_id = cur2.lastrowid
            cur3 = db.execute(
                """INSERT INTO plan_items
                   (project_id, platform, content_type, date, time_slot, title, status, content_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (project_id, "vk", "story", "2026-06-15", "10:00", "VK Story", "generating", content_id),
            )
            item_id = cur3.lastrowid

        with get_db() as db:
            item = db.execute("SELECT * FROM plan_items WHERE id=?", (item_id,)).fetchone()

        _create_schedule(item, content_id)

        with get_db() as db:
            sched = db.execute(
                "SELECT * FROM schedule WHERE content_id=? AND platform='vk'", (content_id,)
            ).fetchall()

        assert len(sched) == 1, "Schedule должен быть создан"
        assert sched[0]["status"] == "planned", (
            f"VK-story schedule должен быть 'planned', но сейчас: {sched[0]['status']}"
        )
