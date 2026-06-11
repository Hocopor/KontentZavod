"""
Тесты детерминированной генерации слотов (этап 8.2, волна C).

build_slots строит слоты по активной стратегии: количество — из mix фаз,
цели — по goal_share, директивы — override поверх mix. LLM не участвует.

Все тесты на FAKE_LLM=1 (patch_env из conftest), но build_slots LLM не зовёт.
"""
import json
import uuid
from collections import Counter
from datetime import date

import pytest

import app.config as cfg_module
from app.db import get_db, init_db
from app.pipeline.slots import build_slots


# ─── Утилиты ──────────────────────────────────────────────────────────────────

# 2026-06-15 — понедельник (weekday()==0); используем как якорь.
ANCHOR = "2026-06-15"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _slug() -> str:
    return f"slots-{uuid.uuid4().hex[:8]}"


def _create_project(db, *, platforms=("telegram",), content_types=None):
    s = _slug()
    cur = db.execute(
        "INSERT INTO projects (slug, name, stage) VALUES (?, 'Тест', 'running')",
        (s,),
    )
    pid = cur.lastrowid
    for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
        enabled = 1 if p in platforms else 0
        ct = None
        if content_types and p in content_types:
            ct = json.dumps(content_types[p], ensure_ascii=False)
        db.execute(
            "INSERT INTO project_platforms "
            "(project_id, platform, enabled, mode, content_types) VALUES (?,?,?,?,?)",
            (pid, p, enabled, "auto", ct),
        )
    return pid


def _create_strategy(db, pid, *, strategy_json, created_at=None):
    if created_at is not None:
        cur = db.execute(
            "INSERT INTO strategies (project_id, version, status, strategy, created_at) "
            "VALUES (?,?,?,?,?)",
            (pid, 1, "active", json.dumps(strategy_json, ensure_ascii=False), created_at),
        )
    else:
        cur = db.execute(
            "INSERT INTO strategies (project_id, version, status, strategy) VALUES (?,?,?,?)",
            (pid, 1, "active", json.dumps(strategy_json, ensure_ascii=False)),
        )
    return cur.lastrowid


def _phase_strategy(mix_telegram, *, goal_share=None, best_times=("09:00",), phases=None):
    """Стратегия v2 с одной бессрочной фазой (или заданными phases)."""
    if goal_share is None:
        goal_share = {"attract": 100}
    if phases is None:
        phases = [{
            "n": 1, "weeks": 10 ** 6,
            "goal_share": goal_share,
            "mix": {"telegram": mix_telegram},
        }]
    return {
        "activated_on": ANCHOR,
        "phases": phases,
        "platforms": {
            "telegram": {"best_times": list(best_times), "rubrics": []},
        },
    }


# ─── Тесты количества/распределения ──────────────────────────────────────────


def test_post_7_per_week_one_per_day(patch_env):
    """mix post=7/нед → ровно 7 слотов/нед, каждый день недели по одному."""
    _setup_db()
    with get_db() as db:
        pid = _create_project(db)
        _create_strategy(db, pid, strategy_json=_phase_strategy({"post": 7}))
        slots = build_slots(db, pid, "telegram", ANCHOR, "2026-06-21")

    assert len(slots) == 7
    weekdays = sorted(date.fromisoformat(s["date"]).weekday() for s in slots)
    assert weekdays == [0, 1, 2, 3, 4, 5, 6]


def test_post_3_per_week_days_0_2_4(patch_env):
    """mix post=3 → дни недели 0, 2, 4."""
    _setup_db()
    with get_db() as db:
        pid = _create_project(db)
        _create_strategy(db, pid, strategy_json=_phase_strategy({"post": 3}))
        slots = build_slots(db, pid, "telegram", ANCHOR, "2026-06-21")

    assert len(slots) == 3
    weekdays = sorted(date.fromisoformat(s["date"]).weekday() for s in slots)
    assert weekdays == [0, 2, 4]


def test_post_10_per_week_times_spread(patch_env):
    """mix post=10 → 10 слотов (несколько дней по 2, время разведено +30мин)."""
    _setup_db()
    with get_db() as db:
        pid = _create_project(db)
        _create_strategy(db, pid, strategy_json=_phase_strategy({"post": 10}))
        slots = build_slots(db, pid, "telegram", ANCHOR, "2026-06-21")

    assert len(slots) == 10
    # На день с двумя слотами времена различаются (есть 09:30 — bump +30)
    by_day = Counter(s["date"] for s in slots)
    doubled = [d for d, c in by_day.items() if c == 2]
    assert doubled, "ожидались дни с двумя слотами"
    for d in doubled:
        times = sorted(s["time_slot"] for s in slots if s["date"] == d)
        assert len(set(times)) == 2, f"времена в один день должны отличаться: {times}"
    assert "09:30" in {s["time_slot"] for s in slots}


def test_phases_switch_by_week(patch_env):
    """1-я фаза weeks=1 post=7, 2-я post=2 → неделя1=7 слотов, неделя2=2."""
    _setup_db()
    phases = [
        {"n": 1, "weeks": 1, "goal_share": {"attract": 100}, "mix": {"telegram": {"post": 7}}},
        {"n": 2, "weeks": 1000, "goal_share": {"attract": 100}, "mix": {"telegram": {"post": 2}}},
    ]
    with get_db() as db:
        pid = _create_project(db)
        _create_strategy(db, pid, strategy_json=_phase_strategy(None, phases=phases))
        # Период — две недели якоря: 2026-06-15..2026-06-28
        slots = build_slots(db, pid, "telegram", ANCHOR, "2026-06-28")

    week1 = [s for s in slots if s["date"] <= "2026-06-21"]
    week2 = [s for s in slots if s["date"] >= "2026-06-22"]
    assert len(week1) == 7
    assert len(week2) == 2


def test_goal_share_distribution(patch_env):
    """goal_share {attract:70, retain:30} на 10 слотах → 7 attract + 3 retain."""
    _setup_db()
    with get_db() as db:
        pid = _create_project(db)
        _create_strategy(
            db, pid,
            strategy_json=_phase_strategy({"post": 10}, goal_share={"attract": 70, "retain": 30}),
        )
        slots = build_slots(db, pid, "telegram", ANCHOR, "2026-06-21")

    goals = Counter(s["goal"] for s in slots)
    assert goals["attract"] == 7
    assert goals["retain"] == 3


# ─── Директивы-override ───────────────────────────────────────────────────────


def test_directive_overrides_mix(patch_env):
    """Директива {telegram, post, per_week:5} перекрывает mix post=7 → 5 слотов."""
    _setup_db()
    with get_db() as db:
        pid = _create_project(db)
        _create_strategy(db, pid, strategy_json=_phase_strategy({"post": 7}))
        db.execute(
            "INSERT INTO directives (project_id, scope, text, parsed, status) "
            "VALUES (?,?,?,?,?)",
            (pid, "plan", "5 постов в неделю",
             json.dumps({"platform": "telegram", "content_type": "post", "per_week": 5}),
             "active"),
        )
        slots = build_slots(db, pid, "telegram", ANCHOR, "2026-06-21")

    posts = [s for s in slots if s["content_type"] == "post"]
    assert len(posts) == 5


# ─── Легаси-стратегия ─────────────────────────────────────────────────────────


def test_legacy_strategy_without_phases(patch_env):
    """Легаси (без phases, content_mix в platforms) → слоты, якорь = created_at."""
    _setup_db()
    legacy = {
        "summary": "s", "positioning": "p",
        "platforms": {
            "telegram": {
                "goals": "g", "rubrics": ["Рубрика 1"],
                "content_mix": {"post": 7},
                "best_times": ["09:00"], "kpi": "k",
            },
        },
    }
    with get_db() as db:
        pid = _create_project(db)
        # created_at = понедельник ANCHOR (без activated_on в JSON)
        _create_strategy(db, pid, strategy_json=legacy, created_at=f"{ANCHOR} 12:00:00")
        slots = build_slots(db, pid, "telegram", ANCHOR, "2026-06-21")

    assert len(slots) == 7
    weekdays = sorted(date.fromisoformat(s["date"]).weekday() for s in slots)
    assert weekdays == [0, 1, 2, 3, 4, 5, 6]


# ─── Дедупликация против существующих plan_items ──────────────────────────────


def test_dedup_against_existing_plan_item(patch_env):
    """Существующий plan_item на (дата,тип,время) → слот не создаётся."""
    _setup_db()
    with get_db() as db:
        pid = _create_project(db)
        sid = _create_strategy(db, pid, strategy_json=_phase_strategy({"post": 7}))
        # Занятая ячейка совпадает с первым слотом (15-е, post, 09:00)
        db.execute(
            "INSERT INTO plan_items (project_id, strategy_id, platform, content_type, "
            "date, time_slot, title, status) VALUES (?,?,?,?,?,?,?,?)",
            (pid, sid, "telegram", "post", "2026-06-15", "09:00", "Занято", "proposed"),
        )
        slots = build_slots(db, pid, "telegram", ANCHOR, "2026-06-21")

    # Слот на 15-е 09:00 не должен появиться
    clash = [s for s in slots if s["date"] == "2026-06-15" and s["time_slot"] == "09:00"]
    assert not clash
    assert len(slots) == 6


def test_only_content_type_filter(patch_env):
    """only_content_type оставляет только указанный тип."""
    _setup_db()
    with get_db() as db:
        pid = _create_project(db)
        _create_strategy(
            db, pid,
            strategy_json=_phase_strategy({"post": 5, "video": 3}),
        )
        slots = build_slots(db, pid, "telegram", ANCHOR, "2026-06-21",
                            only_content_type="post")

    assert all(s["content_type"] == "post" for s in slots)
    assert len(slots) == 5


def test_no_strategy_returns_empty(patch_env):
    """Нет активной стратегии → []."""
    _setup_db()
    with get_db() as db:
        pid = _create_project(db)
        slots = build_slots(db, pid, "telegram", ANCHOR, "2026-06-21")
    assert slots == []
