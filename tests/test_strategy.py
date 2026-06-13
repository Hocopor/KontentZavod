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


# ─── 6. Полный сброс стратегии ────────────────────────────────────────────────


def _create_content_row(db, project_id, *, status="approved"):
    cur = db.execute(
        "INSERT INTO content (project_id, type, title, status) VALUES (?,?,?,?)",
        (project_id, "post", "Контент", status),
    )
    return cur.lastrowid


def _create_schedule_row(db, content_id, *, sched_status="planned"):
    from datetime import date, timedelta
    item_date = (date.today() + timedelta(days=5)).isoformat() + " 10:00:00"
    cur = db.execute(
        "INSERT INTO schedule (content_id, platform, planned_at, status) VALUES (?,?,?,?)",
        (content_id, "telegram", item_date, sched_status),
    )
    return cur.lastrowid


def _create_plan_item_row(db, project_id, *, status="proposed", content_id=None):
    from datetime import date, timedelta
    item_date = (date.today() + timedelta(days=5)).isoformat()
    cur = db.execute(
        """
        INSERT INTO plan_items
            (project_id, platform, content_type, date, time_slot, title, status, content_id)
        VALUES (?, 'telegram', 'post', ?, '10:00', 'Пункт', ?, ?)
        """,
        (project_id, item_date, status, content_id),
    )
    return cur.lastrowid


class TestStrategyReset:

    def test_reset_deletes_strategies_plan_and_content(self, client):
        """POST reset: стратегии, план и контент удалены; проект в draft."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(
                db, platforms=("telegram",), slug=f"reset-{uuid.uuid4().hex[:6]}"
            )
            # Изменяем stage на running
            db.execute("UPDATE projects SET stage='running' WHERE id=?", (project_id,))

            # Стратегии: v1 archived, v2 active
            _create_strategy_row(db, project_id, version=1, status="archived",
                                 strategy={"summary": "v1", "platforms": {}})
            _create_strategy_row(db, project_id, version=2, status="active",
                                 strategy={"summary": "v2", "platforms": {}})

            # plan_item без контента
            _create_plan_item_row(db, project_id, status="proposed")

            # plan_item с контентом и schedule
            cid = _create_content_row(db, project_id)
            _create_schedule_row(db, cid, sched_status="planned")
            _create_plan_item_row(db, project_id, status="approved", content_id=cid)

        resp = client.post(f"/projects/{slug}/strategy/reset", follow_redirects=False)
        assert resp.status_code == 303

        with get_db() as db:
            strategies = db.execute(
                "SELECT id FROM strategies WHERE project_id=?", (project_id,)
            ).fetchall()
            plan_items = db.execute(
                "SELECT id FROM plan_items WHERE project_id=?", (project_id,)
            ).fetchall()
            contents = db.execute(
                "SELECT id FROM content WHERE project_id=?", (project_id,)
            ).fetchall()
            schedules = db.execute(
                "SELECT s.id FROM schedule s JOIN content c ON c.id=s.content_id WHERE c.project_id=?",
                (project_id,),
            ).fetchall()
            proj = db.execute(
                "SELECT stage FROM projects WHERE id=?", (project_id,)
            ).fetchone()

        assert len(strategies) == 0, "Стратегии должны быть удалены"
        assert len(plan_items) == 0, "Пункты плана должны быть удалены"
        assert len(contents) == 0, "Контент должен быть удалён"
        assert len(schedules) == 0, "Schedule должен быть удалён"
        assert proj["stage"] == "draft", "Проект должен вернуться в draft"

    def test_reset_without_strategies_ok(self, client):
        """POST reset без стратегий → 303, без ошибок."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(
                db, platforms=("telegram",), slug=f"reset-{uuid.uuid4().hex[:6]}"
            )

        resp = client.post(f"/projects/{slug}/strategy/reset", follow_redirects=False)
        assert resp.status_code == 303

        with get_db() as db:
            proj = db.execute(
                "SELECT stage FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        assert proj["stage"] == "draft"

    def test_reset_unknown_slug_404(self, client):
        """POST reset для несуществующего проекта → 404."""
        _setup_db()
        resp = client.post("/projects/nonexistent-xyz/strategy/reset", follow_redirects=False)
        assert resp.status_code == 404


# ─── 7. Стратегия v2: фазы, директивы, проверяющий цикл (этап 8.2) ────────────


class TestStrategyV2:

    def test_active_has_phases_and_activated_on(self, client):
        """build_strategy v2: фазы, activated_on=сегодня, content_mix материализован из фазы 1."""
        from datetime import date
        import app.pipeline.prompts as prompts_mod
        prompts_mod._rules_cache = None
        _setup_db()
        with get_db() as db:
            project_id, _ = _create_project(db, platforms=("telegram", "vk"))
            sid = _create_strategy_row(db, project_id)

        from app.pipeline.strategy import build_strategy
        build_strategy(sid)

        with get_db() as db:
            row = db.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
        assert row["status"] == "active"
        strategy = json.loads(row["strategy"])

        # Фазы
        assert isinstance(strategy.get("phases"), list) and strategy["phases"]
        for i, ph in enumerate(strategy["phases"], 1):
            assert ph["n"] == i
            assert ph["weeks"] >= 1
            assert sum(ph["goal_share"].values()) == 100

        # activated_on = сегодня
        assert strategy.get("activated_on") == date.today().isoformat()

        # content_mix материализован из mix фазы 1 (только включённые типы)
        tg_mix = strategy["platforms"]["telegram"]["content_mix"]
        assert tg_mix == strategy["phases"][0]["mix"].get("telegram", {})
        assert tg_mix  # непустой

    def test_revise_inherits_activated_on(self, client):
        """revise: activated_on наследуется от прошлой версии (кампания продолжается)."""
        import app.pipeline.prompts as prompts_mod
        prompts_mod._rules_cache = None
        _setup_db()
        with get_db() as db:
            project_id, _ = _create_project(db, platforms=("telegram", "vk"))
            _create_strategy_row(
                db, project_id, version=1, status="active",
                strategy={"summary": "v1", "positioning": "p",
                          "activated_on": "2025-01-15",
                          "phases": [{"n": 1, "weeks": 4, "name": "ф",
                                      "goal_share": {"attract": 100},
                                      "mix": {"telegram": {"post": 2}}}],
                          "platforms": {"telegram": {"content_mix": {"post": 2}}}},
            )
            v2 = _create_strategy_row(
                db, project_id, version=2, status="generating",
                user_comment="Больше видео",
            )

        from app.pipeline.strategy import build_strategy
        build_strategy(v2)

        with get_db() as db:
            row = db.execute("SELECT * FROM strategies WHERE id=?", (v2,)).fetchone()
        strategy = json.loads(row["strategy"])
        assert strategy["activated_on"] == "2025-01-15"

    def test_user_comment_creates_directives(self, client):
        """user_comment при revise → строки в таблице directives (FAKE directives_parse)."""
        import app.pipeline.prompts as prompts_mod
        prompts_mod._rules_cache = None
        _setup_db()
        with get_db() as db:
            project_id, _ = _create_project(db, platforms=("telegram",))
            _create_strategy_row(
                db, project_id, version=1, status="active",
                strategy={"summary": "v1", "platforms": {"telegram": {"content_mix": {"post": 2}}}},
            )
            v2 = _create_strategy_row(
                db, project_id, version=2, status="generating",
                user_comment="7 постов в неделю, меньше продаж",
            )

        from app.pipeline.strategy import build_strategy
        build_strategy(v2)

        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM directives WHERE project_id=?", (project_id,)
            ).fetchall()
        assert len(rows) >= 1
        # FAKE содержит количественную директиву с parsed
        parsed_dirs = [r for r in rows if r["parsed"]]
        assert parsed_dirs
        p = json.loads(parsed_dirs[0]["parsed"])
        assert "per_week" in p

    def test_strategy_check_fail_triggers_regeneration(self, client, monkeypatch):
        """strategy_check ok=false → регенерация ≤2; неустранение → check_warnings."""
        import app.pipeline.prompts as prompts_mod
        prompts_mod._rules_cache = None
        _setup_db()
        with get_db() as db:
            project_id, _ = _create_project(db, platforms=("telegram",))
            sid = _create_strategy_row(db, project_id)

        import app.pipeline.strategy as strat_mod

        calls = {"strategy": 0, "check": 0}
        good_strategy = json.dumps({
            "summary": "s", "positioning": "p",
            "phases": [{"n": 1, "weeks": 2, "name": "ф",
                        "goal_share": {"attract": 80, "sell": 20},
                        "mix": {"telegram": {"post": 3}}, "notes": ""}],
            "platforms": {"telegram": {"goals": "g",
                          "rubrics": [{"name": "r", "goal": "attract", "description": "d"}],
                          "best_times": ["09:00"], "kpi": "k"}},
        }, ensure_ascii=False)

        def fake_chat(messages, purpose=None, json_mode=False, **kw):
            if purpose == "strategy_analysis":
                return "Записка."
            if purpose == "directives_parse":
                return json.dumps({"directives": []}, ensure_ascii=False)
            if purpose == "strategy":
                calls["strategy"] += 1
                return good_strategy
            if purpose == "strategy_check":
                calls["check"] += 1
                # всегда нарушение
                return json.dumps({"ok": False, "violations": ["нарушение X"]},
                                  ensure_ascii=False)
            return "FAKE"

        monkeypatch.setattr(strat_mod, "chat", fake_chat)
        strat_mod.build_strategy(sid)

        with get_db() as db:
            row = db.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
        assert row["status"] == "active"  # активируется несмотря на нарушения
        strategy = json.loads(row["strategy"])
        assert strategy.get("check_warnings") == ["нарушение X"]
        # 1 первичная + 2 регенерации = 3 вызова strategy
        assert calls["strategy"] == 3
        # 1 первичная проверка + 2 после регенераций = 3 проверки
        assert calls["check"] == 3

    def test_invalid_phases_synthesized(self, client, monkeypatch):
        """LLM вернул стратегию без phases → синтезируется одна фаза, не error."""
        import app.pipeline.prompts as prompts_mod
        prompts_mod._rules_cache = None
        _setup_db()
        with get_db() as db:
            project_id, _ = _create_project(db, platforms=("telegram",))
            sid = _create_strategy_row(db, project_id)

        import app.pipeline.strategy as strat_mod

        def fake_chat(messages, purpose=None, json_mode=False, **kw):
            if purpose == "strategy_analysis":
                return "Записка."
            if purpose == "directives_parse":
                return json.dumps({"directives": []}, ensure_ascii=False)
            if purpose == "strategy":
                # без phases, но с content_mix в platforms (легаси-форма)
                return json.dumps({
                    "summary": "s", "positioning": "p",
                    "platforms": {"telegram": {"goals": "g", "rubrics": ["r"],
                                  "content_mix": {"post": 4},
                                  "best_times": ["09:00"], "kpi": "k"}},
                }, ensure_ascii=False)
            if purpose == "strategy_check":
                return json.dumps({"ok": True, "violations": []}, ensure_ascii=False)
            return "FAKE"

        monkeypatch.setattr(strat_mod, "chat", fake_chat)
        strat_mod.build_strategy(sid)

        with get_db() as db:
            row = db.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
        assert row["status"] == "active"
        strategy = json.loads(row["strategy"])
        phases = strategy["phases"]
        assert len(phases) == 1
        assert phases[0]["name"] == "Постоянный режим"
        # mix синтезирован из content_mix
        assert phases[0]["mix"].get("telegram") == {"post": 4}

    def test_render_platforms_legacy_string_rubrics(self, client):
        """_render_platforms со строковыми (легаси) rubrics не падает."""
        from app.web.strategy import _render_platforms
        strategy = {"platforms": {"telegram": {
            "goals": "g", "rubrics": ["Рубрика А", "Рубрика Б"],
            "content_mix": {"post": 2}, "best_times": ["09:00"], "kpi": "k"}}}
        out = _render_platforms(strategy)
        assert out[0]["rubrics"][0]["name"] == "Рубрика А"
        assert out[0]["rubrics"][0]["goal"] == ""

    def test_legacy_strategy_page_200(self, client):
        """Страница стратегии с легаси-JSON (без phases, строковые rubrics) → 200."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram",))
            _create_strategy_row(
                db, project_id, version=1, status="active",
                strategy={"summary": "легаси", "positioning": "p",
                          "platforms": {"telegram": {
                              "goals": "g", "rubrics": ["Старая рубрика"],
                              "content_mix": {"post": 2},
                              "best_times": ["09:00"], "kpi": "k"}}},
            )
        resp = client.get(f"/projects/{slug}/strategy")
        assert resp.status_code == 200
        assert "Старая рубрика" in resp.text


# ─── 8. Директивы: управление статусом ────────────────────────────────────────


class TestStrategyDirectives:

    def test_directive_done(self, client):
        """POST done → 303; в БД status == 'done'."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram",))
            cur = db.execute(
                "INSERT INTO directives (project_id, scope, text, status) VALUES (?,?,?,?)",
                (project_id, "strategy", "7 постов в неделю", "active"),
            )
            did = cur.lastrowid

        resp = client.post(
            f"/projects/{slug}/strategy/directives/{did}/done",
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with get_db() as db:
            status = db.execute(
                "SELECT status FROM directives WHERE id=?", (did,)
            ).fetchone()["status"]
        assert status == "done"

    def test_directive_dismissed(self, client):
        """POST dismissed → 303; в БД status == 'dismissed'."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram",))
            cur = db.execute(
                "INSERT INTO directives (project_id, scope, text, status) VALUES (?,?,?,?)",
                (project_id, "strategy", "Меньше продаж", "active"),
            )
            did = cur.lastrowid

        resp = client.post(
            f"/projects/{slug}/strategy/directives/{did}/dismissed",
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with get_db() as db:
            status = db.execute(
                "SELECT status FROM directives WHERE id=?", (did,)
            ).fetchone()["status"]
        assert status == "dismissed"

    def test_unknown_action_422(self, client):
        """POST с неизвестным action → 422; статус директивы остался 'active'."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram",))
            cur = db.execute(
                "INSERT INTO directives (project_id, scope, text, status) VALUES (?,?,?,?)",
                (project_id, "strategy", "Тест", "active"),
            )
            did = cur.lastrowid

        resp = client.post(
            f"/projects/{slug}/strategy/directives/{did}/foobar",
            follow_redirects=False,
        )
        assert resp.status_code == 422

        with get_db() as db:
            status = db.execute(
                "SELECT status FROM directives WHERE id=?", (did,)
            ).fetchone()["status"]
        assert status == "active"

    def test_directive_not_found_404(self, client):
        """POST на несуществующий directive_id → 404."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/strategy/directives/999999/done",
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_directive_other_project_404(self, client):
        """Директива проекта A, запрос через slug проекта B → 404; директива не меняется."""
        _setup_db()
        with get_db() as db:
            project_id_a, slug_a = _create_project(db, platforms=("telegram",))
            project_id_b, slug_b = _create_project(db, platforms=("telegram",))
            # Директива принадлежит проекту A
            cur = db.execute(
                "INSERT INTO directives (project_id, scope, text, status) VALUES (?,?,?,?)",
                (project_id_a, "strategy", "Директива A", "active"),
            )
            did_a = cur.lastrowid

        # Запрос через slug проекта B
        resp = client.post(
            f"/projects/{slug_b}/strategy/directives/{did_a}/done",
            follow_redirects=False,
        )
        assert resp.status_code == 404

        # Директива проекта A не изменилась
        with get_db() as db:
            status = db.execute(
                "SELECT status FROM directives WHERE id=?", (did_a,)
            ).fetchone()["status"]
        assert status == "active"

    def test_unknown_slug_404(self, client):
        """POST на несуществующий slug → 404."""
        _setup_db()
        resp = client.post(
            "/projects/nonexistent-xyz-slug/strategy/directives/1/done",
            follow_redirects=False,
        )
        assert resp.status_code == 404


# ─── 9. Правка рубрик с целью (strategy_edit) ─────────────────────────────────


class TestStrategyEditRubricsGoal:

    def _post_edit(self, client, slug, rubrics_telegram):
        """Отправить форму редактирования стратегии с заданным rubrics_telegram."""
        return client.post(
            f"/projects/{slug}/strategy/edit",
            data={
                "summary": "Резюме",
                "positioning": "Позиционирование",
                "goals_telegram": "Цели",
                "rubrics_telegram": rubrics_telegram,
                "best_times_telegram": "09:00",
                "kpi_telegram": "KPI",
                "mix_telegram_post": "3",
                "mix_telegram_video": "1",
            },
            follow_redirects=False,
        )

    def _get_active_strategy_platforms(self, db, project_id):
        row = db.execute(
            "SELECT strategy FROM strategies WHERE project_id=? AND status='active' "
            "ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        assert row is not None, "Нет активной стратегии"
        return json.loads(row["strategy"])["platforms"]

    def test_edit_rubrics_with_goal(self, client):
        """Строка 'Кейс клиента | attract | Истории успеха' парсится в dict; голая строка остаётся строкой."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram",))
            sid = _create_strategy_row(db, project_id)
        from app.pipeline.strategy import build_strategy
        build_strategy(sid)

        resp = self._post_edit(
            client, slug,
            "Кейс клиента | attract | Истории успеха\nПростая рубрика",
        )
        assert resp.status_code == 303

        with get_db() as db:
            platforms = self._get_active_strategy_platforms(db, project_id)

        rubrics = platforms["telegram"]["rubrics"]
        assert rubrics[0] == {"name": "Кейс клиента", "goal": "attract", "description": "Истории успеха"}
        assert rubrics[1] == "Простая рубрика"

    def test_edit_rubric_invalid_goal_defaults_retain(self, client):
        """Невалидный goal ('wrongval') → заменяется на 'retain'."""
        _setup_db()
        with get_db() as db:
            project_id, slug = _create_project(db, platforms=("telegram",))
            sid = _create_strategy_row(db, project_id)
        from app.pipeline.strategy import build_strategy
        build_strategy(sid)

        resp = self._post_edit(
            client, slug,
            "Имя | wrongval | описание",
        )
        assert resp.status_code == 303

        with get_db() as db:
            platforms = self._get_active_strategy_platforms(db, project_id)

        rubrics = platforms["telegram"]["rubrics"]
        assert rubrics[0]["goal"] == "retain"
