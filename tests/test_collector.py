"""
Тесты ежедневного сборщика метрик (app/analytics/collector.py).

pytest tests/test_collector.py -q
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _init_db():
    import app.config as cfg_module
    import app.db as db_module
    from app.db import init_db

    db_module.settings = cfg_module.settings
    init_db(cfg_module.settings.db_path_absolute)


def _insert_project_platform(platform: str, credentials_json: str | None, config_json: str | None = None) -> int:
    """Вставить проект + площадку, вернуть project_id."""
    from app.db import get_db

    slug = f"test-{platform}-{uuid.uuid4().hex[:8]}"
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO projects (slug, name) VALUES (?, ?)",
            (slug, f"Тест {platform}"),
        )
        project_id = cur.lastrowid

        db.execute(
            """
            INSERT INTO project_platforms
              (project_id, platform, enabled, mode, config, credentials)
            VALUES (?, ?, 1, 'auto', ?, ?)
            """,
            (project_id, platform, config_json, credentials_json),
        )
    return project_id


def _insert_content(project_id: int) -> int:
    from app.db import get_db

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO content (project_id, type, status) VALUES (?, 'post', 'approved')",
            (project_id,),
        )
        return cur.lastrowid


def _insert_schedule_published(content_id: int, platform: str, published_url: str) -> int:
    """Вставить schedule со status=published и published_url."""
    from app.db import get_db

    with get_db() as db:
        cur = db.execute(
            """
            INSERT INTO schedule
              (content_id, platform, planned_at, status, published_url, updated_at)
            VALUES (?, ?, datetime('now'), 'published', ?, datetime('now'))
            """,
            (content_id, platform, published_url),
        )
        return cur.lastrowid


def _get_metrics(schedule_id: int, date: str) -> dict | None:
    from app.db import get_db

    with get_db() as db:
        row = db.execute(
            "SELECT * FROM metrics WHERE schedule_id = ? AND date = ?",
            (schedule_id, date),
        ).fetchone()
        return dict(row) if row else None


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ─── Тест 1: VK пост с credentials → метрики в БД ─────────────────────────────


def test_vk_post_metrics_collected(monkeypatch):
    """
    VK-пост с валидными credentials → монкипатч httpx.get →
    строка в metrics с правильными числами.
    """
    import httpx
    from app.security import encrypt

    _init_db()

    creds_json = json.dumps({"access_token": "fake_vk_token"})
    encrypted_creds = encrypt(creds_json)
    config_json = json.dumps({"group_id": 12345})

    project_id = _insert_project_platform("vk", encrypted_creds, config_json)
    content_id = _insert_content(project_id)
    sid = _insert_schedule_published(content_id, "vk", "https://vk.com/wall-12345_99")

    fake_response_data = {
        "response": [
            {
                "id": 99,
                "views": {"count": 150},
                "likes": {"count": 42},
                "comments": {"count": 7},
                "reposts": {"count": 3},
            }
        ]
    }

    class FakeGetResponse:
        status_code = 200
        is_success = True
        def json(self):
            return fake_response_data

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: FakeGetResponse())

    from app.analytics.collector import collect_metrics
    result = collect_metrics()

    assert result["collected"] == 1, f"Ожидалось collected=1, получили {result}"
    assert result["skipped"] == 0
    assert result["errors"] == 0

    today = _today()
    row = _get_metrics(sid, today)
    assert row is not None, "Строка в metrics не создана"
    assert row["views"] == 150
    assert row["likes"] == 42
    assert row["comments"] == 7
    assert row["shares"] == 3


# ─── Тест 2: повторный запуск в тот же день → не дублирует ────────────────────


def test_vk_no_duplicate_same_day(monkeypatch):
    """
    Два вызова collect_metrics в один день →
    не создаётся дублирующая строка (INSERT OR REPLACE).
    """
    import httpx
    from app.security import encrypt
    from app.db import get_db

    _init_db()

    creds_json = json.dumps({"access_token": "fake_vk_token"})
    encrypted_creds = encrypt(creds_json)

    project_id = _insert_project_platform("vk", encrypted_creds)
    content_id = _insert_content(project_id)
    sid = _insert_schedule_published(content_id, "vk", "https://vk.com/wall-12345_100")

    call_count = {"n": 0}

    def fake_get(url, *args, **kwargs):
        call_count["n"] += 1
        # views меняется от вызова к вызову — проверим, что остаётся только одна строка
        views_val = 100 * call_count["n"]

        class Resp:
            status_code = 200
            is_success = True
            def json(self):
                return {
                    "response": [
                        {
                            "id": 100,
                            "views": {"count": views_val},
                            "likes": {"count": 5},
                            "comments": {"count": 2},
                            "reposts": {"count": 1},
                        }
                    ]
                }
        return Resp()

    monkeypatch.setattr(httpx, "get", fake_get)

    from app.analytics.collector import collect_metrics

    collect_metrics()
    collect_metrics()

    today = _today()
    with get_db() as db:
        cnt = db.execute(
            "SELECT COUNT(*) AS n FROM metrics WHERE schedule_id = ? AND date = ?",
            (sid, today),
        ).fetchone()["n"]

    assert cnt == 1, f"Ожидалась 1 строка, получили {cnt}"


# ─── Тест 3: YouTube published + моки → metrics ───────────────────────────────


def test_youtube_metrics_collected(monkeypatch):
    """
    YouTube Shorts с валидными credentials → монкипатч httpx →
    строка в metrics с viewCount/likeCount/commentCount.
    """
    import httpx
    from app.security import encrypt

    _init_db()

    creds_json = json.dumps({
        "client_id": "cid",
        "client_secret": "csecret",
        "refresh_token": "rtoken",
    })
    encrypted_creds = encrypt(creds_json)

    project_id = _insert_project_platform("youtube", encrypted_creds)
    content_id = _insert_content(project_id)
    sid = _insert_schedule_published(
        content_id, "youtube", "https://www.youtube.com/shorts/abcXYZ123"
    )

    class FakeTokenResp:
        status_code = 200
        text = ""
        def json(self):
            return {"access_token": "yt_access_token_fake"}

    class FakeStatsResp:
        status_code = 200
        is_success = True
        text = ""
        def json(self):
            return {
                "items": [
                    {
                        "id": "abcXYZ123",
                        "statistics": {
                            "viewCount": "2500",
                            "likeCount": "300",
                            "commentCount": "45",
                        },
                    }
                ]
            }

    post_responses = [FakeTokenResp()]
    get_responses = [FakeStatsResp()]

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: post_responses.pop(0))
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: get_responses.pop(0))

    from app.analytics.collector import collect_metrics
    result = collect_metrics()

    assert result["collected"] == 1, f"Ожидалось collected=1, получили {result}"
    assert result["skipped"] == 0
    assert result["errors"] == 0

    today = _today()
    row = _get_metrics(sid, today)
    assert row is not None, "Строка в metrics не создана"
    assert row["views"] == 2500
    assert row["likes"] == 300
    assert row["comments"] == 45


# ─── Тест 4: площадка без credentials → skipped, без сетевых вызовов ─────────


def test_no_credentials_skipped(monkeypatch):
    """
    VK площадка без credentials (NULL) → запись считается skipped,
    никаких сетевых вызовов не происходит.
    """
    import httpx

    _init_db()

    # credentials=None
    project_id = _insert_project_platform("vk", None)
    content_id = _insert_content(project_id)
    _insert_schedule_published(content_id, "vk", "https://vk.com/wall-12345_55")

    http_called = {"called": False}

    def fail_if_called(*a, **kw):
        http_called["called"] = True
        raise AssertionError("HTTP вызов не должен происходить при отсутствии credentials")

    monkeypatch.setattr(httpx, "get", fail_if_called)
    monkeypatch.setattr(httpx, "post", fail_if_called)

    from app.analytics.collector import collect_metrics
    result = collect_metrics()

    assert result["skipped"] == 1, f"Ожидалось skipped=1, получили {result}"
    assert result["collected"] == 0
    assert result["errors"] == 0
    assert not http_called["called"]


# ─── Тест 5: ошибка API у одной публикации не валит сбор остальных ────────────


def test_api_error_one_does_not_stop_others(monkeypatch):
    """
    Две VK-публикации: у первой API вернул ошибку, у второй — успех.
    Результат: errors=1, collected=1.
    """
    import httpx
    from app.security import encrypt

    _init_db()

    creds_json = json.dumps({"access_token": "fake_vk_token"})

    # Первая публикация
    project_id_1 = _insert_project_platform("vk", encrypt(creds_json))
    content_id_1 = _insert_content(project_id_1)
    sid1 = _insert_schedule_published(content_id_1, "vk", "https://vk.com/wall-111_1")

    # Вторая публикация
    project_id_2 = _insert_project_platform("vk", encrypt(creds_json))
    content_id_2 = _insert_content(project_id_2)
    sid2 = _insert_schedule_published(content_id_2, "vk", "https://vk.com/wall-222_2")

    call_count = {"n": 0}

    def fake_get(url, params=None, **kwargs):
        call_count["n"] += 1
        # Первый вызов — ошибка API, второй — успех
        if call_count["n"] == 1:
            class ErrResp:
                status_code = 200
                is_success = True
                def json(self):
                    return {"error": {"error_code": 15, "error_msg": "Access denied"}}
            return ErrResp()
        else:
            class OkResp:
                status_code = 200
                is_success = True
                def json(self):
                    return {
                        "response": [
                            {
                                "id": 2,
                                "views": {"count": 77},
                                "likes": {"count": 5},
                                "comments": {"count": 1},
                                "reposts": {"count": 0},
                            }
                        ]
                    }
            return OkResp()

    monkeypatch.setattr(httpx, "get", fake_get)

    from app.analytics.collector import collect_metrics
    result = collect_metrics()

    assert result["errors"] == 1, f"Ожидалось errors=1, получили {result}"
    assert result["collected"] == 1, f"Ожидалось collected=1, получили {result}"

    today = _today()
    # Первая запись — нет строки в metrics (ошибка)
    assert _get_metrics(sid1, today) is None, "У первой записи не должно быть метрик"
    # Вторая — есть строка
    row2 = _get_metrics(sid2, today)
    assert row2 is not None, "У второй записи должны быть метрики"
    assert row2["views"] == 77
