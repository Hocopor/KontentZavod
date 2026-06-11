"""
E2E-тесты КонтентЗавода — сценарий E: полный жизненный цикл нового флоу 2.0.

Сценарий E: generate-profile → проект → telegram-площадка →
            launch-settings → launch → process_brain → /plan одобрение + autogen →
            process_factory → process_due → /queue → паузы plan_paused/gen_paused.

Все тесты работают с FAKE_LLM=1 и PUBLISH_DRY_RUN=1 (из conftest.patch_env).
Сетевых вызовов нет.
"""
import json
from datetime import datetime, timedelta

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


# ═══════════════════════════════════════════════════════════════════════════════
# СЦЕНАРИЙ E — новый флоу «Завод 2.0»
# ═══════════════════════════════════════════════════════════════════════════════


class TestScenarioE:
    """
    Полный e2e-цикл нового флоу (этап 7):
    generate-profile → создание проекта → telegram-площадка →
    launch-settings → launch → process_brain (стратегия + план) →
    /plan одобрение + autogen → process_factory → process_due → /queue / модалка.
    Дополнительно: паузы plan_paused/gen_paused останавливают brain/factory.
    """

    # ── Вспомогательные методы ────────────────────────────────────────────────

    def _create_project_e(self, client) -> tuple[str, int]:
        """
        Шаг а: POST /projects/generate-profile → ИИ-профиль (FAKE_LLM=1).
        Затем POST /projects/new с данными профиля.
        Возвращает (slug, project_id).
        """
        # а1) generate-profile — HTMX-фрагмент с заполненной формой
        resp = client.post(
            "/projects/generate-profile",
            data={
                "name": "Завод 2.0 Тест",
                "description": "Тестовый проект для нового флоу",
                "goals": "Рост подписчиков и вовлечённости",
                "manual": "0",
            },
        )
        assert resp.status_code == 200, f"generate-profile вернул {resp.status_code}"
        # FAKE_LLM возвращает _FAKE_PROFILE — поля audience, tone, cta, themes должны быть
        # в HTML-фрагменте
        assert "audience" in resp.text.lower() or "аудитор" in resp.text.lower() or len(resp.text) > 100, (
            "generate-profile: HTML-фрагмент пустой или не содержит ожидаемых данных"
        )

        # а2) создать проект с ИИ-профилем
        resp = client.post(
            "/projects/new",
            data={
                "name": "Завод 2.0 Тест",
                "description": "Тестовый проект для нового флоу",
                "goals": "Рост подписчиков и вовлечённости",
                "audience": "Предприниматели и маркетологи 25–45 лет, ищут инструменты роста",
                "tone": "Экспертный, но дружелюбный — без жаргона",
                "cta": "Записаться на консультацию",
                "themes": "Маркетинг, автоматизация, кейсы",
                "forbidden": "Политика, негатив",
                "extra": "Акцент на практических результатах",
                "links": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        slug = resp.headers["location"].split("/projects/")[1].strip("/")

        with get_db() as db:
            project_id = db.execute(
                "SELECT id FROM projects WHERE slug=?", (slug,)
            ).fetchone()["id"]

        return slug, project_id

    def _add_telegram_platform(self, client, slug: str) -> None:
        """
        Шаг б: Подключить telegram (auto, bot_token + chat_id).
        """
        resp = client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "telegram",
                "enabled": "1",
                "mode": "auto",
                "bot_token": "bot_e2e_test_token_e",
                "chat_id": "@zavod20test",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # Убедиться, что токен зашифрован
        with get_db() as db:
            pp = db.execute(
                """SELECT credentials FROM project_platforms
                   WHERE project_id=(SELECT id FROM projects WHERE slug=?) AND platform='telegram'""",
                (slug,),
            ).fetchone()
        assert pp is not None and pp["credentials"] is not None
        assert "bot_e2e_test_token_e" not in pp["credentials"], (
            "bot_token не должен храниться в открытом виде"
        )
        # Расшифровать и проверить
        from app.security import decrypt
        creds = json.loads(decrypt(pp["credentials"]))
        assert creds["bot_token"] == "bot_e2e_test_token_e"

    def _launch_project(self, client, slug: str) -> None:
        """
        Шаг в: настройки запуска + POST launch.
        gen_lookahead_days=14 чтобы FAKE_PLAN-пункты (2026-06-15+) попали в окно.
        """
        # в1) launch-settings (HTMX)
        resp = client.post(
            f"/projects/{slug}/launch-settings",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "14",
                "retention_days": "14",
                # Включить тип post для telegram
                "ct_telegram_post": "1",
                "ct_telegram_video": "0",
            },
        )
        assert resp.status_code == 200

        # в2) launch → stage='running', strategies(generating) создана
        resp = client.post(
            f"/projects/{slug}/launch",
            data={
                "plan_horizon_days": "30",
                "gen_lookahead_days": "14",
                "retention_days": "14",
                "ct_telegram_post": "1",
                "ct_telegram_video": "0",
            },
        )
        assert resp.status_code == 200

        with get_db() as db:
            project_row = db.execute(
                "SELECT stage FROM projects WHERE slug=?", (slug,)
            ).fetchone()
            strategy_row = db.execute(
                """SELECT status FROM strategies
                   WHERE project_id=(SELECT id FROM projects WHERE slug=?)
                   ORDER BY version DESC LIMIT 1""",
                (slug,),
            ).fetchone()

        assert project_row["stage"] == "running", (
            f"Ожидался stage=running, получен {project_row['stage']}"
        )
        assert strategy_row is not None, "strategies-запись не создана после launch"
        assert strategy_row["status"] == "generating", (
            f"Ожидался status=generating, получен {strategy_row['status']}"
        )

    # ── Основной тест ─────────────────────────────────────────────────────────

    def test_new_flow_full_cycle(self, client, tmp_path):
        """
        Полный e2e нового флоу: generate-profile → launch → brain → plan →
        factory → process_due → /queue → модалка.
        """
        _setup_db()

        # ── Шаг а–б: создать проект с ИИ-профилем и telegram ─────────────────
        slug, project_id = self._create_project_e(client)
        self._add_telegram_platform(client, slug)

        # ── Шаг в: launch-settings + launch ───────────────────────────────────
        self._launch_project(client, slug)

        # ── Шаг г: тик 1 process_brain → стратегия active ────────────────────
        from app.services.brain import process_brain

        process_brain()

        with get_db() as db:
            strategy_row = db.execute(
                """SELECT status FROM strategies
                   WHERE project_id=? ORDER BY version DESC LIMIT 1""",
                (project_id,),
            ).fetchone()
        assert strategy_row["status"] == "active", (
            f"После первого тика process_brain стратегия должна быть active, "
            f"получен {strategy_row['status']}"
        )

        # ── Шаг г (продолжение): тик 2 → план-пункты появились ───────────────
        process_brain()

        with get_db() as db:
            plan_items = db.execute(
                "SELECT * FROM plan_items WHERE project_id=? ORDER BY date, time_slot",
                (project_id,),
            ).fetchall()

        assert len(plan_items) >= 1, (
            "После второго тика process_brain в plan_items должны появиться пункты"
        )
        # Все пункты proposed (только что сгенерированы, никто не одобрял)
        for item in plan_items:
            assert item["status"] == "proposed", (
                f"plan_item {item['id']}: ожидался proposed, получен {item['status']}"
            )

        # ── Шаг д: GET /plan → шахматка отдаёт пункты ────────────────────────
        today = datetime.now()
        resp = client.get(f"/plan?project={slug}&year={today.year}&month={today.month}")
        assert resp.status_code == 200

        # Найти пункты типа 'post' для telegram (они будут kind=text → фабрика их подхватит)
        with get_db() as db:
            post_items = db.execute(
                """SELECT * FROM plan_items
                   WHERE project_id=? AND platform='telegram' AND content_type='post'
                     AND status='proposed'
                   ORDER BY date LIMIT 1""",
                (project_id,),
            ).fetchall()

        # Если в текущем месяце нет post-пунктов — проверим другой месяц
        if not post_items:
            # FAKE_PLAN использует 2026-06-15: берём из всех пунктов
            with get_db() as db:
                post_items = db.execute(
                    """SELECT * FROM plan_items
                       WHERE project_id=? AND platform='telegram' AND content_type='post'
                         AND status='proposed'
                       ORDER BY date LIMIT 1""",
                    (project_id,),
                ).fetchall()

        assert len(post_items) >= 1, (
            "Должен быть хотя бы один proposed plan_item с platform=telegram, content_type=post"
        )
        item_to_approve = dict(post_items[0])
        item_id = item_to_approve["id"]

        # Одобрить пункт через роут
        resp = client.post(f"/plan/item/{item_id}/approve")
        assert resp.status_code == 200

        # Статус одобрен
        with get_db() as db:
            approved_item = db.execute(
                "SELECT status FROM plan_items WHERE id=?", (item_id,)
            ).fetchone()
        assert approved_item["status"] == "approved", (
            f"plan_item {item_id}: ожидался approved, получен {approved_item['status']}"
        )

        # Включить autogen через роут (toggle)
        resp = client.post(
            f"/plan/autogen/{slug}",
            follow_redirects=False,
        )
        # Роут делает RedirectResponse → 303
        assert resp.status_code in (200, 303), (
            f"POST /plan/autogen/{slug} вернул {resp.status_code}"
        )

        # Проверить что autogen=1 в settings
        with get_db() as db:
            proj_row = db.execute(
                "SELECT settings FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        from app.db import get_project_settings
        proj_settings = get_project_settings(proj_row["settings"])
        assert proj_settings.get("autogen") == 1, (
            f"autogen должен быть 1 после toggle, settings: {proj_settings}"
        )

        # ── Шаг е: тик process_factory → контент создан, schedule запланирован ─
        from app.services.factory import process_factory

        # Сдвинуть дату пункта на сегодня, чтобы попал в lookahead (на случай если FAKE_PLAN
        # вернул дату за пределами 14 дней, хотя при today=2026-06-10 и horizon 14 дней
        # 2026-06-15 уже в окне; но для надёжности — перестраховываемся)
        from datetime import date as date_cls
        today_str = date_cls.today().isoformat()
        with get_db() as db:
            db.execute(
                "UPDATE plan_items SET date=? WHERE id=?",
                (today_str, item_id),
            )

        process_factory()

        # Проверить: plan_item → status=generated, content_id установлен
        with get_db() as db:
            generated_item = db.execute(
                "SELECT status, content_id FROM plan_items WHERE id=?", (item_id,)
            ).fetchone()
        assert generated_item["status"] == "generated", (
            f"После process_factory plan_item должен быть generated, "
            f"получен {generated_item['status']}"
        )
        assert generated_item["content_id"] is not None, (
            "content_id должен быть установлен после генерации"
        )
        content_id = generated_item["content_id"]

        # Проверить schedule создан (для telegram post — publish=auto → planned)
        with get_db() as db:
            schedule_row = db.execute(
                "SELECT * FROM schedule WHERE content_id=? AND platform='telegram'",
                (content_id,),
            ).fetchone()
        assert schedule_row is not None, "schedule для telegram должен быть создан"
        assert schedule_row["status"] == "planned", (
            f"telegram schedule должен быть planned, получен {schedule_row['status']}"
        )
        schedule_id = schedule_row["id"]

        # ── Шаг ж: сдвинуть planned_at в прошлое → process_due → published ─────
        past_dt = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as db:
            db.execute(
                "UPDATE schedule SET planned_at=? WHERE id=?",
                (past_dt, schedule_id),
            )

        from app.services.scheduling import process_due
        process_due(now=datetime.now())

        with get_db() as db:
            sched_after = db.execute(
                "SELECT status, published_url FROM schedule WHERE id=?", (schedule_id,)
            ).fetchone()
        assert sched_after["status"] == "published", (
            f"После process_due schedule должен быть published (dry-run), "
            f"получен {sched_after['status']}"
        )
        assert sched_after["published_url"].startswith("dry-run://"), (
            f"published_url должен начинаться с dry-run://, получен {sched_after['published_url']}"
        )

        # Outbox-файл создан
        import app.config as cfg_module
        data_dir = cfg_module.settings.data_dir_absolute
        outbox_file = data_dir / "outbox" / f"{schedule_id}_telegram.json"
        assert outbox_file.exists(), f"Outbox файл не найден: {outbox_file}"
        outbox_data = json.loads(outbox_file.read_text(encoding="utf-8"))
        assert "text" in outbox_data or "method" in outbox_data, (
            f"Outbox должен содержать text или method, получено: {list(outbox_data.keys())}"
        )

        # ── Шаг з: GET /queue → пункт виден со статусом published ──────────────
        resp = client.get(f"/queue?project={slug}")
        assert resp.status_code == 200

        # Иконка или слово published — элемент виден в шахматке
        assert "published" in resp.text.lower() or "🚀" in resp.text, (
            "/queue должна содержать 'published' или '🚀' после dry-run публикации"
        )

        # Модалка превью: GET /queue/items/{plan_item_id}/modal
        resp = client.get(
            f"/queue/items/{item_id}/modal",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200, (
            f"/queue/items/{item_id}/modal вернул {resp.status_code}"
        )
        # Превью содержит данные контента
        assert len(resp.text) > 50, "Модалка должна содержать контент, не пустая"

        # ── Шаг и: паузы останавливают brain/factory ──────────────────────────
        self._test_pause_brain_and_factory(client, slug, project_id)

    def _test_pause_brain_and_factory(
        self, client, slug: str, project_id: int
    ) -> None:
        """
        Подсценарий: план на паузе → process_brain не генерирует новые plan_items;
        gen_paused → process_factory не берёт пункты в работу.
        """
        from app.services.brain import process_brain
        from app.services.factory import process_factory

        # ── plan_paused → brain пропускает генерацию плана ───────────────────
        with get_db() as db:
            proj = db.execute(
                "SELECT settings FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        from app.db import get_project_settings
        settings_data = get_project_settings(proj["settings"])
        settings_data["plan_paused"] = 1

        with get_db() as db:
            db.execute(
                "UPDATE projects SET settings=? WHERE id=?",
                (json.dumps(settings_data), project_id),
            )

        # Зафиксируем число plan_items ДО тика
        with get_db() as db:
            count_before = db.execute(
                "SELECT COUNT(*) FROM plan_items WHERE project_id=?", (project_id,)
            ).fetchone()[0]

        process_brain()

        # Число plan_items не изменилось (план на паузе)
        with get_db() as db:
            count_after_pause = db.execute(
                "SELECT COUNT(*) FROM plan_items WHERE project_id=?", (project_id,)
            ).fetchone()[0]
        assert count_after_pause == count_before, (
            f"plan_paused=1: process_brain не должен генерировать новые пункты. "
            f"До: {count_before}, после: {count_after_pause}"
        )

        # Снять план-паузу
        settings_data["plan_paused"] = 0
        with get_db() as db:
            db.execute(
                "UPDATE projects SET settings=? WHERE id=?",
                (json.dumps(settings_data), project_id),
            )

        # ── gen_paused → factory не берёт пункты в работу ───────────────────
        # Создать ещё один approved plan_item для теста
        from datetime import date as date_cls
        tomorrow = (date_cls.today() + timedelta(days=1)).isoformat()
        with get_db() as db:
            cur = db.execute(
                """INSERT INTO plan_items
                   (project_id, platform, content_type, date, title, status)
                   VALUES (?, 'telegram', 'post', ?, 'Тест паузы gen', 'approved')""",
                (project_id, tomorrow),
            )
            paused_item_id = cur.lastrowid

        # Включить gen_paused
        settings_data["gen_paused"] = 1
        settings_data["autogen"] = 1  # autogen включён, но gen_paused должен блокировать
        with get_db() as db:
            db.execute(
                "UPDATE projects SET settings=? WHERE id=?",
                (json.dumps(settings_data), project_id),
            )

        process_factory()

        # Пункт должен остаться approved (gen_paused блокирует фабрику)
        with get_db() as db:
            paused_item = db.execute(
                "SELECT status FROM plan_items WHERE id=?", (paused_item_id,)
            ).fetchone()
        assert paused_item["status"] == "approved", (
            f"gen_paused=1: plan_item должен остаться approved, "
            f"получен {paused_item['status']}"
        )

        # Снять gen_paused
        settings_data["gen_paused"] = 0
        with get_db() as db:
            db.execute(
                "UPDATE projects SET settings=? WHERE id=?",
                (json.dumps(settings_data), project_id),
            )
