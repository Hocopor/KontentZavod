"""
Тесты видео-конвейера:
  - generate_video_script
  - produce_video / process_production
  - веб-кнопки видео, ревью, approve, перерендер
  - /files/videos/{id}.mp4 / .jpg

Все тесты: FAKE_LLM=1 (из conftest.patch_env).
TTS и ассеты: monkeypatch или FAKE_TTS/FAKE_ASSETS.
Рендер: monkeypatch produce._do_render (не требует ffmpeg, render.py не импортируется).
"""
import json
import uuid
from pathlib import Path
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
    return project_id, s


def _create_idea(db, project_id: int, text: str = "Тестовая идея для видео") -> int:
    cur = db.execute(
        "INSERT INTO ideas (project_id, text, source, status) VALUES (?,?,?,?)",
        (project_id, text, "manual", "new"),
    )
    return cur.lastrowid


def _create_video_content(
    db,
    project_id: int,
    *,
    content_type: str = "video_footage",
    status: str = "text_review",
    title: str = "Тест видео",
) -> int:
    """Создаёт video-контент с минимальным texts.video."""
    texts = json.dumps({
        "video": {
            "voice": "dmitry",
            "mood": "energetic",
            "scenes": [
                {"text": "Первая сцена хук.", "keywords": ["office", "work"]},
                {"text": "Вторая сцена CTA.", "keywords": ["call to action", "link"]},
            ],
        },
        "telegram": {"text": "Текст для Telegram", "hashtags": ["#тест"]},
        "vk": {"text": "Текст для VK", "hashtags": ["#тест"]},
    }, ensure_ascii=False)
    features = json.dumps({
        "hook_type": "вопрос",
        "topic": "тест видео",
        "length": "short",
        "format": "reel",
        "ab_variant": "null",
    }, ensure_ascii=False)
    cur = db.execute(
        """
        INSERT INTO content (project_id, type, title, texts, features, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, content_type, title, texts, features, status),
    )
    return cur.lastrowid


# ─── Фикстуры ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db_video_project(patch_env):
    """Создаёт проект с площадками (telegram, vk). Возвращает (project_id, slug)."""
    _setup_db()
    with get_db() as db:
        project_id, slug = _create_project(db, platforms=("telegram", "vk"))
    return project_id, slug


@pytest.fixture
def db_video_idea(db_video_project):
    """Создаёт идею для db_video_project. Возвращает (project_id, idea_id, slug)."""
    project_id, slug = db_video_project
    with get_db() as db:
        idea_id = _create_idea(db, project_id)
    return project_id, idea_id, slug


# ─── 1. generate_video_script ─────────────────────────────────────────────────


class TestGenerateVideoScript:
    def test_content_created_correct_type_status(self, db_video_idea):
        """Контент создан с правильным типом и статусом."""
        from app.pipeline.video_script import generate_video_script

        project_id, idea_id, _ = db_video_idea
        content_id = generate_video_script(idea_id, "video_footage")

        assert isinstance(content_id, int)
        with get_db() as db:
            row = db.execute(
                "SELECT * FROM content WHERE id=?", (content_id,)
            ).fetchone()

        assert row is not None
        assert row["type"] == "video_footage"
        assert row["status"] == "text_review"
        assert row["project_id"] == project_id
        assert row["idea_id"] == idea_id

    def test_texts_video_block_valid(self, db_video_idea):
        """texts.video содержит voice, mood, scenes с text и keywords."""
        from app.pipeline.video_script import generate_video_script

        _, idea_id, _ = db_video_idea
        content_id = generate_video_script(idea_id, "video_footage")

        with get_db() as db:
            row = db.execute(
                "SELECT texts FROM content WHERE id=?", (content_id,)
            ).fetchone()

        texts = json.loads(row["texts"])
        assert "video" in texts
        video = texts["video"]
        assert video["voice"] in ("dmitry", "svetlana")
        assert video["mood"] in ("energetic", "calm", "inspiring", "neutral")
        scenes = video["scenes"]
        assert isinstance(scenes, list)
        assert len(scenes) > 0
        for scene in scenes:
            assert scene.get("text"), "scene.text должен быть непустым"
            assert isinstance(scene.get("keywords"), list), "keywords должен быть списком"
            assert len(scene["keywords"]) > 0, "keywords не должен быть пустым"

    def test_features_valid(self, db_video_idea):
        """features содержит обязательные ключи, format=reel."""
        from app.pipeline.video_script import generate_video_script

        _, idea_id, _ = db_video_idea
        content_id = generate_video_script(idea_id, "video_footage")

        with get_db() as db:
            row = db.execute(
                "SELECT features FROM content WHERE id=?", (content_id,)
            ).fetchone()

        features = json.loads(row["features"])
        for key in ("hook_type", "topic", "length", "format"):
            assert key in features, f"features.{key} отсутствует"
        assert features["format"] == "reel"

    def test_idea_becomes_used(self, db_video_idea):
        """Идея переходит в статус 'used'."""
        from app.pipeline.video_script import generate_video_script

        _, idea_id, _ = db_video_idea
        generate_video_script(idea_id, "video_footage")

        with get_db() as db:
            idea = db.execute(
                "SELECT status FROM ideas WHERE id=?", (idea_id,)
            ).fetchone()
        assert idea["status"] == "used"

    def test_slideshow_type(self, db_video_idea):
        """template='video_slideshow' создаёт контент с type='video_slideshow'."""
        from app.pipeline.video_script import generate_video_script

        _, idea_id, _ = db_video_idea
        content_id = generate_video_script(idea_id, "video_slideshow")

        with get_db() as db:
            row = db.execute(
                "SELECT type FROM content WHERE id=?", (content_id,)
            ).fetchone()
        assert row["type"] == "video_slideshow"

    def test_dzen_not_in_texts(self, db_video_idea):
        """dzen не должен быть в texts для видео."""
        from app.pipeline.video_script import generate_video_script

        _, idea_id, _ = db_video_idea
        content_id = generate_video_script(idea_id, "video_footage")

        with get_db() as db:
            row = db.execute(
                "SELECT texts FROM content WHERE id=?", (content_id,)
            ).fetchone()

        texts = json.loads(row["texts"])
        assert "dzen" not in texts, "dzen не должен быть в texts видео-контента"

    def test_invalid_template_raises(self, db_video_idea):
        """Неизвестный template вызывает ValueError."""
        from app.pipeline.video_script import generate_video_script

        _, idea_id, _ = db_video_idea
        with pytest.raises(ValueError, match="шаблон"):
            generate_video_script(idea_id, "video_unknown")


# ─── 2. produce_video ─────────────────────────────────────────────────────────


class TestProduceVideo:
    def _fake_render(self, tmp_path, content_id: int):
        """Создаёт fake mp4 и jpg файлы и возвращает патч-функцию."""
        videos_dir = tmp_path / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        video_path = videos_dir / f"{content_id}.mp4"
        preview_path = videos_dir / f"{content_id}.jpg"
        video_path.write_bytes(b"FAKE_MP4")
        preview_path.write_bytes(b"FAKE_JPG")

        def fake_do_render(*args, **kwargs):
            return video_path, preview_path

        return fake_do_render

    def test_produce_success(self, db_video_project, monkeypatch, tmp_path):
        """Успешный рендер: status='review', files заполнен, cleanup вызван."""
        import app.pipeline.produce as produce_module

        project_id, slug = db_video_project

        with get_db() as db:
            content_id = _create_video_content(
                db, project_id, status="production"
            )

        # Патчим зависимости
        called_cleanup = []

        def fake_synthesize(scene_texts, out_dir, voice="dmitry"):
            from app.pipeline.tts import SceneAudio, WordTiming
            out_dir.mkdir(parents=True, exist_ok=True)
            audios = []
            for i, text in enumerate(scene_texts):
                mp3 = out_dir / f"voice_{i+1:02d}.mp3"
                mp3.write_bytes(b"FAKE_AUDIO")
                words = [WordTiming(word=w, start=float(j)*0.4, end=float(j)*0.4+0.4)
                         for j, w in enumerate(text.split()[:3])]
                audios.append(SceneAudio(index=i, path=mp3, duration=1.2, words=words))
            return audios

        def fake_build_ass(words, out_path, words_per_line=3):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("[Script Info]\n", encoding="utf-8")
            return out_path

        def fake_fetch_assets(content_id, scenes, template):
            assets_dir = tmp_path / "media" / str(content_id) / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            paths = []
            for i in range(len(scenes)):
                f = assets_dir / f"scene_{i+1:02d}.mp4"
                f.write_bytes(b"FAKE_ASSET")
                paths.append(f)
            return paths

        def fake_pick_music(mood):
            return None

        def fake_cleanup(cid):
            called_cleanup.append(cid)

        fake_render = self._fake_render(tmp_path, content_id)

        monkeypatch.setattr(produce_module, "synthesize_scenes", fake_synthesize)
        monkeypatch.setattr(produce_module, "build_ass", fake_build_ass)
        monkeypatch.setattr(produce_module, "fetch_scene_assets", fake_fetch_assets)
        monkeypatch.setattr(produce_module, "pick_music", fake_pick_music)
        monkeypatch.setattr(produce_module, "_do_render", fake_render)
        monkeypatch.setattr(produce_module, "cleanup_after_render", fake_cleanup)

        produce_module.produce_video(content_id)

        with get_db() as db:
            row = db.execute(
                "SELECT status, files FROM content WHERE id=?", (content_id,)
            ).fetchone()

        assert row["status"] == "review"
        files = json.loads(row["files"])
        assert "video_path" in files
        assert "preview_path" in files
        assert "subs_path" in files
        assert called_cleanup == [content_id]

    def test_produce_render_error_sets_produce_error(self, db_video_project, monkeypatch, tmp_path):
        """Ошибка рендера → produce_error в files, attempts=1, статус остался production."""
        import app.pipeline.produce as produce_module

        project_id, slug = db_video_project

        with get_db() as db:
            content_id = _create_video_content(
                db, project_id, status="production"
            )

        def fake_synthesize(scene_texts, out_dir, voice="dmitry"):
            from app.pipeline.tts import SceneAudio, WordTiming
            out_dir.mkdir(parents=True, exist_ok=True)
            audios = []
            for i, text in enumerate(scene_texts):
                mp3 = out_dir / f"voice_{i+1:02d}.mp3"
                mp3.write_bytes(b"FAKE_AUDIO")
                words = [WordTiming(word="слово", start=0.0, end=0.4)]
                audios.append(SceneAudio(index=i, path=mp3, duration=1.0, words=words))
            return audios

        def fake_build_ass(words, out_path, words_per_line=3):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("[Script Info]\n", encoding="utf-8")
            return out_path

        def fake_fetch_assets(content_id, scenes, template):
            assets_dir = tmp_path / "media" / str(content_id) / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            return [assets_dir / "scene_01.mp4"]

        def fake_pick_music(mood):
            return None

        def fail_render(*args, **kwargs):
            raise RuntimeError("рендер сломан")

        monkeypatch.setattr(produce_module, "synthesize_scenes", fake_synthesize)
        monkeypatch.setattr(produce_module, "build_ass", fake_build_ass)
        monkeypatch.setattr(produce_module, "fetch_scene_assets", fake_fetch_assets)
        monkeypatch.setattr(produce_module, "pick_music", fake_pick_music)
        monkeypatch.setattr(produce_module, "_do_render", fail_render)

        produce_module.produce_video(content_id)

        with get_db() as db:
            row = db.execute(
                "SELECT status, files FROM content WHERE id=?", (content_id,)
            ).fetchone()

        assert row["status"] == "production", "Статус должен остаться production при ошибке"
        files = json.loads(row["files"])
        assert "produce_error" in files
        assert files.get("produce_attempts") == 1

    def test_produce_wrong_status_raises(self, db_video_project):
        """Контент не в статусе 'production' → ValueError."""
        import app.pipeline.produce as produce_module

        project_id, _ = db_video_project

        with get_db() as db:
            content_id = _create_video_content(
                db, project_id, status="text_review"
            )

        with pytest.raises(ValueError, match="production"):
            produce_module.produce_video(content_id)

    def test_produce_wrong_type_raises(self, patch_env):
        """Контент с type='post' → ValueError."""
        import app.pipeline.produce as produce_module

        _setup_db()
        with get_db() as db:
            project_id, _ = _create_project(db)
            cur = db.execute(
                """
                INSERT INTO content (project_id, type, title, status)
                VALUES (?, 'post', 'Тест', 'production')
                """,
                (project_id,),
            )
            content_id = cur.lastrowid

        with pytest.raises(ValueError, match="video"):
            produce_module.produce_video(content_id)

    def test_process_production_skips_after_3_attempts(
        self, db_video_project, monkeypatch, tmp_path
    ):
        """После 3 неудачных попыток process_production пропускает контент."""
        import app.pipeline.produce as produce_module

        project_id, _ = db_video_project

        with get_db() as db:
            content_id = _create_video_content(
                db, project_id, status="production"
            )
            # Проставляем 3 попытки в files
            db.execute(
                "UPDATE content SET files=? WHERE id=?",
                (json.dumps({"produce_attempts": 3, "produce_error": "prev error"}), content_id),
            )

        # produce_video НЕ должен быть вызван
        called = []
        original = produce_module.produce_video

        def spy_produce(cid):
            called.append(cid)
            return original(cid)

        monkeypatch.setattr(produce_module, "produce_video", spy_produce)

        produce_module.process_production()

        assert content_id not in called, "Контент с 3 попытками не должен обрабатываться"


# ─── 3. Веб-интеграция ────────────────────────────────────────────────────────


class TestVideoWebGenerate:
    def test_video_footage_button_creates_content(self, client, patch_env):
        """Кнопка «Футажи» создаёт video_footage контент."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram", "vk"))
            idea_id = _create_idea(db, project_id)

        resp = client.post(
            f"/ideas/{idea_id}/video_footage",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute(
                "SELECT * FROM content WHERE project_id=? AND type='video_footage'",
                (project_id,),
            ).fetchone()

        assert row is not None
        assert row["status"] == "text_review"

    def test_video_slideshow_button_creates_content(self, client, patch_env):
        """Кнопка «Слайдшоу» создаёт video_slideshow контент."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram",))
            idea_id = _create_idea(db, project_id)

        resp = client.post(
            f"/ideas/{idea_id}/video_slideshow",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute(
                "SELECT type FROM content WHERE project_id=?", (project_id,)
            ).fetchone()

        assert row is not None
        assert row["type"] == "video_slideshow"

    def test_idea_used_after_video_generate(self, client, patch_env):
        """После генерации видео идея переходит в статус 'used'."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram",))
            idea_id = _create_idea(db, project_id)

        client.post(
            f"/ideas/{idea_id}/video_footage",
            headers={"HX-Request": "true"},
        )

        with get_db() as db:
            idea = db.execute(
                "SELECT status FROM ideas WHERE id=?", (idea_id,)
            ).fetchone()
        assert idea["status"] == "used"


class TestVideoWebReview:
    def test_video_text_review_in_list(self, client, patch_env):
        """Видео в text_review видно в очереди ревью."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram", "vk"))
            content_id = _create_video_content(db, project_id, status="text_review",
                                               title="Видео для ревью")

        resp = client.get("/review")
        assert resp.status_code == 200
        assert "Видео для ревью" in resp.text

    def test_video_review_status_in_list(self, client, patch_env):
        """Видео в status='review' (после рендера) видно в очереди ревью."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram", "vk"))
            content_id = _create_video_content(db, project_id, status="review",
                                               title="Готовое видео")

        resp = client.get("/review")
        assert resp.status_code == 200
        assert "Готовое видео" in resp.text

    def test_video_detail_shows_scenes(self, client, patch_env):
        """Детальная страница видео в text_review показывает сцены."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram", "vk"))
            content_id = _create_video_content(db, project_id, status="text_review")

        resp = client.get(f"/review/{content_id}")
        assert resp.status_code == 200
        # Должен быть textarea сцены
        assert "scene_0" in resp.text
        # Текст сцены виден
        assert "Первая сцена хук" in resp.text

    def test_video_detail_shows_video_tag_when_review(self, client, patch_env, tmp_path):
        """Детальная страница видео в status='review' содержит <video."""
        _setup_db()
        # Создаём фейковый mp4 файл
        videos_dir = tmp_path / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)

        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram", "vk"))
            content_id = _create_video_content(db, project_id, status="review",
                                               title="Готовое видео")
            # Записываем пути в files
            fake_mp4 = videos_dir / f"{content_id}.mp4"
            fake_mp4.write_bytes(b"FAKE_MP4")
            fake_jpg = videos_dir / f"{content_id}.jpg"
            fake_jpg.write_bytes(b"FAKE_JPG")
            files = {
                "video_path": str(fake_mp4),
                "preview_path": str(fake_jpg),
                "subs_path": str(tmp_path / "media" / str(content_id) / "subs.ass"),
            }
            db.execute(
                "UPDATE content SET files=? WHERE id=?",
                (json.dumps(files), content_id),
            )

        resp = client.get(f"/review/{content_id}")
        assert resp.status_code == 200
        assert "<video" in resp.text

    def test_video_approve_text_review_goes_production(self, client, patch_env):
        """Approve видео из text_review → статус 'production', НЕ approved."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram", "vk"))
            content_id = _create_video_content(db, project_id, status="text_review")

        resp = client.post(
            f"/review/{content_id}/approve",
            data={},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute(
                "SELECT status FROM content WHERE id=?", (content_id,)
            ).fetchone()
        assert row["status"] == "production"

    def test_video_approve_review_goes_approved(self, client, patch_env):
        """Approve видео из status='review' → approved + schedule."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram", "vk"))
            content_id = _create_video_content(db, project_id, status="review")

        dt = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        resp = client.post(
            f"/review/{content_id}/approve",
            data={
                "schedule_telegram": "1",
                "planned_at_telegram": dt,
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute(
                "SELECT status FROM content WHERE id=?", (content_id,)
            ).fetchone()
            sched = db.execute(
                "SELECT id FROM schedule WHERE content_id=?", (content_id,)
            ).fetchone()

        assert row["status"] == "approved"
        assert sched is not None

    def test_video_rerender_resets_error(self, client, patch_env):
        """Перерендер сбрасывает produce_error и возвращает статус production."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram", "vk"))
            content_id = _create_video_content(db, project_id, status="production")
            # Устанавливаем ошибку
            files = {"produce_error": "ошибка рендера", "produce_attempts": 2}
            db.execute(
                "UPDATE content SET files=? WHERE id=?",
                (json.dumps(files), content_id),
            )

        resp = client.post(
            f"/review/{content_id}/rerender",
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute(
                "SELECT status, files FROM content WHERE id=?", (content_id,)
            ).fetchone()

        assert row["status"] == "production"
        files_after = json.loads(row["files"]) if row["files"] else {}
        assert "produce_error" not in files_after
        assert "produce_attempts" not in files_after

    def test_video_reject(self, client, patch_env):
        """Reject видео → rejected + learning."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram",))
            content_id = _create_video_content(db, project_id, status="text_review")

        resp = client.post(
            f"/review/{content_id}/reject",
            data={"reason": "Скучные сцены"},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute(
                "SELECT status FROM content WHERE id=?", (content_id,)
            ).fetchone()
            learning = db.execute(
                "SELECT insight FROM learnings WHERE project_id=? AND source='reject'",
                (project_id,),
            ).fetchone()

        assert row["status"] == "rejected"
        assert learning is not None
        assert "Скучные сцены" in learning["insight"]

    def test_produce_error_badge_in_list(self, client, patch_env):
        """Ошибка продакшна показывается в списке ревью."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram",))
            content_id = _create_video_content(db, project_id, status="text_review",
                                               title="Видео с ошибкой")
            files = {"produce_error": "ffmpeg crash", "produce_attempts": 1}
            db.execute(
                "UPDATE content SET files=? WHERE id=?",
                (json.dumps(files), content_id),
            )

        resp = client.get("/review")
        assert resp.status_code == 200
        assert "ошибка продакшна" in resp.text


# ─── 4. /files/videos/ ────────────────────────────────────────────────────────


class TestFilesEndpoint:
    def test_serve_mp4_returns_file(self, client, patch_env, tmp_path):
        """GET /files/videos/{id}.mp4 возвращает файл."""
        _setup_db()
        # Создаём фейковый mp4 в DATA_DIR/videos/
        videos_dir = tmp_path / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        fake_mp4 = videos_dir / "42.mp4"
        fake_mp4.write_bytes(b"FAKE_MP4_CONTENT")

        resp = client.get("/files/videos/42.mp4")
        assert resp.status_code == 200
        assert resp.content == b"FAKE_MP4_CONTENT"

    def test_serve_jpg_returns_file(self, client, patch_env, tmp_path):
        """GET /files/videos/{id}.jpg возвращает файл."""
        _setup_db()
        videos_dir = tmp_path / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        fake_jpg = videos_dir / "42.jpg"
        fake_jpg.write_bytes(b"FAKE_JPG_CONTENT")

        resp = client.get("/files/videos/42.jpg")
        assert resp.status_code == 200
        assert resp.content == b"FAKE_JPG_CONTENT"

    def test_serve_mp4_404_when_missing(self, client, patch_env):
        """GET /files/videos/{id}.mp4 возвращает 404 если файл не существует."""
        _setup_db()
        resp = client.get("/files/videos/99999.mp4")
        assert resp.status_code == 404

    def test_serve_jpg_404_when_missing(self, client, patch_env):
        """GET /files/videos/{id}.jpg возвращает 404 если файл не существует."""
        _setup_db()
        resp = client.get("/files/videos/99999.jpg")
        assert resp.status_code == 404
