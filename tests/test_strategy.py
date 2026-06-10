"""
Тесты пайплайна и веба маркетинговой стратегии (этап 7, волна B).

Все тесты на FAKE_LLM=1 (из conftest.patch_env). TestClient строго с
контекст-менеджером (через фикстуру client).
"""
import json
import uuid

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Вспомогательные ──────────────────────────────────────────────────────────


def _slug() -> str:
    return f"strat-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, *, platforms=("telegram", "vk"), content_types=None, slug=None):
    """Создаёт проект с включёнными площадками. content_types: {platform: {type: bool}}."""
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
        ct = None
        if content_types and p in content_types:
            ct = json.dumps(content_types[p], ensure_ascii=False)
        db.execute(
            "INSERT INTO project_platforms (project_id, platform, enabled, mode, content_types) "
            "VALUES (?,?,?,?,?)",
            (project_id, p, enabled, "auto", ct),
        )
    return project_id, s


def _create_strategy_row(db, project_id, *, version=1, status="generating",
                         user_comment=None, strategy=None):
    cur = db.execute(
        "INSERT INTO strategies (project_id, version, status, user_comment, strategy) "
        "VALUES (?,?,?,?,?)",
        (project_id, version, status, user_comment,
         json.dumps(strategy, ensure_ascii=False) if strategy else None),
    )
    return cur.lastrowid


# ─── 1. build_strategy: базовая генерация ─────────────────────────────────────


def test_build_strategy_generates_active(client):
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram", "vk"))
        sid = _create_strategy_row(db, project_id)

    from app.pipeline.strategy import build_strategy
    build_strategy(sid)

    with get_db() as db:
        row = db.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "active"
    assert row["inputs"]  # inputs сохранены
    inputs = json.loads(row["inputs"])
    assert inputs["profile"]["name"] == "Тест"
    assert set(inputs["platforms"].keys()) == {"telegram", "vk"}

    strategy = json.loads(row["strategy"])
    assert "summary" in strategy
    assert "platforms" in strategy


def test_build_strategy_filters_disabled_platforms(client):
    """Только telegram включён → в стратегии остаётся только telegram."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        sid = _create_strategy_row(db, project_id)

    from app.pipeline.strategy import build_strategy
    build_strategy(sid)

    with get_db() as db:
        row = db.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    strategy = json.loads(row["strategy"])
    assert set(strategy["platforms"].keys()) == {"telegram"}
    # vk из FAKE-стратегии отфильтрован
    assert "vk" not in strategy["platforms"]


def test_build_strategy_filters_disabled_types(client):
    """vk с выключенным video → content_mix не содержит video."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(
            db, platforms=("vk",),
            content_types={"vk": {"post": True, "video": False, "story": False}},
        )
        sid = _create_strategy_row(db, project_id)

    from app.pipeline.strategy import build_strategy
    build_strategy(sid)

    with get_db() as db:
        row = db.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    strategy = json.loads(row["strategy"])
    mix = strategy["platforms"]["vk"]["content_mix"]
    assert "post" in mix
    assert "video" not in mix  # выключенный тип отфильтрован


# ─── 2. revise-ветка ──────────────────────────────────────────────────────────


def test_revise_branch_archives_previous(client):
    """active v1 + новая generating v2 с user_comment → build → v2 active, v1 archived."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram", "vk"))
        v1 = _create_strategy_row(
            db, project_id, version=1, status="active",
            strategy={"summary": "старая", "positioning": "поз",
                      "platforms": {"telegram": {"content_mix": {"post": 2}}}},
        )
        v2 = _create_strategy_row(
            db, project_id, version=2, status="generating",
            user_comment="Больше видео",
        )

    from app.pipeline.strategy import build_strategy
    build_strategy(v2)

    with get_db() as db:
        r1 = db.execute("SELECT status FROM strategies WHERE id=?", (v1,)).fetchone()
        r2 = db.execute("SELECT * FROM strategies WHERE id=?", (v2,)).fetchone()
    assert r1["status"] == "archived"
    assert r2["status"] == "active"
    strategy = json.loads(r2["strategy"])
    # FAKE strategy_revise содержит changes_summary
    assert strategy.get("changes_summary")


# ─── 3. error-ветка ───────────────────────────────────────────────────────────


def test_error_branch_no_exception(client, monkeypatch):
    """chat → мусор дважды → status='error', error_text заполнен, исключение НЕ вылетает."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        sid = _create_strategy_row(db, project_id)

    import app.pipeline.strategy as strat_mod

    def fake_chat(messages, purpose=None, json_mode=False, **kw):
        if purpose == "strategy_analysis":
            return "Аналитическая записка (текст)."
        return "это не JSON вообще {{{ broken"

    monkeypatch.setattr(strat_mod, "chat", fake_chat)

    # не должно бросить
    strat_mod.build_strategy(sid)

    with get_db() as db:
        row = db.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "error"
    assert row["error_text"]


# ─── 4. retry-парсинг ─────────────────────────────────────────────────────────


def test_retry_parsing_first_garbage_second_valid(client, monkeypatch):
    """1-й ответ стратегии мусор, 2-й валидный → активная стратегия."""
    _setup_db()
    with get_db() as db:
        project_id, _ = _create_project(db, platforms=("telegram",))
        sid = _create_strategy_row(db, project_id)

    import app.pipeline.strategy as strat_mod

    calls = {"strategy": 0}

    def fake_chat(messages, purpose=None, json_mode=False, **kw):
        if purpose == "strategy_analysis":
            return "Записка."
        if purpose == "strategy":
            calls["strategy"] += 1
            if calls["strategy"] == 1:
                return "мусор не json"
            return json.dumps({
                "summary": "ок", "positioning": "поз",
                "platforms": {"telegram": {"goals": "g", "rubrics": ["r"],
                                           "content_mix": {"post": 3},
                                           "best_times": ["09:00"], "kpi": "k"}},
            }, ensure_ascii=False)
        return "FAKE"

    monkeypatch.setattr(strat_mod, "chat", fake_chat)
    strat_mod.build_strategy(sid)

    with get_db() as db:
        row = db.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "active"
    assert calls["strategy"] == 2  # был retry


# ─── 5. Веб ───────────────────────────────────────────────────────────────────


def test_web_active_page_shows_rubrics(client):
    _setup_db()
    with get_db() as db:
        project_id, slug = _create_project(db, platforms=("telegram",))
        sid = _create_strategy_row(db, project_id)
    from app.pipeline.strategy import build_strategy
    build_strategy(sid)

    resp = client.get(f"/projects/{slug}/strategy")
    assert resp.status_code == 200
    # активная стратегия с рубриками FAKE_STRATEGY (telegram)
    assert "Кейс клиента" in resp.text or "рубрик" in resp.text.lower()
    assert "Контент-микс" in resp.text


def test_web_generating_status_visible(client):
    _setup_db()
    with get_db() as db:
        project_id, slug = _create_project(db, platforms=("telegram",))
        _create_strategy_row(db, project_id, status="generating")

    resp = client.get(f"/projects/{slug}/strategy")
    assert resp.status_code == 200
    assert "генерир" in resp.text.lower()


def test_web_revise_creates_row(client):
    _setup_db()
    with get_db() as db:
        project_id, slug = _create_project(db, platforms=("telegram",))
        _create_strategy_row(db, project_id, version=1, status="active",
                             strategy={"summary": "s", "platforms": {}})

    resp = client.post(f"/projects/{slug}/strategy/revise",
                       data={"user_comment": "больше видео"}, follow_redirects=False)
    assert resp.status_code == 303

    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM strategies WHERE project_id=? ORDER BY version", (project_id,)
        ).fetchall()
    assert len(rows) == 2
    assert rows[1]["version"] == 2
    assert rows[1]["status"] == "generating"
    assert rows[1]["user_comment"] == "больше видео"


def test_web_edit_saves(client):
    _setup_db()
    with get_db() as db:
        project_id, slug = _create_project(db, platforms=("telegram",))
        sid = _create_strategy_row(db, project_id)
    from app.pipeline.strategy import build_strategy
    build_strategy(sid)

    resp = client.post(
        f"/projects/{slug}/strategy/edit",
        data={
            "summary": "Новое резюме",
            "positioning": "Новое позиционирование",
            "goals_telegram": "Новые цели",
            "rubrics_telegram": "Рубрика 1\nРубрика 2",
            "best_times_telegram": "08:00, 20:00",
            "kpi_telegram": "Новый KPI",
            "mix_telegram_post": "5",
            "mix_telegram_video": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with get_db() as db:
        row = db.execute(
            "SELECT * FROM strategies WHERE project_id=? AND status='active'", (project_id,)
        ).fetchone()
    strategy = json.loads(row["strategy"])
    assert strategy["summary"] == "Новое резюме"
    tg = strategy["platforms"]["telegram"]
    assert tg["goals"] == "Новые цели"
    assert tg["rubrics"] == ["Рубрика 1", "Рубрика 2"]
    assert tg["best_times"] == ["08:00", "20:00"]
    assert tg["content_mix"]["post"] == 5
    assert tg["content_mix"]["video"] == 1


def test_web_retry_after_error(client):
    _setup_db()
    with get_db() as db:
        project_id, slug = _create_project(db, platforms=("telegram",))
        _create_strategy_row(db, project_id, status="error")

    resp = client.post(f"/projects/{slug}/strategy/retry", follow_redirects=False)
    assert resp.status_code == 303

    with get_db() as db:
        row = db.execute(
            "SELECT * FROM strategies WHERE project_id=?", (project_id,)
        ).fetchone()
    assert row["status"] == "generating"
    assert row["error_text"] is None


def test_web_404_unknown_slug(client):
    _setup_db()
    resp = client.get("/projects/no-such-project/strategy")
    assert resp.status_code == 404
