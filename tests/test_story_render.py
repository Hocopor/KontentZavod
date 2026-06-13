"""
Тесты рендера слайдов сторис 2.0 (этап 8.5).

Все тесты на FAKE_LLM=1 (из conftest.patch_env).
ffmpeg не вызывается — _run_ffmpeg мокируется через monkeypatch.
"""
import json
import uuid

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _slug() -> str:
    return f"story-{uuid.uuid4().hex[:8]}"


def _create_project(db, *, slug=None, platforms=("telegram",)):
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage)
        VALUES (?, 'Тест Сторис', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', 'running')
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


def _create_plan_item(
    db,
    project_id: int,
    *,
    platform="instagram",
    content_type="story",
    status="approved",
):
    brief = {
        "hook": "Тестовый хук",
        "outline": "Тезис 1\nТезис 2",
        "cta": "Подпишись",
        "keywords": ["контент", "тест"],
        "rubric": "Советы",
    }
    cur = db.execute(
        """
        INSERT INTO plan_items
            (project_id, platform, content_type, date, time_slot, title, brief, status)
        VALUES (?, ?, ?, '2026-06-15', '10:00', 'Тестовый пункт', ?, ?)
        """,
        (project_id, platform, content_type, json.dumps(brief, ensure_ascii=False), status),
    )
    return cur.lastrowid


# ─── 1. _strip_emoji ──────────────────────────────────────────────────────────


class TestStripEmoji:
    def test_removes_emoji_keeps_text(self):
        from app.pipeline.story_render import _strip_emoji

        result = _strip_emoji("Привет 👋 мир 🚀!")
        assert "👋" not in result
        assert "🚀" not in result
        assert "Привет" in result
        assert "мир" in result


# ─── 2. _build_slide_ass — выравнивание ───────────────────────────────────────


class TestBuildSlideAss:
    def test_top_alignment(self):
        from app.pipeline.story_render import _build_slide_ass

        ass = _build_slide_ass("Тест", "top", "Arial", 96)
        # align=8 → должен быть «,8,» в строке Style
        assert ",8," in ass

    def test_bottom_alignment(self):
        from app.pipeline.story_render import _build_slide_ass

        ass = _build_slide_ass("Тест", "bottom", "Arial", 96)
        assert ",2," in ass

    def test_center_alignment(self):
        from app.pipeline.story_render import _build_slide_ass

        ass = _build_slide_ass("Тест", "center", "Arial", 96)
        assert ",5," in ass

    def test_newline_escape(self):
        from app.pipeline.story_render import _build_slide_ass

        ass = _build_slide_ass("а\nб", "center", "Arial", 96)
        assert "а\\Nб" in ass


# ─── 3. render_story_slides — подсчёт слайдов ─────────────────────────────────


class TestRenderStorySlidesCount:
    def test_returns_correct_count(self, tmp_path, monkeypatch):
        import app.pipeline.story_render as sr_module

        calls: list[str] = []

        def fake_run_ffmpeg(cmd, what):
            calls.append(what)

        monkeypatch.setattr(sr_module, "_run_ffmpeg", fake_run_ffmpeg)
        monkeypatch.setattr(sr_module, "fetch_image", lambda kw, dst: True)

        slides = [
            {"text": "Слайд 1", "image_keywords": ["a"], "position": "center"},
            {"text": "Слайд 2", "image_keywords": ["b"], "position": "top"},
            {"text": "Слайд 3", "image_keywords": ["c"], "position": "bottom"},
        ]

        from app.pipeline.story_render import render_story_slides

        result = render_story_slides(slides, tmp_path)

        assert len(result) == 3
        # _run_ffmpeg вызывается 3 раза — по одному на каждый слайд
        assert len(calls) == 3


# ─── 4. render_story_slides — фоллбэк заглушки фона ──────────────────────────


class TestRenderFallbackBg:
    def test_calls_bg_fallback_when_no_image(self, tmp_path, monkeypatch):
        import app.pipeline.story_render as sr_module

        whats: list[str] = []

        def fake_run_ffmpeg(cmd, what):
            whats.append(what)

        monkeypatch.setattr(sr_module, "_run_ffmpeg", fake_run_ffmpeg)
        monkeypatch.setattr(sr_module, "fetch_image", lambda kw, dst: False)

        slides = [{"text": "Тест", "image_keywords": [], "position": "center"}]

        from app.pipeline.story_render import render_story_slides

        render_story_slides(slides, tmp_path)

        # Должны быть вызовы и для bg fallback, и для slide
        assert any("bg fallback" in w for w in whats), f"нет bg fallback среди: {whats}"
        assert any("story slide" in w for w in whats), f"нет story slide среди: {whats}"


# ─── 5. generate_for_item — v2 (слайды из FAKE_ITEM_STORY) ───────────────────


class TestGenerateStoryV2:
    def test_generates_story_with_slides(self, patch_env, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_ASSETS", "1")
        _setup_db()

        import app.pipeline.from_plan as fp_module

        # Мокируем render_story_slides — возвращаем 2 пути (не вызываем ffmpeg)
        fake_slide_1 = tmp_path / "slide_1.jpg"
        fake_slide_2 = tmp_path / "slide_2.jpg"
        fake_slide_1.write_bytes(b"fake1")
        fake_slide_2.write_bytes(b"fake2")

        def fake_render(slides, out_dir, *, font=None, font_size=None):
            return [fake_slide_1, fake_slide_2]

        monkeypatch.setattr(fp_module, "render_story_slides", fake_render)

        with get_db() as db:
            pid, _ = _create_project(db, platforms=("instagram",))
            item_id = _create_plan_item(
                db, pid, platform="instagram", content_type="story", status="approved"
            )

        from app.pipeline.from_plan import generate_for_item

        content_id = generate_for_item(item_id)

        assert content_id is not None, "generate_for_item вернул None — ошибка генерации"

        with get_db() as db:
            content = db.execute(
                "SELECT * FROM content WHERE id=?", (content_id,)
            ).fetchone()
            item = db.execute(
                "SELECT * FROM plan_items WHERE id=?", (item_id,)
            ).fetchone()

        assert content["type"] == "story"
        files = json.loads(content["files"])
        assert "slides" in files, f"нет 'slides' в files: {files}"
        assert len(files["slides"]) == 2, f"ожидалось 2 слайда, got {len(files['slides'])}"
        assert "image_path" in files, f"нет 'image_path' в files: {files}"
        assert item["status"] == "generated"


# ─── 6. generate_for_item — легаси-фоллбэк (без slides) ─────────────────────


class TestGenerateStoryV1Fallback:
    def test_legacy_story_produces_one_slide(self, patch_env, tmp_path, monkeypatch):
        _setup_db()

        import app.pipeline.from_plan as fp_module

        legacy_json = json.dumps(
            {
                "title": "T",
                "image_prompt": "sunrise coffee",
                "image_keywords": ["sunrise", "coffee"],
                "overlay_text": "Доброе утро",
                "caption": "cap",
                "features": {
                    "hook_type": "факт",
                    "topic": "утро",
                    "length": "short",
                    "format": "story",
                    "ab_variant": "null",
                },
            },
            ensure_ascii=False,
        )

        # Мокируем chat для purpose='item_story' — возвращаем легаси JSON
        original_chat = fp_module.chat

        def fake_chat(messages, purpose=None, **kwargs):
            if purpose == "item_story":
                return legacy_json
            return original_chat(messages, purpose=purpose, **kwargs)

        monkeypatch.setattr(fp_module, "chat", fake_chat)

        # Spy на render_story_slides — фиксируем переданные слайды
        captured_slides: list[list[dict]] = []

        fake_slide = tmp_path / "s1.jpg"
        fake_slide.write_bytes(b"fake")

        def spy_render(slides, out_dir, *, font=None, font_size=None):
            captured_slides.append(list(slides))
            return [fake_slide]

        monkeypatch.setattr(fp_module, "render_story_slides", spy_render)

        with get_db() as db:
            pid, _ = _create_project(db, platforms=("instagram",))
            item_id = _create_plan_item(
                db, pid, platform="instagram", content_type="story", status="approved"
            )

        from app.pipeline.from_plan import generate_for_item

        content_id = generate_for_item(item_id)

        assert content_id is not None, "generate_for_item вернул None — легаси-фоллбэк упал"
        assert len(captured_slides) == 1, "render_story_slides не был вызван"
        slides_passed = captured_slides[0]
        assert len(slides_passed) == 1, (
            f"ожидался 1 слайд (легаси), получено {len(slides_passed)}"
        )
        assert slides_passed[0]["text"] == "Доброе утро", (
            f"text слайда не совпал: {slides_passed[0]['text']!r}"
        )
