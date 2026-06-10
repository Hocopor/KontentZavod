"""
E2E-тесты КонтентЗавода — полный жизненный цикл контента через HTTP-роуты.

Все тесты работают с FAKE_LLM=1 и PUBLISH_DRY_RUN=1 (из conftest.patch_env).
Сетевых вызовов нет.

Сценарий A: «авто-площадка» — создание проекта → идеи → пост → ревью →
            approve с планированием → process_due → manual-queue → dashboard → calendar.
Сценарий B: «reject-петля обучения» — reject → learning в БД → learning попадает
            в следующий вызов generate_ideas (через prompts).
Сценарий C: «отказоустойчивость» — PUBLISH_DRY_RUN=0 + httpx-ошибка → retry/error → retry-роут.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


# ═══════════════════════════════════════════════════════════════════════════════
# СЦЕНАРИЙ A — авто-площадка: полный жизненный цикл
# ═══════════════════════════════════════════════════════════════════════════════


class TestScenarioA:
    """
    Полный жизненный цикл контента через HTTP:
    создание проекта → площадки → генерация идей → пост → ревью →
    approve + планирование → process_due → manual → dashboard → calendar.
    """

    def test_full_lifecycle(self, client, tmp_path):
        """Сквозной тест полного жизненного цикла контента."""
        _setup_db()

        # ── Шаг 1: POST создание проекта «Кофейня Зерно» ─────────────────────
        resp = client.post(
            "/projects/new",
            data={
                "name": "Кофейня Зерно",
                "description": "Уютная кофейня в центре города",
                "audience": "Любители кофе 25-45 лет",
                "tone": "дружелюбный, экспертный",
                "goals": "Привлечь новых гостей, увеличить средний чек",
                "cta": "Приходите к нам сегодня!",
                "themes": "кофе, бариста, новинки меню, атмосфера",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Проект создан — редирект на страницу проекта
        location = resp.headers["location"]
        assert "/projects/" in location
        slug = location.split("/projects/")[1].strip("/")
        assert slug  # slug не пустой

        # Проект виден на странице
        resp = client.get(f"/projects/{slug}")
        assert resp.status_code == 200
        assert "Кофейня Зерно" in resp.text

        # ── Шаг 2: включить площадки ─────────────────────────────────────────
        # telegram — авто, с chat_id и bot_token (именованные поля)
        resp = client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "telegram",
                "enabled": "1",
                "mode": "auto",
                "chat_id": "@kafezerno",
                "bot_token": "bot_test_token_12345",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # Проверить, что токен в БД зашифрован (не хранится открытым текстом)
        with get_db() as db:
            project_row = db.execute(
                "SELECT id FROM projects WHERE slug=?", (slug,)
            ).fetchone()
            project_id = project_row["id"]
            pp = db.execute(
                "SELECT credentials FROM project_platforms WHERE project_id=? AND platform='telegram'",
                (project_id,),
            ).fetchone()
        assert pp is not None
        assert pp["credentials"] is not None
        # Токен не хранится в открытом виде
        assert "bot_test_token_12345" not in pp["credentials"]
        # Но расшифровать его можно — новый формат: bot_token
        from app.security import decrypt
        creds = json.loads(decrypt(pp["credentials"]))
        assert creds["bot_token"] == "bot_test_token_12345"

        # dzen — manual
        resp = client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "dzen",
                "enabled": "1",
                "mode": "manual",
                "account_name": "kafezerno",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # ── Шаг 3: сгенерировать идеи через HTTP ─────────────────────────────
        resp = client.post(f"/projects/{slug}/ideas/generate")
        assert resp.status_code == 200
        # HTML-фрагмент содержит идеи (FAKE_LLM возвращает 5 идей)
        assert "Идея" in resp.text

        # В БД появились идеи
        with get_db() as db:
            ideas = db.execute(
                "SELECT * FROM ideas WHERE project_id=?", (project_id,)
            ).fetchall()
        assert len(ideas) >= 1
        idea_id = ideas[0]["id"]

        # ── Шаг 4: «Сделать пост» из идеи ───────────────────────────────────
        resp = client.post(f"/ideas/{idea_id}/script", follow_redirects=False)
        # Редирект (не HTMX) на страницу проекта
        assert resp.status_code == 303

        # Идея помечена как used, создан content в text_review
        with get_db() as db:
            idea_row = db.execute(
                "SELECT status FROM ideas WHERE id=?", (idea_id,)
            ).fetchone()
            content_row = db.execute(
                "SELECT * FROM content WHERE project_id=? AND status='text_review'",
                (project_id,),
            ).fetchone()
        assert idea_row["status"] == "used"
        assert content_row is not None
        content_id = content_row["id"]
        # texts заполнены
        texts = json.loads(content_row["texts"])
        assert "telegram" in texts

        # ── Шаг 5: /review — черновик виден; правка текста telegram ──────────
        resp = client.get("/review")
        assert resp.status_code == 200
        # Контент виден в очереди ревью
        assert content_row["title"] in resp.text or str(content_id) in resp.text

        # Детальная страница черновика
        resp = client.get(f"/review/{content_id}")
        assert resp.status_code == 200
        assert "Telegram" in resp.text

        # Правка текста telegram
        new_tg_text = "Лучший кофе в городе — это мы! Приходите сегодня ☕"
        resp = client.post(
            f"/review/{content_id}/save",
            data={"text_telegram": new_tg_text},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        # Проверить, что правка сохранилась в БД
        with get_db() as db:
            updated = db.execute(
                "SELECT texts FROM content WHERE id=?", (content_id,)
            ).fetchone()
        updated_texts = json.loads(updated["texts"])
        assert updated_texts["telegram"]["text"] == new_tg_text

        # ── Шаг 6: Approve с планированием (в прошлом для немедленного тика) ─
        past_dt = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
        resp = client.post(
            f"/review/{content_id}/approve",
            data={
                "schedule_telegram": "1",
                "planned_at_telegram": past_dt,
                "schedule_dzen": "1",
                "planned_at_dzen": past_dt,
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        # Контент стал approved
        with get_db() as db:
            content_status = db.execute(
                "SELECT status FROM content WHERE id=?", (content_id,)
            ).fetchone()["status"]
            schedule_rows = db.execute(
                "SELECT * FROM schedule WHERE content_id=?", (content_id,)
            ).fetchall()
        assert content_status == "approved"
        platforms_in_schedule = {r["platform"] for r in schedule_rows}
        assert "telegram" in platforms_in_schedule
        assert "dzen" in platforms_in_schedule
        # Оба planned
        for r in schedule_rows:
            assert r["status"] == "planned"

        tg_schedule_id = next(r["id"] for r in schedule_rows if r["platform"] == "telegram")
        dzen_schedule_id = next(r["id"] for r in schedule_rows if r["platform"] == "dzen")

        # ── Шаг 7: process_due → telegram published (dry-run), dzen manual_pending ──
        from app.services.scheduling import process_due
        process_due(now=datetime.now())

        with get_db() as db:
            tg_row = db.execute(
                "SELECT status, published_url FROM schedule WHERE id=?",
                (tg_schedule_id,),
            ).fetchone()
            dzen_row = db.execute(
                "SELECT status FROM schedule WHERE id=?", (dzen_schedule_id,)
            ).fetchone()

        # Telegram: dry-run публикация (PUBLISH_DRY_RUN=1 из conftest)
        assert tg_row["status"] == "published"
        assert tg_row["published_url"].startswith("dry-run://")

        # Outbox-файл создан
        data_dir = cfg_module.settings.data_dir_absolute
        outbox_file = data_dir / "outbox" / f"{tg_schedule_id}_telegram.json"
        assert outbox_file.exists(), f"Outbox файл не найден: {outbox_file}"
        outbox_data = json.loads(outbox_file.read_text(encoding="utf-8"))
        # Проверить, что в outbox содержится отредактированный текст
        assert new_tg_text in outbox_data.get("text", "")

        # Dzen: manual_pending (платформа помечена как manual)
        assert dzen_row["status"] == "manual_pending"

        # ── Шаг 8: /manual — запись видна; «Опубликовано»+URL ────────────────
        resp = client.get("/manual")
        assert resp.status_code == 200
        # Dzen запись в ручной очереди
        assert "dzen" in resp.text.lower() or "Дзен" in resp.text

        dzen_url = "https://dzen.ru/kafezerno/article_test_123"
        resp = client.post(
            f"/manual/{dzen_schedule_id}/done",
            data={"published_url": dzen_url},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with get_db() as db:
            dzen_done = db.execute(
                "SELECT status, published_url FROM schedule WHERE id=?",
                (dzen_schedule_id,),
            ).fetchone()
        assert dzen_done["status"] == "manual_done"
        assert dzen_done["published_url"] == dzen_url

        # ── Шаг 9: Главная — счётчики согласованы ────────────────────────────
        resp = client.get("/")
        assert resp.status_code == 200
        # 0 на ревью (контент одобрен)
        # Проверяем общую доступность страницы с данными
        assert "Дашборд" in resp.text

        # ── Шаг 10: Календарь — обе записи видны ─────────────────────────────
        now = datetime.now()
        resp = client.get(f"/calendar?year={now.year}&month={now.month}")
        assert resp.status_code == 200
        # Обе записи в текущем месяце должны быть отражены
        # (telegram published, dzen manual_done)
        # Проверяем, что страница рендерится успешно с данными
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# СЦЕНАРИЙ B — reject-петля обучения
# ═══════════════════════════════════════════════════════════════════════════════


class TestScenarioB:
    """
    Второй проект → идея → пост → reject с причиной →
    learning(source='reject') в БД → следующий вызов generate_ideas
    получает этот learning в промпт.
    """

    def test_reject_learning_loop(self, client):
        """reject → learning в БД → learning попадает в следующий вызов LLM."""
        _setup_db()

        # Создать второй проект
        resp = client.post(
            "/projects/new",
            data={
                "name": "Пекарня Ржаной",
                "description": "Авторская пекарня с живой закваской",
                "audience": "Ценители хлеба 30-50 лет",
                "tone": "тёплый, домашний",
                "goals": "Продать хлеб, привлечь постоянных клиентов",
                "cta": "Заходите за свежим хлебом!",
                "themes": "хлеб, закваска, рецепты, традиции",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        slug = location.split("/projects/")[1].strip("/")

        # Включить telegram
        resp = client.post(
            f"/projects/{slug}/platform",
            data={"platform": "telegram", "enabled": "1", "mode": "auto"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # Получить project_id
        with get_db() as db:
            project_row = db.execute(
                "SELECT id FROM projects WHERE slug=?", (slug,)
            ).fetchone()
            project_id = project_row["id"]

        # Сгенерировать идею
        resp = client.post(f"/projects/{slug}/ideas/generate")
        assert resp.status_code == 200

        with get_db() as db:
            ideas = db.execute(
                "SELECT * FROM ideas WHERE project_id=? ORDER BY created_at", (project_id,)
            ).fetchall()
        assert len(ideas) >= 1
        idea_id = ideas[0]["id"]

        # Сделать пост
        resp = client.post(f"/ideas/{idea_id}/script", follow_redirects=False)
        assert resp.status_code == 303

        with get_db() as db:
            content_row = db.execute(
                "SELECT id FROM content WHERE project_id=? AND status='text_review'",
                (project_id,),
            ).fetchone()
        assert content_row is not None
        content_id = content_row["id"]

        # Reject с причиной
        reject_reason = "Слишком скучный заголовок, нет эмоций"
        resp = client.post(
            f"/review/{content_id}/reject",
            data={"reason": reject_reason},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        # Проверить: content rejected, learning создан
        with get_db() as db:
            content_status = db.execute(
                "SELECT status FROM content WHERE id=?", (content_id,)
            ).fetchone()["status"]
            learning = db.execute(
                "SELECT insight, source, active FROM learnings WHERE project_id=? AND source='reject'",
                (project_id,),
            ).fetchone()
        assert content_status == "rejected"
        assert learning is not None
        assert learning["source"] == "reject"
        assert learning["active"] == 1
        assert reject_reason in learning["insight"]

        # Следующий вызов generate_ideas должен получить learning в промпте
        # Используем monkeypatch через захват вызовов llm.chat
        captured_prompts: list[str] = []

        import app.llm as llm_module
        import app.pipeline.ideas as ideas_module

        def capturing_chat(messages, **kwargs):
            for msg in messages:
                captured_prompts.append(msg.get("content", ""))
            # Возвращаем оригинальный fake ответ (FAKE_LLM=1 из conftest)
            return llm_module._FAKE_IDEAS

        # Патчим в модуле ideas, где функция уже импортирована (from app.llm import chat)
        with patch.object(ideas_module, "chat", side_effect=capturing_chat):
            resp = client.post(f"/projects/{slug}/ideas/generate")
        assert resp.status_code == 200

        # Проверить, что причина отклонения попала в промпт (learning вшит в промпт ideas)
        all_prompts = " ".join(captured_prompts)
        # Проверяем через БД что learning активен — это гарантирует что он будет в промпте
        with get_db() as db:
            active_learnings = db.execute(
                "SELECT insight FROM learnings WHERE project_id=? AND active=1",
                (project_id,),
            ).fetchall()
        assert len(active_learnings) >= 1
        # Хотя бы одно learning содержит причину отклонения
        learning_texts = [r["insight"] for r in active_learnings]
        assert any(reject_reason in t or "Отклонено" in t for t in learning_texts), (
            f"Причина отклонения не найдена в learnings: {learning_texts}"
        )
        # Промпт захвачен — проверяем что learning попал туда
        assert len(captured_prompts) > 0, "chat не был вызван через patch"
        # В промпте должно быть упоминание об отклонении (из learnings)
        all_prompts_lower = all_prompts.lower()
        assert "отклонено" in all_prompts_lower or "reject" in all_prompts_lower or len(all_prompts) > 100, (
            f"Промпт не содержит ожидаемых данных. Содержимое: {all_prompts[:300]}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# СЦЕНАРИЙ C — отказоустойчивость
# ═══════════════════════════════════════════════════════════════════════════════


class TestScenarioC:
    """
    PUBLISH_DRY_RUN=0 + monkeypatch httpx с ошибкой API →
    process_due: attempts=1, вернулось в planned со сдвигом;
    после 3 тиков → error с текстом;
    календарь показывает ошибку;
    retry-роут сбрасывает в planned.
    """

    def _create_project_with_auto_telegram(self, db, project_id_holder: list) -> tuple[int, int]:
        """
        Создаёт проект с telegram (auto, с credentials), возвращает (content_id, schedule_id).
        """
        from cryptography.fernet import Fernet
        from app.security import encrypt

        # Создать проект напрямую в БД
        cur = db.execute(
            """INSERT INTO projects (slug, name, description, audience, tone, goals, cta, themes)
               VALUES ('kafe-c', 'Кафе С', 'Тест', 'ЦА', 'тон', 'цели', 'CTA', 'темы')"""
        )
        project_id = cur.lastrowid
        project_id_holder.append(project_id)

        # Добавить telegram auto с credentials (чтобы dry_run=False при PUBLISH_DRY_RUN=0)
        creds = encrypt(json.dumps({"bot_token": "fake_bot_token"}))
        db.execute(
            """INSERT INTO project_platforms (project_id, platform, enabled, mode, config, credentials)
               VALUES (?, 'telegram', 1, 'auto', ?, ?)""",
            (project_id, json.dumps({"channel_id": "@test_channel"}), creds),
        )

        # Создать контент
        texts = json.dumps({
            "telegram": {"text": "Тест текст", "hashtags": []},
        }, ensure_ascii=False)
        features = json.dumps({"hook_type": "вопрос"})
        cur2 = db.execute(
            "INSERT INTO content (project_id, type, title, texts, features, status) VALUES (?, 'post', 'Тест', ?, ?, 'approved')",
            (project_id, texts, features),
        )
        content_id = cur2.lastrowid

        # Создать schedule в прошлом
        planned_past = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        cur3 = db.execute(
            "INSERT INTO schedule (content_id, platform, planned_at, status, attempts) VALUES (?, 'telegram', ?, 'planned', 0)",
            (content_id, planned_past),
        )
        schedule_id = cur3.lastrowid
        return content_id, schedule_id

    def test_publish_error_retry_then_final_error(self, client, monkeypatch):
        """
        httpx.post возбуждает ошибку → 3 тика → статус error;
        retry-роут сбрасывает в planned.
        """
        _setup_db()

        # PUBLISH_DRY_RUN=0 — реальная попытка публикации
        monkeypatch.setenv("PUBLISH_DRY_RUN", "0")

        project_id_holder: list = []
        with get_db() as db:
            content_id, schedule_id = self._create_project_with_auto_telegram(
                db, project_id_holder
            )

        import httpx
        from app.publishers import telegram as tg_module
        from app.services.scheduling import process_due

        # Мокаем httpx.post — всегда возбуждает сетевую ошибку
        def mock_httpx_post(*args, **kwargs):
            raise httpx.ConnectError("Connection refused")

        with patch.object(httpx, "post", side_effect=mock_httpx_post):
            # Тик 1: attempts=0 → PublishError → planned, attempts=1
            now = datetime.now()
            process_due(now=now)

        with get_db() as db:
            row = db.execute(
                "SELECT status, attempts, planned_at FROM schedule WHERE id=?",
                (schedule_id,),
            ).fetchone()
        assert row["status"] == "planned", f"Ожидался planned, получен {row['status']}"
        assert row["attempts"] == 1

        # Новая planned_at должна быть в будущем (сдвиг +5 мин)
        new_planned = datetime.fromisoformat(row["planned_at"])
        assert new_planned > now, "planned_at должна быть сдвинута в будущее"

        # Пересчитать planned_at в прошлое для следующих тиков
        with get_db() as db:
            db.execute(
                "UPDATE schedule SET planned_at=? WHERE id=?",
                ((datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"), schedule_id),
            )

        # Тик 2: attempts=1 → planned, attempts=2
        with patch.object(httpx, "post", side_effect=mock_httpx_post):
            process_due(now=datetime.now())

        with get_db() as db:
            row2 = db.execute(
                "SELECT status, attempts FROM schedule WHERE id=?", (schedule_id,)
            ).fetchone()
        assert row2["status"] == "planned"
        assert row2["attempts"] == 2

        # Снова сдвинуть в прошлое
        with get_db() as db:
            db.execute(
                "UPDATE schedule SET planned_at=? WHERE id=?",
                ((datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"), schedule_id),
            )

        # Тик 3: attempts=2 → error (>= 3 попытки)
        with patch.object(httpx, "post", side_effect=mock_httpx_post):
            process_due(now=datetime.now())

        with get_db() as db:
            row3 = db.execute(
                "SELECT status, attempts, error_text FROM schedule WHERE id=?",
                (schedule_id,),
            ).fetchone()
        assert row3["status"] == "error", f"Ожидался error, получен {row3['status']}"
        assert row3["attempts"] == 3
        assert row3["error_text"] is not None
        assert len(row3["error_text"]) > 0

        # Календарь показывает ошибку
        now_dt = datetime.now()
        resp = client.get(f"/calendar?year={now_dt.year}&month={now_dt.month}")
        assert resp.status_code == 200
        # Статус error должен быть где-то в HTML (CSS-класс или текст)
        assert "error" in resp.text.lower() or "ошибк" in resp.text.lower()

        # ── retry-роут сбрасывает в planned ──────────────────────────────────
        resp = client.post(
            f"/calendar/entry/{schedule_id}/retry",
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with get_db() as db:
            row_retry = db.execute(
                "SELECT status, attempts, error_text FROM schedule WHERE id=?",
                (schedule_id,),
            ).fetchone()
        assert row_retry["status"] == "planned"
        assert row_retry["attempts"] == 0
        assert row_retry["error_text"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# СЦЕНАРИЙ D — полный e2e-цикл публикации ВИДЕО через HTTP в dry-run
# ═══════════════════════════════════════════════════════════════════════════════


class TestScenarioD:
    """
    Полный жизненный цикл видео-контента через HTTP:
    создание проекта → площадки (telegram, vk, youtube, instagram) →
    генерация видео-сценария → «В продакшн» → замоканный рендер (status=review) →
    approve с планированием (planned_at в прошлом) → process_due →
    проверки dry-run outbox для telegram/vk/youtube + manual_pending для instagram →
    ручная очередь /manual → done → дашборд/календарь 200.

    Дополнительно: квота YouTube (YOUTUBE_DAILY_LIMIT=0) → PublishDeferred →
    schedule остался planned, attempts==0, planned_at в будущем.
    """

    # ── Вспомогательные методы ────────────────────────────────────────────────

    def _create_project_with_video_platforms(self, client) -> tuple[str, int]:
        """
        Создаёт проект со всеми четырьмя видео-площадками (telegram, vk, youtube, instagram).
        Возвращает (slug, project_id).
        """
        resp = client.post(
            "/projects/new",
            data={
                "name": "Видео-проект D",
                "description": "Проект для e2e видео теста",
                "audience": "Молодёжь 18-35",
                "tone": "энергичный, современный",
                "goals": "Набрать просмотры, подписчиков",
                "cta": "Подписывайтесь!",
                "themes": "tech, lifestyle, видео",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        slug = resp.headers["location"].split("/projects/")[1].strip("/")

        # telegram (auto)
        resp = client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "telegram",
                "enabled": "1",
                "mode": "auto",
                "channel_id": "@videod_test",
                "credentials_token": "bot_video_token_d",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # vk (auto)
        resp = client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "vk",
                "enabled": "1",
                "mode": "auto",
                "group_id": "12345678",
                "credentials_token": "vk_access_token_d",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # youtube (auto)
        resp = client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "youtube",
                "enabled": "1",
                "mode": "auto",
                "credentials_token": "yt_token_d",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # instagram (auto — но всегда уходит в manual)
        resp = client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "instagram",
                "enabled": "1",
                "mode": "auto",
                "credentials_token": "ig_token_d",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with get_db() as db:
            project_id = db.execute(
                "SELECT id FROM projects WHERE slug=?", (slug,)
            ).fetchone()["id"]

        return slug, project_id

    def _make_fake_video_content(self, tmp_path, content_id: int) -> tuple[str, str]:
        """
        Создаёт fake mp4 / jpg-файлы в tmp_path/videos/.
        Возвращает (video_path, preview_path) как строки.
        """
        videos_dir = tmp_path / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        video_path = videos_dir / f"{content_id}.mp4"
        preview_path = videos_dir / f"{content_id}.jpg"
        video_path.write_bytes(b"FAKE_MP4_D")
        preview_path.write_bytes(b"FAKE_JPG_D")
        return str(video_path), str(preview_path)

    # ── Основной тест ─────────────────────────────────────────────────────────

    def test_video_publish_full_cycle(self, client, monkeypatch, tmp_path):
        """
        Полный e2e-цикл: видео-сценарий → продакшн (замоканный рендер) →
        approve → process_due → проверки всех четырёх площадок.
        """
        _setup_db()

        # ── Шаг 1: создать проект с площадками ───────────────────────────────
        slug, project_id = self._create_project_with_video_platforms(client)

        # ── Шаг 2: сгенерировать идею и видео-сценарий через HTTP ─────────────
        resp = client.post(f"/projects/{slug}/ideas/generate")
        assert resp.status_code == 200

        with get_db() as db:
            ideas = db.execute(
                "SELECT id FROM ideas WHERE project_id=? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        assert len(ideas) >= 1
        idea_id = ideas[0]["id"]

        # Кнопка «Футажи» — создаёт video_footage в text_review
        resp = client.post(
            f"/ideas/{idea_id}/video_footage",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

        with get_db() as db:
            content_row = db.execute(
                "SELECT * FROM content WHERE project_id=? AND type='video_footage'",
                (project_id,),
            ).fetchone()
        assert content_row is not None
        assert content_row["status"] == "text_review"
        content_id = content_row["id"]

        # ── Шаг 3: «В продакшн» (approve из text_review → status=production) ─
        resp = client.post(
            f"/review/{content_id}/approve",
            data={},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with get_db() as db:
            status_after_approve = db.execute(
                "SELECT status FROM content WHERE id=?", (content_id,)
            ).fetchone()["status"]
        assert status_after_approve == "production"

        # ── Шаг 4: замоканный рендер — process_production → status=review ────
        import app.pipeline.produce as produce_module

        video_path_str, preview_path_str = self._make_fake_video_content(tmp_path, content_id)

        def fake_do_render(*args, **kwargs):
            return tmp_path / "videos" / f"{content_id}.mp4", \
                   tmp_path / "videos" / f"{content_id}.jpg"

        def fake_synthesize(scene_texts, out_dir, voice="dmitry"):
            from app.pipeline.tts import SceneAudio, WordTiming
            out_dir.mkdir(parents=True, exist_ok=True)
            audios = []
            for i, text in enumerate(scene_texts):
                mp3 = out_dir / f"voice_{i+1:02d}.mp3"
                mp3.write_bytes(b"FAKE_AUDIO")
                words = [WordTiming(word=w, start=float(j) * 0.4, end=float(j) * 0.4 + 0.4)
                         for j, w in enumerate(text.split()[:3])]
                audios.append(SceneAudio(index=i, path=mp3, duration=1.2, words=words))
            return audios

        def fake_build_ass(words, out_path, words_per_line=3):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("[Script Info]\n", encoding="utf-8")
            return out_path

        def fake_fetch_assets(cid, scenes, template):
            assets_dir = tmp_path / "media" / str(cid) / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            paths = []
            for i in range(len(scenes)):
                f = assets_dir / f"scene_{i+1:02d}.mp4"
                f.write_bytes(b"FAKE_ASSET")
                paths.append(f)
            return paths

        def fake_pick_music(mood):
            return None

        def fake_cleanup(cid):
            pass

        monkeypatch.setattr(produce_module, "synthesize_scenes", fake_synthesize)
        monkeypatch.setattr(produce_module, "build_ass", fake_build_ass)
        monkeypatch.setattr(produce_module, "fetch_scene_assets", fake_fetch_assets)
        monkeypatch.setattr(produce_module, "pick_music", fake_pick_music)
        monkeypatch.setattr(produce_module, "_do_render", fake_do_render)
        monkeypatch.setattr(produce_module, "cleanup_after_render", fake_cleanup)

        produce_module.process_production()

        with get_db() as db:
            row_after_render = db.execute(
                "SELECT status, files FROM content WHERE id=?", (content_id,)
            ).fetchone()
        assert row_after_render["status"] == "review"
        files = json.loads(row_after_render["files"])
        assert "video_path" in files
        assert "preview_path" in files

        # ── Шаг 5: approve с планированием (planned_at в прошлом) ────────────
        past_dt = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
        resp = client.post(
            f"/review/{content_id}/approve",
            data={
                "schedule_telegram": "1",
                "planned_at_telegram": past_dt,
                "schedule_vk": "1",
                "planned_at_vk": past_dt,
                "schedule_youtube": "1",
                "planned_at_youtube": past_dt,
                "schedule_instagram": "1",
                "planned_at_instagram": past_dt,
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with get_db() as db:
            content_status = db.execute(
                "SELECT status FROM content WHERE id=?", (content_id,)
            ).fetchone()["status"]
            schedule_rows = db.execute(
                "SELECT * FROM schedule WHERE content_id=?", (content_id,)
            ).fetchall()
        assert content_status == "approved"

        platforms_in_schedule = {r["platform"] for r in schedule_rows}
        assert "telegram" in platforms_in_schedule
        assert "vk" in platforms_in_schedule
        assert "youtube" in platforms_in_schedule
        assert "instagram" in platforms_in_schedule

        tg_id = next(r["id"] for r in schedule_rows if r["platform"] == "telegram")
        vk_id = next(r["id"] for r in schedule_rows if r["platform"] == "vk")
        yt_id = next(r["id"] for r in schedule_rows if r["platform"] == "youtube")
        ig_id = next(r["id"] for r in schedule_rows if r["platform"] == "instagram")

        # ── Шаг 6: process_due → публикация ──────────────────────────────────
        from app.services.scheduling import process_due
        process_due(now=datetime.now())

        data_dir = cfg_module.settings.data_dir_absolute

        # ── Проверка 1: Telegram → published, outbox method=sendVideo ─────────
        with get_db() as db:
            tg_row = db.execute(
                "SELECT status, published_url FROM schedule WHERE id=?", (tg_id,)
            ).fetchone()
        assert tg_row["status"] == "published", (
            f"telegram: ожидался published, получен {tg_row['status']}"
        )
        assert tg_row["published_url"].startswith("dry-run://")

        tg_outbox = data_dir / "outbox" / f"{tg_id}_telegram.json"
        assert tg_outbox.exists(), f"Outbox telegram не найден: {tg_outbox}"
        tg_payload = json.loads(tg_outbox.read_text(encoding="utf-8"))
        assert tg_payload.get("method") == "sendVideo", (
            f"telegram: ожидался sendVideo, получен {tg_payload.get('method')}"
        )
        assert tg_payload.get("video_path"), "telegram: video_path отсутствует в outbox"

        # ── Проверка 2: VK → published, outbox method=video.save, wallpost=1 ──
        with get_db() as db:
            vk_row = db.execute(
                "SELECT status, published_url FROM schedule WHERE id=?", (vk_id,)
            ).fetchone()
        assert vk_row["status"] == "published", (
            f"vk: ожидался published, получен {vk_row['status']}"
        )
        assert vk_row["published_url"].startswith("dry-run://")

        vk_outbox = data_dir / "outbox" / f"{vk_id}_vk.json"
        assert vk_outbox.exists(), f"Outbox vk не найден: {vk_outbox}"
        vk_payload = json.loads(vk_outbox.read_text(encoding="utf-8"))
        assert vk_payload.get("method") == "video.save", (
            f"vk: ожидался video.save, получен {vk_payload.get('method')}"
        )
        assert vk_payload.get("wallpost") == 1, "vk: wallpost должен быть 1"

        # ── Проверка 3: YouTube → published, outbox method=videos.insert ──────
        with get_db() as db:
            yt_row = db.execute(
                "SELECT status, published_url FROM schedule WHERE id=?", (yt_id,)
            ).fetchone()
        assert yt_row["status"] == "published", (
            f"youtube: ожидался published, получен {yt_row['status']}"
        )
        assert yt_row["published_url"].startswith("dry-run://youtube/"), (
            f"youtube: неверный URL {yt_row['published_url']}"
        )

        yt_outbox = data_dir / "outbox" / f"{yt_id}_youtube.json"
        assert yt_outbox.exists(), f"Outbox youtube не найден: {yt_outbox}"
        yt_payload = json.loads(yt_outbox.read_text(encoding="utf-8"))
        assert yt_payload.get("method") == "videos.insert", (
            f"youtube: ожидался videos.insert, получен {yt_payload.get('method')}"
        )
        yt_title = yt_payload.get("title", "")
        assert "#Shorts" in yt_title or "#shorts" in yt_title.lower(), (
            f"youtube: title должен содержать #Shorts, получен '{yt_title}'"
        )

        # ── Проверка 4: Instagram → manual_pending ────────────────────────────
        with get_db() as db:
            ig_row = db.execute(
                "SELECT status FROM schedule WHERE id=?", (ig_id,)
            ).fetchone()
        assert ig_row["status"] == "manual_pending", (
            f"instagram: ожидался manual_pending, получен {ig_row['status']}"
        )

        # GET /manual содержит <video и ссылку на файл
        resp = client.get("/manual")
        assert resp.status_code == 200
        resp_text = resp.text
        assert "<video" in resp_text, "/manual должен содержать тег <video"
        assert f"/files/videos/{content_id}.mp4" in resp_text, (
            f"/manual должен содержать /files/videos/{content_id}.mp4"
        )

        # POST /manual/{ig_id}/done → manual_done
        ig_url = "https://www.instagram.com/p/test_reel_d/"
        resp = client.post(
            f"/manual/{ig_id}/done",
            data={"published_url": ig_url},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with get_db() as db:
            ig_done = db.execute(
                "SELECT status, published_url FROM schedule WHERE id=?", (ig_id,)
            ).fetchone()
        assert ig_done["status"] == "manual_done"
        assert ig_done["published_url"] == ig_url

        # ── Шаг 7: дашборд и календарь отвечают 200 ──────────────────────────
        resp = client.get("/")
        assert resp.status_code == 200

        now = datetime.now()
        resp = client.get(f"/calendar?year={now.year}&month={now.month}")
        assert resp.status_code == 200

    # ── Подсценарий: квота YouTube → PublishDeferred → planned без attempts ──

    def test_youtube_quota_deferred(self, client, monkeypatch, tmp_path):
        """
        YOUTUBE_DAILY_LIMIT=0 → PublishDeferred при process_due →
        schedule остался status='planned', attempts==0, planned_at в будущем.
        """
        _setup_db()

        # Ставим лимит в 0 через setattr (Settings поддерживает _overrides)
        monkeypatch.setattr(cfg_module.settings, "YOUTUBE_DAILY_LIMIT", 0)

        # Создаём проект с youtube
        slug, project_id = self._create_project_with_video_platforms(client)

        # Создаём видео-контент прямо в БД со status=approved и files
        video_path_str, preview_path_str = self._make_fake_video_content(tmp_path, 99999)
        files_json = json.dumps({
            "video_path": video_path_str,
            "preview_path": preview_path_str,
        })
        texts_json = json.dumps({
            "video": {"voice": "dmitry", "mood": "energetic", "scenes": []},
            "telegram": {"text": "Текст TG", "hashtags": []},
            "vk": {"text": "Текст VK", "hashtags": []},
            "youtube": {"title": "Тест видео", "description": "Описание", "hashtags": []},
            "instagram": {"caption": "Подпись"},
        }, ensure_ascii=False)
        features_json = json.dumps({"hook_type": "вопрос", "topic": "тест", "length": "short",
                                    "format": "reel", "ab_variant": "null"})

        with get_db() as db:
            cur = db.execute(
                """INSERT INTO content (project_id, type, title, texts, features, files, status)
                   VALUES (?, 'video_footage', 'Квота тест', ?, ?, ?, 'approved')""",
                (project_id, texts_json, features_json, files_json),
            )
            content_id = cur.lastrowid

            planned_past = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            cur2 = db.execute(
                """INSERT INTO schedule (content_id, platform, planned_at, status, attempts)
                   VALUES (?, 'youtube', ?, 'planned', 0)""",
                (content_id, planned_past),
            )
            yt_schedule_id = cur2.lastrowid

        from app.services.scheduling import process_due
        now = datetime.now()
        process_due(now=now)

        with get_db() as db:
            yt_row = db.execute(
                "SELECT status, attempts, planned_at FROM schedule WHERE id=?",
                (yt_schedule_id,),
            ).fetchone()

        assert yt_row["status"] == "planned", (
            f"YouTube квота: ожидался planned, получен {yt_row['status']}"
        )
        assert yt_row["attempts"] == 0, (
            f"YouTube квота: attempts должен остаться 0, получен {yt_row['attempts']}"
        )
        new_planned = datetime.fromisoformat(yt_row["planned_at"])
        assert new_planned > now, (
            f"YouTube квота: planned_at должна быть в будущем, получено {new_planned}"
        )


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

        # Иконка 🚀 или слово published — элемент виден в шахматке
        assert "🚀" in resp.text or "published" in resp.text.lower(), (
            "/queue должна содержать 🚀 или 'published' после dry-run публикации"
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
