"""
Тесты двухуровневой защиты контента — цензор (этап 8.3).

Все тесты на FAKE_LLM=1 (из conftest.patch_env).
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
    return f"censor-{uuid.uuid4().hex[:8]}"


def _create_project(db, *, slug=None, platforms=("telegram",)):
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage)
        VALUES (?, 'Тест Цензор', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', 'running')
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
    platform="telegram",
    content_type="post",
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


def _make_valid_post_json() -> str:
    return json.dumps(
        {
            "title": "T",
            "text": "текст",
            "hashtags": [],
            "image_prompt": "p",
            "image_keywords": ["a"],
            "features": {
                "hook_type": "факт",
                "topic": "t",
                "length": "short",
                "format": "text",
                "ab_variant": "null",
            },
        },
        ensure_ascii=False,
    )


def _make_valid_video_json() -> str:
    """Минимально валидный видео-сценарий (telegram включён)."""
    return json.dumps(
        {
            "title": "Видео тест",
            "video": {
                "voice": "dmitry",
                "mood": "energetic",
                "scenes": [
                    {"text": "Сцена 1 хук.", "keywords": ["business", "work"]},
                    {"text": "Сцена 2 суть.", "keywords": ["product", "result"]},
                    {"text": "Сцена 3 детали.", "keywords": ["team", "office"]},
                    {"text": "Сцена 4 CTA.", "keywords": ["call", "action"]},
                ],
            },
            "telegram": {
                "text": "Текст для Telegram",
                "hashtags": ["#тест"],
            },
            "features": {
                "hook_type": "факт",
                "topic": "тест",
                "length": "short",
                "format": "reel",
                "ab_variant": "null",
            },
        },
        ensure_ascii=False,
    )


# ─── 1. Модульные тесты check_content ─────────────────────────────────────────


def test_check_content_fake_ok(patch_env):
    """При FAKE_LLM=1 check_content возвращает (True, "")."""
    _setup_db()
    from app.pipeline.censor import check_content

    ok, reason = check_content("любой текст")
    assert ok is True
    assert reason == ""


def test_check_content_block(patch_env, monkeypatch):
    """Если цензор вернул block — check_content возвращает (False, reason)."""
    _setup_db()
    import app.pipeline.censor as censor_mod

    monkeypatch.setattr(
        censor_mod,
        "chat",
        lambda *a, **kw: json.dumps({"verdict": "block", "reason": "18+"}, ensure_ascii=False),
    )
    ok, reason = censor_mod.check_content("нехороший контент", context="пост")
    assert ok is False
    assert reason == "18+"


def test_check_content_empty_skips(patch_env, monkeypatch):
    """Пустой текст пропускается без вызова chat."""
    _setup_db()
    import app.pipeline.censor as censor_mod

    def _should_not_be_called(*a, **kw):
        raise AssertionError("chat не должен вызываться для пустого текста")

    monkeypatch.setattr(censor_mod, "chat", _should_not_be_called)

    ok, reason = censor_mod.check_content("   ")
    assert ok is True
    assert reason == ""


def test_check_content_failopen(patch_env, monkeypatch):
    """При LLMError цензор fail-open: (True, "")."""
    _setup_db()
    import app.pipeline.censor as censor_mod
    from app.llm import LLMError

    monkeypatch.setattr(
        censor_mod,
        "chat",
        lambda *a, **kw: (_ for _ in ()).throw(LLMError("сеть")),
    )
    ok, reason = censor_mod.check_content("текст")
    assert ok is True
    assert reason == ""


def test_check_content_bad_json_failopen(patch_env, monkeypatch):
    """При невалидном JSON цензор fail-open: (True, "")."""
    _setup_db()
    import app.pipeline.censor as censor_mod

    monkeypatch.setattr(censor_mod, "chat", lambda *a, **kw: "не json {{{{")

    ok, reason = censor_mod.check_content("текст")
    assert ok is True
    assert reason == ""


# ─── 2. Интеграционные тесты generate_for_item ───────────────────────────────


def test_integration_censor_block_all_attempts(patch_env, monkeypatch):
    """Цензор блокирует все 3 попытки → item.status='error', error_text содержит 'Цензор'."""
    _setup_db()
    import app.pipeline.from_plan as fp_mod
    import app.pipeline.censor as censor_mod

    # from_plan.chat возвращает валидный пост
    monkeypatch.setattr(fp_mod, "chat", lambda *a, **kw: _make_valid_post_json())
    # censor.chat всегда block
    monkeypatch.setattr(
        censor_mod,
        "chat",
        lambda *a, **kw: json.dumps({"verdict": "block", "reason": "18+"}, ensure_ascii=False),
    )

    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        item_id = _create_plan_item(db, project_id, platform="telegram", content_type="post")

    from app.pipeline.from_plan import generate_for_item

    result = generate_for_item(item_id)
    assert result is None

    with get_db() as db:
        row = db.execute("SELECT * FROM plan_items WHERE id=?", (item_id,)).fetchone()
    assert row["status"] == "error"
    assert "Цензор" in (row["error_text"] or "")


def test_integration_censor_ok(patch_env, monkeypatch):
    """Цензор пропускает → content создан, item.status='generated'."""
    _setup_db()
    import app.pipeline.from_plan as fp_mod
    import app.pipeline.censor as censor_mod

    monkeypatch.setattr(fp_mod, "chat", lambda *a, **kw: _make_valid_post_json())
    monkeypatch.setattr(
        censor_mod,
        "chat",
        lambda *a, **kw: json.dumps({"verdict": "ok", "reason": ""}, ensure_ascii=False),
    )

    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        item_id = _create_plan_item(db, project_id, platform="telegram", content_type="post")

    from app.pipeline.from_plan import generate_for_item

    result = generate_for_item(item_id)
    assert result is not None

    with get_db() as db:
        row = db.execute("SELECT * FROM plan_items WHERE id=?", (item_id,)).fetchone()
    assert row["status"] == "generated"


def test_integration_censor_regen(patch_env, monkeypatch):
    """Цензор блокирует первый раз, второй — ok. chat вызван ≥2 раз (from_plan.chat)."""
    _setup_db()
    import app.pipeline.from_plan as fp_mod
    import app.pipeline.censor as censor_mod

    fp_call_count = {"n": 0}

    def _fp_chat(*a, **kw):
        fp_call_count["n"] += 1
        return _make_valid_post_json()

    censor_call_count = {"n": 0}

    def _censor_chat(*a, **kw):
        censor_call_count["n"] += 1
        # Первый вызов — block, остальные — ok
        if censor_call_count["n"] == 1:
            return json.dumps({"verdict": "block", "reason": "тест"}, ensure_ascii=False)
        return json.dumps({"verdict": "ok", "reason": ""}, ensure_ascii=False)

    monkeypatch.setattr(fp_mod, "chat", _fp_chat)
    monkeypatch.setattr(censor_mod, "chat", _censor_chat)

    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        item_id = _create_plan_item(db, project_id, platform="telegram", content_type="post")

    from app.pipeline.from_plan import generate_for_item

    result = generate_for_item(item_id)
    assert result is not None
    # from_plan.chat вызван минимум 2 раза (генерация + регенерация)
    assert fp_call_count["n"] >= 2


# ─── 3. Тест что RULES подставляются в промпт ────────────────────────────────


def test_rules_in_item_prompt(patch_env):
    """load_prompt('item_post', RULES=load_rules(), ...) подставляет правила, нет <<RULES>>."""
    _setup_db()
    from app.pipeline.prompts import load_prompt, load_rules

    rules = load_rules()
    result = load_prompt(
        "item_post",
        PLATFORM="telegram",
        PROJECT_NAME="",
        PROJECT_DESCRIPTION="",
        PROJECT_AUDIENCE="",
        PROJECT_TONE="",
        PROJECT_GOALS="",
        PROJECT_CTA="",
        PROJECT_EXTRA="",
        STRATEGY_BLOCK="",
        ITEM_TITLE="",
        ITEM_HOOK="",
        ITEM_OUTLINE="",
        ITEM_CTA="",
        ITEM_KEYWORDS="",
        ITEM_RUBRIC="",
        PROJECT_LEARNINGS="",
        PLATFORM_SPEC="",
        RULES=rules,
    )
    # Плейсхолдер подставлен
    assert "<<RULES>>" not in result
    # Что-то из forbidden_ru.md присутствует в результате
    assert "18+" in result or "запрещено" in result.lower() or "насил" in result.lower()


# ─── 4. Тест цензора для video_script ────────────────────────────────────────


def _create_idea(db, project_id: int, text: str = "Тестовая идея для видео") -> int:
    cur = db.execute(
        "INSERT INTO ideas (project_id, text, source, status) VALUES (?,?,?,?)",
        (project_id, text, "manual", "new"),
    )
    return cur.lastrowid


def test_video_script_censor_block(patch_env, monkeypatch):
    """Цензор блокирует видео-сценарий → generate_video_script поднимает CensorError."""
    _setup_db()
    import app.pipeline.video_script as vs_mod
    import app.pipeline.censor as censor_mod

    monkeypatch.setattr(vs_mod, "chat", lambda *a, **kw: _make_valid_video_json())
    monkeypatch.setattr(
        censor_mod,
        "chat",
        lambda *a, **kw: json.dumps({"verdict": "block", "reason": "18+"}, ensure_ascii=False),
    )

    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        idea_id = _create_idea(db, project_id)

    from app.pipeline.video_script import generate_video_script
    from app.pipeline.censor import CensorError

    with pytest.raises(CensorError):
        generate_video_script(idea_id, "video_footage")
