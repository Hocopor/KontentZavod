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


# Якорь стратегии v2 — понедельник; недельные слоты считаются от него.
ANCHOR = "2026-06-15"


def _create_active_strategy(db, project_id, *, strategy_data=None, activated_on=ANCHOR):
    """
    Создаёт активную стратегию v2 проекта (с фазами и mix).

    По умолчанию: telegram post=4/нед + video=2/нед, vk post=3/нед,
    одна бессрочная фаза, якорь ANCHOR.
    """
    if strategy_data is None:
        strategy_data = {
            "summary": "Тест",
            "positioning": "поз",
            "activated_on": activated_on,
            "phases": [{
                "n": 1, "weeks": 10 ** 6,
                "goal_share": {"attract": 50, "retain": 30, "sell": 20},
                "mix": {
                    "telegram": {"post": 4, "video": 2},
                    "vk": {"post": 3},
                },
            }],
            "platforms": {
                "telegram": {
                    "goals": "цель",
                    "rubrics": [
                        {"name": "Рубрика 1", "goal": "attract", "description": "desc"},
                        {"name": "Рубрика 2", "goal": "sell", "description": "desc"},
                    ],
                    "best_times": ["09:00", "18:00"],
                    "kpi": "охват",
                },
                "vk": {
                    "goals": "цель VK",
                    "rubrics": [{"name": "Разбор", "goal": "attract", "description": "d"}],
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


def _fake_plan_json(n: int = 60) -> str:
    """JSON плана v2 с n элементами по slot_id 1..n (планнер джойнит по slot_id)."""
    items = []
    for i in range(1, n + 1):
        items.append({
            "slot_id": i,
            "title": f"Пункт плана {i}",
            "brief": {
                "hook": f"Хук {i}",
                "outline": f"Структура {i}",
                "cta": "Переходи по ссылке",
                "keywords": ["тест"],
                "rubric": "Рубрика 1",
            },
        })
    return json.dumps({"items": items}, ensure_ascii=False)


# ─── 1. generate_plan: базовая генерация ─────────────────────────────────────


def test_generate_plan_creates_items_with_fake_llm(patch_env, monkeypatch):
    """mix telegram = post:4 + video:2 = 6/нед. За одну неделю → 6 пунктов,
    goal заполнен из слота."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_active_strategy(db, project_id)

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "telegram", ANCHOR, "2026-06-21")
    assert count == 6

    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM plan_items WHERE project_id=? AND platform='telegram'",
            (project_id,),
        ).fetchall()
    assert len(rows) == 6
    statuses = {r["status"] for r in rows}
    assert statuses == {"proposed"}
    # goal заполнен из слота
    assert all(r["goal"] for r in rows)


def test_generate_plan_filters_out_of_period_dates(patch_env, monkeypatch):
    """Период короче недели → слотов меньше (только даты в окне)."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_active_strategy(db, project_id)

    from app.pipeline.planner import generate_plan
    # Окно 2 дня: 17–18 июня (среда-четверг недели якоря)
    count = generate_plan(project_id, "telegram", "2026-06-17", "2026-06-18")
    # post i=2,3 (дни floor(2*7/4)=3→18, floor(3*7/4)=5→20) ... считаем фактически
    with get_db() as db:
        rows = db.execute(
            "SELECT date FROM plan_items WHERE project_id=?", (project_id,)
        ).fetchall()
    assert count == len(rows)
    assert all("2026-06-17" <= r["date"] <= "2026-06-18" for r in rows)


def test_generate_plan_filters_disabled_content_types(patch_env, monkeypatch):
    """video выключен → только post-слоты (4/нед)."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(
            db,
            platforms=("telegram",),
            content_types={"telegram": {"post": True, "video": False}},
        )
        _create_active_strategy(db, project_id)

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "telegram", ANCHOR, "2026-06-21")
    # mix post=4, video=2 → video выключен → 4 post
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
                "activated_on": ANCHOR,
                "phases": [{
                    "n": 1, "weeks": 10 ** 6,
                    "goal_share": {"attract": 100},
                    "mix": {"telegram": {"post": 2}},
                }],
                "platforms": {
                    "telegram": {
                        "goals": "g",
                        "rubrics": [{"name": "r", "goal": "attract", "description": "d"}],
                        "best_times": ["09:00"],
                        "kpi": "k",
                    }
                },
            },
        )

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "youtube", ANCHOR, "2026-06-22")
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
    count = generate_plan(project_id, "telegram", ANCHOR, "2026-06-22")
    assert count == 0


def test_generate_plan_deduplication(patch_env):
    """Повторный вызов в том же окне → 0 новых (слоты дедуплены против plan_items)."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_active_strategy(db, project_id)

    from app.pipeline.planner import generate_plan

    count1 = generate_plan(project_id, "telegram", ANCHOR, "2026-06-21")
    assert count1 == 6

    # Повторный вызов — те же слоты (дата+тип+время заняты) → 0 новых
    count2 = generate_plan(project_id, "telegram", ANCHOR, "2026-06-21")
    assert count2 == 0


def test_generate_plan_dedup_different_platform_no_conflict(patch_env):
    """Слоты другой платформы независимы — дедуп не срабатывает между платформами."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(
            db,
            platforms=("telegram", "vk"),
        )
        _create_active_strategy(db, project_id)

    from app.pipeline.planner import generate_plan
    count_tg = generate_plan(project_id, "telegram", ANCHOR, "2026-06-21")
    assert count_tg == 6

    # vk mix = post:3 → 3 слота; независимы от telegram
    count_vk = generate_plan(project_id, "vk", ANCHOR, "2026-06-21")
    assert count_vk == 3


def test_generate_plan_uses_slot_fields_not_llm(patch_env, monkeypatch):
    """date/time/content_type берутся ИЗ СЛОТА, что бы LLM ни вернул в этих полях."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_active_strategy(db, project_id)

    import app.pipeline.planner as planner_mod

    # LLM возвращает мусорные date/content_type/time — планнер их игнорирует
    def fake_chat(messages, purpose=None, json_mode=False, **kw):
        items = [{
            "slot_id": i,
            "date": "2099-01-01",          # игнор
            "content_type": "short",        # игнор
            "time": "23:59",                # игнор
            "title": f"Тема {i}",
            "brief": {"hook": "h", "outline": "o", "cta": "",
                      "keywords": ["k"], "rubric": "Рубрика 1"},
        } for i in range(1, 7)]
        return json.dumps({"items": items}, ensure_ascii=False)

    monkeypatch.setattr(planner_mod, "chat", fake_chat)

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "telegram", ANCHOR, "2026-06-21")
    assert count == 6

    with get_db() as db:
        rows = db.execute(
            "SELECT date, content_type, time_slot FROM plan_items WHERE project_id=?",
            (project_id,),
        ).fetchall()
    # Ни одна дата/тип/время не из LLM-мусора
    assert all("2026-06-15" <= r["date"] <= "2026-06-21" for r in rows)
    assert all(r["content_type"] in ("post", "video") for r in rows)
    assert all(r["time_slot"] != "23:59" for r in rows)


def test_generate_plan_missing_slot_ids_placeholder(patch_env, monkeypatch):
    """LLM вернул половину slot_id → retry → недостающие = плейсхолдеры."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_active_strategy(db, project_id)

    import app.pipeline.planner as planner_mod

    # Всегда возвращаем только slot_id 1,2,3 (половину из 6) — и в основном, и в retry
    def fake_chat(messages, purpose=None, json_mode=False, **kw):
        items = [{
            "slot_id": i,
            "title": f"Тема {i}",
            "brief": {"hook": "h", "outline": "o", "cta": "",
                      "keywords": ["k"], "rubric": "Рубрика 1"},
        } for i in (1, 2, 3)]
        return json.dumps({"items": items}, ensure_ascii=False)

    monkeypatch.setattr(planner_mod, "chat", fake_chat)

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "telegram", ANCHOR, "2026-06-21")
    assert count == 6  # все 6 слотов вставлены (часть — плейсхолдеры)

    with get_db() as db:
        rows = db.execute(
            "SELECT title, brief FROM plan_items WHERE project_id=?", (project_id,)
        ).fetchall()
    placeholders = [r for r in rows if r["title"] == planner_mod._PLACEHOLDER_TITLE]
    assert placeholders, "должны быть плейсхолдеры по недостающим slot_id"
    assert all(r["brief"] is None for r in placeholders)


def test_generate_plan_retry_on_invalid_json(patch_env, monkeypatch):
    """Первый ответ — мусор; второй — валидный JSON → пункты созданы."""
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
        # retry: валидный план на все слоты
        return _fake_plan_json(60)

    monkeypatch.setattr(planner_mod, "chat", fake_chat)

    from app.pipeline.planner import generate_plan
    count = generate_plan(project_id, "telegram", ANCHOR, "2026-06-21")
    assert count == 6
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


# ─── 9.7 C1 — Параллельность недельных LLM-вызовов ──────────────────────────


class TestPlanConcurrency:
    """
    Тесты C1: параллельное наполнение недель плана.
    """

    def _fake_chat_by_slot(self, messages, purpose=None, json_mode=False, **kw):
        """
        Детерминированный фейк: возвращает слоты с заголовками по slot_id.
        Работает корректно для любого числа слотов — парсит SLOTS из промпта.
        """
        # Найдём slot_id из переданных сообщений (JSON в user-промпте)
        content = messages[-1]["content"] if messages else ""
        # Пытаемся найти числа после "slot_id"
        import re as _re
        slot_ids = [int(m) for m in _re.findall(r'"slot_id":\s*(\d+)', content)]
        if not slot_ids:
            slot_ids = list(range(1, 7))
        items = [
            {
                "slot_id": sid,
                "title": f"Тема-детерм-{sid}",
                "brief": {"hook": "h", "outline": "o", "cta": "", "keywords": ["k"], "rubric": "Рубрика 1"},
            }
            for sid in slot_ids
        ]
        return json.dumps({"items": items}, ensure_ascii=False)

    def test_parallel_same_result_as_sequential(self, patch_env, monkeypatch):
        """
        При PLAN_LLM_CONCURRENCY=4 и нескольких неделях результат вставки
        идентичен PLAN_LLM_CONCURRENCY=1 (число пунктов и набор title).
        """
        _setup_db()
        with get_db() as db:
            project_id_seq, _ = _create_project(db, platforms=("telegram",), slug=_slug())
            _create_active_strategy(db, project_id_seq)
            project_id_par, _ = _create_project(db, platforms=("telegram",), slug=_slug())
            _create_active_strategy(db, project_id_par)

        import app.pipeline.planner as planner_mod
        import app.config as cfg_mod

        monkeypatch.setattr(planner_mod, "chat", self._fake_chat_by_slot)

        # Последовательно (concurrency=1)
        monkeypatch.setattr(cfg_mod.settings, "PLAN_LLM_CONCURRENCY", 1)
        from app.pipeline.planner import generate_plan
        count_seq = generate_plan(project_id_seq, "telegram", ANCHOR, "2026-06-28")

        # Параллельно (concurrency=4)
        monkeypatch.setattr(cfg_mod.settings, "PLAN_LLM_CONCURRENCY", 4)
        count_par = generate_plan(project_id_par, "telegram", ANCHOR, "2026-06-28")

        assert count_seq > 0, "sequential должен вставить хотя бы 1 пункт"
        assert count_seq == count_par, (
            f"parallel ({count_par}) != sequential ({count_seq}): число пунктов должно совпадать"
        )

        with get_db() as db:
            titles_seq = {
                r["title"] for r in db.execute(
                    "SELECT title FROM plan_items WHERE project_id=?", (project_id_seq,)
                ).fetchall()
            }
            titles_par = {
                r["title"] for r in db.execute(
                    "SELECT title FROM plan_items WHERE project_id=?", (project_id_par,)
                ).fetchall()
            }

        assert titles_seq == titles_par, "Наборы заголовков sequential и parallel должны совпадать"

    def test_parallel_chat_called_once_per_week(self, patch_env, monkeypatch):
        """
        При PLAN_LLM_CONCURRENCY=4 и двух неделях chat вызывается ровно 2 раза
        (по одному на неделю), без лишних вызовов.
        Период 2026-06-15 – 2026-06-28 → 2 недели по 6 слотов.
        """
        _setup_db()
        with get_db() as db:
            project_id, _ = _create_project(db, platforms=("telegram",), slug=_slug())
            _create_active_strategy(db, project_id)

        import app.pipeline.planner as planner_mod
        import app.config as cfg_mod

        call_count = {"n": 0}

        def counting_chat(messages, purpose=None, json_mode=False, **kw):
            call_count["n"] += 1
            return self._fake_chat_by_slot(messages, purpose=purpose, json_mode=json_mode)

        monkeypatch.setattr(planner_mod, "chat", counting_chat)
        monkeypatch.setattr(cfg_mod.settings, "PLAN_LLM_CONCURRENCY", 4)

        from app.pipeline.planner import generate_plan
        count = generate_plan(project_id, "telegram", ANCHOR, "2026-06-28")

        # 2 недели → 2 первичных chat-вызова (без retry, т.к. все slot_id приходят)
        assert count == 12, f"Ожидалось 12 пунктов (6×2 недели), получено {count}"
        assert call_count["n"] == 2, (
            f"Ожидалось 2 chat-вызова (по 1 на неделю), получено {call_count['n']}"
        )

    def test_sequential_concurrency_1(self, patch_env, monkeypatch):
        """
        При PLAN_LLM_CONCURRENCY=1 план генерируется последовательно — результат корректный.
        """
        _setup_db()
        with get_db() as db:
            project_id, _ = _create_project(db, platforms=("telegram",), slug=_slug())
            _create_active_strategy(db, project_id)

        import app.pipeline.planner as planner_mod
        import app.config as cfg_mod

        monkeypatch.setattr(planner_mod, "chat", self._fake_chat_by_slot)
        monkeypatch.setattr(cfg_mod.settings, "PLAN_LLM_CONCURRENCY", 1)

        from app.pipeline.planner import generate_plan
        count = generate_plan(project_id, "telegram", ANCHOR, "2026-06-21")
        assert count == 6, f"Ожидалось 6 пунктов, получено {count}"


# ─── 9.7 C2 — Лимит дней за тик brain._fill_plan ────────────────────────────


class TestBrainTickMaxDays:
    """
    Тесты C2: _fill_plan ограничивает date_to через PLAN_TICK_MAX_DAYS.
    """

    def test_fill_plan_date_to_capped_by_tick_max(self, patch_env, monkeypatch):
        """
        _fill_plan с PLAN_TICK_MAX_DAYS=7 вызывает generate_plan с
        date_to = date_from + 7, а не полным горизонтом 30 дней.
        """
        _setup_db()
        settings_json = json.dumps({"plan_horizon_days": 30})
        with get_db() as db:
            project_id, _ = _create_project(
                db, platforms=("telegram",), stage="running",
                settings_json=settings_json, slug=_slug(),
            )
            _create_active_strategy(db, project_id)

        import app.services.brain as brain_mod
        import app.pipeline.planner as planner_mod
        import app.config as cfg_mod

        monkeypatch.setattr(cfg_mod.settings, "PLAN_TICK_MAX_DAYS", 7)

        plan_calls = []

        def fake_generate_plan(pid, platform, date_from, date_to):
            plan_calls.append({"date_from": date_from, "date_to": date_to})
            return 3

        monkeypatch.setattr(planner_mod, "generate_plan", fake_generate_plan)

        brain_mod.process_brain()

        assert len(plan_calls) == 1, "generate_plan должен быть вызван ровно 1 раз"
        call = plan_calls[0]
        from datetime import date as _d, timedelta
        expected_date_from = _d.today()
        expected_date_to = expected_date_from + timedelta(days=7)
        assert call["date_from"] == expected_date_from.isoformat()
        assert call["date_to"] == expected_date_to.isoformat(), (
            f"date_to должен быть ограничен тиком до {expected_date_to.isoformat()}, "
            f"получено {call['date_to']}"
        )

    def test_fill_plan_date_to_not_exceeds_horizon(self, patch_env, monkeypatch):
        """
        Если tick_max > horizon, date_to = target_date (не выходит за горизонт).
        """
        _setup_db()
        settings_json = json.dumps({"plan_horizon_days": 5})
        with get_db() as db:
            project_id, _ = _create_project(
                db, platforms=("telegram",), stage="running",
                settings_json=settings_json, slug=_slug(),
            )
            _create_active_strategy(db, project_id)

        import app.services.brain as brain_mod
        import app.pipeline.planner as planner_mod
        import app.config as cfg_mod

        monkeypatch.setattr(cfg_mod.settings, "PLAN_TICK_MAX_DAYS", 14)

        plan_calls = []

        def fake_generate_plan(pid, platform, date_from, date_to):
            plan_calls.append({"date_from": date_from, "date_to": date_to})
            return 3

        monkeypatch.setattr(planner_mod, "generate_plan", fake_generate_plan)

        brain_mod.process_brain()

        assert len(plan_calls) == 1
        call = plan_calls[0]
        from datetime import date as _d, timedelta
        expected_date_to = _d.today() + timedelta(days=5)
        assert call["date_to"] == expected_date_to.isoformat(), (
            f"date_to не должен превышать горизонт {expected_date_to.isoformat()}, "
            f"получено {call['date_to']}"
        )
