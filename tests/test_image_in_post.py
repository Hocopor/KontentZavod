"""
Тест: текстовый пост с картинкой (FAKE_ASSETS=1).

Проверяет, что после генерации text-поста через from_plan._generate_text
в content.files сохраняется image_path и файл существует на диске.
Также проверяет, что image_keywords из ответа LLM используются для поиска картинки.
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
    """Если LLM не вернул image_prompt и image_keywords, content.files = NULL (пост без картинки)."""
    item_id = db_with_project["item_id"]

    # Патчим chat так, чтобы не возвращал image_prompt и image_keywords
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
                # image_prompt и image_keywords НЕ включены
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

    # Без image_prompt и image_keywords files должен быть NULL
    assert content["files"] is None or content["files"] == "", (
        f"Ожидался NULL files, получили: {content['files']}"
    )


def test_text_post_uses_image_keywords(db_with_project, monkeypatch):
    """LLM вернул image_keywords → fetch_image вызывается с этими ключевыми словами."""
    item_id = db_with_project["item_id"]

    import app.pipeline.from_plan as fp_module
    import app.pipeline.assets as assets_mod
    import json as _json

    fetch_image_calls = []

    def mock_fetch_image(keywords, dest):
        fetch_image_calls.append(keywords)
        # Имитируем FAKE_ASSETS: создаём файл
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10)
        return True

    def chat_with_keywords(messages, purpose=None, **kwargs):
        if purpose == "item_post":
            return _json.dumps({
                "title": "Тест с keywords",
                "text": "Текст с image_keywords",
                "hashtags": [],
                "image_prompt": "business analytics dashboard modern office",
                "image_keywords": ["business", "analytics", "office"],
                "features": {
                    "hook_type": "факт",
                    "topic": "тест",
                    "length": "short",
                    "format": "text",
                    "ab_variant": "null",
                },
            }, ensure_ascii=False)
        return "FAKE_LLM response"

    monkeypatch.setattr(fp_module, "chat", chat_with_keywords)
    monkeypatch.setattr(fp_module, "fetch_image", mock_fetch_image)

    process_factory()

    with get_db() as db:
        item = db.execute(
            "SELECT * FROM plan_items WHERE id=?", (item_id,)
        ).fetchone()

    assert item["status"] == "generated"

    # fetch_image должен был вызваться с image_keywords, а не с image_prompt
    assert len(fetch_image_calls) == 1, f"fetch_image вызван {len(fetch_image_calls)} раз"
    assert fetch_image_calls[0] == ["business", "analytics", "office"], (
        f"Ожидались image_keywords, получили: {fetch_image_calls[0]}"
    )


def test_text_post_keywords_fallback_to_image_prompt_words(db_with_project, monkeypatch):
    """Нет image_keywords → фоллбэк: первые 5 слов image_prompt."""
    item_id = db_with_project["item_id"]

    import app.pipeline.from_plan as fp_module
    import json as _json

    fetch_image_calls = []

    def mock_fetch_image(keywords, dest):
        fetch_image_calls.append(keywords)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10)
        return True

    def chat_no_keywords(messages, purpose=None, **kwargs):
        if purpose == "item_post":
            return _json.dumps({
                "title": "Тест без keywords",
                "text": "Текст без image_keywords",
                "hashtags": [],
                "image_prompt": "word1 word2 word3 word4 word5 word6 word7",
                # image_keywords отсутствует
                "features": {
                    "hook_type": "факт",
                    "topic": "тест",
                    "length": "short",
                    "format": "text",
                    "ab_variant": "null",
                },
            }, ensure_ascii=False)
        return "FAKE_LLM response"

    monkeypatch.setattr(fp_module, "chat", chat_no_keywords)
    monkeypatch.setattr(fp_module, "fetch_image", mock_fetch_image)

    process_factory()

    assert len(fetch_image_calls) == 1
    # Фоллбэк: первые 5 слов image_prompt
    assert fetch_image_calls[0] == ["word1", "word2", "word3", "word4", "word5"], (
        f"Фоллбэк должен давать первые 5 слов image_prompt: {fetch_image_calls[0]}"
    )


def test_files_images_route_serves_post_jpg(patch_env):
    """Регрессия 9.2: GET /files/images/{id}.jpg отдаёт картинку поста (post.jpg),
    а не только слайд истории — раньше роут 404-ил для постов."""
    from fastapi.testclient import TestClient
    from app.main import app

    media = cfg_module.settings.data_dir_absolute / "media" / "987654"
    media.mkdir(parents=True, exist_ok=True)
    (media / "post.jpg").write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIFpostimage")

    with TestClient(app) as c:
        resp = c.get("/files/images/987654.jpg")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/jpeg")
    assert resp.content.startswith(b"\xff\xd8\xff")
