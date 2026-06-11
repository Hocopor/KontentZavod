"""
Тесты публикации по расписанию (волна 2).

pytest tests/test_publishers.py -q
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

# ─── Фикстуры ─────────────────────────────────────────────────────────────────


@pytest.fixture
def db_with_data():
    """
    БД с тестовым набором данных:
      - Один проект
      - Площадки: telegram (auto, fake token), vk (auto, БЕЗ токена), dzen (manual)
      - Один контент со status='approved' и texts по схеме llm.py

    Зависит от autouse-фикстуры patch_env (уже выполнена к этому моменту).
    """
    import app.config as cfg_module
    import app.db as db_module
    from app.db import init_db, get_db
    from app.security import encrypt

    # Убедимся, что db_module использует актуальный settings (patch_env уже применён)
    db_module.settings = cfg_module.settings

    init_db(cfg_module.settings.db_path_absolute)

    # Уникальный slug чтобы избежать конфликтов при параллельных запусках тестов
    slug = f"test-project-{uuid.uuid4().hex[:8]}"

    with get_db() as db:
        # Проект
        cur = db.execute(
            "INSERT INTO projects (slug, name, description) VALUES (?, ?, ?)",
            (slug, "Тестовый проект", "Описание"),
        )
        project_id = cur.lastrowid

        # Telegram: auto, с фейковым токеном
        tg_creds = encrypt(json.dumps({"bot_token": "fake:AAABBBCCC"}))
        tg_config = json.dumps({"channel_id": "@test_channel"})
        db.execute(
            """
            INSERT INTO project_platforms (project_id, platform, enabled, mode, config, credentials)
            VALUES (?, 'telegram', 1, 'auto', ?, ?)
            """,
            (project_id, tg_config, tg_creds),
        )

        # VK: auto, БЕЗ токена (credentials=NULL)
        vk_config = json.dumps({"group_id": 123456})
        db.execute(
            """
            INSERT INTO project_platforms (project_id, platform, enabled, mode, config, credentials)
            VALUES (?, 'vk', 1, 'auto', ?, NULL)
            """,
            (project_id, vk_config),
        )

        # Dzen: manual
        db.execute(
            """
            INSERT INTO project_platforms (project_id, platform, enabled, mode, config, credentials)
            VALUES (?, 'dzen', 1, 'manual', NULL, NULL)
            """,
            (project_id,),
        )

        # Контент с status='approved' и полными texts
        texts = json.dumps({
            "telegram": {
                "text": "Тестовый пост для Telegram",
                "hashtags": ["#тест", "#контент"],
            },
            "vk": {
                "text": "Тестовый пост для VK",
                "hashtags": ["#тест"],
            },
            "dzen": {
                "title": "Заголовок для Дзен",
                "text": "Текст для Дзен",
            },
        })
        cur = db.execute(
            """
            INSERT INTO content (project_id, type, status, texts)
            VALUES (?, 'post', 'approved', ?)
            """,
            (project_id, texts),
        )
        content_id = cur.lastrowid

    return {"project_id": project_id, "content_id": content_id}


def _past() -> datetime:
    """Момент в прошлом для немедленного срабатывания."""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)


def _future() -> datetime:
    """Момент в будущем — не должен срабатывать."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)


def _insert_schedule(content_id: int, platform: str, planned_at: datetime) -> int:
    from app.db import get_db
    with get_db() as db:
        cur = db.execute(
            """
            INSERT INTO schedule (content_id, platform, planned_at, status)
            VALUES (?, ?, ?, 'planned')
            """,
            (content_id, platform, planned_at.strftime("%Y-%m-%d %H:%M:%S")),
        )
        return cur.lastrowid


def _get_schedule(schedule_id: int) -> dict:
    from app.db import get_db
    with get_db() as db:
        row = db.execute("SELECT * FROM schedule WHERE id = ?", (schedule_id,)).fetchone()
        return dict(row) if row else {}


# ─── Тест 1: dry-run telegram ─────────────────────────────────────────────────


def test_dry_run_telegram(db_with_data, monkeypatch):
    """DRY-RUN=1 → status published, url dry-run://, outbox-файл содержит channel_id и хэштеги."""
    import app.config as cfg_module
    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", True)

    content_id = db_with_data["content_id"]
    sid = _insert_schedule(content_id, "telegram", _past())

    from app.publishers.base import publish
    url = publish(sid)

    assert url.startswith("dry-run://telegram/"), f"Неожиданный URL: {url}"

    row = _get_schedule(sid)
    # publish() не меняет status — это делает process_due
    # Проверяем outbox-файл
    import app.config as cfg
    outbox_path = cfg.settings.data_dir_absolute / "outbox" / f"{sid}_telegram.json"
    assert outbox_path.exists(), f"outbox файл не создан: {outbox_path}"

    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["channel_id"] == "@test_channel"
    assert "#тест" in payload["text"]
    assert "#контент" in payload["text"]
    assert payload["dry_run"] is True


# ─── Тест 2: VK без токена → тоже dry-run ────────────────────────────────────


def test_vk_no_credentials_is_dry_run(db_with_data, monkeypatch):
    """VK без credentials → автоматический dry-run (не ошибка)."""
    import app.config as cfg_module
    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", False)

    content_id = db_with_data["content_id"]
    sid = _insert_schedule(content_id, "vk", _past())

    from app.publishers.base import publish
    url = publish(sid)

    assert url.startswith("dry-run://vk/"), f"Неожиданный URL: {url}"

    import app.config as cfg
    outbox_path = cfg.settings.data_dir_absolute / "outbox" / f"{sid}_vk.json"
    assert outbox_path.exists()
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True


# ─── Тест 3: dzen → manual_pending; mark_manual_done ─────────────────────────


def test_dzen_manual_pending_and_done(db_with_data, monkeypatch):
    """Dzen площадка → status=manual_pending; mark_manual_done → manual_done."""
    import app.config as cfg_module
    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", False)

    content_id = db_with_data["content_id"]
    sid = _insert_schedule(content_id, "dzen", _past())

    from app.publishers.base import publish
    url = publish(sid)

    assert url.startswith("manual://dzen/"), f"Неожиданный URL: {url}"

    row = _get_schedule(sid)
    assert row["status"] == "manual_pending"

    # Симулируем ручную публикацию
    from app.publishers.manual import mark_manual_done
    mark_manual_done(sid, "https://dzen.ru/article/abc123")

    row = _get_schedule(sid)
    assert row["status"] == "manual_done"
    assert row["published_url"] == "https://dzen.ru/article/abc123"


# ─── Тест 4: реальный режим + ошибка API → retry-логика ─────────────────────


def test_real_mode_api_error_retry(db_with_data, monkeypatch):
    """
    PUBLISH_DRY_RUN=0 + ответ Telegram {"ok": false} →
    после 1 неудачи: attempts=1, status=planned, planned_at сдвинут вперёд.
    """
    import app.config as cfg_module
    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", False)

    # Мокаем httpx.Client в proxies-сервисе → возвращает {"ok": false}
    import app.services.proxies as proxies_mod
    import httpx

    class FakeResponse:
        def json(self):
            return {"ok": False, "description": "chat not found"}

    class FakeClient:
        def __init__(self, proxy=None, timeout=None):
            pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def request(self, method, url, **kwargs): return FakeResponse()

    monkeypatch.setattr(proxies_mod.httpx, "Client", FakeClient)

    content_id = db_with_data["content_id"]
    sid = _insert_schedule(content_id, "telegram", _past())
    now = _past()

    from app.services.scheduling import process_due
    process_due(now=now)

    row = _get_schedule(sid)
    assert row["attempts"] == 1, f"attempts={row['attempts']}, ожидалось 1"
    assert row["status"] == "planned", f"status={row['status']}, ожидалось planned"

    # planned_at должен быть сдвинут вперёд относительно now
    new_planned = datetime.fromisoformat(row["planned_at"])
    expected_min = now + timedelta(minutes=4, seconds=59)
    assert new_planned > expected_min, (
        f"planned_at={new_planned} не сдвинут вперёд (now={now})"
    )


def test_real_mode_three_failures_become_error(db_with_data, monkeypatch):
    """После 3 неудач → status=error с описанием ошибки."""
    import app.config as cfg_module
    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", False)

    import app.services.proxies as proxies_mod
    import httpx

    class FakeResponse:
        def json(self):
            return {"ok": False, "description": "chat not found"}

    class FakeClient:
        def __init__(self, proxy=None, timeout=None):
            pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def request(self, method, url, **kwargs): return FakeResponse()

    monkeypatch.setattr(proxies_mod.httpx, "Client", FakeClient)

    content_id = db_with_data["content_id"]
    sid = _insert_schedule(content_id, "telegram", _past())

    from app.services.scheduling import process_due
    from app.db import get_db

    for i in range(3):
        # Каждый раз берём актуальный planned_at из БД
        row = _get_schedule(sid)
        planned_at = datetime.fromisoformat(row["planned_at"])
        # Передаём now >= planned_at, чтобы запись попала в выборку
        process_due(now=planned_at + timedelta(seconds=1))

    row = _get_schedule(sid)
    assert row["status"] == "error", f"status={row['status']}, ожидалось error"
    assert row["attempts"] == 3
    assert "chat not found" in row["error_text"]


# ─── Тест 5: process_due не трогает записи с planned_at в будущем ─────────────


def test_process_due_ignores_future(db_with_data, monkeypatch):
    """Запись с planned_at в будущем не затрагивается тиком планировщика."""
    import app.config as cfg_module
    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", True)

    content_id = db_with_data["content_id"]
    sid = _insert_schedule(content_id, "telegram", _future())

    from app.services.scheduling import process_due
    process_due(now=datetime.now(timezone.utc).replace(tzinfo=None))

    row = _get_schedule(sid)
    assert row["status"] == "planned", f"status={row['status']}, ожидалось planned"
    assert row["attempts"] == 0


# ─── Тест 6: process_due успешно публикует в dry-run ─────────────────────────


def test_process_due_publishes_dry_run(db_with_data, monkeypatch):
    """process_due с dry-run → статус published, url dry-run://."""
    import app.config as cfg_module
    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", True)

    content_id = db_with_data["content_id"]
    sid = _insert_schedule(content_id, "telegram", _past())

    from app.services.scheduling import process_due
    process_due(now=datetime.now(timezone.utc).replace(tzinfo=None))

    row = _get_schedule(sid)
    assert row["status"] == "published"
    assert row["published_url"].startswith("dry-run://telegram/")
