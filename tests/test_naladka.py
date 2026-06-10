"""
Тесты волны D этапа 7 (наладка):
1. Формы credentials per-платформа — сохранение и предзаполнение
2. /check для всех 5 платформ (httpx замокан: успех и ошибка)
3. rotate_media с per-project retention (проект 7 дней vs глобальный фоллбэк)
4. Story в /manual (картинка + caption)
5. Аналитика по типам контента — разрез и фильтр
"""
import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Общие утилиты ────────────────────────────────────────────────────────────


def _slug() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def _setup_db_path():
    """Инициализировать тестовую БД."""
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, slug=None, settings_json=None):
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects (slug, name, description, settings)
        VALUES (?, 'Тест Наладка', 'Описание', ?)
        """,
        (s, settings_json),
    )
    project_id = cur.lastrowid
    for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
        db.execute(
            "INSERT OR IGNORE INTO project_platforms (project_id, platform, enabled, mode)"
            " VALUES (?,?,?,?)",
            (project_id, p, 0, "auto"),
        )
    return project_id, s


def _create_content(db, project_id, ctype="post", title="Тест контент",
                    texts=None, files=None):
    texts_json = json.dumps(texts, ensure_ascii=False) if texts else None
    files_json = json.dumps(files, ensure_ascii=False) if files else None
    cur = db.execute(
        "INSERT INTO content (project_id, type, title, texts, files, status)"
        " VALUES (?, ?, ?, ?, ?, 'approved')",
        (project_id, ctype, title, texts_json, files_json),
    )
    return cur.lastrowid


def _create_schedule(db, content_id, platform="telegram", status="manual_pending",
                     planned_at=None):
    if planned_at is None:
        planned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = db.execute(
        "INSERT INTO schedule (content_id, platform, planned_at, status)"
        " VALUES (?, ?, ?, ?)",
        (content_id, platform, planned_at, status),
    )
    return cur.lastrowid


def _create_metric(db, schedule_id, views=100, likes=10, comments=2, shares=1,
                   watch_pct=0.0, offset_days=0):
    d = (date.today() - timedelta(days=offset_days)).isoformat()
    db.execute(
        "INSERT OR REPLACE INTO metrics"
        " (schedule_id, date, views, likes, comments, shares, watch_pct)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (schedule_id, d, views, likes, comments, shares, watch_pct),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 1: Формы credentials per-платформа
# ═══════════════════════════════════════════════════════════════════════════════


class TestCredentialsForms:
    """Тесты сохранения и предзаполнения форм credentials."""

    def test_telegram_save_named_fields(self, client, patch_env):
        """POST /projects/{slug}/platform для telegram сохраняет bot_token и chat_id."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)

        resp = client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "telegram",
                "enabled": "1",
                "mode": "auto",
                "bot_token": "bot123:TOKEN",
                "chat_id": "@mychannel",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with get_db() as db:
            row = db.execute(
                "SELECT credentials, config FROM project_platforms"
                " WHERE project_id=? AND platform='telegram'",
                (pid,),
            ).fetchone()

        assert row is not None
        assert row["credentials"] is not None
        # credentials — зашифрованный JSON с bot_token
        from app.security import decrypt
        creds = json.loads(decrypt(row["credentials"]))
        assert creds.get("bot_token") == "bot123:TOKEN"

        # config содержит chat_id
        config = json.loads(row["config"])
        assert config.get("channel_id") == "@mychannel"

    def test_vk_save_named_fields(self, client, patch_env):
        """POST для vk сохраняет access_token в credentials, group_id в config."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)

        resp = client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "vk",
                "enabled": "1",
                "mode": "auto",
                "access_token": "vktoken_abc",
                "group_id": "123456",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with get_db() as db:
            row = db.execute(
                "SELECT credentials, config FROM project_platforms"
                " WHERE project_id=? AND platform='vk'",
                (pid,),
            ).fetchone()

        from app.security import decrypt
        creds = json.loads(decrypt(row["credentials"]))
        assert creds.get("access_token") == "vktoken_abc"
        config = json.loads(row["config"])
        assert config.get("group_id") == "123456"

    def test_youtube_save_named_fields(self, client, patch_env):
        """POST для youtube сохраняет client_id, client_secret, refresh_token."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)

        resp = client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "youtube",
                "enabled": "1",
                "mode": "auto",
                "client_id": "myid",
                "client_secret": "mysecret",
                "refresh_token": "myrefresh",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with get_db() as db:
            row = db.execute(
                "SELECT credentials FROM project_platforms"
                " WHERE project_id=? AND platform='youtube'",
                (pid,),
            ).fetchone()

        from app.security import decrypt
        creds = json.loads(decrypt(row["credentials"]))
        assert creds.get("client_id") == "myid"
        assert creds.get("client_secret") == "mysecret"
        assert creds.get("refresh_token") == "myrefresh"

    def test_instagram_no_credentials_needed(self, client, patch_env):
        """POST для instagram без credentials → credentials=None (ручная публикация)."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)

        resp = client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "instagram",
                "enabled": "1",
                "mode": "manual",
                "note": "Публикуется вручную",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with get_db() as db:
            row = db.execute(
                "SELECT credentials FROM project_platforms"
                " WHERE project_id=? AND platform='instagram'",
                (pid,),
            ).fetchone()
        # Для instagram/dzen credentials не обязательны
        assert row is not None

    def test_dzen_no_credentials_needed(self, client, patch_env):
        """POST для dzen без credentials → 303, сохранение проходит."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)

        resp = client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "dzen",
                "enabled": "0",
                "mode": "manual",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_credentials_prefilled_in_detail_page(self, client, patch_env):
        """Страница /projects/{slug} показывает 'токен задан ✓' после сохранения credentials."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)

        # Сохраняем credentials для telegram
        client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "telegram",
                "enabled": "1",
                "mode": "auto",
                "bot_token": "bot999:TOKEN",
                "chat_id": "@testchan",
            },
            follow_redirects=False,
        )

        resp = client.get(f"/projects/{slug}")
        assert resp.status_code == 200
        # Страница должна показать, что credentials заданы
        assert "токен задан" in resp.text or "задан ✓" in resp.text

    def test_empty_credentials_do_not_overwrite_existing(self, client, patch_env):
        """Пустые поля credentials не затирают ранее сохранённый токен."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)

        # Первый раз — сохраняем токен
        client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "telegram",
                "enabled": "1",
                "mode": "auto",
                "bot_token": "bot_original:TOKEN",
                "chat_id": "@chan",
            },
            follow_redirects=False,
        )

        # Второй раз — обновляем только режим, без credentials
        client.post(
            f"/projects/{slug}/platform",
            data={
                "platform": "telegram",
                "enabled": "1",
                "mode": "manual",
                # bot_token и chat_id не передаём
            },
            follow_redirects=False,
        )

        with get_db() as db:
            row = db.execute(
                "SELECT credentials FROM project_platforms"
                " WHERE project_id=? AND platform='telegram'",
                (pid,),
            ).fetchone()

        from app.security import decrypt
        creds = json.loads(decrypt(row["credentials"]))
        assert creds.get("bot_token") == "bot_original:TOKEN"

    def test_detail_page_shows_credential_fields(self, client, patch_env):
        """Страница /projects/{slug} отображает форму с именованными полями credentials."""
        _setup_db_path()
        with get_db() as db:
            _, slug = _create_project(db)

        resp = client.get(f"/projects/{slug}")
        assert resp.status_code == 200
        # Форма должна содержать именованные поля
        assert "bot_token" in resp.text
        assert "chat_id" in resp.text
        assert "access_token" in resp.text
        assert "group_id" in resp.text
        assert "client_id" in resp.text or "refresh_token" in resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 2: Кнопка «Проверить подключение» — POST /projects/{slug}/platforms/{platform}/check
# ═══════════════════════════════════════════════════════════════════════════════


class TestPlatformCheck:
    """Тесты HTMX-эндпоинта проверки подключения."""

    def _setup_telegram(self, client, slug, pid):
        """Вспомогательная установка telegram credentials."""
        from app.security import encrypt
        creds = encrypt(json.dumps({"bot_token": "bot123:ABC", "chat_id": "@testchan"}))
        with get_db() as db:
            db.execute(
                "UPDATE project_platforms SET credentials=?, config=?"
                " WHERE project_id=? AND platform='telegram'",
                (creds, json.dumps({"channel_id": "@testchan"}), pid),
            )

    def _setup_vk(self, client, slug, pid):
        from app.security import encrypt
        creds = encrypt(json.dumps({"access_token": "vktoken_abc"}))
        with get_db() as db:
            db.execute(
                "UPDATE project_platforms SET credentials=?, config=?"
                " WHERE project_id=? AND platform='vk'",
                (creds, json.dumps({"group_id": "12345"}), pid),
            )

    def _setup_youtube(self, slug, pid):
        from app.security import encrypt
        creds = encrypt(json.dumps({
            "client_id": "myid",
            "client_secret": "mysecret",
            "refresh_token": "myrefresh",
        }))
        with get_db() as db:
            db.execute(
                "UPDATE project_platforms SET credentials=?"
                " WHERE project_id=? AND platform='youtube'",
                (creds, pid),
            )

    def test_telegram_check_success(self, client, patch_env, monkeypatch):
        """telegram /check с успешным getMe → зелёный HTML-фрагмент."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)
        self._setup_telegram(client, slug, pid)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "ok": True,
            "result": {"id": 123, "username": "testbot", "first_name": "TestBot"},
        }

        import httpx
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: mock_resp)

        resp = client.post(f"/projects/{slug}/platforms/telegram/check")
        assert resp.status_code == 200
        assert "✓" in resp.text or "ok" in resp.text.lower() or "testbot" in resp.text.lower()

    def test_telegram_check_failure(self, client, patch_env, monkeypatch):
        """telegram /check с HTTP-ошибкой → красный HTML-фрагмент с сообщением."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)
        self._setup_telegram(client, slug, pid)

        import httpx
        monkeypatch.setattr(
            httpx, "get",
            lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("timeout")),
        )

        resp = client.post(f"/projects/{slug}/platforms/telegram/check")
        assert resp.status_code == 200
        assert "✗" in resp.text or "ошибка" in resp.text.lower() or "error" in resp.text.lower()

    def test_vk_check_success(self, client, patch_env, monkeypatch):
        """vk /check с успешным groups.getById → зелёный фрагмент."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)
        self._setup_vk(client, slug, pid)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "response": [{"id": 12345, "name": "Test Group", "screen_name": "testgroup"}]
        }

        import httpx
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: mock_resp)

        resp = client.post(f"/projects/{slug}/platforms/vk/check")
        assert resp.status_code == 200
        assert "✓" in resp.text or "test group" in resp.text.lower() or "ok" in resp.text.lower()

    def test_vk_check_api_error(self, client, patch_env, monkeypatch):
        """vk /check с ошибкой API → красный фрагмент."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)
        self._setup_vk(client, slug, pid)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "error": {"error_code": 5, "error_msg": "User authorization failed"}
        }

        import httpx
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: mock_resp)

        resp = client.post(f"/projects/{slug}/platforms/vk/check")
        assert resp.status_code == 200
        assert "✗" in resp.text or "ошибка" in resp.text.lower() or "error" in resp.text.lower()

    def test_youtube_check_success(self, client, patch_env, monkeypatch):
        """youtube /check с успешным обновлением токена → зелёный фрагмент."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)
        self._setup_youtube(slug, pid)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "newtoken123", "expires_in": 3600}

        import httpx
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: mock_resp)

        resp = client.post(f"/projects/{slug}/platforms/youtube/check")
        assert resp.status_code == 200
        assert "✓" in resp.text or "ok" in resp.text.lower() or "access_token" in resp.text.lower()

    def test_youtube_check_failure(self, client, patch_env, monkeypatch):
        """youtube /check с ошибкой токена → красный фрагмент."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)
        self._setup_youtube(slug, pid)

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": "invalid_grant", "error_description": "Token expired"}

        import httpx
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: mock_resp)

        resp = client.post(f"/projects/{slug}/platforms/youtube/check")
        assert resp.status_code == 200
        assert "✗" in resp.text or "ошибка" in resp.text.lower() or "error" in resp.text.lower()

    def test_instagram_check_manual(self, client, patch_env):
        """instagram /check → ответ «ручная публикация» без сетевых запросов."""
        _setup_db_path()
        with get_db() as db:
            _, slug = _create_project(db)

        resp = client.post(f"/projects/{slug}/platforms/instagram/check")
        assert resp.status_code == 200
        assert "ручная" in resp.text.lower() or "вручную" in resp.text.lower() or "manual" in resp.text.lower()

    def test_dzen_check_manual(self, client, patch_env):
        """dzen /check → ответ «ручная публикация» без сетевых запросов."""
        _setup_db_path()
        with get_db() as db:
            _, slug = _create_project(db)

        resp = client.post(f"/projects/{slug}/platforms/dzen/check")
        assert resp.status_code == 200
        assert "ручная" in resp.text.lower() or "вручную" in resp.text.lower() or "manual" in resp.text.lower()

    def test_check_unknown_platform_returns_400(self, client, patch_env):
        """Неизвестная платформа → 400."""
        _setup_db_path()
        with get_db() as db:
            _, slug = _create_project(db)

        resp = client.post(f"/projects/{slug}/platforms/fakeplatform/check")
        assert resp.status_code == 400

    def test_check_unknown_project_returns_404(self, client, patch_env):
        """Несуществующий проект → 404."""
        _setup_db_path()
        resp = client.post("/projects/no-such-project/platforms/telegram/check")
        assert resp.status_code == 404

    def test_network_error_does_not_crash_page(self, client, patch_env, monkeypatch):
        """Сетевая ошибка не роняет страницу — возвращает 200 с сообщением об ошибке."""
        _setup_db_path()
        with get_db() as db:
            pid, slug = _create_project(db)
        self._setup_telegram(client, slug, pid)

        import httpx
        monkeypatch.setattr(
            httpx, "get",
            lambda *a, **kw: (_ for _ in ()).throw(httpx.TimeoutException("timeout")),
        )

        resp = client.post(f"/projects/{slug}/platforms/telegram/check")
        # Страница не должна упасть с 500
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 3: retention per-project в rotate_media
# ═══════════════════════════════════════════════════════════════════════════════


def _insert_test_content_with_project(db_path: Path, settings_json=None) -> tuple[int, int]:
    """Вставить проект + контент, вернуть (project_id, content_id)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        cur = conn.execute(
            "INSERT INTO projects (slug, name, settings) VALUES (?, ?, ?)",
            (f"proj-{uuid.uuid4().hex[:8]}", "Тест", settings_json),
        )
        project_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO content (project_id, type, status) VALUES (?, 'video_footage', 'approved')",
            (project_id,),
        )
        content_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return project_id, content_id


def _insert_schedule_with_age(db_path: Path, content_id: int, status: str, days_ago: int) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            f"""
            INSERT INTO schedule (content_id, platform, planned_at, status, updated_at)
            VALUES (?, 'telegram', datetime('now'), ?, datetime('now', '-{days_ago} days'))
            """,
            (content_id, status),
        )
        conn.commit()
    finally:
        conn.close()


class TestRotateMediaPerProject:
    """Тесты per-project retention в rotate_media."""

    def test_project_retention_7days_purges_when_old_enough(self, tmp_path, monkeypatch):
        """Проект с retention_days=7: контент 10 дней → удаляется."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MEDIA_RETENTION_DAYS", "30")  # глобальный фоллбэк = 30

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("DB_PATH", str(db_path))
        init_db(db_path)

        settings_json = json.dumps({"retention_days": 7})
        _, content_id = _insert_test_content_with_project(db_path, settings_json)
        _insert_schedule_with_age(db_path, content_id, "published", days_ago=10)

        videos_dir = tmp_path / "videos"
        videos_dir.mkdir()
        (videos_dir / f"{content_id}.mp4").write_bytes(b"FAKE")

        from app.services.cleanup import rotate_media
        result = rotate_media()

        assert result["purged_content"] == 1
        assert not (videos_dir / f"{content_id}.mp4").exists()

    def test_project_retention_7days_keeps_recent(self, tmp_path, monkeypatch):
        """Проект с retention_days=7: контент 3 дня → НЕ удаляется."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MEDIA_RETENTION_DAYS", "30")

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("DB_PATH", str(db_path))
        init_db(db_path)

        settings_json = json.dumps({"retention_days": 7})
        _, content_id = _insert_test_content_with_project(db_path, settings_json)
        _insert_schedule_with_age(db_path, content_id, "published", days_ago=3)

        videos_dir = tmp_path / "videos"
        videos_dir.mkdir()
        mp4 = videos_dir / f"{content_id}.mp4"
        mp4.write_bytes(b"FAKE")

        from app.services.cleanup import rotate_media
        result = rotate_media()

        assert result["purged_content"] == 0
        assert mp4.exists()

    def test_global_fallback_when_no_project_settings(self, tmp_path, monkeypatch):
        """Контент без project.settings берёт глобальный MEDIA_RETENTION_DAYS."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MEDIA_RETENTION_DAYS", "14")

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("DB_PATH", str(db_path))
        init_db(db_path)

        # settings_json=None → нет настроек у проекта
        _, content_id = _insert_test_content_with_project(db_path, settings_json=None)
        _insert_schedule_with_age(db_path, content_id, "published", days_ago=20)

        videos_dir = tmp_path / "videos"
        videos_dir.mkdir()
        (videos_dir / f"{content_id}.mp4").write_bytes(b"FAKE")

        from app.services.cleanup import rotate_media
        result = rotate_media()

        # Глобальный retention=14, контент 20 дней — должен быть удалён
        assert result["purged_content"] == 1

    def test_per_project_overrides_global_retention(self, tmp_path, monkeypatch):
        """
        Два проекта: один retention=7, другой=30.
        Контент 10 дней: первый удаляется, второй нет.
        """
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MEDIA_RETENTION_DAYS", "30")  # глобальный фоллбэк

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("DB_PATH", str(db_path))
        init_db(db_path)

        # Проект 1: retention=7
        settings_7 = json.dumps({"retention_days": 7})
        _, cid1 = _insert_test_content_with_project(db_path, settings_7)
        _insert_schedule_with_age(db_path, cid1, "published", days_ago=10)

        # Проект 2: retention=30
        settings_30 = json.dumps({"retention_days": 30})
        _, cid2 = _insert_test_content_with_project(db_path, settings_30)
        _insert_schedule_with_age(db_path, cid2, "published", days_ago=10)

        videos_dir = tmp_path / "videos"
        videos_dir.mkdir()
        mp4_1 = videos_dir / f"{cid1}.mp4"
        mp4_2 = videos_dir / f"{cid2}.mp4"
        mp4_1.write_bytes(b"FAKE")
        mp4_2.write_bytes(b"FAKE")

        from app.services.cleanup import rotate_media
        result = rotate_media()

        assert result["purged_content"] == 1
        assert not mp4_1.exists(), "retention=7, 10 дней → должно быть удалено"
        assert mp4_2.exists(), "retention=30, 10 дней → должно остаться"


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 4: Story в /manual
# ═══════════════════════════════════════════════════════════════════════════════


class TestStoryInManual:
    """Тесты отображения story-контента в ручной очереди."""

    def test_story_shows_image_tag(self, client, patch_env):
        """story в /manual показывает тег <img> с URL картинки."""
        _setup_db_path()
        with get_db() as db:
            pid, _ = _create_project(db)
            texts = {
                "telegram": {"caption": "Подпись к истории", "hashtags": ["#test"]}
            }
            cid = _create_content(db, pid, ctype="story", title="История 1", texts=texts)
            _create_schedule(db, cid, platform="telegram", status="manual_pending")

        resp = client.get("/manual")
        assert resp.status_code == 200
        # Должна быть картинка story
        assert f"/files/images/{cid}.jpg" in resp.text

    def test_story_shows_download_button(self, client, patch_env):
        """story в /manual показывает кнопку «Скачать картинку»."""
        _setup_db_path()
        with get_db() as db:
            pid, _ = _create_project(db)
            texts = {"telegram": {"caption": "Подпись", "hashtags": []}}
            cid = _create_content(db, pid, ctype="story", title="История 2", texts=texts)
            _create_schedule(db, cid, platform="telegram", status="manual_pending")

        resp = client.get("/manual")
        assert resp.status_code == 200
        assert "download" in resp.text
        assert f"/files/images/{cid}.jpg" in resp.text

    def test_story_shows_caption(self, client, patch_env):
        """story в /manual показывает caption из texts."""
        _setup_db_path()
        with get_db() as db:
            pid, _ = _create_project(db)
            texts = {"telegram": {"caption": "Уникальная подпись истории ХЗ", "hashtags": []}}
            cid = _create_content(db, pid, ctype="story", title="История 3", texts=texts)
            _create_schedule(db, cid, platform="telegram", status="manual_pending")

        resp = client.get("/manual")
        assert resp.status_code == 200
        assert "Уникальная подпись истории ХЗ" in resp.text

    def test_story_mark_done_works(self, client, patch_env):
        """story можно отметить как опубликованную через POST /manual/{id}/done."""
        _setup_db_path()
        with get_db() as db:
            pid, _ = _create_project(db)
            texts = {"telegram": {"caption": "Тест дан", "hashtags": []}}
            cid = _create_content(db, pid, ctype="story", texts=texts)
            sid = _create_schedule(db, cid, platform="telegram", status="manual_pending")

        resp = client.post(
            f"/manual/{sid}/done",
            data={"published_url": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        with get_db() as db:
            row = db.execute("SELECT status FROM schedule WHERE id=?", (sid,)).fetchone()
        assert row["status"] == "manual_done"

    def test_non_story_content_no_image_tag(self, client, patch_env):
        """Обычный post в /manual НЕ показывает блок с img story."""
        _setup_db_path()
        with get_db() as db:
            pid, _ = _create_project(db)
            texts = {"telegram": {"text": "Обычный пост", "hashtags": []}}
            cid = _create_content(db, pid, ctype="post", texts=texts)
            _create_schedule(db, cid, platform="telegram", status="manual_pending")

        resp = client.get("/manual")
        assert resp.status_code == 200
        # У post-контента не должно быть story-картинки
        assert f"/files/images/{cid}.jpg" not in resp.text


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 5: Аналитика по типам контента
# ═══════════════════════════════════════════════════════════════════════════════


class TestAnalyticsByContentType:
    """Тесты разреза аналитики по типам контента."""

    def test_analytics_page_contains_type_breakdown(self, client, patch_env):
        """Страница /analytics содержит раздел по типам контента."""
        _setup_db_path()
        with get_db() as db:
            pid, _ = _create_project(db)
            cid_post = _create_content(db, pid, ctype="post", title="Пост тип")
            cid_story = _create_content(db, pid, ctype="story", title="История тип")
            sid1 = _create_schedule(db, cid_post, platform="telegram", status="published")
            sid2 = _create_schedule(db, cid_story, platform="telegram", status="published")
            _create_metric(db, sid1, views=100)
            _create_metric(db, sid2, views=200)

        resp = client.get("/analytics")
        assert resp.status_code == 200
        # Должен быть раздел по типам
        assert "post" in resp.text or "story" in resp.text

    def test_analytics_type_filter_post_only(self, client, patch_env):
        """Фильтр ?content_type=post показывает только post, не story."""
        _setup_db_path()
        with get_db() as db:
            pid, _ = _create_project(db)
            cid_post = _create_content(db, pid, ctype="post", title="Только пост Х")
            cid_story = _create_content(db, pid, ctype="story", title="Только история Х")
            sid1 = _create_schedule(db, cid_post, status="published")
            sid2 = _create_schedule(db, cid_story, status="published")
            _create_metric(db, sid1, views=50)
            _create_metric(db, sid2, views=80)

        resp = client.get("/analytics?content_type=post")
        assert resp.status_code == 200
        assert "Только пост Х" in resp.text
        assert "Только история Х" not in resp.text

    def test_analytics_type_filter_story_only(self, client, patch_env):
        """Фильтр ?content_type=story показывает только story."""
        _setup_db_path()
        with get_db() as db:
            pid, _ = _create_project(db)
            cid_post = _create_content(db, pid, ctype="post", title="Пост не-история Х")
            cid_story = _create_content(db, pid, ctype="story", title="История только Х")
            sid1 = _create_schedule(db, cid_post, status="published")
            sid2 = _create_schedule(db, cid_story, status="published")
            _create_metric(db, sid1, views=50)
            _create_metric(db, sid2, views=80)

        resp = client.get("/analytics?content_type=story")
        assert resp.status_code == 200
        assert "История только Х" in resp.text
        assert "Пост не-история Х" not in resp.text

    def test_analytics_type_avg_views_computed(self, client, patch_env):
        """Средние метрики по типам контента вычисляются корректно."""
        _setup_db_path()
        with get_db() as db:
            pid, _ = _create_project(db)
            # 2 поста: 100 и 200 просмотров → среднее 150
            cid1 = _create_content(db, pid, ctype="post", title="Пост А")
            cid2 = _create_content(db, pid, ctype="post", title="Пост Б")
            sid1 = _create_schedule(db, cid1, status="published")
            sid2 = _create_schedule(db, cid2, status="published")
            _create_metric(db, sid1, views=100)
            _create_metric(db, sid2, views=200)

        resp = client.get("/analytics")
        assert resp.status_code == 200
        # Среднее должно быть 150 или близкое число на странице
        assert "150" in resp.text or "150.0" in resp.text

    def test_analytics_no_type_filter_shows_all(self, client, patch_env):
        """Без фильтра типа — показываются все типы."""
        _setup_db_path()
        with get_db() as db:
            pid, _ = _create_project(db)
            cid1 = _create_content(db, pid, ctype="post", title="Пост все типы Х")
            cid2 = _create_content(db, pid, ctype="story", title="История все типы Х")
            sid1 = _create_schedule(db, cid1, status="published")
            sid2 = _create_schedule(db, cid2, status="published")
            _create_metric(db, sid1, views=10)
            _create_metric(db, sid2, views=20)

        resp = client.get("/analytics")
        assert resp.status_code == 200
        assert "Пост все типы Х" in resp.text
        assert "История все типы Х" in resp.text

    def test_analytics_existing_behavior_preserved(self, client, patch_env):
        """Существующее поведение аналитики (сводные карточки, топ-5) не сломано."""
        _setup_db_path()
        with get_db() as db:
            pid, _ = _create_project(db)
            cid = _create_content(db, pid, ctype="post", title="Сохранённое поведение")
            sid = _create_schedule(db, cid, status="published")
            _create_metric(db, sid, views=777)

        resp = client.get("/analytics")
        assert resp.status_code == 200
        assert "777" in resp.text
        assert "Сохранённое поведение" in resp.text
