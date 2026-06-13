"""
Тесты фабрики готового контента (этап 7, волна C).

Все тесты: FAKE_LLM=1, FAKE_ASSETS=1 (без сети/ffmpeg-футажей).
Рендер видео мокается через app.pipeline.produce._do_render.

Покрытие:
  - text-пункт → content type='post', texts своей платформы, schedule planned, item generated;
  - dzen text → manual_pending;
  - story → файл картинки, files.image_path, schedule manual_pending;
  - video → idea+content production, item generating; затем готовый рендер → schedule+generated;
  - видео-провал (3 попытки) → item error;
  - autogen=0 / gen_paused=1 / date за окном → не берёт;
  - повторный тик не создаёт второй content/schedule;
  - ошибка LLM (мусор дважды) → item error, тик не падает.
"""
import json
import uuid
from datetime import date, timedelta

import pytest

import app.config as cfg_module
from app.db import get_db, init_db
from app.services import factory as factory_module
from app.services.factory import process_factory


# ─── Вспомогательные ──────────────────────────────────────────────────────────


def _slug() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(
    db,
    *,
    platforms=("telegram",),
    stage="running",
    autogen=1,
    gen_paused=0,
    gen_lookahead_days=3,
):
    settings_json = json.dumps({
        "autogen": autogen,
        "gen_paused": gen_paused,
        "gen_lookahead_days": gen_lookahead_days,
    })
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage, settings)
        VALUES (?, 'Тест', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', ?, ?)
        """,
        (_slug(), stage, settings_json),
    )
    project_id = cur.lastrowid
    for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
        enabled = 1 if p in platforms else 0
        db.execute(
            "INSERT INTO project_platforms (project_id, platform, enabled, mode) "
            "VALUES (?,?,?,?)",
            (project_id, p, enabled, "auto"),
        )
    return project_id


def _create_plan_item(
    db,
    project_id,
    *,
    platform="telegram",
    content_type="post",
    item_date=None,
    time_slot="09:00",
    status="approved",
):
    d = item_date or date.today().isoformat()
    brief = json.dumps({
        "hook": "Хук пункта",
        "outline": "1. Пункт. 2. Пункт.",
        "cta": "Подпишитесь",
        "keywords": ["маркетинг", "контент"],
        "rubric": "Разбор",
    }, ensure_ascii=False)
    cur = db.execute(
        """
        INSERT INTO plan_items
            (project_id, platform, content_type, date, time_slot, title, brief, status)
        VALUES (?, ?, ?, ?, ?, 'Заголовок пункта', ?, ?)
        """,
        (project_id, platform, content_type, d, time_slot, brief, status),
    )
    return cur.lastrowid


@pytest.fixture
def db_ready(patch_env, monkeypatch):
    """БД готова + FAKE_ASSETS=1."""
    monkeypatch.setenv("FAKE_ASSETS", "1")
    _setup_db()
    yield


def _get_item(item_id):
    with get_db() as db:
        return db.execute("SELECT * FROM plan_items WHERE id=?", (item_id,)).fetchone()


def _get_content(content_id):
    with get_db() as db:
        return db.execute("SELECT * FROM content WHERE id=?", (content_id,)).fetchone()


def _get_schedule_for(content_id):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM schedule WHERE content_id=?", (content_id,)
        ).fetchall()


# ─── 1. Текстовый пункт ───────────────────────────────────────────────────────


class TestTextItem:
    def test_telegram_post_generated_with_schedule(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",))
            item_id = _create_plan_item(db, pid, platform="telegram", content_type="post")

        process_factory()

        item = _get_item(item_id)
        assert item["status"] == "generated"
        assert item["content_id"] is not None

        content = _get_content(item["content_id"])
        assert content["type"] == "post"
        assert content["status"] == "approved"
        texts = json.loads(content["texts"])
        assert "telegram" in texts
        assert texts["telegram"]["text"]
        assert "hashtags" in texts["telegram"]

        sched = _get_schedule_for(item["content_id"])
        assert len(sched) == 1
        assert sched[0]["platform"] == "telegram"
        assert sched[0]["status"] == "planned"
        # planned_at на дату+время плана
        assert sched[0]["planned_at"].startswith(item["date"])
        assert "09:00" in sched[0]["planned_at"]

    def test_vk_post_texts_structure(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("vk",))
            item_id = _create_plan_item(db, pid, platform="vk", content_type="post")

        process_factory()
        item = _get_item(item_id)
        content = _get_content(item["content_id"])
        texts = json.loads(content["texts"])
        assert "vk" in texts and "text" in texts["vk"]

    def test_dzen_post_manual_pending(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("dzen",))
            item_id = _create_plan_item(db, pid, platform="dzen", content_type="post")

        process_factory()
        item = _get_item(item_id)
        assert item["status"] == "generated"
        content = _get_content(item["content_id"])
        texts = json.loads(content["texts"])
        assert "dzen" in texts and "title" in texts["dzen"] and "text" in texts["dzen"]
        sched = _get_schedule_for(item["content_id"])
        assert len(sched) == 1
        assert sched[0]["status"] == "manual_pending"

    def test_dzen_article_content_type(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("dzen",))
            item_id = _create_plan_item(db, pid, platform="dzen", content_type="article")

        process_factory()
        item = _get_item(item_id)
        content = _get_content(item["content_id"])
        assert content["type"] == "article"


# ─── 2. Story ─────────────────────────────────────────────────────────────────


class TestStoryItem:
    def test_instagram_story(self, db_ready, monkeypatch, tmp_path):
        import app.pipeline.from_plan as fp_module
        from pathlib import Path

        # Мокируем render_story_slides — не вызываем реальный ffmpeg
        fake_slide = tmp_path / "slide_1.jpg"
        fake_slide.write_bytes(b"fake")

        def fake_render(slides, out_dir, *, font=None, font_size=None):
            return [fake_slide]

        monkeypatch.setattr(fp_module, "render_story_slides", fake_render)

        with get_db() as db:
            pid = _create_project(db, platforms=("instagram",))
            item_id = _create_plan_item(
                db, pid, platform="instagram", content_type="story"
            )

        process_factory()

        item = _get_item(item_id)
        assert item["status"] == "generated"
        content = _get_content(item["content_id"])
        assert content["type"] == "story"
        assert content["status"] == "approved"

        texts = json.loads(content["texts"])
        assert "instagram" in texts
        assert "caption" in texts["instagram"]
        # v2: texts содержит story_type, slides находятся в files
        assert "story_type" in texts["instagram"]

        files = json.loads(content["files"])
        assert "image_path" in files
        # image_path = первый слайд
        assert Path(files["image_path"]).exists()
        assert "slides" in files
        assert len(files["slides"]) >= 1

        sched = _get_schedule_for(item["content_id"])
        assert len(sched) == 1
        assert sched[0]["status"] == "manual_pending"


# ─── 3. Видео ─────────────────────────────────────────────────────────────────


class TestVideoItem:
    def test_video_creates_idea_and_production(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",))
            item_id = _create_plan_item(
                db, pid, platform="telegram", content_type="video"
            )

        process_factory()

        item = _get_item(item_id)
        assert item["status"] == "generating"
        assert item["content_id"] is not None

        content = _get_content(item["content_id"])
        assert content["type"] in ("video_footage", "video_slideshow")
        assert content["status"] == "production"

        # idea создана
        with get_db() as db:
            ideas = db.execute(
                "SELECT * FROM ideas WHERE project_id=?", (pid,)
            ).fetchall()
        assert len(ideas) == 1
        assert ideas[0]["status"] == "used"

        # schedule ещё НЕ создан (видео не отрендерено)
        assert len(_get_schedule_for(item["content_id"])) == 0

    def test_video_finalized_after_render(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",))
            item_id = _create_plan_item(
                db, pid, platform="telegram", content_type="video", time_slot="15:00"
            )

        process_factory()
        item = _get_item(item_id)
        assert item["status"] == "generating"
        content_id = item["content_id"]

        # Смоделировать готовый рендер
        with get_db() as db:
            db.execute(
                "UPDATE content SET status='review', files=? WHERE id=?",
                (json.dumps({"video_path": "/x.mp4", "preview_path": "/x.jpg"}), content_id),
            )

        process_factory()  # блок A дожимает

        item = _get_item(item_id)
        assert item["status"] == "generated"
        content = _get_content(content_id)
        assert content["status"] == "approved"

        sched = _get_schedule_for(content_id)
        assert len(sched) == 1
        assert sched[0]["platform"] == "telegram"
        assert sched[0]["status"] == "planned"
        assert "15:00" in sched[0]["planned_at"]

    def test_video_failed_after_3_attempts(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",))
            item_id = _create_plan_item(
                db, pid, platform="telegram", content_type="video"
            )

        process_factory()
        item = _get_item(item_id)
        content_id = item["content_id"]

        # Смоделировать исчерпанные попытки рендера
        with get_db() as db:
            db.execute(
                "UPDATE content SET files=? WHERE id=?",
                (json.dumps({"produce_attempts": 3, "produce_error": "ffmpeg сломался"}),
                 content_id),
            )

        process_factory()  # блок A → error

        item = _get_item(item_id)
        assert item["status"] == "error"
        assert "ffmpeg" in (item["error_text"] or "")
        # schedule не создан
        assert len(_get_schedule_for(content_id)) == 0


# ─── 4. Условия отбора ────────────────────────────────────────────────────────


class TestSelection:
    def test_autogen_off_skips(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",), autogen=0)
            item_id = _create_plan_item(db, pid)

        process_factory()
        item = _get_item(item_id)
        assert item["status"] == "approved"
        assert item["content_id"] is None

    def test_gen_paused_skips(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",), gen_paused=1)
            item_id = _create_plan_item(db, pid)

        process_factory()
        item = _get_item(item_id)
        assert item["status"] == "approved"

    def test_not_running_skips(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",), stage="draft")
            item_id = _create_plan_item(db, pid)

        process_factory()
        item = _get_item(item_id)
        assert item["status"] == "approved"

    def test_date_beyond_lookahead_skips(self, db_ready):
        far = (date.today() + timedelta(days=10)).isoformat()
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",), gen_lookahead_days=3)
            item_id = _create_plan_item(db, pid, item_date=far)

        process_factory()
        item = _get_item(item_id)
        assert item["status"] == "approved"

    def test_disabled_platform_skips(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",))
            # пункт на vk, но vk выключен
            item_id = _create_plan_item(db, pid, platform="vk", content_type="post")

        process_factory()
        item = _get_item(item_id)
        assert item["status"] == "approved"

    def test_date_within_lookahead_taken(self, db_ready):
        soon = (date.today() + timedelta(days=2)).isoformat()
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",), gen_lookahead_days=3)
            item_id = _create_plan_item(db, pid, item_date=soon)

        process_factory()
        item = _get_item(item_id)
        assert item["status"] == "generated"


# ─── 5. Идемпотентность и порядок ─────────────────────────────────────────────


class TestIdempotency:
    def test_second_tick_no_duplicate(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",))
            item_id = _create_plan_item(db, pid)

        process_factory()
        item = _get_item(item_id)
        content_id = item["content_id"]
        assert item["status"] == "generated"

        process_factory()  # второй тик не должен создать дубль

        with get_db() as db:
            n_content = db.execute(
                "SELECT COUNT(*) AS n FROM content WHERE project_id=?", (pid,)
            ).fetchone()["n"]
        assert n_content == 1
        assert len(_get_schedule_for(content_id)) == 1

    def test_one_item_per_tick(self, db_ready):
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",))
            id1 = _create_plan_item(db, pid, time_slot="09:00")
            id2 = _create_plan_item(db, pid, time_slot="10:00")

        process_factory()

        statuses = {_get_item(id1)["status"], _get_item(id2)["status"]}
        # ровно один сгенерирован за тик
        assert statuses == {"generated", "approved"}

    def test_order_by_date_then_slot(self, db_ready):
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",))
            early = _create_plan_item(db, pid, item_date=today, time_slot="08:00")
            late = _create_plan_item(db, pid, item_date=tomorrow, time_slot="08:00")

        process_factory()
        # ранний по дате должен взяться первым
        assert _get_item(early)["status"] == "generated"
        assert _get_item(late)["status"] == "approved"


# ─── 6. Ошибка LLM ────────────────────────────────────────────────────────────


class TestLLMError:
    def test_garbage_llm_sets_error(self, db_ready, monkeypatch):
        with get_db() as db:
            pid = _create_project(db, platforms=("telegram",))
            item_id = _create_plan_item(db, pid)

        # chat возвращает мусор дважды → LLMError внутри generate_for_item
        import app.pipeline.from_plan as fp_module

        def garbage(*args, **kwargs):
            return "это не JSON {{{"

        monkeypatch.setattr(fp_module, "chat", garbage)

        # тик не должен упасть
        process_factory()

        item = _get_item(item_id)
        assert item["status"] == "error"
        assert item["error_text"]
        # контент не создан
        with get_db() as db:
            n = db.execute(
                "SELECT COUNT(*) AS n FROM content WHERE project_id=?", (pid,)
            ).fetchone()["n"]
        assert n == 0
