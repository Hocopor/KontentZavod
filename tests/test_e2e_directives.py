"""
E2E-тесты волны E этапа 8.2: директивы → слоты → план.

Сценарии:
  1. Директива per_week=7 перебивает mix=3 → ровно 7 пунктов в неделю.
  2. 7 публикаций на 7 РАЗНЫХ дней.
  3. Легаси-стратегия (без phases, плоский content_mix) не падает, генерирует план.
  4. revise через build_strategy создаёт директивы с parsed{per_week} и фазовую стратегию.
"""
import json
import uuid
from datetime import date, timedelta

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Вспомогательные ─────────────────────────────────────────────────────────


def _slug() -> str:
    return f"e2edir-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, *, platforms=("telegram",), slug=None):
    """Создаёт проект с включёнными площадками (все 5, enabled по platforms).
    Возвращает (project_id, slug).
    """
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage)
        VALUES (?, 'Тест', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', 'running')
        """,
        (s,),
    )
    project_id = cur.lastrowid
    for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
        enabled = 1 if p in platforms else 0
        db.execute(
            "INSERT INTO project_platforms (project_id, platform, enabled, mode, content_types) "
            "VALUES (?,?,?,?,?)",
            (project_id, p, enabled, "auto", None),
        )
    return project_id, s


def _create_strategy_row(db, project_id, *, version=1, status="active",
                          user_comment=None, strategy=None):
    """Создаёт строку стратегии. Возвращает strategy_id."""
    cur = db.execute(
        "INSERT INTO strategies (project_id, version, status, user_comment, strategy) "
        "VALUES (?,?,?,?,?)",
        (project_id, version, status, user_comment,
         json.dumps(strategy, ensure_ascii=False) if strategy else None),
    )
    return cur.lastrowid


def _prepare_directive_test(db):
    """
    Общая подготовка для тестов 1 и 2: проект telegram + активная стратегия
    с phases mix=3, директива per_week=7.
    Возвращает (project_id, ANCHOR, DATE_TO).
    """
    ANCHOR = (date.today() + timedelta(days=1)).isoformat()
    DATE_TO = (date.today() + timedelta(days=7)).isoformat()

    strategy_json = {
        "summary": "s",
        "positioning": "p",
        "activated_on": ANCHOR,
        "phases": [
            {
                "n": 1,
                "weeks": 4,
                "name": "Запуск",
                "objective": "набор аудитории",
                "goal_share": {"attract": 70, "retain": 25, "sell": 5},
                # НАРОЧНО 3 — директива должна перебить на 7
                "mix": {"telegram": {"post": 3}},
                "notes": "",
            }
        ],
        "platforms": {
            "telegram": {
                "goals": "g",
                "rubrics": [{"name": "Совет", "goal": "attract", "description": "d"}],
                "best_times": ["09:00", "18:00"],
                "kpi": "k",
            }
        },
    }

    project_id, _ = _create_project(db, platforms=("telegram",))
    _create_strategy_row(db, project_id, version=1, status="active", strategy=strategy_json)

    # Директива: ровно 7 telegram/post в неделю
    db.execute(
        "INSERT INTO directives (project_id, scope, text, parsed, status) VALUES (?,?,?,?,?)",
        (
            project_id,
            "plan",
            "По 7 постов в неделю в Telegram",
            json.dumps(
                {"platform": "telegram", "content_type": "post", "per_week": 7},
                ensure_ascii=False,
            ),
            "active",
        ),
    )
    return project_id, ANCHOR, DATE_TO


# ─── Тест 1: директива override mix 3→7, ровно 7 пунктов ─────────────────────


def test_directive_overrides_mix_to_seven(client):
    """
    Директива per_week=7 должна перебить mix=3 и породить ровно 7 telegram/post
    в окне недели. У всех 7 пунктов должна быть задана цель (goal IS NOT NULL).
    """
    _setup_db()
    with get_db() as db:
        project_id, ANCHOR, DATE_TO = _prepare_directive_test(db)

    from app.pipeline.planner import generate_plan
    generate_plan(project_id, "telegram", ANCHOR, DATE_TO)

    with get_db() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM plan_items "
            "WHERE project_id=? AND platform='telegram' AND content_type='post' "
            "AND date BETWEEN ? AND ?",
            (project_id, ANCHOR, DATE_TO),
        ).fetchone()[0]

        count_with_goal = db.execute(
            "SELECT COUNT(*) FROM plan_items "
            "WHERE project_id=? AND platform='telegram' AND content_type='post' "
            "AND date BETWEEN ? AND ? AND goal IS NOT NULL",
            (project_id, ANCHOR, DATE_TO),
        ).fetchone()[0]

    # Директива перебила mix 3→7 — это ключевое закрытие жалобы «говорю 7, делает 3»
    assert count == 7, (
        f"Ожидалось 7 пунктов (директива перебила mix=3), получено {count}"
    )
    assert count_with_goal == 7, (
        f"Ожидалось 7 пунктов с goal IS NOT NULL, получено {count_with_goal}"
    )


# ─── Тест 2: 7 публикаций на 7 разных дней ────────────────────────────────────


def test_seven_on_distinct_days(client):
    """
    7 публикаций telegram/post должны разлетаться по 7 РАЗНЫМ дням.
    """
    _setup_db()
    with get_db() as db:
        project_id, ANCHOR, DATE_TO = _prepare_directive_test(db)

    from app.pipeline.planner import generate_plan
    generate_plan(project_id, "telegram", ANCHOR, DATE_TO)

    with get_db() as db:
        rows = db.execute(
            "SELECT date FROM plan_items "
            "WHERE project_id=? AND platform='telegram' AND content_type='post' "
            "AND date BETWEEN ? AND ?",
            (project_id, ANCHOR, DATE_TO),
        ).fetchall()

    dates = [r["date"] for r in rows]
    unique_dates = set(dates)
    assert len(unique_dates) == 7, (
        f"Ожидалось 7 разных дат, получено {len(unique_dates)}: {sorted(unique_dates)}"
    )


# ─── Тест 3: легаси-стратегия не падает и даёт план ──────────────────────────


def test_legacy_strategy_generates_plan(client):
    """
    Легаси-стратегия (без phases, строковые рубрики, плоский content_mix post=4)
    не должна вызывать исключений и должна создавать пункты плана.

    Количество пунктов: легаси-слой берёт post=4 из content_mix. Якорь =
    created_at стратегии (today). Диапазон [today+1; today+7] перекрывает
    неделю 0 (today..today+6) и неделю 1 (today+7..today+13).
    Из недели 0: слоты today+1, today+3, today+5 (3 попадают в [today+1;today+7]).
    Из недели 1: слот today+7 (попадает в [today+1;today+7]).
    Итого 4 пункта — соответствует content_mix post=4.
    """
    _setup_db()
    ANCHOR = (date.today() + timedelta(days=1)).isoformat()
    DATE_TO = (date.today() + timedelta(days=7)).isoformat()

    legacy = {
        "summary": "легаси",
        "positioning": "p",
        "platforms": {
            "telegram": {
                "goals": "g",
                "rubrics": ["Старая рубрика"],
                "content_mix": {"post": 4},
                "best_times": ["09:00"],
                "kpi": "k",
            }
        },
    }

    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_strategy_row(db, project_id, version=1, status="active", strategy=legacy)

    from app.pipeline.planner import generate_plan
    # Не должно кидать исключение
    n = generate_plan(project_id, "telegram", ANCHOR, DATE_TO)

    with get_db() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM plan_items WHERE project_id=? AND content_type='post'",
            (project_id,),
        ).fetchone()[0]

    assert count >= 1, "Легаси-стратегия должна создать хотя бы один пункт"
    # Легаси-слой берёт количество из content_mix (post=4) → 4 пункта/нед;
    # якорь = today (created_at), диапазон today+1..today+7:
    #   неделя 0 → слоты today+1, today+3, today+5 (3 в диапазоне);
    #   неделя 1 → слот today+7 (1 в диапазоне);
    # итого 4.
    assert count == 4, (
        f"Легаси content_mix post=4 → ожидалось 4 пункта, получено {count}"
    )


# ─── Тест 4: revise создаёт директивы и фазовую стратегию ────────────────────


def test_revise_creates_directive_and_phase_strategy(client):
    """
    build_strategy с user_comment «по 7 постов в неделю, меньше продаж» (revise):
    (а) стратегия v2 становится active с ключом «phases» (≥1 фаза);
    (б) в directives появляется активная запись с parsed{per_week}.
    """
    import app.pipeline.prompts as pm
    pm._rules_cache = None

    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        _create_strategy_row(
            db, project_id, version=1, status="active",
            strategy={
                "summary": "v1",
                "platforms": {"telegram": {"content_mix": {"post": 2}}},
            },
        )
        v2 = _create_strategy_row(
            db, project_id, version=2, status="generating",
            user_comment="по 7 постов в неделю, меньше продаж",
        )

    from app.pipeline.strategy import build_strategy
    build_strategy(v2)

    with get_db() as db:
        row = db.execute(
            "SELECT * FROM strategies WHERE id=?", (v2,)
        ).fetchone()

        directive_rows = db.execute(
            "SELECT * FROM directives WHERE project_id=? AND status='active'",
            (project_id,),
        ).fetchall()

    # (а) v2 стала active и содержит phases
    assert row["status"] == "active", (
        f"Ожидался status='active', получено '{row['status']}'"
    )
    strategy = json.loads(row["strategy"])
    phases = strategy.get("phases")
    assert isinstance(phases, list) and len(phases) >= 1, (
        f"Ожидался список phases ≥1 фазы, получено: {phases!r}"
    )

    # (б) Есть активная директива с parsed{per_week}
    assert len(directive_rows) >= 1, (
        "После revise с user_comment ожидается ≥1 активная директива"
    )
    parsed_with_per_week = [
        r for r in directive_rows
        if r["parsed"] and "per_week" in json.loads(r["parsed"])
    ]
    assert len(parsed_with_per_week) >= 1, (
        "Ожидается ≥1 директива с ключом 'per_week' в parsed; "
        f"директивы: {[dict(r) for r in directive_rows]}"
    )
