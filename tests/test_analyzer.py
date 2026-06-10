"""
Тесты еженедельного LLM-анализа (app/analytics/analyzer.py).

Патчим app.analytics.analyzer.chat, чтобы контролировать ответ LLM.
БД временная (conftest.patch_env), FAKE_LLM=1.
"""
import json
import uuid

import pytest

from app.db import get_db, init_db
from app.llm import LLMError
import app.config as cfg_module
import app.analytics.analyzer as analyzer


def _unique_slug() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def db_project(patch_env):
    """Создаёт активный проект, возвращает project_id."""
    init_db(cfg_module.settings.db_path_absolute)
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO projects (slug, name, description, audience, status) "
            "VALUES (?, 'Аналитик-проект', 'Описание', 'ЦА', 'active')",
            (_unique_slug(),),
        )
        return cur.lastrowid


def _add_publication(
    project_id: int,
    *,
    hook_type="вопрос",
    ab_variant="null",
    platform="telegram",
    views=1000,
    likes=50,
    comments=10,
    shares=5,
    watch_pct=0.0,
    status="published",
    date="2026-06-01",
):
    """Создаёт content + schedule + metrics (одна публикация с метрикой)."""
    features = json.dumps(
        {
            "hook_type": hook_type,
            "topic": "тема",
            "length": "short",
            "format": "text",
            "ab_variant": ab_variant,
        },
        ensure_ascii=False,
    )
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO content (project_id, type, title, features, status) "
            "VALUES (?, 'post', 'T', ?, 'approved')",
            (project_id, features),
        )
        content_id = cur.lastrowid
        cur = db.execute(
            "INSERT INTO schedule (content_id, platform, planned_at, status) "
            "VALUES (?, ?, '2026-06-01 10:00', ?)",
            (content_id, platform, status),
        )
        schedule_id = cur.lastrowid
        db.execute(
            "INSERT INTO metrics (schedule_id, date, views, likes, comments, shares, watch_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (schedule_id, date, views, likes, comments, shares, watch_pct),
        )
    return content_id, schedule_id


def _seed_publications(project_id: int, n: int):
    for i in range(n):
        _add_publication(project_id, views=1000 + i)


def _patch_chat(monkeypatch, response):
    """Патчит analyzer.chat фиксированной строкой-ответом; считает вызовы."""
    calls = {"n": 0}

    def mock_chat(messages, purpose=None, json_mode=False, **kwargs):
        calls["n"] += 1
        return response

    monkeypatch.setattr(analyzer, "chat", mock_chat)
    return calls


def _active_analyzer_learnings(project_id):
    with get_db() as db:
        return db.execute(
            "SELECT insight, weight FROM learnings "
            "WHERE project_id=? AND active=1 AND source='analyzer'",
            (project_id,),
        ).fetchall()


# ─── 1. Мало данных → skip, LLM не вызван ────────────────────────────────────


def test_too_few_publications_skips(db_project, monkeypatch):
    _seed_publications(db_project, 4)
    calls = _patch_chat(monkeypatch, "не важно")

    result = analyzer.analyze_project(db_project)

    assert result == {"skipped": "недостаточно данных"}
    assert calls["n"] == 0, "LLM не должен вызываться при нехватке данных"


# ─── 2. Достаточно данных → инсайты записаны, weight clamped ─────────────────


def test_enough_publications_writes_insights_clamped(db_project, monkeypatch):
    _seed_publications(db_project, 5)
    response = json.dumps(
        {
            "insights": [
                {"insight": "Хук-вопрос лучше факта", "weight": 5.0},   # clamp → 2.0
                {"insight": "Короткие посты заходят", "weight": 0.1},   # clamp → 0.5
            ],
            "deactivate": [],
        },
        ensure_ascii=False,
    )
    _patch_chat(monkeypatch, response)

    result = analyzer.analyze_project(db_project)
    assert result["insights_added"] == 2

    rows = {r["insight"]: r["weight"] for r in _active_analyzer_learnings(db_project)}
    assert rows["Хук-вопрос лучше факта"] == 2.0
    assert rows["Короткие посты заходят"] == 0.5


# ─── 3. Дедуп: повторный прогон не плодит дубли ──────────────────────────────


def test_dedup_no_duplicates(db_project, monkeypatch):
    _seed_publications(db_project, 5)
    response = json.dumps(
        {"insights": [{"insight": "Уникальный инсайт", "weight": 1.0}], "deactivate": []},
        ensure_ascii=False,
    )
    _patch_chat(monkeypatch, response)

    first = analyzer.analyze_project(db_project)
    second = analyzer.analyze_project(db_project)

    assert first["insights_added"] == 1
    assert second["insights_added"] == 0

    with get_db() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM learnings WHERE project_id=? AND insight='Уникальный инсайт'",
            (db_project,),
        ).fetchone()[0]
    assert count == 1


# ─── 4. deactivate выключает analyzer, но не reject ──────────────────────────


def test_deactivate_only_analyzer_not_reject(db_project, monkeypatch):
    _seed_publications(db_project, 5)
    shared_text = "Спорный инсайт"
    with get_db() as db:
        db.execute(
            "INSERT INTO learnings (project_id, insight, source, weight, active) "
            "VALUES (?, ?, 'analyzer', 1.0, 1)",
            (db_project, shared_text),
        )
        db.execute(
            "INSERT INTO learnings (project_id, insight, source, weight, active) "
            "VALUES (?, ?, 'reject', 1.0, 1)",
            (db_project, shared_text),
        )

    response = json.dumps(
        {"insights": [], "deactivate": [shared_text]}, ensure_ascii=False
    )
    _patch_chat(monkeypatch, response)

    result = analyzer.analyze_project(db_project)
    assert result["deactivated"] == 1

    with get_db() as db:
        analyzer_row = db.execute(
            "SELECT active FROM learnings WHERE project_id=? AND source='analyzer' AND insight=?",
            (db_project, shared_text),
        ).fetchone()
        reject_row = db.execute(
            "SELECT active FROM learnings WHERE project_id=? AND source='reject' AND insight=?",
            (db_project, shared_text),
        ).fetchone()

    assert analyzer_row["active"] == 0, "analyzer-инсайт должен быть деактивирован"
    assert reject_row["active"] == 1, "reject-инсайт трогать нельзя"


# ─── 5. Переполнение: >10 активных → деактивированы с наименьшим weight ──────


def test_overflow_deactivates_lowest_weight(db_project, monkeypatch):
    _seed_publications(db_project, 5)
    # 8 существующих активных analyzer-инсайтов с весами 1.0
    with get_db() as db:
        for i in range(8):
            db.execute(
                "INSERT INTO learnings (project_id, insight, source, weight, active) "
                "VALUES (?, ?, 'analyzer', 1.0, 1)",
                (db_project, f"Старый инсайт {i}"),
            )

    # LLM добавляет 5 новых с высоким весом → всего 13 → урезать до 10
    insights = [
        {"insight": f"Новый инсайт {i}", "weight": 2.0} for i in range(5)
    ]
    response = json.dumps({"insights": insights, "deactivate": []}, ensure_ascii=False)
    _patch_chat(monkeypatch, response)

    analyzer.analyze_project(db_project)

    active = _active_analyzer_learnings(db_project)
    assert len(active) == 10, "должно остаться ровно 10 активных"
    # Все 5 новых (вес 2.0) должны выжить
    active_texts = {r["insight"] for r in active}
    for i in range(5):
        assert f"Новый инсайт {i}" in active_texts
    # Часть старых (вес 1.0) деактивирована
    old_active = [t for t in active_texts if t.startswith("Старый")]
    assert len(old_active) == 5


# ─── 6. Невалидный JSON дважды → LLMError, learnings не изменились ────────────


def test_invalid_json_twice_raises(db_project, monkeypatch):
    _seed_publications(db_project, 5)
    calls = _patch_chat(monkeypatch, "это не json")

    with pytest.raises(LLMError):
        analyzer.analyze_project(db_project)

    assert calls["n"] == 2
    with get_db() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM learnings WHERE project_id=?", (db_project,)
        ).fetchone()[0]
    assert count == 0


# ─── 7. analyze_all: 2 проекта, у одного LLMError → второй обработан ──────────


def test_analyze_all_one_fails_other_ok(db_project, monkeypatch):
    # db_project — первый. Создаём второй.
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO projects (slug, name, description, audience, status) "
            "VALUES (?, 'P2', 'D', 'A', 'active')",
            (_unique_slug(),),
        )
        project2 = cur.lastrowid

    _seed_publications(db_project, 5)
    _seed_publications(project2, 5)

    good = json.dumps(
        {"insights": [{"insight": "Норм инсайт", "weight": 1.0}], "deactivate": []},
        ensure_ascii=False,
    )

    def mock_chat(messages, purpose=None, json_mode=False, **kwargs):
        # Первый проект (меньший id) — ломаем, второй — ок.
        prompt = messages[0]["content"]
        # Различаем проекты по тексту таблицы недостаточно — используем счётчик
        # вместо этого ломаем по содержимому: оба одинаковы, поэтому
        # завязываемся на порядок через mutable.
        raise_for = mock_chat.fail_project_prompt
        if raise_for in prompt:
            return "сломанный не-json"
        return good

    # Привяжем «плохой» проект к названию первого проекта в таблице публикаций:
    # таблицы одинаковы, поэтому различаем по project name в промпте.
    # Имя первого проекта — 'Аналитик-проект', второго — 'P2'.
    mock_chat.fail_project_prompt = "Аналитик-проект"
    monkeypatch.setattr(analyzer, "chat", mock_chat)

    result = analyzer.analyze_all()

    assert result["projects"] == 2
    # Второй проект обработан, инсайт добавлен
    assert result["insights_added"] == 1
    rows = _active_analyzer_learnings(project2)
    assert any(r["insight"] == "Норм инсайт" for r in rows)
    # Первый проект упал — у него инсайтов нет
    assert len(_active_analyzer_learnings(db_project)) == 0
