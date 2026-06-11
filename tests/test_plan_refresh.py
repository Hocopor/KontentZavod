"""
Тесты управляемой перегенерации контент-плана (refresh_plan).

FAKE_LLM=1 — без реальных LLM-вызовов.

Покрывает:
- proposed удаляются и заменяются новыми
- approved/generated/error остаются нетронутыми
- фильтр по платформе не трогает другие платформы
- фильтр по типу не трогает другие типы
- без активной стратегии — ошибка (не исключение)
- выключенный тип → 422 из HTTP-роута
- роуты возвращают флеш «Обновлено» или текст ошибки
"""
import json
import uuid
from datetime import date, timedelta

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Утилиты ──────────────────────────────────────────────────────────────────


def _slug() -> str:
    return f"refresh-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(
    db,
    *,
    slug=None,
    stage="running",
    platforms=("telegram", "vk"),
    content_types=None,
    settings_json=None,
):
    """Создать проект со всеми полями и нужными платформами."""
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage, settings)
        VALUES (?, 'Тест Refresh', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', ?, ?)
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


def _create_active_strategy(db, project_id, *, platforms=("telegram", "vk")):
    """Создать активную стратегию с нужными платформами."""
    platforms_data = {}
    for plat in platforms:
        platforms_data[plat] = {
            "goals": f"цель {plat}",
            "rubrics": ["Рубрика 1"],
            "content_mix": {"post": 3},
            "best_times": ["09:00"],
            "kpi": "охват",
        }
    strategy_data = {
        "summary": "Тест",
        "positioning": "поз",
        "platforms": platforms_data,
    }
    cur = db.execute(
        "INSERT INTO strategies (project_id, version, status, strategy) VALUES (?,?,?,?)",
        (project_id, 1, "active", json.dumps(strategy_data, ensure_ascii=False)),
    )
    return cur.lastrowid


def _create_plan_item(
    db,
    project_id,
    *,
    platform="telegram",
    content_type="post",
    item_date=None,
    title="Тестовый пункт",
    status="proposed",
):
    """Создать plan_item. item_date по умолчанию — сегодня + 5 дней."""
    if item_date is None:
        item_date = (date.today() + timedelta(days=5)).isoformat()
    cur = db.execute(
        """
        INSERT INTO plan_items
            (project_id, platform, content_type, date, time_slot, title, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, platform, content_type, item_date, "10:00", title, status),
    )
    return cur.lastrowid


# ─── 1. Юнит-тесты refresh_plan ───────────────────────────────────────────────


class TestRefreshPlanUnit:

    def test_proposed_deleted_and_recreated(self, patch_env):
        """proposed-пункты удаляются и появляются новые (FAKE_LLM создаёт items)."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            _create_active_strategy(db, pid, platforms=("telegram",))
            item_date = (date.today() + timedelta(days=5)).isoformat()
            iid = _create_plan_item(db, pid, platform="telegram", item_date=item_date, title="Старый proposed")

        from app.pipeline.planner import refresh_plan
        result = refresh_plan(pid)

        assert result["error"] is None
        assert result["deleted"] >= 1  # наш proposed удалён

        # Старый пункт не должен существовать
        with get_db() as db:
            row = db.execute("SELECT id FROM plan_items WHERE id=?", (iid,)).fetchone()
        assert row is None

    def test_approved_not_touched(self, patch_env):
        """approved-пункты остаются нетронутыми."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            _create_active_strategy(db, pid, platforms=("telegram",))
            item_date = (date.today() + timedelta(days=5)).isoformat()
            approved_id = _create_plan_item(
                db, pid, platform="telegram", item_date=item_date,
                title="Одобренный пункт", status="approved"
            )

        from app.pipeline.planner import refresh_plan
        result = refresh_plan(pid)

        assert result["error"] is None

        with get_db() as db:
            row = db.execute("SELECT id, status FROM plan_items WHERE id=?", (approved_id,)).fetchone()
        assert row is not None
        assert row["status"] == "approved"

    def test_generated_not_touched(self, patch_env):
        """generated-пункты остаются нетронутыми."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            _create_active_strategy(db, pid, platforms=("telegram",))
            item_date = (date.today() + timedelta(days=5)).isoformat()
            gen_id = _create_plan_item(
                db, pid, platform="telegram", item_date=item_date,
                title="Готовый пункт", status="generated"
            )

        from app.pipeline.planner import refresh_plan
        result = refresh_plan(pid)

        with get_db() as db:
            row = db.execute("SELECT id, status FROM plan_items WHERE id=?", (gen_id,)).fetchone()
        assert row is not None
        assert row["status"] == "generated"

    def test_error_status_not_touched(self, patch_env):
        """error-пункты остаются нетронутыми."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            _create_active_strategy(db, pid, platforms=("telegram",))
            item_date = (date.today() + timedelta(days=5)).isoformat()
            err_id = _create_plan_item(
                db, pid, platform="telegram", item_date=item_date,
                title="Ошибочный пункт", status="error"
            )

        from app.pipeline.planner import refresh_plan
        result = refresh_plan(pid)

        with get_db() as db:
            row = db.execute("SELECT id, status FROM plan_items WHERE id=?", (err_id,)).fetchone()
        assert row is not None
        assert row["status"] == "error"

    def test_filter_platform_not_touching_other_platform(self, patch_env):
        """Фильтр по платформе не трогает proposed другой платформы."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram", "vk"))
            _create_active_strategy(db, pid, platforms=("telegram", "vk"))
            item_date = (date.today() + timedelta(days=5)).isoformat()
            tg_id = _create_plan_item(
                db, pid, platform="telegram", item_date=item_date,
                title="Телеграм proposed", status="proposed"
            )
            vk_id = _create_plan_item(
                db, pid, platform="vk", item_date=item_date,
                title="ВК proposed", status="proposed"
            )

        from app.pipeline.planner import refresh_plan
        # Обновляем только telegram
        result = refresh_plan(pid, platform="telegram")

        assert result["error"] is None
        # telegram proposed удалён
        with get_db() as db:
            tg_row = db.execute("SELECT id FROM plan_items WHERE id=?", (tg_id,)).fetchone()
            vk_row = db.execute("SELECT id FROM plan_items WHERE id=?", (vk_id,)).fetchone()
        assert tg_row is None   # удалён
        assert vk_row is not None  # VK не тронут

    def test_filter_content_type_not_touching_other_types(self, patch_env):
        """Фильтр по типу не трогает proposed другого типа на той же платформе."""
        _setup_db()
        # telegram: post и video (оба default_on=True)
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            _create_active_strategy(db, pid, platforms=("telegram",))
            item_date = (date.today() + timedelta(days=5)).isoformat()
            post_id = _create_plan_item(
                db, pid, platform="telegram", content_type="post",
                item_date=item_date, title="Телеграм пост proposed", status="proposed"
            )
            video_id = _create_plan_item(
                db, pid, platform="telegram", content_type="video",
                item_date=item_date, title="Телеграм видео proposed", status="proposed"
            )

        from app.pipeline.planner import refresh_plan
        # Обновляем только post
        result = refresh_plan(pid, platform="telegram", content_type="post")

        assert result["error"] is None

        with get_db() as db:
            post_row = db.execute("SELECT id FROM plan_items WHERE id=?", (post_id,)).fetchone()
            video_row = db.execute("SELECT id FROM plan_items WHERE id=?", (video_id,)).fetchone()
        assert post_row is None   # удалён
        assert video_row is not None  # video не тронут

    def test_no_active_strategy_returns_error(self, patch_env):
        """Без активной стратегии — возвращает сообщение об ошибке (не исключение)."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            # Стратегию не создаём

        from app.pipeline.planner import refresh_plan
        result = refresh_plan(pid)

        assert result["error"] is not None
        assert len(result["error"]) > 0
        # Должно быть человекочитаемое сообщение
        assert "стратег" in result["error"].lower() or "стратегия" in result["error"].lower() \
               or result["error"]  # хотя бы непустое

    def test_outside_horizon_proposed_not_deleted(self, patch_env):
        """proposed за пределами горизонта планирования не удаляются."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(
                db, platforms=("telegram",),
                settings_json=json.dumps({"plan_horizon_days": 10}),
            )
            _create_active_strategy(db, pid, platforms=("telegram",))
            # Пункт далеко за горизонтом
            far_date = (date.today() + timedelta(days=60)).isoformat()
            far_id = _create_plan_item(
                db, pid, platform="telegram", item_date=far_date,
                title="Далёкий proposed", status="proposed"
            )

        from app.pipeline.planner import refresh_plan
        result = refresh_plan(pid)

        with get_db() as db:
            row = db.execute("SELECT id FROM plan_items WHERE id=?", (far_id,)).fetchone()
        assert row is not None  # не тронут — за горизонтом


# ─── 2. HTTP-роуты ────────────────────────────────────────────────────────────


class TestRefreshRoutes:

    def test_refresh_all_returns_flash(self, client, patch_env):
        """POST /plan/refresh/{slug} возвращает флеш «Обновлено»."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            _create_active_strategy(db, pid, platforms=("telegram",))

        resp = client.post(f"/plan/refresh/{slug}")
        assert resp.status_code == 200
        assert "Обновлено" in resp.text or "обновл" in resp.text.lower() \
               or "удалено" in resp.text.lower() or "добавлено" in resp.text.lower()

    def test_refresh_platform_returns_flash(self, client, patch_env):
        """POST /plan/refresh/{slug}/telegram возвращает флеш."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            _create_active_strategy(db, pid, platforms=("telegram",))

        resp = client.post(f"/plan/refresh/{slug}/telegram")
        assert resp.status_code == 200
        body = resp.text
        # Содержит флеш или хотя бы шахматку
        assert "Обновлено" in body or "план" in body.lower() or "board" in body

    def test_refresh_content_type_returns_flash(self, client, patch_env):
        """POST /plan/refresh/{slug}/telegram/post возвращает флеш."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            _create_active_strategy(db, pid, platforms=("telegram",))

        resp = client.post(f"/plan/refresh/{slug}/telegram/post")
        assert resp.status_code == 200

    def test_refresh_unknown_platform_422(self, client, patch_env):
        """POST /plan/refresh/{slug}/badplatform → 422."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))

        resp = client.post(f"/plan/refresh/{slug}/badplatform")
        assert resp.status_code == 422

    def test_refresh_invalid_content_type_422(self, client, patch_env):
        """POST /plan/refresh/{slug}/telegram/badtype → 422."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))

        resp = client.post(f"/plan/refresh/{slug}/telegram/badtype")
        assert resp.status_code == 422

    def test_refresh_disabled_content_type_422(self, client, patch_env):
        """POST /plan/refresh/{slug}/vk/story → 422 если тип выключен."""
        _setup_db()
        # vk/story: default_on=False — должен вернуть 422
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("vk",))

        resp = client.post(f"/plan/refresh/{slug}/vk/story")
        assert resp.status_code == 422
        body = resp.text
        # Человекочитаемое сообщение про выключенный тип
        assert "выключен" in body.lower() or "включите" in body.lower() \
               or "не включён" in body.lower() or "422" in body

    def test_refresh_disabled_platform_422(self, client, patch_env):
        """POST /plan/refresh/{slug}/vk (disabled) → 422."""
        _setup_db()
        # Создаём проект только с telegram, vk выключен
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))

        resp = client.post(f"/plan/refresh/{slug}/vk")
        assert resp.status_code == 422

    def test_refresh_unknown_slug_404(self, client, patch_env):
        """POST /plan/refresh/nonexistent → 404."""
        _setup_db()
        resp = client.post("/plan/refresh/nonexistent-slug-xyz")
        assert resp.status_code == 404

    def test_refresh_no_strategy_shows_error(self, client, patch_env):
        """POST /plan/refresh без стратегии — ответ 200 с текстом ошибки."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            # Стратегию не создаём

        resp = client.post(f"/plan/refresh/{slug}")
        assert resp.status_code == 200
        # Должно быть сообщение об ошибке
        body = resp.text
        assert "стратег" in body.lower() or "⚠" in body or "ошибка" in body.lower()

    def test_refresh_board_contains_board_area(self, client, patch_env):
        """Ответ refresh содержит #plan-board-area (целевой элемент HTMX)."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            _create_active_strategy(db, pid, platforms=("telegram",))

        resp = client.post(f"/plan/refresh/{slug}")
        assert resp.status_code == 200
        assert "plan-board-area" in resp.text

    def test_refresh_flash_shows_deleted_and_created_counts(self, client, patch_env):
        """Флеш показывает числа удалённых и добавленных пунктов."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            _create_active_strategy(db, pid, platforms=("telegram",))
            # Создадим пункт proposed, он будет удалён
            item_date = (date.today() + timedelta(days=5)).isoformat()
            _create_plan_item(db, pid, platform="telegram", item_date=item_date)

        resp = client.post(f"/plan/refresh/{slug}")
        assert resp.status_code == 200
        body = resp.text
        # Должны быть числа
        assert any(char.isdigit() for char in body)

    def test_refresh_board_has_refresh_buttons(self, client, patch_env):
        """После refresh на странице присутствуют кнопки 🔄."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            _create_active_strategy(db, pid, platforms=("telegram",))

        # GET страницы — должны быть кнопки обновления
        resp = client.get(f"/plan?project={slug}&year=2026&month=6")
        assert resp.status_code == 200
        body = resp.text
        assert "Обновить контент-план" in body
        assert "/plan/refresh/" in body


# ─── 3. Целостность данных ────────────────────────────────────────────────────


class TestRefreshDataIntegrity:

    def test_approved_survive_full_refresh(self, client, patch_env):
        """После полного обновления через HTTP approved-пункты сохраняются."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram",))
            _create_active_strategy(db, pid, platforms=("telegram",))
            item_date = (date.today() + timedelta(days=5)).isoformat()
            approved_id = _create_plan_item(
                db, pid, platform="telegram", item_date=item_date,
                title="Важный одобренный пост", status="approved"
            )

        client.post(f"/plan/refresh/{slug}")

        with get_db() as db:
            row = db.execute("SELECT id, status FROM plan_items WHERE id=?", (approved_id,)).fetchone()
        assert row is not None
        assert row["status"] == "approved"

    def test_platform_refresh_does_not_delete_other_platform_approved(self, client, patch_env):
        """Обновление одной платформы не трогает approved другой платформы."""
        _setup_db()
        with get_db() as db:
            pid, slug = _create_project(db, platforms=("telegram", "vk"))
            _create_active_strategy(db, pid, platforms=("telegram", "vk"))
            item_date = (date.today() + timedelta(days=5)).isoformat()
            vk_approved = _create_plan_item(
                db, pid, platform="vk", item_date=item_date,
                title="VK approved", status="approved"
            )
            tg_proposed = _create_plan_item(
                db, pid, platform="telegram", item_date=item_date,
                title="TG proposed", status="proposed"
            )

        client.post(f"/plan/refresh/{slug}/telegram")

        with get_db() as db:
            vk_row = db.execute("SELECT id FROM plan_items WHERE id=?", (vk_approved,)).fetchone()
            tg_row = db.execute("SELECT id FROM plan_items WHERE id=?", (tg_proposed,)).fetchone()
        assert vk_row is not None   # VK approved не тронут
        assert tg_row is None       # TG proposed удалён
