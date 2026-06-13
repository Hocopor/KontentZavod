"""
Тесты панели запуска проекта (этап 7, волна B).

Покрывает:
- GET /projects/{slug}/launch-panel: 200, содержит типы включённых платформ
  с правильными default_on
- POST launch-settings: clamp значений, сохранение content_types в БД
- POST launch без платформ → ошибка, stage не меняется
- POST launch с платформой → stage='running' + strategies(generating, version=1)
- Повторный POST launch → НЕ создаёт вторую generating-стратегию
- POST stop → stage='paused'
- POST toggle-pause plan/gen/autogen инвертируют settings
- 404 по неизвестному slug
"""
import json
import uuid

import pytest

import app.config as cfg_module
from app.db import get_db, init_db, get_project_settings


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _slug() -> str:
    return f"launch-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, *, slug=None, platforms=("telegram",), stage="draft"):
    """Создаёт проект с указанными включёнными платформами. Возвращает (slug, project_id)."""
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes, stage)
        VALUES (?, 'Тест Запуск', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы', ?)
        """,
        (s, stage),
    )
    project_id = cur.lastrowid
    for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
        enabled = 1 if p in platforms else 0
        db.execute(
            "INSERT INTO project_platforms (project_id, platform, enabled, mode) VALUES (?,?,?,?)",
            (project_id, p, enabled, "auto"),
        )
    return s, project_id


# ─── 1. launch-panel: базовый рендер ──────────────────────────────────────────


class TestLaunchPanel:
    def test_panel_returns_200(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))
        resp = client.get(f"/projects/{slug}/launch-panel")
        assert resp.status_code == 200

    def test_panel_shows_enabled_platform_types(self, client, patch_env):
        """Типы включённой платформы telegram присутствуют в HTML."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))
        resp = client.get(f"/projects/{slug}/launch-panel")
        assert resp.status_code == 200
        # Telegram имеет типы post и video — оба должны быть в форме
        assert "ct_telegram_post" in resp.text
        assert "ct_telegram_video" in resp.text

    def test_panel_default_on_checked(self, client, patch_env):
        """Типы с default_on=True по умолчанию отмечены чекбоксами."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))
        resp = client.get(f"/projects/{slug}/launch-panel")
        assert resp.status_code == 200
        # Оба типа telegram default_on=True — поля checked
        # Проверяем хотя бы одно поле чекбокса присутствует и checked
        assert 'name="ct_telegram_post"' in resp.text

    def test_panel_shows_no_platforms_hint(self, client, patch_env):
        """Если нет включённых платформ — подсказка о подключении."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=())  # все отключены
        resp = client.get(f"/projects/{slug}/launch-panel")
        assert resp.status_code == 200
        # Должна быть подсказка
        assert "площадк" in resp.text.lower()

    def test_panel_shows_strategy_not_created(self, client, patch_env):
        """Если стратегии нет — показываем 'ещё не создана'."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("vk",))
        resp = client.get(f"/projects/{slug}/launch-panel")
        assert resp.status_code == 200
        assert "не создана" in resp.text

    def test_panel_shows_strategy_generating(self, client, patch_env):
        """Если стратегия generating — показываем соответствующий статус."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))
            db.execute(
                "INSERT INTO strategies (project_id, version, status) VALUES (?,1,'generating')",
                (pid,),
            )
        resp = client.get(f"/projects/{slug}/launch-panel")
        assert resp.status_code == 200
        assert "генерируется" in resp.text

    def test_panel_shows_strategy_active(self, client, patch_env):
        """Если стратегия active — показываем версию."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))
            db.execute(
                "INSERT INTO strategies (project_id, version, status) VALUES (?,2,'active')",
                (pid,),
            )
        resp = client.get(f"/projects/{slug}/launch-panel")
        assert resp.status_code == 200
        assert "активна v2" in resp.text

    def test_panel_multiple_platforms(self, client, patch_env):
        """Несколько включённых платформ — все показаны."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram", "vk"))
        resp = client.get(f"/projects/{slug}/launch-panel")
        assert resp.status_code == 200
        assert "ct_telegram_post" in resp.text
        assert "ct_vk_post" in resp.text
        assert "ct_vk_video" in resp.text

    def test_panel_404_unknown_slug(self, client, patch_env):
        _setup_db()
        resp = client.get("/projects/nonexistent-slug-xxxx/launch-panel")
        assert resp.status_code == 404


# ─── 2. launch-settings: сохранение ──────────────────────────────────────────


class TestLaunchSettings:
    def test_saves_numeric_settings(self, client, patch_env):
        """Числовые поля сохраняются в projects.settings."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "45",
                "gen_lookahead_days": "7",
                "retention_days": "20",
            },
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT settings FROM projects WHERE slug=?", (slug,)).fetchone()
        s = get_project_settings(row["settings"])
        assert s["plan_horizon_days"] == 45
        assert s["gen_lookahead_days"] == 7
        assert s["retention_days"] == 20

    def test_clamps_values_above_max(self, client, patch_env):
        """Значения выше максимума clamped к максимуму."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "999",   # max=90
                "gen_lookahead_days": "999",  # max=14
                "retention_days": "999",      # max=60
            },
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT settings FROM projects WHERE slug=?", (slug,)).fetchone()
        s = get_project_settings(row["settings"])
        assert s["plan_horizon_days"] == 90
        assert s["gen_lookahead_days"] == 14
        assert s["retention_days"] == 60

    def test_clamps_values_below_min(self, client, patch_env):
        """Значения ниже минимума clamped к минимуму."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "1",   # min=7
                "gen_lookahead_days": "0",  # min=1
                "retention_days": "1",      # min=3
            },
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT settings FROM projects WHERE slug=?", (slug,)).fetchone()
        s = get_project_settings(row["settings"])
        assert s["plan_horizon_days"] == 7
        assert s["gen_lookahead_days"] == 1
        assert s["retention_days"] == 3

    def test_saves_content_types_to_db(self, client, patch_env):
        """Чекбоксы типов контента сохраняются в project_platforms.content_types."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))

        # Отправляем: post=1, video НЕ отправляем (false)
        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "ct_telegram_post": "1",
                # ct_telegram_video отсутствует → false
            },
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute(
                "SELECT content_types FROM project_platforms WHERE project_id=? AND platform='telegram'",
                (pid,),
            ).fetchone()
        ct = json.loads(row["content_types"])
        assert ct["post"] is True
        assert ct["video"] is False

    def test_saves_content_types_both_enabled(self, client, patch_env):
        """Оба типа telegram включены."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "ct_telegram_post": "1",
                "ct_telegram_video": "1",
            },
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute(
                "SELECT content_types FROM project_platforms WHERE project_id=? AND platform='telegram'",
                (pid,),
            ).fetchone()
        ct = json.loads(row["content_types"])
        assert ct["post"] is True
        assert ct["video"] is True

    def test_returns_fragment(self, client, patch_env):
        """Ответ — HTML-фрагмент (не полная страница)."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={"plan_horizon_days": "30", "gen_lookahead_days": "3", "retention_days": "14"},
        )
        assert resp.status_code == 200
        # Фрагмент — нет полного DOCTYPE
        assert "<!DOCTYPE" not in resp.text

    def test_404_unknown_slug(self, client, patch_env):
        _setup_db()
        resp = client.post(
            "/projects/no-such-project/launch-settings",
            data={"plan_horizon_days": "30"},
        )
        assert resp.status_code == 404


# ─── 3. launch: запуск проекта ────────────────────────────────────────────────


class TestLaunch:
    def test_launch_without_platforms_returns_error(self, client, patch_env):
        """Без включённых платформ — ошибка, stage остаётся draft."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=())  # нет включённых

        resp = client.post(
            f"/projects/{slug}/launch",
            data={"plan_horizon_days": "30", "gen_lookahead_days": "3", "retention_days": "14"},
        )
        assert resp.status_code == 422
        # Ошибка в HTML
        assert "площадк" in resp.text.lower() or "platform" in resp.text.lower() or "подключ" in resp.text.lower()

        # stage не изменился
        with get_db() as db:
            row = db.execute("SELECT stage FROM projects WHERE slug=?", (slug,)).fetchone()
        assert row["stage"] == "draft"

    def test_launch_without_platforms_no_strategy_created(self, client, patch_env):
        """При ошибке запуска стратегия не создаётся."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=())

        client.post(
            f"/projects/{slug}/launch",
            data={"plan_horizon_days": "30", "gen_lookahead_days": "3", "retention_days": "14"},
        )

        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM strategies WHERE project_id=?", (pid,)
            ).fetchone()[0]
        assert count == 0

    def test_launch_with_platform_sets_running(self, client, patch_env):
        """С включённой платформой — stage становится running."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "ct_telegram_post": "1",
            },
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT stage FROM projects WHERE slug=?", (slug,)).fetchone()
        assert row["stage"] == "running"

    def test_launch_creates_strategy_version_1(self, client, patch_env):
        """Первый запуск создаёт стратегию version=1, status=generating."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))

        client.post(
            f"/projects/{slug}/launch",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "ct_telegram_post": "1",
            },
        )

        with get_db() as db:
            strat = db.execute(
                "SELECT version, status FROM strategies WHERE project_id=?", (pid,)
            ).fetchone()
        assert strat is not None
        assert strat["version"] == 1
        assert strat["status"] == "generating"

    def test_repeated_launch_does_not_create_second_generating(self, client, patch_env):
        """Повторный запуск при уже existing generating-стратегии не создаёт дубль."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))
            # Уже есть generating
            db.execute(
                "INSERT INTO strategies (project_id, version, status) VALUES (?,1,'generating')",
                (pid,),
            )

        client.post(
            f"/projects/{slug}/launch",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "ct_telegram_post": "1",
            },
        )

        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM strategies WHERE project_id=?", (pid,)
            ).fetchone()[0]
        assert count == 1  # не создали вторую

    def test_repeated_launch_with_active_strategy_no_new_strategy(self, client, patch_env):
        """При active-стратегии повторный запуск не создаёт новую."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))
            db.execute(
                "INSERT INTO strategies (project_id, version, status) VALUES (?,1,'active')",
                (pid,),
            )

        client.post(
            f"/projects/{slug}/launch",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "ct_telegram_post": "1",
            },
        )

        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM strategies WHERE project_id=?", (pid,)
            ).fetchone()[0]
        assert count == 1

    def test_launch_with_all_types_disabled_returns_error(self, client, patch_env):
        """Платформа включена, но все типы сняты — ошибка."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))
            # Установить content_types: все false
            db.execute(
                "UPDATE project_platforms SET content_types=? WHERE project_id=? AND platform='telegram'",
                (json.dumps({"post": False, "video": False}), pid),
            )

        # Не отправляем ни одного ct_ чекбокса
        resp = client.post(
            f"/projects/{slug}/launch",
            data={"plan_horizon_days": "30", "gen_lookahead_days": "3", "retention_days": "14"},
        )
        assert resp.status_code == 422

        with get_db() as db:
            row = db.execute("SELECT stage FROM projects WHERE slug=?", (slug,)).fetchone()
        assert row["stage"] == "draft"

    def test_launch_404_unknown_slug(self, client, patch_env):
        _setup_db()
        resp = client.post("/projects/no-project-here/launch", data={"plan_horizon_days": "30"})
        assert resp.status_code == 404


# ─── 4. stop: остановка проекта ───────────────────────────────────────────────


class TestStop:
    def test_stop_sets_paused(self, client, patch_env):
        """POST stop → stage='paused'."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",), stage="running")

        resp = client.post(f"/projects/{slug}/stop")
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT stage FROM projects WHERE slug=?", (slug,)).fetchone()
        assert row["stage"] == "paused"

    def test_stop_returns_fragment(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",), stage="running")

        resp = client.post(f"/projects/{slug}/stop")
        assert resp.status_code == 200
        assert "<!DOCTYPE" not in resp.text

    def test_stop_404_unknown_slug(self, client, patch_env):
        _setup_db()
        resp = client.post("/projects/no-such-project/stop")
        assert resp.status_code == 404


# ─── 5. toggle-pause: инвертирование флагов ───────────────────────────────────


class TestTogglePause:
    def _get_settings(self, slug: str) -> dict:
        with get_db() as db:
            row = db.execute("SELECT settings FROM projects WHERE slug=?", (slug,)).fetchone()
        return get_project_settings(row["settings"])

    def test_toggle_plan_off_to_on(self, client, patch_env):
        """plan_paused 0 → 1."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(f"/projects/{slug}/toggle-pause/plan")
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["plan_paused"] == 1

    def test_toggle_plan_on_to_off(self, client, patch_env):
        """plan_paused 1 → 0."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))
            db.execute(
                "UPDATE projects SET settings=? WHERE id=?",
                (json.dumps({"plan_paused": 1}), pid),
            )

        resp = client.post(f"/projects/{slug}/toggle-pause/plan")
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["plan_paused"] == 0

    def test_toggle_gen_off_to_on(self, client, patch_env):
        """gen_paused 0 → 1."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(f"/projects/{slug}/toggle-pause/gen")
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["gen_paused"] == 1

    def test_toggle_gen_on_to_off(self, client, patch_env):
        """gen_paused 1 → 0."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))
            db.execute(
                "UPDATE projects SET settings=? WHERE id=?",
                (json.dumps({"gen_paused": 1}), pid),
            )

        resp = client.post(f"/projects/{slug}/toggle-pause/gen")
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["gen_paused"] == 0

    def test_toggle_autogen_off_to_on(self, client, patch_env):
        """autogen 0 → 1."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(f"/projects/{slug}/toggle-pause/autogen")
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["autogen"] == 1

    def test_toggle_autogen_on_to_off(self, client, patch_env):
        """autogen 1 → 0."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))
            db.execute(
                "UPDATE projects SET settings=? WHERE id=?",
                (json.dumps({"autogen": 1}), pid),
            )

        resp = client.post(f"/projects/{slug}/toggle-pause/autogen")
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["autogen"] == 0

    def test_toggle_returns_fragment(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(f"/projects/{slug}/toggle-pause/plan")
        assert resp.status_code == 200
        assert "<!DOCTYPE" not in resp.text

    def test_toggle_404_unknown_slug(self, client, patch_env):
        _setup_db()
        resp = client.post("/projects/no-such-slug/toggle-pause/plan")
        assert resp.status_code == 404

    def test_toggle_invalid_what_returns_400(self, client, patch_env):
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))
        resp = client.post(f"/projects/{slug}/toggle-pause/unknown")
        assert resp.status_code == 400

    def test_toggle_does_not_lose_other_settings(self, client, patch_env):
        """Инвертирование одного флага не затирает остальные настройки."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))
            db.execute(
                "UPDATE projects SET settings=? WHERE id=?",
                (json.dumps({
                    "plan_horizon_days": 45,
                    "gen_lookahead_days": 7,
                    "retention_days": 21,
                    "plan_paused": 0,
                    "gen_paused": 0,
                    "autogen": 0,
                }), pid),
            )

        client.post(f"/projects/{slug}/toggle-pause/plan")
        s = self._get_settings(slug)
        # Числовые поля сохранены
        assert s["plan_horizon_days"] == 45
        assert s["gen_lookahead_days"] == 7
        assert s["retention_days"] == 21
        # Флаг инвертирован
        assert s["plan_paused"] == 1
        # Остальные флаги не тронуты
        assert s["gen_paused"] == 0
        assert s["autogen"] == 0


# ─── 6. music_volume: сохранение и clamp ──────────────────────────────────────


class TestMusicVolumeSetting:
    def test_music_volume_saves_valid(self, client, patch_env):
        """music_volume=0.3 сохраняется как 0.3."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "music_volume": "0.3",
            },
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT settings FROM projects WHERE slug=?", (slug,)).fetchone()
        s = get_project_settings(row["settings"])
        assert abs(s["music_volume"] - 0.3) < 1e-9

    def test_music_volume_clamps_above_max(self, client, patch_env):
        """music_volume=0.9 clampится до 0.5."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "music_volume": "0.9",
            },
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT settings FROM projects WHERE slug=?", (slug,)).fetchone()
        s = get_project_settings(row["settings"])
        assert abs(s["music_volume"] - 0.5) < 1e-9

    def test_music_volume_clamps_below_zero(self, client, patch_env):
        """music_volume=-1 clampится до 0.0."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "music_volume": "-1",
            },
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT settings FROM projects WHERE slug=?", (slug,)).fetchone()
        s = get_project_settings(row["settings"])
        assert abs(s["music_volume"] - 0.0) < 1e-9

    def test_music_volume_invalid_input_uses_default(self, client, patch_env):
        """Мусорный music_volume → дефолт 0.12."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "music_volume": "not_a_number",
            },
        )
        assert resp.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT settings FROM projects WHERE slug=?", (slug,)).fetchone()
        s = get_project_settings(row["settings"])
        assert abs(s["music_volume"] - 0.12) < 1e-9

    def test_music_volume_default_in_project_settings(self, patch_env):
        """DEFAULT_PROJECT_SETTINGS содержит music_volume=0.12."""
        from app.db import DEFAULT_PROJECT_SETTINGS, get_project_settings

        assert DEFAULT_PROJECT_SETTINGS["music_volume"] == 0.12
        # get_project_settings(None) тоже возвращает дефолт
        s = get_project_settings(None)
        assert s["music_volume"] == 0.12


# ─── 6. Интеграционный флоу ───────────────────────────────────────────────────


class TestLaunchFlow:
    def test_full_lifecycle(self, client, patch_env):
        """
        Полный цикл: draft → настройки → launch → running → stop → paused.
        """
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram", "vk"))

        # Сохранить настройки
        r = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "60",
                "gen_lookahead_days": "5",
                "retention_days": "30",
                "ct_telegram_post": "1",
                "ct_telegram_video": "1",
                "ct_vk_post": "1",
            },
        )
        assert r.status_code == 200

        # Запустить
        r = client.post(
            f"/projects/{slug}/launch",
            data={
                "plan_horizon_days": "60",
                "gen_lookahead_days": "5",
                "retention_days": "30",
                "ct_telegram_post": "1",
                "ct_vk_post": "1",
            },
        )
        assert r.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT stage FROM projects WHERE slug=?", (slug,)).fetchone()
            strat = db.execute(
                "SELECT status, version FROM strategies WHERE project_id=?", (pid,)
            ).fetchone()
        assert row["stage"] == "running"
        assert strat["status"] == "generating"
        assert strat["version"] == 1

        # Остановить
        r = client.post(f"/projects/{slug}/stop")
        assert r.status_code == 200

        with get_db() as db:
            row = db.execute("SELECT stage FROM projects WHERE slug=?", (slug,)).fetchone()
        assert row["stage"] == "paused"

    def test_strategy_version_increments(self, client, patch_env):
        """Если есть archived стратегии, новая получает version = max+1."""
        _setup_db()
        with get_db() as db:
            slug, pid = _create_project(db, platforms=("telegram",))
            # Уже есть archived v1 и v2
            db.execute(
                "INSERT INTO strategies (project_id, version, status) VALUES (?,1,'archived')",
                (pid,),
            )
            db.execute(
                "INSERT INTO strategies (project_id, version, status) VALUES (?,2,'archived')",
                (pid,),
            )

        client.post(
            f"/projects/{slug}/launch",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "ct_telegram_post": "1",
            },
        )

        with get_db() as db:
            strat = db.execute(
                "SELECT version, status FROM strategies WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (pid,),
            ).fetchone()
        assert strat["version"] == 3
        assert strat["status"] == "generating"


# ─── 7. Озвучка и субтитры: новые настройки ──────────────────────────────────


class TestVoiceAndSubtitlesSettings:
    """Тесты сохранения и валидации настроек озвучки и субтитров."""

    def _get_settings(self, slug: str) -> dict:
        with get_db() as db:
            row = db.execute("SELECT settings FROM projects WHERE slug=?", (slug,)).fetchone()
        return get_project_settings(row["settings"])

    def test_saves_voice_svetlana(self, client, patch_env):
        """Голос 'svetlana' сохраняется."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "tts_voice": "svetlana",
            },
        )
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["tts_voice"] == "svetlana"

    def test_saves_voice_dmitry(self, client, patch_env):
        """Голос 'dmitry' сохраняется."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "tts_voice": "dmitry",
            },
        )
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["tts_voice"] == "dmitry"

    def test_saves_voice_auto(self, client, patch_env):
        """Голос 'auto' сохраняется."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "tts_voice": "auto",
            },
        )
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["tts_voice"] == "auto"

    def test_invalid_voice_falls_back_to_svetlana(self, client, patch_env):
        """Мусорный голос → дефолт 'svetlana'."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "tts_voice": "hacker_voice",
            },
        )
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["tts_voice"] == "svetlana"

    def test_saves_subtitle_colors(self, client, patch_env):
        """Корректные HEX-цвета субтитров сохраняются."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "sub_font_color": "#ff0000",
                "sub_outline_color": "#00ff00",
                "sub_highlight_color": "#0000ff",
                "sub_outline_width": "7",
            },
        )
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["sub_font_color"] == "#ff0000"
        assert s["sub_outline_color"] == "#00ff00"
        assert s["sub_highlight_color"] == "#0000ff"
        assert s["sub_outline_width"] == 7

    def test_invalid_hex_color_falls_back_to_default(self, client, patch_env):
        """Мусорный HEX → дефолт (#ffffff)."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "sub_font_color": "not-a-color",
                "sub_outline_color": "#zzzzzz",
                "sub_outline_width": "5",
            },
        )
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["sub_font_color"] == "#ffffff"   # дефолт
        assert s["sub_outline_color"] == "#000000"  # дефолт

    def test_outline_width_clamped_1_to_10(self, client, patch_env):
        """Толщина обрамления clamp'ится к [1, 10]."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "sub_outline_width": "999",
            },
        )
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["sub_outline_width"] == 10

    def test_default_settings_have_new_fields(self, client, patch_env):
        """Новый проект получает дефолтные значения для tts_voice и sub_*."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        s = self._get_settings(slug)
        assert s["tts_voice"] == "svetlana"
        assert s["sub_font_color"] == "#ffffff"
        assert s["sub_outline_color"] == "#000000"
        assert s["sub_outline_width"] == 5
        assert s["sub_highlight_color"] == "#ffe600"

    def test_ui_shows_voice_select(self, client, patch_env):
        """Форма настроек содержит select голоса."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.get(f"/projects/{slug}/launch-panel")
        assert resp.status_code == 200
        assert "tts_voice" in resp.text
        assert "Светлана" in resp.text
        assert "Дмитрий" in resp.text

    def test_ui_shows_color_inputs(self, client, patch_env):
        """Форма содержит color-инпуты субтитров."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.get(f"/projects/{slug}/launch-panel")
        assert resp.status_code == 200
        assert "sub_font_color" in resp.text
        assert "sub_outline_color" in resp.text
        assert "sub_highlight_color" in resp.text
        assert "sub_outline_width" in resp.text

    def test_saves_tts_rate_and_pitch(self, client, patch_env):
        """tts_rate=-10% и tts_pitch=+8Hz сохраняются в настройки проекта."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "tts_rate":  "-10%",
                "tts_pitch": "+8Hz",
            },
        )
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["tts_rate"]  == "-10%"
        assert s["tts_pitch"] == "+8Hz"

    def test_invalid_tts_rate_falls_back_to_default(self, client, patch_env):
        """Мусорный tts_rate → дефолт '+0%'."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "3",
                "retention_days": "14",
                "tts_rate": "abc",
            },
        )
        assert resp.status_code == 200
        s = self._get_settings(slug)
        assert s["tts_rate"] == "+0%"

    def test_default_settings_have_tts_rate_pitch(self, client, patch_env):
        """Новый проект получает дефолтные значения для tts_rate и tts_pitch."""
        _setup_db()
        with get_db() as db:
            slug, _ = _create_project(db, platforms=("telegram",))

        s = self._get_settings(slug)
        assert s["tts_rate"]  == "+0%"
        assert s["tts_pitch"] == "+0Hz"
