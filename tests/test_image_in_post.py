"""
Тест: текстовый пост с картинкой (FAKE_ASSETS=1).

Проверяет, что после генерации text-поста через from_plan._generate_text
в content.files сохраняется image_path и файл существует на диске.
"""
import json
from pathlib import Path
import pytest
import app.config as cfg_module
from app.db import get_db, init_db
from app.services.factory import process_factory


@pytest.fixture
def db_with_project(patch_env, monkeypatch):
    """БД с running-проектом и одним telegram-постом."""
    monkeypatch.setenv("FAKE_ASSETS", "1")
    init_db(cfg_module.settings.db_path_absolute)

    with get_db() as db:
        import uuid
        slug = f"test-{uuid.uuid4().hex[:8]}"
        settings_json = json.dumps({
            "autogen": 1,
            "gen_paused": 0,
            "gen_lookahead_days": 3,
        })
        cur = db.execute(
            """
            INSERT INTO projects
                (slug, name, description, audience, tone, goals, cta, themes, stage, settings)
            VALUES (?, 'Тест', 'Описание', 'Аудитория', 'тон', 'цели', 'CTA', 'темы', 'running', ?)
            """,
            (slug, settings_json),
        )
        pid = cur.lastrowid

        for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
            enabled = 1 if p == "telegram" else 0
            db.execute(
                "INSERT INTO project_platforms (project_id, platform, enabled, mode) "
                "VALUES (?,?,?,?)",
                (pid, p, enabled, "auto"),
            )

        brief = json.dumps({
            "hook": "Хук",
            "outline": "1. Пункт. 2. Пункт.",
            "cta": "Подпишитесь",
            "keywords": ["маркетинг"],
            "rubric": "Разбор",
        }, ensure_ascii=False)

        cur2 = db.execute(
            """
            INSERT INTO plan_items
                (project_id, platform, content_type, date, time_slot, title, brief, status)
            VALUES (?, 'telegram', 'post', date('now'), '09:00', 'Заголовок теста', ?, 'approved')
            """,
            (pid, brief),
        )
        item_id = cur2.lastrowid

    return {"project_id": pid, "item_id": item_id}


def test_text_post_has_image_path(db_with_project):
    """После генерации text-поста content.files содержит image_path."""
    item_id = db_with_project["item_id"]

    process_factory()

    with get_db() as db:
        item = db.execute(
            "SELECT * FROM plan_items WHERE id=?", (item_id,)
        ).fetchone()

    assert item["status"] == "generated", f"Неожиданный статус: {item['status']}"
    content_id = item["content_id"]
    assert content_id is not None

    with get_db() as db:
        content = db.execute(
            "SELECT * FROM content WHERE id=?", (content_id,)
        ).fetchone()

    assert content is not None
    files_raw = content["files"]
    # FAKE_LLM возвращает image_prompt → при FAKE_ASSETS=1 файл должен быть создан
    assert files_raw, "content.files не заполнен — картинка не сохранена"
    files = json.loads(files_raw)
    assert "image_path" in files, f"image_path отсутствует в files: {files}"
    image_path = Path(files["image_path"])
    assert image_path.exists(), f"Файл картинки не существует: {image_path}"
    assert image_path.stat().st_size > 0, "Файл картинки пустой"


def test_text_post_without_image_prompt_skips_image(db_with_project, monkeypatch):
    """Если LLM не вернул image_prompt, content.files = NULL (пост без картинки)."""
    item_id = db_with_project["item_id"]

    # Патчим chat так, чтобы не возвращал image_prompt
    import app.pipeline.from_plan as fp_module
    import json as _json

    def chat_no_image(messages, purpose=None, **kwargs):
        if purpose == "item_post":
            return _json.dumps({
                "title": "Тест",
                "text": "Текст без картинки",
                "hashtags": [],
                "features": {
                    "hook_type": "факт",
                    "topic": "тест",
                    "length": "short",
                    "format": "text",
                    "ab_variant": "null",
                },
                # image_prompt НЕ включён
            }, ensure_ascii=False)
        return "FAKE_LLM response"

    monkeypatch.setattr(fp_module, "chat", chat_no_image)

    process_factory()

    with get_db() as db:
        item = db.execute(
            "SELECT * FROM plan_items WHERE id=?", (item_id,)
        ).fetchone()

    assert item["status"] == "generated"
    with get_db() as db:
        content = db.execute(
            "SELECT * FROM content WHERE id=?", (item["content_id"],)
        ).fetchone()

    # Без image_prompt files должен быть NULL
    assert content["files"] is None or content["files"] == "", (
        f"Ожидался NULL files, получили: {content['files']}"
    )
