"""
Тесты маркетинговых целей контента (этап 8.4).

Покрывает:
- goal_label / goal_guidance — модульные проверки меток и инструкций
- Промпты item_post/item_story/video_script — нет незаполненных <<...>> после подстановки
- from_plan._generate_text — goal пробрасывается в load_prompt через spy
- video_script.generate_video_script — goal пробрасывается в load_prompt через spy
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
    return f"goal-{uuid.uuid4().hex[:8]}"


def _create_project(db, *, slug=None, platforms=("telegram",)):
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage)
        VALUES (?, 'Тест Цель', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', 'running')
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
    goal=None,
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
            (project_id, platform, content_type, date, time_slot, title, brief, status, goal)
        VALUES (?, ?, ?, '2026-06-15', '10:00', 'Тестовый пункт', ?, ?, ?)
        """,
        (project_id, platform, content_type, json.dumps(brief, ensure_ascii=False), status, goal),
    )
    return cur.lastrowid


def _create_idea(db, project_id: int, text: str = "Тестовая идея для видео") -> int:
    cur = db.execute(
        "INSERT INTO ideas (project_id, text, source, status) VALUES (?,?,?,?)",
        (project_id, text, "manual", "new"),
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


# ─── 1. Модульные тесты goal_label ────────────────────────────────────────────


def test_goal_label(patch_env):
    """goal_label возвращает правильные русские метки."""
    from app.pipeline.goals import goal_label

    assert goal_label("sell") == "продажа"
    assert goal_label("attract") == "привлечение новой аудитории"
    assert goal_label("retain") == "удержание и вовлечение текущей аудитории"
    assert goal_label("brand") == "укрепление бренда"
    assert goal_label(None) == "не задана"
    assert goal_label("мусор") == "не задана"
    assert goal_label("") == "не задана"


# ─── 2. Модульные тесты goal_guidance ─────────────────────────────────────────


def test_goal_guidance_distinct(patch_env):
    """goal_guidance возвращает правильные инструкции; разные цели — разные тексты."""
    from app.pipeline.goals import goal_guidance

    sell_g = goal_guidance("sell")
    attract_g = goal_guidance("attract")
    retain_g = goal_guidance("retain")
    brand_g = goal_guidance("brand")
    neutral_g = goal_guidance(None)

    # Инструкция продажи содержит маркер продажи
    assert "ПРОДАЖА" in sell_g or "оффер" in sell_g

    # Инструкция привлечения содержит маркер «НЕ продавай» или «ПРИВЛЕЧЬ»
    assert "НЕ продавай" in attract_g or "ПРИВЛЕЧЬ" in attract_g

    # Все четыре цели дают разные тексты
    guidances = [sell_g, attract_g, retain_g, brand_g]
    assert len(set(guidances)) == 4

    # Нейтральная инструкция для пустой/неизвестной цели
    assert goal_guidance(None) == goal_guidance("")
    assert "не задана" in neutral_g


# ─── 3. Промпты не содержат незаполненных плейсхолдеров ──────────────────────


def test_prompts_have_no_unfilled_placeholders(patch_env):
    """После подстановки всех плейсхолдеров в item_post и item_story не должно быть <<...>>."""
    import app.pipeline.prompts as pm
    pm._rules_cache = None
    from app.pipeline.prompts import load_prompt, load_rules
    from app.pipeline.goals import goal_label, goal_guidance

    rules = load_rules()
    sell_label = goal_label("sell")
    sell_guidance = goal_guidance("sell")

    # item_post
    result_post = load_prompt(
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
        ITEM_GOAL_LABEL=sell_label,
        GOAL_GUIDANCE=sell_guidance,
    )
    assert "<<" not in result_post, f"Незаполненные плейсхолдеры в item_post: {result_post}"

    # item_story
    result_story = load_prompt(
        "item_story",
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
        RULES=rules,
        ITEM_GOAL_LABEL=sell_label,
        GOAL_GUIDANCE=sell_guidance,
    )
    assert "<<" not in result_story, f"Незаполненные плейсхолдеры в item_story: {result_story}"

    # video_script
    result_video = load_prompt(
        "video_script",
        PROJECT_NAME="",
        PROJECT_DESCRIPTION="",
        PROJECT_AUDIENCE="",
        PROJECT_TONE="",
        PROJECT_GOALS="",
        PROJECT_CTA="",
        PROJECT_EXTRA="",
        PROJECT_LEARNINGS="",
        IDEA_TEXT="",
        PLATFORMS_SPEC="",
        RULES=rules,
        ITEM_GOAL_LABEL=sell_label,
        GOAL_GUIDANCE=sell_guidance,
    )
    assert "<<" not in result_video, f"Незаполненные плейсхолдеры в video_script: {result_video}"


# ─── 4. from_plan._generate_text пробрасывает goal в load_prompt ──────────────


def test_generate_text_passes_goal(patch_env, monkeypatch):
    """generate_for_item с goal='sell' → load_prompt получает ITEM_GOAL_LABEL='продажа'."""
    _setup_db()
    import app.pipeline.from_plan as fp
    import app.pipeline.censor as censor_mod

    captured = {}
    orig_load_prompt = fp.load_prompt

    def spy_load_prompt(name, **kw):
        if name == "item_post":
            captured.update(kw)
        return orig_load_prompt(name, **kw)

    monkeypatch.setattr(fp, "load_prompt", spy_load_prompt)
    # FAKE_LLM=1 → chat возвращает _FAKE_ITEM_POST, но на всякий случай мокируем явно
    monkeypatch.setattr(fp, "chat", lambda *a, **kw: _make_valid_post_json())
    # Цензор пропускает
    monkeypatch.setattr(
        censor_mod,
        "chat",
        lambda *a, **kw: json.dumps({"verdict": "ok"}, ensure_ascii=False),
    )

    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        item_id = _create_plan_item(
            db, project_id, platform="telegram", content_type="post",
            status="approved", goal="sell",
        )

    result = fp.generate_for_item(item_id)
    assert result is not None, "generate_for_item вернул None — контент не создан"

    assert "ITEM_GOAL_LABEL" in captured, "load_prompt не получил ITEM_GOAL_LABEL"
    assert captured["ITEM_GOAL_LABEL"] == "продажа"

    assert "GOAL_GUIDANCE" in captured, "load_prompt не получил GOAL_GUIDANCE"
    assert "ПРОДАЖА" in captured["GOAL_GUIDANCE"] or "оффер" in captured["GOAL_GUIDANCE"]


# ─── 5. video_script.generate_video_script пробрасывает goal в load_prompt ────


def test_generate_video_passes_goal(patch_env, monkeypatch):
    """generate_video_script с goal='attract' → load_prompt получает корректные плейсхолдеры."""
    _setup_db()
    import app.pipeline.video_script as vs
    import app.pipeline.censor as censor_mod

    captured = {}
    orig_load_prompt = vs.load_prompt

    def spy_load_prompt(name, **kw):
        if name == "video_script":
            captured.update(kw)
        return orig_load_prompt(name, **kw)

    monkeypatch.setattr(vs, "load_prompt", spy_load_prompt)
    monkeypatch.setattr(vs, "chat", lambda *a, **kw: _make_valid_video_json())
    # Цензор пропускает
    monkeypatch.setattr(
        censor_mod,
        "chat",
        lambda *a, **kw: json.dumps({"verdict": "ok"}, ensure_ascii=False),
    )

    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        idea_id = _create_idea(db, project_id)

    from app.pipeline.video_script import generate_video_script

    content_id = generate_video_script(idea_id, "video_footage", goal="attract")
    assert content_id is not None

    assert "ITEM_GOAL_LABEL" in captured, "load_prompt не получил ITEM_GOAL_LABEL"
    assert captured["ITEM_GOAL_LABEL"] == "привлечение новой аудитории"

    assert "GOAL_GUIDANCE" in captured, "load_prompt не получил GOAL_GUIDANCE"
    assert "ПРИВЛЕЧЬ" in captured["GOAL_GUIDANCE"] or "НЕ продавай" in captured["GOAL_GUIDANCE"]
