"""
Тесты генератора контент-плана и фонового мозга (этап 7, волна C).

Все тесты на FAKE_LLM=1 (из conftest.patch_env).
Для тестов, где даты FAKE_PLAN выходят за нужный период, используем
monkeypatch chat с собственным фиксированным JSON.
"""
import json
import uuid
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Утилиты ──────────────────────────────────────────────────────────────────


def _slug() -> str:
    return f"plan-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, *, platforms=("telegram",), content_types=None,
                    stage="running", slug=None, settings_json=None):
    """Создаёт проект со всеми полями и включёнными площадками."""
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage, settings)
        VALUES (?, 'Тест', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', ?, ?)
        """,
        (s, stage, settings_json),
    )
    project_id = cur.lastrowid
    for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
        enabled = 1 if p in platforms else 0
        ct = None
        if content_types and p in content_types:
            ct = json.dumps(content_types[p], ensure_ascii=False)
        db.execute(
            "INSERT INTO project_platforms "
            "(project_id, platform, enabled, mode, content_types) VALUES (?,?,?,?,?)",
            (project_id, p, enabled, "auto", ct),
        )
    return project_id, s


def _create_active_strategy(db, project_id, *, strategy_data=None):
    """Создаёт активную стратегию проекта."""
    if strategy_data is None:
        strategy_data = {
            "summary": "Тест",
            "positioning": "поз",
            "platforms": {
                "telegram": {
                    "goals": "цель",
                    "rubrics": ["Рубрика 1", "Рубрика 2"],
                    "content_mix": {"post": 3, "video": 2},
                    "best_times": ["09:00", "18:00"],
                    "kpi": "охват",
                },
                "vk": {
                    "goals": "цель VK",
                    "rubrics": ["Разбор"],
                    "content_mix": {"post": 3},
                    "best_times": ["10:00"],
                    "kpi": "охват VK",
                },
            },
        }
    cur = db.execute(
        "INSERT INTO strategies (project_id, version, status, strategy) VALUES (?,?,?,?)",
        (project_id, 1, "active", json.dumps(strategy_data, ensure_ascii=False)),
    )
    return cur.lastrowid


def _fake_plan_json(date_from: str, ctype: str = "post", n: int = 3) -> str:
    """Генерирует фиксированный JSON плана с n пунктами начиная с date_from."""
    d = date.fromisoformat(date_from)
    items = []
    for i in range(n):
        items.append({
            "date": (d + timedelta(days=i)).isoformat(),
            "time": "09:00",
            "content_type": ctype,
            "title": f"Пункт плана {i + 1}",
            "brief": {
                "hook": f"Хук {i + 1}",
                "outline": f"Структура {i + 1}",
                "cta": "Переходи по ссылке",
                "keywords": ["тест"],
                "rubric": "Рубрика 1",
            },
        })
    return json.dumps({"items": items}, ensure_ascii=False)


# ─── 1. generate_plan: базовая генерация ─────────────────────────────────────


def test_generate_plan_creates_items_with_fake_llm(patch_env, monkeypatch):
    """FAKE_LLM возвращает план с датами 2026-06-15..20, типы post/video.
    При периоде 2026-06-15..2026-06-22 все валидные items вставляются."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_active_strategy(db, project_id)

    from app.pipeline.planner import generate_plan
    # FAKE_PLAN возвращает 6 items: 4 post + 2 video, даты 15–20 июня
    # все типы (post, video) включены по умолчанию для telegram
    count = generate_plan(project_id, "telegram", "2026-06-15", "2026-06-22")
    assert count == 6

    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM plan_items WHERE project_id=? AND platform='telegram'",
            (project_id,),
        ).fetchall()
    assert len(rows) == 6
    statuses = {r["status"] for r in rows}
    assert statuses == {"proposed"}


def test_generate_plan_filters_out_of_period_dates(patch_env, monkeypatch):
    """FAKE_LLM возвращает даты 15–20 июня. Период 17–18 → только 2 items."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_active_strategy(db, project_id)

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "telegram", "2026-06-17", "2026-06-18")
    # 17-го: post "Инструмент недели", 18-го: post "5 метрик" — 2 items
    assert count == 2


def test_generate_plan_filters_disabled_content_types(patch_env, monkeypatch):
    """video выключен → только post-items из FAKE_PLAN."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(
            db,
            platforms=("telegram",),
            content_types={"telegram": {"post": True, "video": False}},
        )
        _create_active_strategy(db, project_id)

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "telegram", "2026-06-15", "2026-06-22")
    # FAKE_PLAN: 4 post + 2 video → только 4 post прошли фильтр
    assert count == 4

    with get_db() as db:
        rows = db.execute(
            "SELECT content_type FROM plan_items WHERE project_id=?", (project_id,)
        ).fetchall()
    ctypes = {r["content_type"] for r in rows}
    assert "video" not in ctypes
    assert "post" in ctypes


def test_generate_plan_no_strategy_returns_zero(patch_env):
    """Нет активной стратегии → 0."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        # Стратегию не создаём

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "telegram", "2026-06-15", "2026-06-22")
    assert count == 0


def test_generate_plan_no_platform_in_strategy_returns_zero(patch_env):
    """Стратегия есть, но нет секции для youtube → 0."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(
            db,
            platforms=("telegram", "youtube"),
            content_types={"youtube": {"short": True}},
        )
        # Стратегия только для telegram
        _create_active_strategy(
            db, project_id,
            strategy_data={
                "summary": "s",
                "positioning": "p",
                "platforms": {
                    "telegram": {
                        "goals": "g",
                        "rubrics": ["r"],
                        "content_mix": {"post": 2},
                        "best_times": ["09:00"],
                        "kpi": "k",
                    }
                },
            },
        )

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "youtube", "2026-06-15", "2026-06-22")
    assert count == 0


def test_generate_plan_no_enabled_types_returns_zero(patch_env):
    """Все типы выключены → 0."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(
            db,
            platforms=("telegram",),
            content_types={"telegram": {"post": False, "video": False}},
        )
        _create_active_strategy(db, project_id)

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "telegram", "2026-06-15", "2026-06-22")
    assert count == 0


def test_generate_plan_deduplication(patch_env):
    """Повторный вызов с тем же FAKE-ответом → 0 новых (дедупликация по title)."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_active_strategy(db, project_id)

    from app.pipeline.planner import generate_plan

    count1 = generate_plan(project_id, "telegram", "2026-06-15", "2026-06-22")
    assert count1 == 6

    # Повторный вызов — те же items, те же titles → 0 новых
    count2 = generate_plan(project_id, "telegram", "2026-06-15", "2026-06-22")
    assert count2 == 0


def test_generate_plan_dedup_different_platform_no_conflict(patch_env):
    """Тот же title для другой платформы — НЕ дедуп, вставляется."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(
            db,
            platforms=("telegram", "vk"),
        )
        _create_active_strategy(db, project_id)

    from app.pipeline.planner import generate_plan
    count_tg = generate_plan(project_id, "telegram", "2026-06-15", "2026-06-22")
    assert count_tg == 6

    # vk: FAKE_PLAN возвращает только post/video.
    # vk имеет post и video по умолчанию (video default_on=True).
    # Те же titles что у telegram — но другая platform → дедуп не срабатывает.
    count_vk = generate_plan(project_id, "vk", "2026-06-15", "2026-06-22")
    # vk plan_items — независимы от telegram
    assert count_vk == 6


def test_generate_plan_custom_json_items(patch_env, monkeypatch):
    """Собственный monkeypatch chat: валидные + невалидные items."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_active_strategy(db, project_id)

    import app.pipeline.planner as planner_mod

    custom_items = [
        # валидный
        {
            "date": "2026-06-15",
            "time": "09:00",
            "content_type": "post",
            "title": "Валидный пост",
            "brief": {"hook": "h", "outline": "o", "cta": "c",
                      "keywords": ["k"], "rubric": "r"},
        },
        # content_type невалиден для telegram
        {
            "date": "2026-06-15",
            "time": "09:00",
            "content_type": "short",  # youtube-only тип
            "title": "Невалидный тип",
            "brief": {},
        },
        # дата вне периода
        {
            "date": "2026-07-01",
            "time": "09:00",
            "content_type": "post",
            "title": "Дата вне периода",
            "brief": {},
        },
        # пустой title
        {
            "date": "2026-06-16",
            "time": "09:00",
            "content_type": "post",
            "title": "",
            "brief": {},
        },
    ]

    def fake_chat(messages, purpose=None, json_mode=False, **kw):
        return json.dumps({"items": custom_items}, ensure_ascii=False)

    monkeypatch.setattr(planner_mod, "chat", fake_chat)

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "telegram", "2026-06-15", "2026-06-20")
    # Только 1 валидный item
    assert count == 1


def test_generate_plan_default_time_slot(patch_env, monkeypatch):
    """item без поля time или с неверным форматом → дефолт '12:00'."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_active_strategy(db, project_id)

    import app.pipeline.planner as planner_mod

    custom_items = [
        {
            "date": "2026-06-15",
            # time отсутствует
            "content_type": "post",
            "title": "Без времени",
            "brief": {},
        },
        {
            "date": "2026-06-16",
            "time": "неверный формат",
            "content_type": "post",
            "title": "Неверное время",
            "brief": {},
        },
    ]

    def fake_chat(messages, purpose=None, json_mode=False, **kw):
        return json.dumps({"items": custom_items}, ensure_ascii=False)

    monkeypatch.setattr(planner_mod, "chat", fake_chat)

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "telegram", "2026-06-15", "2026-06-20")
    assert count == 2

    with get_db() as db:
        rows = db.execute(
            "SELECT time_slot FROM plan_items WHERE project_id=?", (project_id,)
        ).fetchall()
    for row in rows:
        assert row["time_slot"] == "12:00"


def test_generate_plan_retry_on_invalid_json(patch_env, monkeypatch):
    """Первый ответ — мусор; второй — валидный JSON → items созданы."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_active_strategy(db, project_id)

    import app.pipeline.planner as planner_mod

    call_count = {"n": 0}

    def fake_chat(messages, purpose=None, json_mode=False, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "мусор не JSON {{{"
        # retry: возвращаем валидный план
        return _fake_plan_json("2026-06-15", "post", 2)

    monkeypatch.setattr(planner_mod, "chat", fake_chat)

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "telegram", "2026-06-15", "2026-06-20")
    assert count == 2
    assert call_count["n"] == 2  # был retry


# ─── 2. process_brain: generating-стратегии ───────────────────────────────────


def test_process_brain_handles_generating_strategy(patch_env, monkeypatch):
    """generating-стратегия существует → build_strategy вызван, ранний выход."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        cur = db.execute(
            "INSERT INTO strategies (project_id, version, status) VALUES (?,?,?)",
            (project_id, 1, "generating"),
        )
        strategy_id = cur.lastrowid

    import app.services.brain as brain_mod

    called_with = {}

    def fake_build_strategy(sid):
        called_with["sid"] = sid

    monkeypatch.setattr(brain_mod, "_pick_generating_strategy", lambda: strategy_id)
    # Патчим import внутри функции
    import app.pipeline.strategy as strat_mod
    monkeypatch.setattr(strat_mod, "build_strategy", fake_build_strategy)

    brain_mod.process_brain()

    assert called_with.get("sid") == strategy_id


def test_process_brain_generating_strategy_early_exit(patch_env, monkeypatch):
    """Если есть generating-стратегия — process_brain выходит без генерации плана."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_active_strategy(db, project_id)
        db.execute(
            "INSERT INTO strategies (project_id, version, status) VALUES (?,?,?)",
            (project_id, 2, "generating"),
        )

    import app.services.brain as brain_mod
    import app.pipeline.planner as planner_mod

    plan_called = {"n": 0}

    def fake_generate_plan(*args, **kwargs):
        plan_called["n"] += 1
        return 0

    # Патчим generate_plan через brain_mod namespace
    monkeypatch.setattr(brain_mod, "_pick_generating_strategy",
                        lambda: _get_any_generating_strategy_id(project_id))

    import app.pipeline.strategy as strat_mod
    monkeypatch.setattr(strat_mod, "build_strategy", lambda sid: None)
    monkeypatch.setattr(planner_mod, "generate_plan", fake_generate_plan)

    brain_mod.process_brain()

    # generate_plan не вызывался — вышли раньше
    assert plan_called["n"] == 0


def _get_any_generating_strategy_id(project_id: int) -> int:
    """Вспомогательная: вернуть id generating-стратегии проекта."""
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM strategies WHERE project_id=? AND status='generating'",
            (project_id,),
        ).fetchone()
    return row["id"] if row else None


# ─── 3. process_brain: rolling-план ──────────────────────────────────────────


def test_process_brain_fills_empty_plan(patch_env, monkeypatch):
    """running-проект с активной стратегией, план пустой → generate_plan вызван."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",), stage="running")
        _create_active_strategy(db, project_id)

    import app.services.brain as brain_mod
    import app.pipeline.planner as planner_mod

    plan_calls = []

    def fake_generate_plan(pid, platform, date_from, date_to):
        plan_calls.append({"project_id": pid, "platform": platform,
                           "date_from": date_from, "date_to": date_to})
        return 3

    monkeypatch.setattr(planner_mod, "generate_plan", fake_generate_plan)

    brain_mod.process_brain()

    assert len(plan_calls) == 1
    assert plan_calls[0]["project_id"] == project_id
    assert plan_calls[0]["platform"] == "telegram"


def test_process_brain_plan_paused_no_generation(patch_env, monkeypatch):
    """plan_paused=1 → generate_plan НЕ вызывается."""
    _setup_db()
    settings_json = json.dumps({"plan_paused": 1})
    with get_db() as db:
        project_id, _ = _create_project(
            db, platforms=("telegram",), stage="running",
            settings_json=settings_json,
        )
        _create_active_strategy(db, project_id)

    import app.services.brain as brain_mod
    import app.pipeline.planner as planner_mod

    plan_called = {"n": 0}

    def fake_generate_plan(*args, **kwargs):
        plan_called["n"] += 1
        return 0

    monkeypatch.setattr(planner_mod, "generate_plan", fake_generate_plan)

    brain_mod.process_brain()

    assert plan_called["n"] == 0


def test_process_brain_paused_project_no_generation(patch_env, monkeypatch):
    """stage='paused' → generate_plan НЕ вызывается."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",), stage="paused")
        _create_active_strategy(db, project_id)

    import app.services.brain as brain_mod
    import app.pipeline.planner as planner_mod

    plan_called = {"n": 0}

    def fake_generate_plan(*args, **kwargs):
        plan_called["n"] += 1
        return 0

    monkeypatch.setattr(planner_mod, "generate_plan", fake_generate_plan)

    brain_mod.process_brain()

    assert plan_called["n"] == 0


def test_process_brain_fully_covered_no_generation(patch_env, monkeypatch):
    """План уже покрыт до горизонта → generate_plan НЕ вызывается."""
    _setup_db()
    settings_json = json.dumps({"plan_horizon_days": 7})
    with get_db() as db:
        project_id, _ = _create_project(
            db, platforms=("telegram",), stage="running",
            settings_json=settings_json,
        )
        _create_active_strategy(db, project_id)

        # Вставляем plan_item с датой = today + 8 дней (за горизонтом)
        future_date = (date.today() + timedelta(days=8)).isoformat()
        db.execute(
            "INSERT INTO plan_items (project_id, strategy_id, platform, content_type, "
            "date, time_slot, title, status) VALUES (?,?,?,?,?,?,?,?)",
            (project_id, None, "telegram", "post", future_date,
             "09:00", "Уже запланировано", "proposed"),
        )

    import app.services.brain as brain_mod
    import app.pipeline.planner as planner_mod

    plan_called = {"n": 0}

    def fake_generate_plan(*args, **kwargs):
        plan_called["n"] += 1
        return 0

    monkeypatch.setattr(planner_mod, "generate_plan", fake_generate_plan)

    brain_mod.process_brain()

    assert plan_called["n"] == 0


def test_process_brain_llm_error_no_exception(patch_env, monkeypatch):
    """LLMError в generate_plan → process_brain не падает."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",), stage="running")
        _create_active_strategy(db, project_id)

    import app.services.brain as brain_mod
    import app.pipeline.planner as planner_mod
    from app.llm import LLMError

    def fake_generate_plan(*args, **kwargs):
        raise LLMError("Тестовая ошибка LLM")

    monkeypatch.setattr(planner_mod, "generate_plan", fake_generate_plan)

    # Не должно бросить исключение
    brain_mod.process_brain()


def test_process_brain_generic_exception_no_propagation(patch_env, monkeypatch):
    """Любое исключение в generate_plan → process_brain не падает наружу."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",), stage="running")
        _create_active_strategy(db, project_id)

    import app.services.brain as brain_mod
    import app.pipeline.planner as planner_mod

    def fake_generate_plan(*args, **kwargs):
        raise RuntimeError("Случайная ошибка")

    monkeypatch.setattr(planner_mod, "generate_plan", fake_generate_plan)

    # Не должно бросить исключение
    brain_mod.process_brain()


def test_process_brain_one_platform_per_tick(patch_env, monkeypatch):
    """Два проекта / две платформы → за один тик только одна платформа."""
    _setup_db()
    with get_db() as db:
        project_id1, _ = _create_project(db, platforms=("telegram", "vk"),
                                          stage="running", slug=_slug())
        _create_active_strategy(db, project_id1)

    import app.services.brain as brain_mod
    import app.pipeline.planner as planner_mod

    plan_calls = []

    def fake_generate_plan(pid, platform, date_from, date_to):
        plan_calls.append(platform)
        return 3

    monkeypatch.setattr(planner_mod, "generate_plan", fake_generate_plan)

    brain_mod.process_brain()

    # За один тик — только одна платформа (ранний return после первой генерации)
    assert len(plan_calls) == 1


def test_process_brain_no_active_strategy_no_generation(patch_env, monkeypatch):
    """running-проект без активной стратегии → generate_plan не вызывается."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",), stage="running")
        # Стратегию не создаём

    import app.services.brain as brain_mod
    import app.pipeline.planner as planner_mod

    plan_called = {"n": 0}

    def fake_generate_plan(*args, **kwargs):
        plan_called["n"] += 1
        return 0

    monkeypatch.setattr(planner_mod, "generate_plan", fake_generate_plan)

    brain_mod.process_brain()

    assert plan_called["n"] == 0


def test_process_brain_rejected_items_not_counted_as_coverage(patch_env, monkeypatch):
    """Отклонённые plan_items (rejected) не считаются покрытием → план догенерируется."""
    _setup_db()
    settings_json = json.dumps({"plan_horizon_days": 7})
    with get_db() as db:
        project_id, _ = _create_project(
            db, platforms=("telegram",), stage="running",
            settings_json=settings_json,
        )
        _create_active_strategy(db, project_id)

        # Вставляем REJECTED item с будущей датой
        future_date = (date.today() + timedelta(days=8)).isoformat()
        db.execute(
            "INSERT INTO plan_items (project_id, strategy_id, platform, content_type, "
            "date, time_slot, title, status) VALUES (?,?,?,?,?,?,?,?)",
            (project_id, None, "telegram", "post", future_date,
             "09:00", "Отклонённый", "rejected"),
        )

    import app.services.brain as brain_mod
    import app.pipeline.planner as planner_mod

    plan_called = {"n": 0}

    def fake_generate_plan(*args, **kwargs):
        plan_called["n"] += 1
        return 3

    monkeypatch.setattr(planner_mod, "generate_plan", fake_generate_plan)

    brain_mod.process_brain()

    # rejected не считается покрытием → generate_plan должен вызваться
    assert plan_called["n"] == 1
