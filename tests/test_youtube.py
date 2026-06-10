"""
Тесты паблишера YouTube Shorts.

pytest tests/test_youtube.py -q
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _past() -> datetime:
    """Момент в прошлом для немедленного срабатывания."""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)


def _insert_project_and_content(tmp_path, texts: dict | None = None, files: dict | None = None):
    """
    Вставляет в БД проект, YouTube-площадку и контент.
    Возвращает (project_id, content_id).
    """
    import app.config as cfg_module
    import app.db as db_module
    from app.db import init_db, get_db
    from app.security import encrypt

    db_module.settings = cfg_module.settings
    init_db(cfg_module.settings.db_path_absolute)

    slug = f"yt-test-{uuid.uuid4().hex[:8]}"

    default_texts = {
        "youtube": {
            "title": "Тестовый шорт",
            "description": "Описание для YouTube",
        }
    }
    default_files = {"video_path": str(tmp_path / "test_video.mp4")}

    actual_texts = texts if texts is not None else default_texts
    actual_files = files if files is not None else default_files

    yt_creds = encrypt(json.dumps({
        "client_id": "fake_client_id",
        "client_secret": "fake_client_secret",
        "refresh_token": "fake_refresh_token",
    }))
    yt_config = json.dumps({"privacy": "private"})

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO projects (slug, name, description) VALUES (?, ?, ?)",
            (slug, "YT-тест", "проект"),
        )
        project_id = cur.lastrowid

        db.execute(
            """
            INSERT INTO project_platforms
              (project_id, platform, enabled, mode, config, credentials)
            VALUES (?, 'youtube', 1, 'auto', ?, ?)
            """,
            (project_id, yt_config, yt_creds),
        )

        cur = db.execute(
            """
            INSERT INTO content (project_id, type, status, texts, files)
            VALUES (?, 'video_footage', 'approved', ?, ?)
            """,
            (project_id, json.dumps(actual_texts), json.dumps(actual_files)),
        )
        content_id = cur.lastrowid

    return project_id, content_id


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


def _insert_published_youtube(content_id: int, count: int) -> None:
    """Вставить N записей schedule youtube/published за сегодня (для теста квоты)."""
    from app.db import get_db
    for _ in range(count):
        with get_db() as db:
            db.execute(
                """
                INSERT INTO schedule (content_id, platform, planned_at, status, updated_at)
                VALUES (?, 'youtube', datetime('now'), 'published', datetime('now'))
                """,
                (content_id,),
            )


# ─── Тест 1: dry-run → outbox-файл, в title добавлен #Shorts ─────────────────


def test_dry_run_adds_shorts_tag(tmp_path, monkeypatch):
    """DRY-RUN=1 → outbox-файл создан, title содержит #Shorts."""
    import app.config as cfg_module
    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", True)

    _, content_id = _insert_project_and_content(tmp_path)
    sid = _insert_schedule(content_id, "youtube", _past())

    from app.publishers.base import publish
    url = publish(sid)

    assert url.startswith("dry-run://youtube/"), f"Неожиданный URL: {url}"

    outbox_path = cfg_module.settings.data_dir_absolute / "outbox" / f"{sid}_youtube.json"
    assert outbox_path.exists(), f"outbox-файл не создан: {outbox_path}"

    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["platform"] == "youtube"
    assert "#Shorts" in payload["title"] or "#shorts" in payload["title"].lower()


# ─── Тест 2: title уже содержит #shorts → не дублируется ──────────────────────


def test_dry_run_no_duplicate_shorts_tag(tmp_path, monkeypatch):
    """Если title уже содержит '#shorts' (любой регистр), дублирования нет."""
    import app.config as cfg_module
    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", True)

    texts = {
        "youtube": {
            "title": "Мой ролик #Shorts",
            "description": "Описание",
        }
    }
    _, content_id = _insert_project_and_content(tmp_path, texts=texts)
    sid = _insert_schedule(content_id, "youtube", _past())

    from app.publishers.base import publish
    url = publish(sid)

    outbox_path = cfg_module.settings.data_dir_absolute / "outbox" / f"{sid}_youtube.json"
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))

    title = payload["title"]
    # Считаем вхождения '#shorts' без учёта регистра
    assert title.lower().count("#shorts") == 1, (
        f"#Shorts дублируется в title: {title!r}"
    )


# ─── Тест 3: нет video_path → PublishError ────────────────────────────────────


def test_no_video_path_raises_publish_error(tmp_path, monkeypatch):
    """files без video_path → PublishError, не зависит от dry-run/real."""
    import app.config as cfg_module
    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", True)

    _, content_id = _insert_project_and_content(tmp_path, files={})
    sid = _insert_schedule(content_id, "youtube", _past())

    from app.publishers.base import publish
    from app.publishers.base import PublishError

    with pytest.raises(PublishError, match="video_path"):
        publish(sid)


# ─── Тест 4: квота исчерпана → PublishDeferred ───────────────────────────────


def test_quota_exceeded_raises_deferred(tmp_path, monkeypatch):
    """
    6 записей youtube/published за сегодня → publish_youtube бросает PublishDeferred
    с retry_at в будущем.
    """
    import app.config as cfg_module
    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", True)
    monkeypatch.setattr(cfg_module.settings, "YOUTUBE_DAILY_LIMIT", 6)

    _, content_id = _insert_project_and_content(tmp_path)
    _insert_published_youtube(content_id, 6)

    sid = _insert_schedule(content_id, "youtube", _past())

    from app.publishers.base import PublishDeferred
    from app.publishers.youtube import publish_youtube

    creds = {"client_id": "x", "client_secret": "x", "refresh_token": "x"}
    texts = {"youtube": {"title": "Тест", "description": "Описание"}}
    files = {"video_path": str(tmp_path / "v.mp4")}

    with pytest.raises(PublishDeferred) as exc_info:
        publish_youtube(
            schedule_id=sid,
            credentials=creds,
            config={},
            texts=texts,
            files=files,
            dry_run=True,
        )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert exc_info.value.retry_at > now, (
        f"retry_at={exc_info.value.retry_at} должен быть в будущем"
    )


# ─── Тест 5: process_due + квота → attempts НЕ вырос, planned_at = retry_at ──


def test_process_due_quota_deferred_no_attempts(tmp_path, monkeypatch):
    """
    process_due + PublishDeferred → статус снова planned,
    attempts НЕ вырос, planned_at = retry_at.
    """
    import app.config as cfg_module
    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", False)
    monkeypatch.setattr(cfg_module.settings, "YOUTUBE_DAILY_LIMIT", 6)

    _, content_id = _insert_project_and_content(tmp_path)
    _insert_published_youtube(content_id, 6)

    sid = _insert_schedule(content_id, "youtube", _past())

    # Мокаем publish так, чтобы он бросил PublishDeferred
    from datetime import datetime as dt
    from app.publishers.base import PublishDeferred

    retry_moment = dt.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=12)

    # process_due импортирует publish напрямую из app.publishers.base,
    # поэтому патчим через модуль app.services.scheduling
    import app.services.scheduling as sched_module
    monkeypatch.setattr(
        sched_module,
        "publish",
        lambda sid: (_ for _ in ()).throw(PublishDeferred("квота", retry_at=retry_moment)),
    )

    from app.services.scheduling import process_due
    process_due(now=_past())

    row = _get_schedule(sid)
    assert row["status"] == "planned", f"status={row['status']}, ожидалось planned"
    assert row["attempts"] == 0, f"attempts={row['attempts']}, ожидалось 0"

    new_planned = datetime.fromisoformat(row["planned_at"])
    # Допуск 1 секунда — формат datetime без микросекунд
    assert abs((new_planned - retry_moment).total_seconds()) < 2, (
        f"planned_at={new_planned} не совпадает с retry_at={retry_moment}"
    )


# ─── Тест 6: реальный путь через monkeypatch httpx ───────────────────────────


def test_real_upload_success(tmp_path, monkeypatch):
    """
    Монкипатч httpx: token → resumable Location → PUT id=abc123
    → возвращается https://www.youtube.com/shorts/abc123.
    """
    import app.config as cfg_module
    import httpx

    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", False)
    monkeypatch.setattr(cfg_module.settings, "YOUTUBE_DAILY_LIMIT", 6)

    # Создаём реальный mp4-файл (1 байт — достаточно для теста)
    video_file = tmp_path / "short.mp4"
    video_file.write_bytes(b"\x00")

    _, content_id = _insert_project_and_content(
        tmp_path,
        files={"video_path": str(video_file)},
    )

    call_log: list[str] = []

    class FakeTokenResponse:
        status_code = 200
        text = ""
        is_success = True
        def json(self):
            return {"access_token": "test_access_token"}

    class FakeInitResponse:
        status_code = 200
        text = ""
        is_success = True
        headers = {"Location": "https://upload.youtube.com/fake_resumable_url"}
        def json(self):
            return {}

    class FakePutResponse:
        status_code = 200
        text = '{"id": "abc123"}'
        is_success = True
        def json(self):
            return {"id": "abc123"}

    responses = [FakeTokenResponse(), FakeInitResponse()]

    def fake_post(url, *args, **kwargs):
        call_log.append(f"POST {url}")
        return responses.pop(0)

    def fake_put(url, *args, **kwargs):
        call_log.append(f"PUT {url}")
        return FakePutResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "put", fake_put)

    from app.publishers.youtube import publish_youtube

    creds = {
        "client_id": "cid",
        "client_secret": "csecret",
        "refresh_token": "rtoken",
    }
    texts = {"youtube": {"title": "Тест #shorts", "description": "Описание"}}
    files = {"video_path": str(video_file)}

    url = publish_youtube(
        schedule_id=1,
        credentials=creds,
        config={"privacy": "private"},
        texts=texts,
        files=files,
        dry_run=False,
    )

    assert url == "https://www.youtube.com/shorts/abc123", f"Неожиданный URL: {url}"
    assert any("oauth2.googleapis.com" in c for c in call_log)


# ─── Тест 7: 403 quotaExceeded на init → PublishDeferred ─────────────────────


def test_403_quota_exceeded_on_init(tmp_path, monkeypatch):
    """HTTP 403 с reason=quotaExceeded при resumable init → PublishDeferred."""
    import app.config as cfg_module
    import httpx

    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", False)
    monkeypatch.setattr(cfg_module.settings, "YOUTUBE_DAILY_LIMIT", 6)

    # Инициализируем БД (нужна для проверки квоты внутри publish_youtube)
    from app.db import init_db
    init_db(cfg_module.settings.db_path_absolute)

    video_file = tmp_path / "short.mp4"
    video_file.write_bytes(b"\x00")

    class FakeTokenResponse:
        status_code = 200
        text = ""
        is_success = True
        def json(self):
            return {"access_token": "test_token"}

    class Fake403Response:
        status_code = 403
        text = '{"error": {"errors": [{"reason": "quotaExceeded"}]}}'
        is_success = False
        def json(self):
            return {"error": {"errors": [{"reason": "quotaExceeded"}]}}

    responses = [FakeTokenResponse(), Fake403Response()]

    def fake_post(url, *args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(httpx, "post", fake_post)

    from app.publishers.base import PublishDeferred
    from app.publishers.youtube import publish_youtube

    creds = {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
    texts = {"youtube": {"title": "Тест", "description": ""}}
    files = {"video_path": str(video_file)}

    with pytest.raises(PublishDeferred) as exc_info:
        publish_youtube(
            schedule_id=99,
            credentials=creds,
            config={},
            texts=texts,
            files=files,
            dry_run=False,
        )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert exc_info.value.retry_at > now


# ─── Тест 8: невалидный refresh_token (400 от OAuth) → PublishError ──────────


def test_invalid_refresh_token_raises_publish_error(tmp_path, monkeypatch):
    """HTTP 400 от OAuth при refresh → PublishError."""
    import app.config as cfg_module
    import httpx

    monkeypatch.setattr(cfg_module.settings, "PUBLISH_DRY_RUN", False)
    monkeypatch.setattr(cfg_module.settings, "YOUTUBE_DAILY_LIMIT", 6)

    # Инициализируем БД (нужна для проверки квоты внутри publish_youtube)
    from app.db import init_db
    init_db(cfg_module.settings.db_path_absolute)

    video_file = tmp_path / "short.mp4"
    video_file.write_bytes(b"\x00")

    class Fake400Response:
        status_code = 400
        text = '{"error": "invalid_grant", "error_description": "Token has been expired"}'
        is_success = False
        def json(self):
            return {"error": "invalid_grant"}

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: Fake400Response())

    from app.publishers.base import PublishError
    from app.publishers.youtube import publish_youtube

    creds = {"client_id": "c", "client_secret": "s", "refresh_token": "bad_token"}
    texts = {"youtube": {"title": "Тест", "description": ""}}
    files = {"video_path": str(video_file)}

    with pytest.raises(PublishError, match="400"):
        publish_youtube(
            schedule_id=88,
            credentials=creds,
            config={},
            texts=texts,
            files=files,
            dry_run=False,
        )
